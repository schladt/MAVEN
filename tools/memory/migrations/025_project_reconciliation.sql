-- New storage only. A project identity is still the reviewed owner project entity;
-- never create one on a reconcile request or import any legacy knowledge here.
CREATE TABLE maven.reconcile_projects (
    project_id uuid PRIMARY KEY REFERENCES maven.memory_items(id),
    version uuid NOT NULL,
    record_count bigint NOT NULL CHECK (record_count >= 0)
)
-- statement
CREATE TABLE maven.reconcile_records (
    project_id uuid NOT NULL REFERENCES maven.reconcile_projects(project_id),
    id uuid NOT NULL,
    -- Preserve exact canonical JSON bytes, including floating-point spellings.
    -- The separately stored small inventory projection never contains history.
    wire text NOT NULL CHECK (octet_length(wire) BETWEEN 1 AND 524288),
    item jsonb NOT NULL CHECK (jsonb_typeof(item) = 'object' AND octet_length(item::text) <= 16384),
    PRIMARY KEY (project_id,id),
    CHECK (item->>'id' IS NOT DISTINCT FROM id::text AND wire::jsonb->'record'->>'id' IS NOT DISTINCT FROM id::text),
    CHECK (item->>'digest' IS NOT DISTINCT FROM encode(sha256(convert_to(wire,'UTF8')),'hex')),
    CHECK (CASE WHEN wire::jsonb->'record' ? 'erased' OR item->>'kind' = 'erased'
           THEN item = jsonb_build_object('id',id,'written_at',wire::jsonb->'record'->>'written_at',
             'digest',item->>'digest','kind','erased','erased',true,'summary','Erased')
             AND wire::jsonb = jsonb_build_object('record',jsonb_build_object(
             'id',id,'written_at',item->>'written_at','erased',true),'history','[]'::jsonb) ELSE true END)
)
-- statement
REVOKE ALL ON maven.reconcile_projects, maven.reconcile_records FROM PUBLIC
-- statement
CREATE FUNCTION maven.reconcile_token(project uuid, epoch uuid, revision integer, lifecycle bigint, version uuid) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS $$
    SELECT encode(sha256(convert_to(concat_ws(':',project,epoch,revision,lifecycle,COALESCE(version::text,'empty')),'UTF8')),'hex')
$$
-- statement
-- Only the signed Lambda service may supply internal prepared summaries and wires.
-- Every call (including continuation pages) pins identity and project visibility.
-- A VOLATILE function takes a fresh snapshot after acquiring the project lock.
CREATE FUNCTION maven.reconcile_request(action text, request jsonb) RETURNS json
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    state jsonb := maven.lock_service_state(); p uuid; project_rev integer; lifecycle bigint;
    project_version uuid; total bigint; view_key text; result json; page json; cursor_id uuid;
    entry jsonb; rid uuid; projected jsonb; old_item jsonb; changed uuid[] := '{}'; added integer := 0;
BEGIN
    IF state->>'state' IS DISTINCT FROM 'ready' OR state->>'instance_id' IS DISTINCT FROM request->>'instance_id'
        OR state->>'recovery_epoch' IS DISTINCT FROM request->>'recovery_epoch' THEN
        result := json_build_object('status','context_stale');
    ELSIF action='status' THEN
        result := json_build_object('instance_id',state->>'instance_id','recovery_epoch',state->>'recovery_epoch',
                                    'capability','project_reconcile_v1');
    ELSIF action IN ('inventory','read','prior','apply') THEN
        p := (request->>'project_id')::uuid;
        IF action='apply' THEN
            PERFORM pg_advisory_xact_lock(hashtextextended('project-work:' || p::text,0));
        ELSE
            PERFORM pg_advisory_xact_lock_shared(hashtextextended('project-work:' || p::text,0));
        END IF;
        SELECT r.revision INTO project_rev FROM maven.records r JOIN maven.memory_items m ON m.id=r.id
        WHERE r.id=p AND r.kind='entity' AND r.data_scope='owner' AND m.project_id IS NULL
          AND m.data->>'entity_type'='project' AND maven.memory_visible(p) FOR SHARE OF r,m;
        IF project_rev IS NULL THEN
            result := json_build_object('status','project_unavailable');
        ELSIF maven.project_archived(p) THEN
            result := json_build_object('status','project_archived');
        ELSE
            -- Protect the current cited sources from Forget/Erase through commit as well.
            PERFORM s.id FROM maven.record_sources c JOIN maven.records s ON s.id=c.source_id
            WHERE c.record_id=p AND c.revision=project_rev FOR SHARE OF s;
            IF NOT maven.memory_visible(p) THEN
                result := json_build_object('status','project_unavailable');
            END IF;
        END IF;
        IF result IS NULL THEN
            SELECT generation INTO lifecycle FROM maven.project_lifecycle WHERE project_id=p;
            SELECT version,record_count INTO project_version,total FROM maven.reconcile_projects WHERE project_id=p;
            total := COALESCE(total,0);
            view_key := maven.reconcile_token(p,(state->>'recovery_epoch')::uuid,project_rev,lifecycle,project_version);
            IF (action <> 'inventory' OR request ? 'after_id' OR request ? 'expected_token')
                AND view_key IS DISTINCT FROM request->>'expected_token' THEN
                result := json_build_object('status','view_conflict');
            ELSIF action='inventory' THEN
                -- Keyset pagination: no full record or old wording enters this query.
                WITH selected AS MATERIALIZED (
                    SELECT id,item FROM maven.reconcile_records WHERE project_id=p
                    AND (NOT request ? 'after_id' OR id>(request->>'after_id')::uuid) ORDER BY id LIMIT 33
                ), visible AS (SELECT * FROM selected ORDER BY id LIMIT 32)
                SELECT COALESCE(json_agg(item ORDER BY id),'[]'),
                    CASE WHEN (SELECT count(*) FROM selected)>32 THEN (SELECT id FROM visible ORDER BY id DESC LIMIT 1) END
                INTO page,cursor_id FROM visible;
                result := json_build_object('token',view_key,'items',page,'next_after_id',cursor_id,'total',total);
            ELSIF action IN ('read','prior') THEN
                -- Return a contiguous prefix of the requested IDs, preserving wire JSON as json, not jsonb.
                -- Each accepted envelope fits a page by construction. NULL denotes an absent ID, not erasure.
                WITH selected AS (
                    SELECT i.ord,r.wire, sum(COALESCE(octet_length(r.wire),4)+1) OVER (ORDER BY i.ord) AS bytes
                    FROM jsonb_array_elements_text(request->'ids') WITH ORDINALITY i(id,ord)
                    LEFT JOIN maven.reconcile_records r ON r.project_id=p AND r.id=i.id::uuid
                ) SELECT COALESCE(json_agg(wire::json ORDER BY ord),'[]') INTO page FROM selected WHERE bytes<=540672;
                result := json_build_object('token',view_key,'records',page,'remaining',
                    jsonb_array_length(request->'ids')-json_array_length(page));
            ELSE
                IF jsonb_typeof(request->'changes') IS DISTINCT FROM 'array' OR jsonb_array_length(request->'changes')>50 THEN
                    result := json_build_object('status','invalid_request');
                END IF;
                -- Preflight the whole atomic batch before mutating anything.
                FOR entry IN SELECT value FROM jsonb_array_elements(request->'changes') LOOP
                    EXIT WHEN result IS NOT NULL;
                    projected := entry->'item'; rid := (projected->>'id')::uuid;
                    SELECT item INTO old_item FROM maven.reconcile_records WHERE project_id=p AND id=rid;
                    IF old_item->>'kind'='erased' AND projected->>'kind' IS DISTINCT FROM 'erased' THEN
                        result := json_build_object('status','erased_record');
                    ELSIF projected->>'kind'<>'erased' AND old_item->>'digest' IS DISTINCT FROM entry->>'prior_digest' THEN
                        result := json_build_object('status','view_conflict');
                    ELSIF old_item->>'digest' IS DISTINCT FROM projected->>'digest' THEN
                        changed := array_append(changed,rid);
                        IF old_item IS NULL THEN added := added+1; END IF;
                    END IF;
                END LOOP;
                IF result IS NULL THEN
                    IF cardinality(changed)>0 THEN
                        project_version := gen_random_uuid();
                        INSERT INTO maven.reconcile_projects(project_id,version,record_count) VALUES(p,project_version,total+added)
                        ON CONFLICT(project_id) DO UPDATE SET version=excluded.version,record_count=excluded.record_count;
                        INSERT INTO maven.reconcile_records(project_id,id,wire,item)
                        SELECT p,(c->'item'->>'id')::uuid,c->>'wire',c->'item' FROM jsonb_array_elements(request->'changes') c
                        WHERE (c->'item'->>'id')::uuid=ANY(changed)
                        ON CONFLICT(project_id,id) DO UPDATE SET wire=excluded.wire,item=excluded.item;
                        view_key := maven.reconcile_token(p,(state->>'recovery_epoch')::uuid,project_rev,lifecycle,project_version);
                    END IF;
                    result := json_build_object('token',view_key,'changed',changed);
                END IF;
            END IF;
        END IF;
    ELSE
        result := json_build_object('status','invalid_request');
    END IF;
    RETURN json_build_object('service_state',state,'authority_generation',0,'result',result);
END
$$
-- statement
REVOKE ALL ON FUNCTION maven.reconcile_token(uuid,uuid,integer,bigint,uuid), maven.reconcile_request(text,jsonb) FROM PUBLIC
-- statement
-- Keep governed administration, catalog, and approval operations, not the old agent write workflow.
-- Old installed Lambda code also calls this dispatcher: fence it in the database during an upgrade.
ALTER FUNCTION maven.memory_request(text,jsonb,text,text,text,integer) RENAME TO memory_request_before_reconcile
-- statement
CREATE FUNCTION maven.memory_request(route text, request jsonb, request_hash text, candidate_hash text, actor text, client_protocol integer) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog AS $$
BEGIN
    IF route='/v1/memory/claim' OR (route='/v1/memory/write' AND NOT (
        request->>'kind' IS NOT DISTINCT FROM 'entity' AND request->>'data_scope' IS NOT DISTINCT FROM 'owner'
        AND request->'data'->>'entity_type' IS NOT DISTINCT FROM 'project' AND NOT request ? 'project_id')) THEN
        RETURN maven.memory_service_state() || jsonb_build_object('result',jsonb_build_object('status','client_incompatible'));
    END IF;
    RETURN maven.memory_request_before_reconcile(route,request,request_hash,candidate_hash,actor,client_protocol);
END
$$
-- statement
REVOKE ALL ON FUNCTION maven.memory_request(text,jsonb,text,text,text,integer) FROM PUBLIC
