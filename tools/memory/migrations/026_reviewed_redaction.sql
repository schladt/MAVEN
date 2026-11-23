-- Redaction controls contain only system-issued identities, revision evidence and terminal state.
-- Do not grant these objects to the service login. Redaction is a reviewed administration action.
ALTER TABLE maven.service_state ADD COLUMN redaction_checkpoint text
    CHECK (redaction_checkpoint ~ '^[0-9a-f]{64}$')
-- statement
CREATE OR REPLACE FUNCTION maven.lock_service_state() RETURNS jsonb
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT jsonb_build_object('instance_id',instance_id,'recovery_epoch',recovery_epoch,
        'schema_version',schema_version,'state',state,'data_scope',data_scope,
        'redaction_checkpoint',redaction_checkpoint) FROM maven.service_state WHERE singleton FOR SHARE
$$
-- statement
CREATE TABLE maven.redaction_identity (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton), salt uuid NOT NULL
)
-- statement
INSERT INTO maven.redaction_identity(salt) VALUES (gen_random_uuid())
-- statement
CREATE FUNCTION maven.redaction_salt() RETURNS uuid LANGUAGE sql STABLE SET search_path = pg_catalog AS $$
    SELECT salt FROM maven.redaction_identity WHERE singleton
$$
-- statement
CREATE FUNCTION maven.redaction_bundle_id(sha text) RETURNS uuid LANGUAGE sql STABLE STRICT SET search_path = pg_catalog AS $$
    SELECT substr(encode(sha256(convert_to(maven.redaction_salt()::text || ':skill_bundle:' || sha,'UTF8')),'hex'),1,32)::uuid
    WHERE sha ~ '^[0-9a-f]{64}$'
$$
-- statement
CREATE TABLE maven.redaction_markers (
    kind text NOT NULL, id uuid NOT NULL, project_id uuid NOT NULL,
    marked_at timestamptz NOT NULL DEFAULT transaction_timestamp(), PRIMARY KEY(kind,id,project_id)
)
-- statement
CREATE TABLE maven.redaction_jobs (
    job_id uuid PRIMARY KEY, targets jsonb NOT NULL, permanent_targets jsonb NOT NULL,
    plan_digest text CHECK (plan_digest ~ '^[0-9a-f]{64}$'), result jsonb NOT NULL
)
-- statement
-- A work row authorizes only DELETE of rows belonging to that exact typed target, and only
-- while executing the reviewed transaction. No GUC, receipt id, timestamp, or client role can
-- mint this privilege. No work rows survive the sanitizer, even inside the calling transaction.
CREATE TABLE maven.redaction_work (
    tx xid8 NOT NULL, kind text NOT NULL, id uuid NOT NULL, project_id uuid NOT NULL,
    PRIMARY KEY(tx,kind,id,project_id)
)
-- statement
CREATE FUNCTION maven.redaction_target(k text, i uuid, p uuid DEFAULT NULL) RETURNS jsonb
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS $$
    SELECT jsonb_strip_nulls(jsonb_build_object('kind',k,'id',i,'project_id',p))
$$
-- statement
CREATE FUNCTION maven.redaction_validate(selection jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog AS $$
DECLARE entry jsonb; k text; re constant text := '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'; result jsonb;
BEGIN
    IF jsonb_typeof(selection) IS DISTINCT FROM 'array' OR jsonb_array_length(selection)=0 THEN
        PERFORM maven.cleanup_refuse('redaction_targets'); END IF;
    FOR entry IN SELECT value FROM jsonb_array_elements(selection) LOOP
        k:=entry->>'kind';
        IF jsonb_typeof(entry) IS DISTINCT FROM 'object' OR k IS NULL OR k NOT IN
           ('record','source','page','project','project_record','proposal','challenge','publication',
            'instruction_receipt','operation','memory_receipt','consumption','cleanup','lifecycle','citation','skill_bundle','recovery_object')
           OR COALESCE(entry->>'id','') !~ re OR entry - ARRAY['kind','id','project_id'] <> '{}'::jsonb
           OR (entry ? 'project_id') <> (k='project_record')
           OR (k='project_record' AND COALESCE(entry->>'project_id','') !~ re) THEN
            PERFORM maven.cleanup_refuse('redaction_targets'); END IF;
    END LOOP;
    SELECT jsonb_agg(t ORDER BY t) INTO result FROM (SELECT DISTINCT value AS t FROM jsonb_array_elements(selection)) s;
    IF jsonb_array_length(result)<>jsonb_array_length(selection) THEN PERFORM maven.cleanup_refuse('redaction_targets'); END IF;
    RETURN result;
END
$$
-- statement
CREATE FUNCTION maven.redaction_result(job uuid, permanent_targets jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS $$
    SELECT jsonb_build_object('job_id',job,'live_data_redacted',true,'pending_bundles',coalesce(
        jsonb_agg(t->'id' ORDER BY t) FILTER (WHERE t->>'kind'='skill_bundle'),'[]'::jsonb))
    FROM jsonb_array_elements(permanent_targets) t
$$
-- statement
-- Append-only controls cannot become another store of unredactable prose. Enforce the same
-- sanitized shape even on owner SQL inserts, before these rows acquire immutability.
ALTER TABLE maven.redaction_jobs ADD CHECK (targets=maven.redaction_validate(targets)
    AND permanent_targets=maven.redaction_validate(permanent_targets) AND permanent_targets @> targets
    AND result=maven.redaction_result(job_id,permanent_targets))
-- statement
ALTER TABLE maven.redaction_markers ADD CHECK ((kind='project_record' OR project_id='00000000-0000-0000-0000-000000000000')
    AND maven.redaction_validate(jsonb_build_array(maven.redaction_target(kind,id,
        CASE WHEN kind='project_record' THEN project_id END))) IS NOT NULL)
-- statement
CREATE TRIGGER immutable_redaction_job BEFORE UPDATE OR DELETE ON maven.redaction_jobs
    FOR EACH ROW EXECUTE FUNCTION maven.immutable_version()
-- statement
CREATE TRIGGER immutable_redaction_marker BEFORE UPDATE OR DELETE ON maven.redaction_markers
    FOR EACH ROW EXECUTE FUNCTION maven.immutable_version()
-- statement
-- Evidence is derived from row identities and MVCC revisions, never from original payloads,
-- candidate/request hashes, locator strings or free text. Include every storage projection.
CREATE VIEW maven.redaction_versions AS
    SELECT maven.redaction_target('record',id) AS target, 'record:'||revision||':'||xmin::text AS rev FROM maven.records
    UNION ALL SELECT maven.redaction_target('record',record_id),'version:'||revision||':'||xmin::text FROM maven.record_versions
    UNION ALL SELECT maven.redaction_target('record',id),'memory:'||xmin::text FROM maven.memory_items
    UNION ALL SELECT maven.redaction_target('record',id),'fact:'||xmin::text FROM maven.facts
    UNION ALL SELECT maven.redaction_target('source',id),xmin::text FROM maven.sources
    UNION ALL SELECT maven.redaction_target('page',id),xmin::text FROM maven.pages
    UNION ALL SELECT maven.redaction_target('project',id),xmin::text FROM maven.memory_items WHERE data->>'entity_type'='project'
    UNION ALL SELECT maven.redaction_target('project',project_id),'inventory:'||xmin::text FROM maven.reconcile_projects
    UNION ALL SELECT maven.redaction_target('project_record',c.id,c.project_id),
        concat_ws(':',c.xmin::text,p.version,r.revision,r.xmin::text,m.xmin::text,l.generation,l.xmin::text)
        FROM maven.reconcile_records c JOIN maven.reconcile_projects p ON p.project_id=c.project_id
        JOIN maven.records r ON r.id=c.project_id JOIN maven.memory_items m ON m.id=r.id
        LEFT JOIN maven.project_lifecycle l ON l.project_id=r.id
    UNION ALL SELECT maven.redaction_target('proposal',id),xmin::text FROM maven.instruction_proposals
    UNION ALL SELECT maven.redaction_target('challenge',id),xmin::text FROM maven.instruction_challenges
    UNION ALL SELECT maven.redaction_target('publication',proposal_id),revision||':'||xmin::text FROM maven.page_publications
    UNION ALL SELECT maven.redaction_target('instruction_receipt',request_key),xmin::text FROM maven.instruction_receipts
    UNION ALL SELECT maven.redaction_target('operation',request_key),xmin::text FROM maven.operations
    UNION ALL SELECT maven.redaction_target('memory_receipt',request_key),xmin::text FROM maven.memory_receipts
    UNION ALL SELECT maven.redaction_target('consumption',invocation_id),xmin::text FROM maven.secret_adapter_consumptions
    UNION ALL SELECT maven.redaction_target('cleanup',id),'receipt:'||xmin::text FROM maven.cleanup_receipts
    UNION ALL SELECT maven.redaction_target('cleanup',operation_id),'operation:'||xmin::text FROM maven.cleanup_operations
    UNION ALL SELECT maven.redaction_target('lifecycle',project_id),generation||':'||xmin::text FROM maven.project_lifecycle
    UNION ALL SELECT maven.redaction_target('citation',source_id),concat_ws(':',record_id,revision,source_revision,xmin::text) FROM maven.record_sources
-- statement
-- Relationships whose existence/content is managed by MAVEN. Peer copies of the same text
-- without a stored provenance/dependency edge require their own explicit typed selection.
CREATE VIEW maven.redaction_links AS
    SELECT maven.redaction_target('record',id) AS a,maven.redaction_target('source',id) AS b,true AS mutual FROM maven.sources
    UNION ALL SELECT maven.redaction_target('record',id),maven.redaction_target('page',id),true FROM maven.pages
    UNION ALL SELECT maven.redaction_target('record',id),maven.redaction_target('project',id),true FROM maven.memory_items WHERE data->>'entity_type'='project'
    UNION ALL SELECT maven.redaction_target('record',project_id),maven.redaction_target('project',project_id),true FROM maven.reconcile_projects
    UNION ALL SELECT maven.redaction_target('record',project_id),maven.redaction_target('project',project_id),true FROM maven.project_lifecycle
    UNION ALL SELECT maven.redaction_target('project',project_id),maven.redaction_target('lifecycle',project_id),false FROM maven.project_lifecycle
    UNION ALL SELECT maven.redaction_target('project',project_id),maven.redaction_target('project_record',id,project_id),false FROM maven.reconcile_records
    UNION ALL SELECT maven.redaction_target('record',rel),maven.redaction_target('record',id),false FROM maven.memory_items
        CROSS JOIN LATERAL unnest(ARRAY[project_id,subject_id,object_id,session_id,claim_session]) rel WHERE rel IS NOT NULL
    UNION ALL SELECT maven.redaction_target('record',r.id),maven.redaction_target('record',v.record_id),false FROM maven.record_versions v
        CROSS JOIN LATERAL unnest(ARRAY[v.snapshot->>'project_id',v.snapshot#>>'{data,subject_id}',v.snapshot#>>'{data,object_id}',v.snapshot#>>'{data,session_id}']) rel
        JOIN maven.records r ON r.id::text=rel
    UNION ALL SELECT maven.redaction_target('record',r.id),maven.redaction_target('record',v.record_id),false FROM maven.record_versions v
        CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(v.snapshot#>'{data,memory_refs}')='array' THEN v.snapshot#>'{data,memory_refs}' ELSE '[]'::jsonb END) ref
        JOIN maven.records r ON r.id::text=ref->>'record_id'
    UNION ALL SELECT maven.redaction_target('record',record_id),maven.redaction_target('citation',source_id),true FROM maven.record_sources
    UNION ALL SELECT maven.redaction_target('citation',source_id),maven.redaction_target('source',source_id),true FROM maven.record_sources
    UNION ALL SELECT maven.redaction_target('record',id),maven.redaction_target('source',source_id),true FROM maven.facts
    UNION ALL SELECT maven.redaction_target('record',record_id),maven.redaction_target('operation',request_key),false FROM maven.operations
    UNION ALL SELECT maven.redaction_target('record',record_id),maven.redaction_target('memory_receipt',request_key),false FROM maven.memory_receipts
    UNION ALL SELECT maven.redaction_target('record',r.id),maven.redaction_target('memory_receipt',m.request_key),false FROM maven.memory_receipts m
        JOIN maven.records r ON r.id::text=m.result->>'session_id'
    UNION ALL SELECT maven.redaction_target('record',rid),maven.redaction_target('cleanup',id),action='erase' FROM maven.cleanup_receipts
        CROSS JOIN LATERAL unnest(record_ids) rid
    UNION ALL SELECT maven.redaction_target('page',page_id),maven.redaction_target('proposal',id),accepted_revision IS NOT NULL FROM maven.instruction_proposals
    UNION ALL SELECT maven.redaction_target('source',source_id),maven.redaction_target('proposal',id),true FROM maven.instruction_proposals
    UNION ALL SELECT maven.redaction_target('proposal',proposal_id),maven.redaction_target('challenge',id),true FROM maven.instruction_challenges
    UNION ALL SELECT maven.redaction_target('proposal',proposal_id),maven.redaction_target('publication',proposal_id),true FROM maven.page_publications
    UNION ALL SELECT maven.redaction_target('source',approval_source_id),maven.redaction_target('publication',proposal_id),true FROM maven.page_publications
    UNION ALL SELECT maven.redaction_target('proposal',proposal_id),maven.redaction_target('instruction_receipt',request_key),false FROM maven.instruction_receipts
    UNION ALL SELECT maven.redaction_target('project',m.id),maven.redaction_target('proposal',p.id),false FROM maven.instruction_proposals p
        JOIN maven.memory_items m ON m.id::text=maven.project_scope_key(p.candidate#>>'{scope,project}')
    UNION ALL SELECT maven.redaction_target('project',m.id),maven.redaction_target('page',p.id),false FROM maven.pages p
        JOIN maven.memory_items m ON m.id::text=maven.project_scope_key(p.scope->>'project')
    UNION ALL SELECT maven.redaction_target('page',p.id),maven.redaction_target('proposal',i.id),false FROM maven.instruction_proposals i
        JOIN maven.pages p ON (i.candidate->'requires') ? p.slug
    UNION ALL SELECT maven.redaction_target('record',m.id),maven.redaction_target('proposal',i.id),false FROM maven.instruction_proposals i
        JOIN maven.memory_items m ON m.id::text=maven.memory_json_object(i.candidate->>'body_markdown')->>'catalog_record_id'
        WHERE i.candidate->>'slug' LIKE 'secret-grant-%'
    UNION ALL SELECT maven.redaction_target('page',p.id),maven.redaction_target('proposal',i.id),false FROM maven.instruction_proposals i
        JOIN maven.pages p ON p.slug=maven.memory_json_object(i.candidate->>'body_markdown')->>'adapter'
        WHERE i.candidate->>'slug' LIKE 'secret-grant-%'
    UNION ALL SELECT maven.redaction_target('project',home_id),maven.redaction_target('consumption',invocation_id),false FROM maven.secret_adapter_consumptions
    UNION ALL SELECT maven.redaction_target('page',adapter_page_id),maven.redaction_target('consumption',invocation_id),false FROM maven.secret_adapter_consumptions
    -- Never make a spent single-use grant reusable, even replaying into a snapshot taken before the spend.
    UNION ALL SELECT maven.redaction_target('page',grant_page_id),maven.redaction_target('consumption',invocation_id),single_use FROM maven.secret_adapter_consumptions
    UNION ALL SELECT maven.redaction_target('skill_bundle',maven.redaction_bundle_id(candidate#>>'{bundle,sha256}')),maven.redaction_target('proposal',id),true
        FROM maven.instruction_proposals WHERE maven.redaction_bundle_id(candidate#>>'{bundle,sha256}') IS NOT NULL
    UNION ALL SELECT maven.redaction_target('skill_bundle',maven.redaction_bundle_id(snapshot#>>'{bundle,sha256}')),maven.redaction_target('record',record_id),true
        FROM maven.record_versions WHERE maven.redaction_bundle_id(snapshot#>>'{bundle,sha256}') IS NOT NULL
-- statement
CREATE FUNCTION maven.redaction_plan(selection jsonb, restoring boolean) RETURNS jsonb
LANGUAGE plpgsql STABLE SET search_path = pg_catalog AS $$
DECLARE targets jsonb:=maven.redaction_validate(selection); body jsonb; identity_value jsonb;
BEGIN
    IF NOT restoring AND (jsonb_array_length(targets)>200 OR EXISTS (SELECT 1 FROM jsonb_array_elements(targets) t
        WHERE t->>'kind' NOT IN ('skill_bundle','recovery_object') AND NOT EXISTS (SELECT 1 FROM maven.redaction_versions v WHERE v.target=t))) THEN
        PERFORM maven.cleanup_refuse('redaction_unavailable'); END IF;
    WITH RECURSIVE links AS MATERIALIZED (SELECT * FROM maven.redaction_links), edges AS MATERIALIZED (
        SELECT a,b FROM links UNION SELECT b,a FROM links WHERE mutual
    ), closure(t) AS (
        SELECT value FROM jsonb_array_elements(targets) UNION SELECT e.b FROM closure c JOIN edges e ON e.a=c.t
    ), evidence AS (
        SELECT c.t,encode(sha256(convert_to(coalesce(string_agg(v.rev,',' ORDER BY v.rev),'absent'),'UTF8')),'hex') AS token
        FROM closure c LEFT JOIN maven.redaction_versions v ON v.target=c.t GROUP BY c.t
    ) SELECT jsonb_build_object('targets',targets,'permanent_targets',jsonb_agg(t ORDER BY t),
        'revisions',jsonb_agg(t || jsonb_build_object('revision_token',token) ORDER BY t),
        'bundles',coalesce(jsonb_agg(t->'id' ORDER BY t) FILTER(WHERE t->>'kind'='skill_bundle'),'[]'::jsonb)) INTO body FROM evidence;
    SELECT jsonb_build_array(instance_id,recovery_epoch,maven.redaction_salt()) INTO STRICT identity_value FROM maven.service_state WHERE singleton;
    RETURN body || jsonb_build_object('plan_digest',encode(sha256(convert_to(jsonb_build_array(identity_value,body)::text,'UTF8')),'hex'));
END
$$
-- statement
CREATE FUNCTION maven.redaction_review(targets jsonb) RETURNS jsonb LANGUAGE sql STABLE SET search_path = pg_catalog AS $$
    SELECT maven.redaction_plan(targets,false)
$$
-- statement
-- Private, ephemeral provider input only. Do not persist or return raw locators in reviews,
-- jobs, ledger events or ordinary APIs. Orphan bundle handles resolve by version inventory.
CREATE FUNCTION maven.redaction_bundle_keys(targets jsonb) RETURNS jsonb LANGUAGE sql STABLE SET search_path = pg_catalog AS $$
    WITH candidates(sha) AS (
        SELECT candidate#>>'{bundle,sha256}' FROM maven.instruction_proposals WHERE candidate ? 'bundle'
        UNION SELECT snapshot#>>'{bundle,sha256}' FROM maven.record_versions WHERE snapshot ? 'bundle'
    ), mapped AS (SELECT maven.redaction_bundle_id(sha)::text AS id,sha FROM candidates)
    SELECT coalesce(jsonb_object_agg(id,sha),'{}'::jsonb) FROM mapped WHERE targets ? id
$$
-- statement
CREATE FUNCTION maven.redaction_working(k text, i uuid, p uuid DEFAULT '00000000-0000-0000-0000-000000000000') RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog AS $$
    SELECT EXISTS (SELECT 1 FROM maven.redaction_work WHERE tx=pg_current_xact_id_if_assigned() AND kind=k AND id=i AND project_id=p)
$$
-- statement
-- Preserve the old INSERT/UPDATE guards, including 016's narrowly shaped erase UPDATE.
-- Only the DELETE branch changes, and it checks a protected write-set and an exact row key.
DO $$
DECLARE spec record;
BEGIN
    FOR spec IN SELECT * FROM (VALUES
        ('record_versions','immutable_history','immutable_version','record','record_id','UPDATE'),
        ('operations','immutable_receipt','immutable_version','operation','request_key','UPDATE'),
        ('record_sources','immutable_citation','immutable_version','citation','source_id','UPDATE'),
        ('instruction_receipts','immutable_instruction_receipt','immutable_version','instruction_receipt','request_key','UPDATE'),
        ('page_publications','immutable_publication','immutable_version','publication','proposal_id','UPDATE'),
        ('instruction_challenges','immutable_challenge','immutable_version','challenge','id','UPDATE'),
        ('instruction_proposals','immutable_proposal','guard_proposal','proposal','id','UPDATE'),
        ('memory_receipts','immutable_memory_receipt','immutable_version','memory_receipt','request_key','UPDATE'),
        ('cleanup_receipts','immutable_cleanup_receipt','immutable_version','cleanup','id','UPDATE'),
        ('cleanup_operations','immutable_cleanup_operation','immutable_version','cleanup','operation_id','UPDATE'),
        ('secret_adapter_consumptions','immutable_secret_adapter_consumption','immutable_version','consumption','invocation_id','UPDATE'),
        ('memory_items','project_work_memory','guard_project_work','record','id','INSERT OR UPDATE'),
        ('records','project_work_record','guard_project_work','record','id','UPDATE'),
        ('instruction_proposals','project_work_proposal','guard_project_work','proposal','id','INSERT OR UPDATE'),
        ('pages','project_work_page','guard_project_work','page','id','INSERT OR UPDATE')
    ) AS guards(tbl,trig,fn,kind,col,events) LOOP
        EXECUTE format('DROP TRIGGER %I ON maven.%I',spec.trig,spec.tbl);
        EXECUTE format('CREATE TRIGGER %I BEFORE %s ON maven.%I FOR EACH ROW EXECUTE FUNCTION maven.%I()',
            spec.trig,spec.events,spec.tbl,spec.fn);
        EXECUTE format('CREATE TRIGGER %I BEFORE DELETE ON maven.%I FOR EACH ROW WHEN (NOT maven.redaction_working(%L,OLD.%I)) EXECUTE FUNCTION maven.%I()',
            spec.trig||'_delete',spec.tbl,spec.kind,spec.col,spec.fn);
    END LOOP;
END
$$
-- statement
CREATE FUNCTION maven.redaction_marked(k text, i uuid, p uuid DEFAULT '00000000-0000-0000-0000-000000000000') RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog AS $$
    SELECT EXISTS (SELECT 1 FROM maven.redaction_markers WHERE kind=k AND id=i AND project_id=p)
$$
-- statement
-- A lifecycle can have been redacted AFTER the isolated snapshot was taken. Even with no
-- archived row in that image the marker must not silently reopen a terminal work barrier.
CREATE OR REPLACE FUNCTION maven.project_archived(project uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT EXISTS (SELECT 1 FROM maven.project_lifecycle WHERE project_id=project AND archived)
        OR maven.redaction_marked('lifecycle',project)
$$
-- statement
CREATE OR REPLACE FUNCTION maven.require_project_work(project uuid) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog AS $$
BEGIN
    IF project IS NULL THEN RETURN; END IF;
    PERFORM pg_advisory_xact_lock_shared(hashtextextended('project-work:' || project::text,0));
    IF maven.project_archived(project) THEN RAISE EXCEPTION 'project_archived' USING ERRCODE='55000'; END IF;
END
$$
-- statement
CREATE FUNCTION maven.redaction_wire(i uuid) RETURNS text LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog AS $$
    SELECT '{"history":[],"record":{"erased":true,"id":"'||i::text||'","written_at":"1970-01-01T00:00:00Z"}}'
$$
-- statement
CREATE FUNCTION maven.redaction_item(i uuid) RETURNS jsonb LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog AS $$
    SELECT jsonb_build_object('id',i,'written_at','1970-01-01T00:00:00Z','digest',
        encode(sha256(convert_to(maven.redaction_wire(i),'UTF8')),'hex'),'kind','erased','erased',true,'summary','Erased')
$$
-- statement
-- Deny re-use on the storage boundary, including direct calls to older signed dispatchers.
-- If the receipt insert is last, the exception rolls back all earlier writes in its statement.
CREATE FUNCTION maven.redaction_guard() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE denied boolean; k text:=TG_ARGV[0]; i uuid;
BEGIN
    IF k='request' THEN
        i:=NEW.request_key;
        denied:=EXISTS (SELECT 1 FROM maven.redaction_markers WHERE id=i
            AND kind IN ('operation','instruction_receipt','memory_receipt'));
    ELSIF k='proposal' THEN
        denied:=maven.redaction_marked('proposal',NEW.id) OR maven.redaction_marked('page',NEW.page_id)
            OR maven.redaction_marked('skill_bundle',maven.redaction_bundle_id(NEW.candidate#>>'{bundle,sha256}'))
            OR EXISTS (SELECT 1 FROM maven.redaction_markers m WHERE m.kind='project'
                AND m.id::text=maven.project_scope_key(NEW.candidate#>>'{scope,project}'));
    ELSIF k='version' THEN
        denied:=maven.redaction_marked('record',NEW.record_id)
            OR maven.redaction_marked('skill_bundle',maven.redaction_bundle_id(NEW.snapshot#>>'{bundle,sha256}'));
    ELSIF k='project_record' THEN
        denied:=(maven.redaction_marked('project',NEW.project_id) OR maven.redaction_marked('project_record',NEW.id,NEW.project_id))
            AND (NEW.wire IS DISTINCT FROM maven.redaction_wire(NEW.id) OR NEW.item IS DISTINCT FROM maven.redaction_item(NEW.id));
    ELSIF k='lifecycle' THEN
        -- The review retires the selected lifecycle without turning archival into an unarchive.
        denied:=maven.redaction_marked(k,NEW.project_id) AND NOT
            (NEW.archived AND NEW.actor_id='redacted' AND NEW.action_key=repeat('0',64));
    ELSIF k='consumption' THEN denied:=maven.redaction_marked(k,NEW.invocation_id);
    ELSIF k='publication' THEN denied:=maven.redaction_marked(k,NEW.proposal_id);
    ELSE denied:=maven.redaction_marked(k,NEW.id);
    END IF;
    IF denied THEN PERFORM maven.cleanup_refuse('redacted_identifier'); END IF;
    RETURN NEW;
END
$$
-- statement
DO $$
DECLARE spec record;
BEGIN
    FOR spec IN SELECT * FROM (VALUES
        ('records','record','INSERT'),('record_versions','version','INSERT'),
        ('operations','request','INSERT'),('memory_receipts','request','INSERT'),('instruction_receipts','request','INSERT'),
        ('instruction_proposals','proposal','INSERT'),('instruction_challenges','challenge','INSERT'),
        ('page_publications','publication','INSERT'),('secret_adapter_consumptions','consumption','INSERT'),
        ('cleanup_receipts','cleanup','INSERT'),('project_lifecycle','lifecycle','INSERT OR UPDATE'),
        ('reconcile_records','project_record','INSERT OR UPDATE')
    ) AS guards(tbl,kind,events) LOOP
        EXECUTE format('CREATE TRIGGER redaction_terminal BEFORE %s ON maven.%I FOR EACH ROW EXECUTE FUNCTION maven.redaction_guard(%L)',
            spec.events,spec.tbl,spec.kind);
    END LOOP;
END
$$
-- statement
-- Rare, deliberately global maintenance barrier. Neither an advisory lock taken only by new
-- code nor locking today's selected rows would fence a concurrent first publication, new
-- reference or new project record. EXCLUSIVE also fences FOR SHARE approvals/consumption.
-- Ordinary readers see the committed before- or after-image. Always plan anew AFTER locking.
CREATE FUNCTION maven.redaction_lock() RETURNS void LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog AS $$
BEGIN
    IF current_setting('transaction_isolation') <> 'read committed' THEN PERFORM maven.cleanup_refuse('redaction_isolation'); END IF;
    PERFORM singleton FROM maven.service_state WHERE singleton FOR UPDATE;
    LOCK TABLE maven.redaction_jobs,maven.redaction_identity,maven.instruction_state,maven.records,maven.memory_items,
        maven.project_lifecycle,maven.reconcile_projects,maven.reconcile_records,maven.pages,maven.sources,maven.facts,
        maven.record_versions,maven.record_sources,maven.operations,maven.instruction_proposals,maven.instruction_challenges,
        maven.page_publications,maven.instruction_receipts,maven.memory_receipts,maven.secret_adapter_consumptions,
        maven.cleanup_receipts,maven.cleanup_operations,maven.redaction_markers IN EXCLUSIVE MODE;
END
$$
-- statement
CREATE FUNCTION maven.redaction_scrub(job uuid, plan jsonb) RETURNS jsonb LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog AS $$
BEGIN
    INSERT INTO maven.redaction_work(tx,kind,id,project_id)
        SELECT pg_current_xact_id(),t->>'kind',(t->>'id')::uuid,coalesce((t->>'project_id')::uuid,'00000000-0000-0000-0000-000000000000')
        FROM jsonb_array_elements(plan->'permanent_targets') t;
    INSERT INTO maven.redaction_markers(kind,id,project_id)
        SELECT kind,id,project_id FROM maven.redaction_work WHERE tx=pg_current_xact_id() ON CONFLICT DO NOTHING;

    -- 025 bytes (not just summaries), and the token independently pinning these bytes.
    -- A retired project is deleted altogether; delivery uses permanent markers, not visibility.
    UPDATE maven.reconcile_records SET wire=maven.redaction_wire(id),item=maven.redaction_item(id)
        WHERE maven.redaction_working('project_record',id,project_id) AND NOT maven.redaction_working('project',project_id)
        AND (wire<>maven.redaction_wire(id) OR item<>maven.redaction_item(id));
    UPDATE maven.reconcile_projects p SET version=gen_random_uuid() WHERE NOT maven.redaction_working('project',p.project_id)
        AND EXISTS (SELECT 1 FROM maven.redaction_work w WHERE w.tx=pg_current_xact_id() AND w.kind='project_record' AND w.project_id=p.project_id);
    DELETE FROM maven.reconcile_records WHERE maven.redaction_working('project',project_id);
    DELETE FROM maven.reconcile_projects WHERE maven.redaction_working('project',project_id);

    -- FK order is intentional. Delete the edge itself, not just the payload on its endpoints.
    DELETE FROM maven.cleanup_operations WHERE maven.redaction_working('cleanup',operation_id);
    DELETE FROM maven.cleanup_receipts WHERE maven.redaction_working('cleanup',id);
    DELETE FROM maven.secret_adapter_consumptions WHERE maven.redaction_working('consumption',invocation_id);
    DELETE FROM maven.instruction_receipts WHERE maven.redaction_working('instruction_receipt',request_key);
    DELETE FROM maven.page_publications WHERE maven.redaction_working('publication',proposal_id);
    DELETE FROM maven.instruction_challenges WHERE maven.redaction_working('challenge',id);
    DELETE FROM maven.instruction_proposals WHERE maven.redaction_working('proposal',id);
    DELETE FROM maven.memory_receipts WHERE maven.redaction_working('memory_receipt',request_key);
    DELETE FROM maven.operations WHERE maven.redaction_working('operation',request_key);
    DELETE FROM maven.record_sources WHERE maven.redaction_working('citation',source_id);
    DELETE FROM maven.facts WHERE maven.redaction_working('record',id);
    DELETE FROM maven.pages WHERE maven.redaction_working('page',id);
    DELETE FROM maven.project_lifecycle WHERE maven.redaction_working('project',project_id);
    UPDATE maven.project_lifecycle SET archived=true,actor_id='redacted',action_key=repeat('0',64),generation=generation+1
        WHERE maven.redaction_working('lifecycle',project_id) AND (NOT archived OR actor_id<>'redacted' OR action_key<>repeat('0',64));
    DELETE FROM maven.memory_items WHERE maven.redaction_working('record',id);
    DELETE FROM maven.sources WHERE maven.redaction_working('source',id);
    DELETE FROM maven.record_versions WHERE maven.redaction_working('record',record_id);
    DELETE FROM maven.records WHERE maven.redaction_working('record',id);
    IF EXISTS (SELECT 1 FROM maven.redaction_work WHERE tx=pg_current_xact_id() AND kind IN ('proposal','page','project','source','publication')) THEN
        UPDATE maven.instruction_state SET generation=generation+1 WHERE singleton;
    END IF;

    -- Verify logical postconditions from the same inventories used during review. Never return
    -- free-text fields, original hashes, or storage locators even if a postcondition fails.
    IF EXISTS (SELECT 1 FROM maven.redaction_versions v JOIN jsonb_array_elements(plan->'permanent_targets') t ON v.target=t
        WHERE t->>'kind' NOT IN ('project_record','lifecycle')) OR EXISTS (
        SELECT 1 FROM maven.reconcile_records WHERE maven.redaction_working('project_record',id,project_id)
        AND (item<>maven.redaction_item(id) OR wire<>maven.redaction_wire(id))) OR EXISTS (
        SELECT 1 FROM maven.project_lifecycle WHERE maven.redaction_working('lifecycle',project_id)
        AND (NOT archived OR actor_id<>'redacted' OR action_key<>repeat('0',64))) THEN
        PERFORM maven.cleanup_refuse('redaction_incomplete'); END IF;
    DELETE FROM maven.redaction_work WHERE tx=pg_current_xact_id();
    RETURN maven.redaction_result(job,plan->'permanent_targets');
END
$$
-- statement
CREATE FUNCTION maven.redaction_apply(job uuid, targets jsonb, plan_digest text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog AS $$
DECLARE plan jsonb; prior maven.redaction_jobs%ROWTYPE; result_value jsonb; selection jsonb:=maven.redaction_validate(targets);
BEGIN
    IF job IS NULL OR plan_digest IS NULL OR plan_digest !~ '^[0-9a-f]{64}$' THEN PERFORM maven.cleanup_refuse('redaction_request'); END IF;
    PERFORM maven.redaction_lock();
    SELECT * INTO prior FROM maven.redaction_jobs WHERE job_id=job;
    IF FOUND THEN
        IF prior.targets<>selection OR prior.plan_digest IS DISTINCT FROM plan_digest THEN PERFORM maven.cleanup_refuse('operation_conflict'); END IF;
        RETURN prior.result;
    END IF;
    plan:=maven.redaction_plan(selection,false);
    IF plan->>'plan_digest'<>plan_digest THEN PERFORM maven.cleanup_refuse('revision_conflict'); END IF;
    result_value:=maven.redaction_scrub(job,plan);
    INSERT INTO maven.redaction_jobs(job_id,targets,permanent_targets,plan_digest,result)
        VALUES(job,selection,plan->'permanent_targets',plan_digest,result_value);
    RETURN result_value;
END
$$
-- statement
CREATE FUNCTION maven.redaction_job(job uuid) RETURNS jsonb LANGUAGE sql STABLE SET search_path = pg_catalog AS $$
    SELECT result || jsonb_build_object('plan_digest',plan_digest) FROM maven.redaction_jobs WHERE job_id=job
$$
-- statement
-- Called only on an isolated restore. A live CAS against a snapshot would either fail or
-- leave older payloads intact. Fence absent identities, but a backup-only dependent is NOT
-- authorized collateral. Leave recovery isolated for a new review in that case.
CREATE FUNCTION maven.redaction_replay(job uuid, permanent_targets jsonb) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog AS $$
DECLARE selection jsonb:=maven.redaction_validate(permanent_targets); plan jsonb; result_value jsonb; prior maven.redaction_jobs%ROWTYPE;
BEGIN
    IF job IS NULL THEN PERFORM maven.cleanup_refuse('redaction_request'); END IF;
    PERFORM maven.redaction_lock();
    IF (SELECT state FROM maven.service_state WHERE singleton) IS DISTINCT FROM 'maintenance' THEN PERFORM maven.cleanup_refuse('restore_isolation'); END IF;
    SELECT * INTO prior FROM maven.redaction_jobs WHERE job_id=job;
    IF FOUND AND prior.permanent_targets<>selection THEN PERFORM maven.cleanup_refuse('operation_conflict'); END IF;
    plan:=maven.redaction_plan(selection,true);
    IF plan->'permanent_targets'<>selection THEN PERFORM maven.cleanup_refuse('restore_scope_changed'); END IF;
    result_value:=maven.redaction_scrub(job,plan);
    IF prior.job_id IS NULL THEN
        INSERT INTO maven.redaction_jobs(job_id,targets,permanent_targets,result) VALUES(job,selection,selection,result_value);
    END IF;
    RETURN result_value;
END
$$
-- statement
REVOKE ALL ON maven.redaction_identity,maven.redaction_markers,maven.redaction_jobs,maven.redaction_work,
    maven.redaction_versions,maven.redaction_links FROM PUBLIC
-- statement
-- Migration 026 owns all current redaction_* routines; revoke by exact catalog identity to
-- avoid accidentally leaving a default-PUBLIC helper capable of minting deletion authority.
DO $$
DECLARE signature regprocedure;
BEGIN
    FOR signature IN SELECT oid::regprocedure FROM pg_proc WHERE pronamespace='maven'::regnamespace AND left(proname,10)='redaction_' LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',signature);
    END LOOP;
END
$$
