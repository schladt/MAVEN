-- Agentic memory contracts; applied migrations and historical publication receipts stay unchanged.
-- Upgrade service/control schema and client protocol together under reviewed maintenance.
ALTER TABLE maven.service_state DROP CONSTRAINT service_state_schema_version_check
-- statement
UPDATE maven.service_state SET schema_version=2 WHERE singleton
-- statement
ALTER TABLE maven.service_state ADD CONSTRAINT service_state_schema_version_check CHECK (schema_version=2)
-- statement
ALTER TABLE maven.service_state DROP CONSTRAINT service_state_minimum_client_protocol_check
-- statement
UPDATE maven.service_state SET minimum_client_protocol=3 WHERE singleton
-- statement
ALTER TABLE maven.service_state ALTER COLUMN minimum_client_protocol SET DEFAULT 3
-- statement
ALTER TABLE maven.service_state ADD CONSTRAINT service_state_minimum_client_protocol_check CHECK (minimum_client_protocol=3)
-- statement
CREATE OR REPLACE FUNCTION maven.memory_data_valid(kind_value text, payload jsonb, project uuid, tags_value jsonb) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog AS $$
DECLARE
    required text[]; optional text[] := ARRAY[]::text[]; key_value text; member jsonb;
    maximum integer; states text[];
BEGIN
    IF jsonb_typeof(payload) IS DISTINCT FROM 'object' OR octet_length(payload::text)>196608
       OR jsonb_typeof(tags_value) IS DISTINCT FROM 'array' THEN RETURN false; END IF;
    IF jsonb_array_length(tags_value)>20 OR EXISTS (
      SELECT 1 FROM jsonb_array_elements(tags_value) t WHERE jsonb_typeof(t)<>'string' OR length(t#>>'{}') NOT BETWEEN 1 AND 80)
      OR (SELECT count(*)<>count(DISTINCT t) FROM jsonb_array_elements(tags_value) t) THEN RETURN false; END IF;
    CASE kind_value
      WHEN 'entity' THEN required:=ARRAY['entity_type','name','aliases'];
      WHEN 'fact' THEN required:=ARRAY['subject_id','predicate','value'];
      WHEN 'relationship' THEN required:=ARRAY['subject_id','predicate','object_id'];
      WHEN 'task' THEN required:=ARRAY['title','description','state']; optional:=ARRAY['subject_id']; states:=ARRAY['open','in_progress','blocked','completed','cancelled'];
      WHEN 'decision' THEN required:=ARRAY['title','body_markdown','state']; optional:=ARRAY['subject_id']; states:=ARRAY['proposed','accepted','superseded'];
      WHEN 'risk' THEN required:=ARRAY['title','body_markdown','state']; optional:=ARRAY['subject_id']; states:=ARRAY['open','mitigated','closed'];
      WHEN 'note' THEN required:=ARRAY['title','body_markdown','category'];
      WHEN 'session' THEN required:=ARRAY['state','summary']; optional:=ARRAY['workflow','operation']; states:=ARRAY['active','closed','abandoned'];
      WHEN 'operation' THEN required:=ARRAY['session_id','name','state','summary']; states:=ARRAY['started','completed','failed','blocked'];
      WHEN 'setting' THEN required:=ARRAY['key','value','scope']; optional:=ARRAY['secret_ref'];
      WHEN 'checkpoint' THEN
        IF payload ? 'cutoff' THEN
          required:=ARRAY['session_id','cutoff','memory_refs','state']; optional:=ARRAY['summary']; states:=ARRAY['prepared','saved','aborted'];
        ELSE
          required:=ARRAY['session_id','candidate_digest','git_base','memory_refs','state']; optional:=ARRAY['final_commit','summary']; states:=ARRAY['prepared','committed','aborted'];
        END IF;
      ELSE RETURN false;
    END CASE;
    IF NOT payload ?& required OR EXISTS (SELECT 1 FROM jsonb_object_keys(payload) k WHERE NOT k=ANY(required || optional)) THEN RETURN false; END IF;
    IF EXISTS (SELECT 1 FROM unnest(required) k WHERE k<>'value' AND payload->k='null'::jsonb) THEN RETURN false; END IF;
    IF states IS NOT NULL AND (jsonb_typeof(payload->'state')<>'string' OR NOT (payload->>'state')=ANY(states)) THEN RETURN false; END IF;
    FOREACH key_value IN ARRAY ARRAY['title','name','description','summary','predicate','category','workflow','operation','key','body_markdown'] LOOP
      IF payload ? key_value THEN
        maximum:=CASE WHEN key_value IN ('description','summary') THEN 16000 WHEN key_value='body_markdown' THEN 131072
          WHEN key_value='key' OR (key_value='name' AND kind_value='operation') THEN 160
          WHEN key_value IN ('title','name') THEN 240 ELSE 80 END;
        IF jsonb_typeof(payload->key_value)<>'string' OR length(payload->>key_value)>maximum
          OR (key_value NOT IN ('description','summary','body_markdown') AND length(btrim(payload->>key_value))=0) THEN RETURN false; END IF;
      END IF;
    END LOOP;
    IF payload ? 'body_markdown' AND octet_length(payload->>'body_markdown')>131072 THEN RETURN false; END IF;
    FOREACH key_value IN ARRAY ARRAY['subject_id','object_id','session_id'] LOOP
      IF payload ? key_value AND (jsonb_typeof(payload->key_value)<>'string'
        OR (payload->>key_value)!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN RETURN false; END IF;
    END LOOP;
    IF kind_value='entity' THEN
      IF payload->>'entity_type' NOT IN ('person','organization','project','topic')
        OR jsonb_typeof(payload->'aliases') IS DISTINCT FROM 'array' THEN RETURN false; END IF;
      IF jsonb_array_length(payload->'aliases')>32 OR EXISTS (SELECT 1 FROM jsonb_array_elements(payload->'aliases') a
        WHERE jsonb_typeof(a)<>'string' OR length(btrim(a#>>'{}'))=0 OR length(a#>>'{}')>240)
        OR (SELECT count(*)<>count(DISTINCT a) FROM jsonb_array_elements(payload->'aliases') a) THEN RETURN false; END IF;
    END IF;
    IF kind_value='setting' THEN
      IF payload->>'scope' NOT IN ('global','project') OR ((payload->>'scope'='project')<>(project IS NOT NULL)) THEN RETURN false; END IF;
      IF payload ? 'secret_ref' THEN
        member:=payload->'secret_ref';
        IF jsonb_typeof(member) IS DISTINCT FROM 'object' OR NOT member ?& ARRAY['provider','name'] THEN RETURN false; END IF;
        IF jsonb_typeof(member->'provider')<>'string' OR member->>'provider' NOT IN ('ssm','secretsmanager') OR jsonb_typeof(member->'name')<>'string'
          OR length(member->>'name') NOT BETWEEN 1 AND 512 OR member->>'name' !~ '^[A-Za-z0-9_./:@+=-]+$'
          OR EXISTS (SELECT 1 FROM jsonb_object_keys(member) k WHERE k NOT IN ('provider','name')) THEN RETURN false; END IF;
      END IF;
    END IF;
    IF kind_value='checkpoint' THEN
      IF payload ? 'cutoff' THEN
        IF jsonb_typeof(payload->'cutoff') IS DISTINCT FROM 'number'
          OR payload->>'cutoff' !~ '^(0|[1-9][0-9]{0,9})$' THEN RETURN false; END IF;
        IF (payload->>'cutoff')::bigint>2147483647 THEN RETURN false; END IF;
      ELSE
        IF jsonb_typeof(payload->'candidate_digest')<>'string' OR jsonb_typeof(payload->'git_base')<>'string'
          OR payload->>'candidate_digest' !~ '^[0-9a-f]{64}$' OR payload->>'git_base' !~ '^([0-9a-f]{40}|[0-9a-f]{64})$'
          OR ((payload->>'state'='committed')<>(payload ? 'final_commit'))
          OR (payload ? 'final_commit' AND (jsonb_typeof(payload->'final_commit')<>'string' OR payload->>'final_commit' !~ '^([0-9a-f]{40}|[0-9a-f]{64})$')) THEN RETURN false; END IF;
      END IF;
      IF jsonb_typeof(payload->'memory_refs') IS DISTINCT FROM 'array' THEN RETURN false; END IF;
      IF jsonb_array_length(payload->'memory_refs')>100 OR (SELECT count(*)<>count(DISTINCT r) FROM jsonb_array_elements(payload->'memory_refs') r) THEN RETURN false; END IF;
      FOR member IN SELECT value FROM jsonb_array_elements(payload->'memory_refs') LOOP
        IF jsonb_typeof(member) IS DISTINCT FROM 'object' OR NOT member ?& ARRAY['record_id','revision'] THEN RETURN false; END IF;
        IF jsonb_typeof(member->'record_id')<>'string' OR jsonb_typeof(member->'revision')<>'number'
          OR member->>'record_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
          OR member->>'revision' !~ '^[1-9][0-9]{0,9}$' OR (member->>'revision')::bigint>2147483647
          OR EXISTS (SELECT 1 FROM jsonb_object_keys(member) k WHERE k NOT IN ('record_id','revision')) THEN RETURN false; END IF;
      END LOOP;
    END IF;
    RETURN true;
END
$$
-- statement
CREATE OR REPLACE FUNCTION maven.memory_write(action text, request jsonb, canonical_hash text, actor text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    key_id uuid := (request->>'request_key')::uuid;
    target uuid := (request->>'record_id')::uuid;
    current_record maven.records%ROWTYPE;
    item maven.memory_items%ROWTYPE;
    receipt maven.memory_receipts%ROWTYPE;
    scope_value text;
    kind_value text;
    project uuid;
    payload jsonb;
    tag_values jsonb;
    evidence uuid;
    next_revision integer;
    result jsonb;
    ref jsonb;
    related uuid;
    session_record maven.records%ROWTYPE;
BEGIN
    IF NOT maven.memory_protocol_allowed() THEN RETURN jsonb_build_object('status','client_incompatible'); END IF;
    IF NOT maven.memory_safe_json(request) THEN RETURN jsonb_build_object('status','invalid_request'); END IF;
    IF action NOT IN ('write','claim') OR key_id IS NULL OR target IS NULL OR actor IS NULL
       OR canonical_hash IS NULL OR canonical_hash !~ '^[0-9a-f]{64}$' THEN
        RETURN jsonb_build_object('status','invalid_request');
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('request:' || key_id::text,0));
    IF EXISTS (SELECT 1 FROM maven.operations WHERE request_key=key_id)
       OR EXISTS (SELECT 1 FROM maven.instruction_receipts WHERE request_key=key_id) THEN
        RETURN jsonb_build_object('status','idempotency_conflict');
    END IF;
    SELECT * INTO receipt FROM maven.memory_receipts WHERE request_key=key_id;
    IF FOUND THEN
        IF receipt.action<>action OR receipt.request_hash<>canonical_hash THEN RETURN jsonb_build_object('status','idempotency_conflict'); END IF;
        IF NOT maven.memory_visible(receipt.record_id,receipt.revision) THEN RETURN jsonb_build_object('status','record_unavailable'); END IF;
        RETURN receipt.result || jsonb_build_object('replayed',true);
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('record:' || target::text,0));
    SELECT * INTO current_record FROM maven.records WHERE id=target FOR UPDATE;
    SELECT * INTO item FROM maven.memory_items WHERE id=target FOR UPDATE;
    IF current_record.id IS NOT NULL AND (item.id IS NULL OR NOT maven.memory_visible(target)) THEN
        RETURN jsonb_build_object('status','record_unavailable');
    END IF;
    IF COALESCE(current_record.revision,0) <> (request->>'expected_revision')::integer THEN
        RETURN jsonb_build_object('status','revision_conflict','current_revision',COALESCE(current_record.revision,0));
    END IF;
    IF action='write' THEN
        kind_value:=request->>'kind'; scope_value:=request->>'data_scope'; project:=(request->>'project_id')::uuid;
        payload:=request->'data'; tag_values:=COALESCE(request->'tags','[]'::jsonb);
        IF request->>'classification' IS DISTINCT FROM 'Sensitive' OR scope_value NOT IN ('owner','synthetic')
           OR kind_value NOT IN ('entity','fact','relationship','task','decision','risk','note','session','operation','setting','checkpoint')
           OR jsonb_typeof(payload) IS DISTINCT FROM 'object' THEN RETURN jsonb_build_object('status','invalid_request'); END IF;
        IF NOT maven.memory_data_valid(kind_value,payload,project,tag_values) THEN RETURN jsonb_build_object('status','invalid_request'); END IF;
        IF current_record.id IS NOT NULL AND (current_record.kind<>kind_value OR current_record.data_scope<>scope_value OR item.project_id IS DISTINCT FROM project) THEN
            RETURN jsonb_build_object('status','revision_conflict','current_revision',current_record.revision);
        END IF;
        IF kind_value='entity' AND item.id IS NOT NULL AND item.data->>'entity_type' IS DISTINCT FROM payload->>'entity_type' THEN
            RETURN jsonb_build_object('status','revision_conflict','current_revision',current_record.revision);
        END IF;
        IF kind_value='session' AND payload->>'state'<>'active' AND EXISTS (
            SELECT 1 FROM maven.memory_items m WHERE m.claim_session=target AND m.claim_expires_at>clock_timestamp()) THEN
            RETURN jsonb_build_object('status','claim_conflict');
        END IF;
        IF kind_value='checkpoint' AND item.id IS NULL AND payload->>'state'<>'prepared' THEN
            RETURN jsonb_build_object('status','invalid_request');
        END IF;
        IF kind_value='task' AND (payload->>'state' IN ('completed','in_progress')
            OR (item.data->>'state' IN ('completed','cancelled'))
            OR item.claim_session IS NOT NULL) THEN
            -- Retiring a representation is not a task transition. Preserve its
            -- exact facts, existing tags and fence; never retire a live claim.
            IF item.id IS NULL OR payload IS DISTINCT FROM item.data
              OR item.claim_expires_at>clock_timestamp()
              OR NOT tag_values ? 'maven-scope-superseded'
              OR NOT tag_values @> item.tags
              OR NOT (item.tags || '["maven-scope-superseded"]'::jsonb) @> tag_values THEN
                RETURN jsonb_build_object('status','claim_conflict');
            END IF;
        END IF;
        IF kind_value='session' AND item.data->>'state' IN ('closed','abandoned') THEN RETURN jsonb_build_object('status','revision_conflict','current_revision',current_record.revision); END IF;
        IF kind_value='checkpoint' AND item.id IS NOT NULL THEN
            IF (item.data ? 'cutoff') IS DISTINCT FROM (payload ? 'cutoff')
              OR item.data->>'state'<>'prepared' THEN
                RETURN jsonb_build_object('status','revision_conflict','current_revision',current_record.revision);
            END IF;
            IF payload ? 'cutoff' THEN
                IF (item.data - 'state') <> (payload - 'state') OR item.tags <> tag_values
                  OR payload->>'state' NOT IN ('saved','aborted') THEN
                    RETURN jsonb_build_object('status','revision_conflict','current_revision',current_record.revision);
                END IF;
            ELSIF (item.data - 'state' - 'final_commit' - 'summary') <> (payload - 'state' - 'final_commit' - 'summary') THEN
                RETURN jsonb_build_object('status','revision_conflict','current_revision',current_record.revision);
            END IF;
        END IF;
        IF project IS NOT NULL AND NOT EXISTS (SELECT 1 FROM maven.memory_items p JOIN maven.records r ON r.id=p.id
            WHERE p.id=project AND r.kind='entity' AND p.data->>'entity_type'='project' AND r.data_scope=scope_value AND maven.memory_visible(p.id)) THEN
            RETURN jsonb_build_object('status','reference_unavailable');
        END IF;
        FOREACH related IN ARRAY ARRAY[(payload->>'subject_id')::uuid,(payload->>'object_id')::uuid] LOOP
            IF related IS NOT NULL AND NOT EXISTS (SELECT 1 FROM maven.records r JOIN maven.memory_items m ON m.id=r.id
                WHERE r.id=related AND r.kind='entity' AND r.data_scope=scope_value AND maven.memory_visible(r.id)) THEN
                RETURN jsonb_build_object('status','reference_unavailable');
            END IF;
        END LOOP;
        IF payload ? 'session_id' THEN
            SELECT r.* INTO session_record FROM maven.records r JOIN maven.memory_items m ON m.id=r.id
            WHERE r.id=(payload->>'session_id')::uuid AND r.kind='session' AND r.data_scope=scope_value
              AND m.project_id IS NOT DISTINCT FROM project AND m.data->>'state'='active' AND maven.memory_visible(r.id) FOR SHARE OF r;
            IF NOT FOUND THEN RETURN jsonb_build_object('status','session_unavailable'); END IF;
        END IF;
        IF kind_value='checkpoint' THEN
            FOR ref IN SELECT value FROM jsonb_array_elements(payload->'memory_refs') LOOP
                IF NOT EXISTS (SELECT 1 FROM maven.records r JOIN maven.record_versions v ON v.record_id=r.id
                    WHERE r.id=(ref->>'record_id')::uuid AND v.revision=(ref->>'revision')::integer
                    AND r.data_scope=scope_value AND r.visibility_state='visible' AND r.archived_at IS NULL
                    AND (NOT payload ? 'cutoff' OR (
                      (r.kind<>'page' AND (v.snapshot->>'project_id' IS NULL OR v.snapshot->>'project_id'=project::text))
                      OR (r.kind='page' AND (NOT (v.snapshot->'scope') ? 'project' OR v.snapshot->'scope'->>'project'=project::text))))
                    AND (maven.memory_visible(r.id,v.revision) OR EXISTS (
                      SELECT 1 FROM maven.page_publications pub WHERE pub.page_id=r.id AND pub.revision=v.revision
                        AND maven.instruction_visible(pub.proposal_id)))) THEN
                    RETURN jsonb_build_object('status','reference_unavailable');
                END IF;
            END LOOP;
        END IF;
        IF kind_value='setting' THEN
            PERFORM pg_advisory_xact_lock(hashtextextended('setting:' || COALESCE(project::text,'global') || ':' || (payload->>'key'),0));
            IF EXISTS (SELECT 1 FROM maven.memory_items m WHERE m.project_id IS NOT DISTINCT FROM project AND m.data->>'key'=payload->>'key' AND m.id<>target) THEN
                RETURN jsonb_build_object('status','setting_conflict');
            END IF;
        END IF;
    ELSE
        IF item.id IS NULL OR current_record.kind<>'task' THEN RETURN jsonb_build_object('status','record_unavailable'); END IF;
        kind_value:=current_record.kind; scope_value:=current_record.data_scope; project:=item.project_id;
        payload:=item.data; tag_values:=item.tags;
        SELECT r.* INTO session_record FROM maven.records r JOIN maven.memory_items m ON m.id=r.id
        WHERE r.id=(request->>'session_id')::uuid AND r.kind='session' AND r.data_scope=scope_value
          AND (project IS NULL OR m.project_id IS NOT DISTINCT FROM project)
          AND m.data->>'state'='active' AND maven.memory_visible(r.id) FOR SHARE OF r;
        IF NOT FOUND THEN RETURN jsonb_build_object('status','session_unavailable'); END IF;
        -- Universal tasks are available to a project session, but that session's
        -- project must still permit work even though the task has no project.
        PERFORM maven.require_project_work((SELECT project_id FROM maven.memory_items WHERE id=session_record.id));
        IF request->>'action'='claim' THEN
            IF payload->>'state' NOT IN ('open','blocked','in_progress') OR item.claim_expires_at>clock_timestamp()
                OR (request->>'lease_seconds')::integer NOT BETWEEN 30 AND 3600 THEN RETURN jsonb_build_object('status','claim_conflict'); END IF;
            item.claim_fence:=item.claim_fence+1;
            item.claim_session:=session_record.id;
            item.claim_expires_at:=clock_timestamp()+make_interval(secs=>(request->>'lease_seconds')::integer);
            payload:=jsonb_set(payload,'{state}','"in_progress"');
        ELSIF request->>'action' IN ('release','complete') THEN
            IF item.claim_session IS DISTINCT FROM session_record.id OR item.claim_fence IS DISTINCT FROM (request->>'fence')::integer
                OR item.claim_expires_at IS NULL OR item.claim_expires_at<=clock_timestamp() THEN RETURN jsonb_build_object('status','claim_conflict'); END IF;
            item.claim_session:=NULL; item.claim_expires_at:=NULL;
            payload:=jsonb_set(payload,'{state}',to_jsonb(CASE WHEN request->>'action'='complete' THEN 'completed'::text ELSE 'open'::text END));
        ELSE RETURN jsonb_build_object('status','invalid_request'); END IF;
    END IF;
    next_revision:=COALESCE(current_record.revision,0)+1;
    evidence:=maven.instruction_source(request->'source',actor,request->>'change_reason',scope_value);
    IF current_record.id IS NULL THEN
        INSERT INTO maven.records(id,kind,revision,data_scope) VALUES(target,kind_value,next_revision,scope_value);
        INSERT INTO maven.memory_items(id,project_id,data,tags,subject_id,object_id,session_id)
        VALUES(target,project,payload,tag_values,(payload->>'subject_id')::uuid,(payload->>'object_id')::uuid,(payload->>'session_id')::uuid);
    ELSE
        UPDATE maven.records SET revision=next_revision,updated_at=transaction_timestamp() WHERE id=target;
        UPDATE maven.memory_items SET data=payload,tags=tag_values,subject_id=(payload->>'subject_id')::uuid,
          object_id=(payload->>'object_id')::uuid,session_id=(payload->>'session_id')::uuid,
          claim_session=item.claim_session,claim_fence=item.claim_fence,claim_expires_at=item.claim_expires_at WHERE id=target;
    END IF;
    INSERT INTO maven.record_versions(record_id,revision,snapshot,content_hash,actor_id,change_reason)
    VALUES(target,next_revision,jsonb_build_object('data',payload,'tags',tag_values,'project_id',project),
      encode(sha256(convert_to(jsonb_build_object('data',payload,'tags',tag_values,'project_id',project)::text,'UTF8')),'hex'),actor,request->>'change_reason');
    INSERT INTO maven.record_sources(record_id,revision,source_id,source_revision) VALUES(target,next_revision,evidence,1);
    result:=jsonb_build_object('record_id',target,'revision',next_revision,'request_key',key_id,'replayed',false);
    IF action='claim' THEN result:=result || jsonb_build_object('fence',item.claim_fence,'session_id',item.claim_session,
       'expires_at',item.claim_expires_at,'state',payload->>'state'); END IF;
    INSERT INTO maven.memory_receipts(request_key,request_hash,action,record_id,revision,result) VALUES(key_id,canonical_hash,action,target,next_revision,result);
    RETURN result;
END
$$
-- statement
CREATE OR REPLACE FUNCTION maven.memory_search(request jsonb) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    WITH selected AS (
      SELECT r.id AS record_id,r.revision,r.kind,m.project_id,r.data_scope,
        COALESCE(m.data->>'title',m.data->>'name',m.data->>'key',m.data->>'predicate','') AS title,m.data->>'state' AS state,m.tags
      FROM maven.records r JOIN maven.memory_items m ON m.id=r.id
      WHERE maven.memory_visible(r.id)
        AND NOT m.tags ? 'maven-scope-superseded'
        AND (NOT request ? 'kind' OR r.kind=request->>'kind')
        AND CASE request->>'scope'
          WHEN 'universal' THEN m.project_id IS NULL
          WHEN 'project' THEN m.project_id=(request->>'project_id')::uuid
          WHEN 'context' THEN m.project_id IS NULL OR m.project_id=(request->>'project_id')::uuid
          WHEN 'all' THEN true
          ELSE NOT request ? 'scope' AND (NOT request ? 'project_id' OR m.project_id=(request->>'project_id')::uuid)
        END
        AND (NOT request ? 'subject_id' OR m.subject_id=(request->>'subject_id')::uuid)
        AND (NOT request ? 'state' OR m.data->>'state'=request->>'state')
        AND (NOT request ? 'tag' OR m.tags ? (request->>'tag'))
        AND (NOT request ? 'query' OR strpos(lower(m.data::text),lower(request->>'query'))>0)
        AND (NOT request ? 'after_id' OR r.id>(request->>'after_id')::uuid)
      ORDER BY r.id LIMIT LEAST(COALESCE((request->>'limit')::integer,20),20)+1
    ), page AS (SELECT * FROM selected ORDER BY record_id LIMIT LEAST(COALESCE((request->>'limit')::integer,20),20))
    SELECT jsonb_build_object('records',COALESCE((SELECT jsonb_agg(to_jsonb(p) ORDER BY record_id) FROM page p),'[]'::jsonb),
      'next_after_id',CASE WHEN (SELECT count(*) FROM selected)>LEAST(COALESCE((request->>'limit')::integer,20),20)
        THEN (SELECT record_id FROM page ORDER BY record_id DESC LIMIT 1) ELSE NULL END)
$$
-- statement
CREATE OR REPLACE FUNCTION maven.memory_context(request jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    pages_value jsonb; tasks_value jsonb; row_value record; dependency text; dependency_page record;
    selected_ids uuid[] := ARRAY[]::uuid[]; queue uuid[]; current_id uuid; index_value integer:=1;
BEGIN
    SELECT COALESCE(array_agg(selected.id ORDER BY selected.slug COLLATE "C"),ARRAY[]::uuid[]) INTO selected_ids
    FROM (
      SELECT p.id,p.slug FROM maven.pages p JOIN maven.records r ON r.id=p.id
      JOIN maven.page_publications pub ON pub.page_id=p.id AND pub.revision=r.revision
      WHERE p.authority_state='active' AND r.data_scope='owner' AND maven.instruction_visible(pub.proposal_id)
        AND (NOT p.scope ? 'project' OR p.scope->>'project'=request->>'project_id')
        AND (NOT p.scope ? 'workflow' OR p.scope->>'workflow'=request->>'workflow')
        AND (NOT p.scope ? 'operation' OR p.scope->>'operation'=request->>'operation')
        AND (p.page_kind='directive' OR p.slug=request->>'workflow' OR p.slug=request->>'operation'
          OR (p.page_kind='skill' AND p.slug=request->>'skill_slug')
          OR p.scope->>'workflow'=request->>'workflow' OR p.scope->>'operation'=request->>'operation')
      ORDER BY p.slug COLLATE "C" LIMIT 101
    ) selected;
    IF cardinality(selected_ids)>100 THEN RETURN jsonb_build_object('status','context_incomplete'); END IF;
    IF request ? 'skill_slug' AND NOT EXISTS (
      SELECT 1 FROM maven.pages WHERE id=ANY(selected_ids) AND slug=request->>'skill_slug' AND page_kind='skill'
    ) THEN
      RETURN jsonb_build_object('status','page_unavailable');
    END IF;
    queue:=selected_ids;
    WHILE index_value<=cardinality(queue) LOOP
        current_id:=queue[index_value]; index_value:=index_value+1;
        SELECT v.snapshot INTO row_value FROM maven.records r JOIN maven.record_versions v ON v.record_id=r.id AND v.revision=r.revision WHERE r.id=current_id;
        FOR dependency IN SELECT jsonb_array_elements_text(COALESCE(row_value.snapshot->'requires','[]'::jsonb)) LOOP
            SELECT p.id,p.scope,p.authority_state,pub.proposal_id,r.data_scope INTO dependency_page
            FROM maven.pages p JOIN maven.records r ON r.id=p.id JOIN maven.page_publications pub ON pub.page_id=p.id AND pub.revision=r.revision WHERE p.slug=dependency;
            IF NOT FOUND OR dependency_page.data_scope<>'owner' OR dependency_page.authority_state<>'active' OR NOT maven.instruction_visible(dependency_page.proposal_id)
               OR (dependency_page.scope ? 'project' AND dependency_page.scope->>'project' IS DISTINCT FROM request->>'project_id') THEN
                RETURN jsonb_build_object('status','dependency_unavailable');
            END IF;
            IF NOT dependency_page.id=ANY(selected_ids) THEN
                selected_ids:=array_append(selected_ids,dependency_page.id); queue:=array_append(queue,dependency_page.id);
                IF cardinality(selected_ids)>100 THEN RETURN jsonb_build_object('status','context_incomplete'); END IF;
            END IF;
        END LOOP;
    END LOOP;
    IF EXISTS (WITH RECURSIVE edges AS (
        SELECT p.slug,jsonb_array_elements_text(COALESCE(v.snapshot->'requires','[]'::jsonb)) AS dependency
        FROM maven.pages p JOIN maven.records r ON r.id=p.id JOIN maven.record_versions v ON v.record_id=r.id AND v.revision=r.revision WHERE p.id=ANY(selected_ids)
      ), reach(root,node) AS (SELECT edges.slug,edges.dependency FROM edges UNION SELECT reach.root,edges.dependency FROM reach JOIN edges ON edges.slug=reach.node)
      SELECT 1 FROM reach WHERE root=node) THEN RETURN jsonb_build_object('status','dependency_conflict'); END IF;
    SELECT COALESCE(jsonb_agg(jsonb_build_object('page_id',p.id,'slug',p.slug,'page_kind',p.page_kind,'title',p.title,
      'scope',p.scope,'revision',r.revision,'content_hash',v.content_hash,'data_scope',r.data_scope)
      || CASE WHEN v.snapshot ? 'requires' THEN jsonb_build_object('requires',v.snapshot->'requires') ELSE '{}'::jsonb END
      || CASE WHEN v.snapshot ? 'bundle' THEN jsonb_build_object('bundle',v.snapshot->'bundle') ELSE '{}'::jsonb END ORDER BY p.slug COLLATE "C"),'[]'::jsonb)
      INTO pages_value FROM maven.pages p JOIN maven.records r ON r.id=p.id
      JOIN maven.record_versions v ON v.record_id=r.id AND v.revision=r.revision WHERE p.id=ANY(selected_ids);
    -- Working tasks are explicitly bounded; terminal tasks are not context.
    WITH selected AS (
      SELECT r.id AS record_id,r.revision,r.kind,m.project_id,r.data_scope,m.data->>'title' AS title,m.data->>'state' AS state,m.tags
      FROM maven.records r JOIN maven.memory_items m ON m.id=r.id WHERE r.kind='task' AND r.data_scope='owner' AND maven.memory_visible(r.id)
        AND NOT m.tags ? 'maven-scope-superseded'
        AND m.data->>'state' IN ('open','blocked','in_progress') AND (m.project_id IS NULL OR m.project_id=(request->>'project_id')::uuid)
      ORDER BY r.id LIMIT 21
    ), page AS (SELECT * FROM selected ORDER BY record_id LIMIT 20)
    SELECT jsonb_build_object('records',COALESCE((SELECT jsonb_agg(to_jsonb(p) ORDER BY record_id) FROM page p),'[]'::jsonb),
      'next_after_id',CASE WHEN (SELECT count(*) FROM selected)>20 THEN (SELECT record_id FROM page ORDER BY record_id DESC LIMIT 1) ELSE NULL END) INTO tasks_value;
    RETURN jsonb_build_object('pages',pages_value,'authority_generation',(SELECT generation FROM maven.instruction_state WHERE singleton),
      'tasks',tasks_value->'records','next_task_after_id',tasks_value->'next_after_id',
      'minimum_client_protocol',(SELECT minimum_client_protocol FROM maven.service_state WHERE singleton));
END
$$

