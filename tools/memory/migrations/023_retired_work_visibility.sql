-- UUID scopes compare as identities; deliberate legacy names remain case-sensitive.
-- Stored proposals/snapshots and their exact reviewed hashes are never rewritten.
CREATE FUNCTION maven.project_scope_key(value text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog AS $$
    SELECT CASE WHEN value ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        THEN value::uuid::text ELSE value END
$$
-- statement
CREATE OR REPLACE FUNCTION maven.require_project_scope(scope_value jsonb) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE project_text text := scope_value->>'project';
BEGIN
    -- Existing named scopes remain valid. Only UUID scopes bind cloud projects.
    IF project_text ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        PERFORM maven.require_project_work(project_text::uuid);
    END IF;
END
$$
-- statement
CREATE OR REPLACE FUNCTION maven.instruction_index(request jsonb) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    WITH selected AS (
      SELECT p.slug, jsonb_build_object('page_id',p.id,'slug',p.slug,'page_kind',p.page_kind,
        'title',p.title,'scope',p.scope,'revision',r.revision,'content_hash',v.content_hash,'data_scope',r.data_scope)
        || CASE WHEN v.snapshot ? 'requires' THEN jsonb_build_object('requires',v.snapshot->'requires') ELSE '{}'::jsonb END
        || CASE WHEN v.snapshot ? 'bundle' THEN jsonb_build_object('bundle',v.snapshot->'bundle') ELSE '{}'::jsonb END
        || CASE WHEN v.snapshot ? 'description' THEN jsonb_build_object('description',v.snapshot->'description') ELSE '{}'::jsonb END AS metadata
      FROM maven.pages p JOIN maven.records r ON r.id=p.id
      JOIN maven.record_versions v ON v.record_id=r.id AND v.revision=r.revision
      JOIN maven.page_publications pub ON pub.page_id=r.id AND pub.revision=r.revision
      WHERE p.authority_state='active' AND maven.instruction_visible(pub.proposal_id)
        AND (NOT request ? 'scope' OR (r.data_scope='owner' AND (
          NOT p.scope ? 'project' OR (request->>'scope'='context' AND maven.project_scope_key(p.scope->>'project')=maven.project_scope_key(request->>'project_id')))))
        AND (NOT request ? 'page_kind' OR p.page_kind=request->>'page_kind')
        AND (NOT request ? 'after_slug' OR p.slug COLLATE "C" > (request->>'after_slug') COLLATE "C")
      ORDER BY p.slug COLLATE "C" LIMIT LEAST(COALESCE((request->>'limit')::integer,10),10)+1
    ), page AS (
      SELECT * FROM selected ORDER BY slug COLLATE "C" LIMIT LEAST(COALESCE((request->>'limit')::integer,10),10)
    )
    SELECT jsonb_build_object('pages',COALESCE((SELECT jsonb_agg(metadata ORDER BY slug COLLATE "C") FROM page),'[]'::jsonb),
      'next_after_slug',CASE WHEN (SELECT count(*) FROM selected)>LEAST(COALESCE((request->>'limit')::integer,10),10)
        THEN (SELECT slug FROM page ORDER BY slug COLLATE "C" DESC LIMIT 1) ELSE NULL END,
      'authority_generation',(SELECT generation FROM maven.instruction_state WHERE singleton),
      'capabilities',jsonb_build_array('instructions_only'))
$$
-- statement
CREATE OR REPLACE FUNCTION maven.memory_context(request jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    pages_value jsonb; tasks_value jsonb; row_value record; dependency text; dependency_page record;
    selected_ids uuid[] := ARRAY[]::uuid[]; queue uuid[]; current_id uuid; index_value integer:=1;
    inline_value boolean;
BEGIN
    IF maven.project_archived((request->>'project_id')::uuid) THEN
        RETURN jsonb_build_object('status','project_archived');
    END IF;
    SELECT COALESCE(array_agg(selected.id ORDER BY selected.slug COLLATE "C"),ARRAY[]::uuid[]) INTO selected_ids
    FROM (
      SELECT p.id,p.slug FROM maven.pages p JOIN maven.records r ON r.id=p.id
      JOIN maven.page_publications pub ON pub.page_id=p.id AND pub.revision=r.revision
      WHERE p.authority_state='active' AND r.data_scope='owner' AND maven.instruction_visible(pub.proposal_id)
        AND (NOT p.scope ? 'project' OR maven.project_scope_key(p.scope->>'project')=maven.project_scope_key(request->>'project_id'))
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
               OR (dependency_page.scope ? 'project' AND maven.project_scope_key(dependency_page.scope->>'project') IS DISTINCT FROM maven.project_scope_key(request->>'project_id')) THEN
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
    SELECT COALESCE(sum(octet_length(v.snapshot->>'body_markdown')),0)<=98304 INTO inline_value
      FROM maven.records r JOIN maven.record_versions v ON v.record_id=r.id AND v.revision=r.revision WHERE r.id=ANY(selected_ids);
    SELECT COALESCE(jsonb_agg(jsonb_build_object('page_id',p.id,'slug',p.slug,'page_kind',p.page_kind,'title',p.title,
      'scope',p.scope,'revision',r.revision,'content_hash',v.content_hash,'data_scope',r.data_scope)
      || CASE WHEN v.snapshot ? 'requires' THEN jsonb_build_object('requires',v.snapshot->'requires') ELSE '{}'::jsonb END
      || CASE WHEN v.snapshot ? 'bundle' THEN jsonb_build_object('bundle',v.snapshot->'bundle') ELSE '{}'::jsonb END
      || CASE WHEN v.snapshot ? 'description' THEN jsonb_build_object('description',v.snapshot->'description') ELSE '{}'::jsonb END
      || CASE WHEN inline_value THEN jsonb_build_object('body_markdown',v.snapshot->>'body_markdown',
           'body_sha256',encode(sha256(convert_to(v.snapshot->>'body_markdown','UTF8')),'hex')) ELSE '{}'::jsonb END
      ORDER BY p.slug COLLATE "C"),'[]'::jsonb)
      INTO pages_value FROM maven.pages p JOIN maven.records r ON r.id=p.id
      JOIN maven.record_versions v ON v.record_id=r.id AND v.revision=r.revision WHERE p.id=ANY(selected_ids);
    IF request->'instructions_only'='true'::jsonb THEN
        tasks_value:=jsonb_build_object('records','[]'::jsonb,'next_after_id',NULL);
    ELSE
        WITH selected AS (
          SELECT r.id AS record_id,r.revision,r.kind,m.project_id,r.data_scope,m.data->>'title' AS title,m.data->>'state' AS state,m.tags
          FROM maven.records r JOIN maven.memory_items m ON m.id=r.id WHERE r.kind='task' AND r.data_scope='owner' AND maven.memory_visible(r.id)
            AND NOT m.tags ? 'maven-scope-superseded' AND NOT maven.memory_archived(m.tags,m.data)
            AND m.data->>'state' IN ('open','blocked','in_progress') AND (m.project_id IS NULL OR m.project_id=(request->>'project_id')::uuid)
          ORDER BY r.id LIMIT 21
        ), page AS (SELECT * FROM selected ORDER BY record_id LIMIT 20)
        SELECT jsonb_build_object('records',COALESCE((SELECT jsonb_agg(to_jsonb(p) ORDER BY record_id) FROM page p),'[]'::jsonb),
          'next_after_id',CASE WHEN (SELECT count(*) FROM selected)>20 THEN (SELECT record_id FROM page ORDER BY record_id DESC LIMIT 1) ELSE NULL END) INTO tasks_value;
    END IF;
    RETURN jsonb_build_object('pages',pages_value,'bodies_inline',inline_value,
      'authority_generation',(SELECT generation FROM maven.instruction_state WHERE singleton),
      'tasks',tasks_value->'records','next_task_after_id',tasks_value->'next_after_id',
      'minimum_client_protocol',(SELECT minimum_client_protocol FROM maven.service_state WHERE singleton))
      || CASE WHEN request->'instructions_only'='true'::jsonb THEN '{"instructions_only":true}'::jsonb ELSE '{}'::jsonb END;
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
                      OR (r.kind='page' AND (NOT (v.snapshot->'scope') ? 'project' OR maven.project_scope_key(v.snapshot->'scope'->>'project')=project::text))))
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
            -- Retirement prevents new work, not historical reads or original receipt replay.
            IF item.tags ? 'maven-scope-superseded' OR maven.memory_archived(item.tags,item.data) THEN
                RETURN jsonb_build_object('status','claim_conflict');
            END IF;
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
CREATE OR REPLACE FUNCTION maven.secret_adapter_policy(request jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE home uuid; page_value record; document jsonb; governing_context jsonb;
BEGIN
    IF jsonb_typeof(request) IS DISTINCT FROM 'object'
       OR request - ARRAY['home_id','slug'] <> '{}'::jsonb
       OR NOT request ?& ARRAY['home_id','slug']
       OR COALESCE(request->>'slug','') !~ '^secret-(adapter|grant)-[a-z0-9]+(-[a-z0-9]+)*$'
       OR length(request->>'slug')>80 THEN
        RETURN jsonb_build_object('status','invalid_request');
    END IF;
    home:=(request->>'home_id')::uuid;
    IF NOT EXISTS (SELECT 1 FROM maven.records r JOIN maven.memory_items m ON m.id=r.id
        WHERE r.id=home AND r.kind='entity' AND r.data_scope='owner' AND m.project_id IS NULL
          AND m.data->>'entity_type'='project' AND maven.memory_visible(r.id))
       OR maven.project_archived(home) THEN
        RETURN jsonb_build_object('status','reference_unavailable');
    END IF;
    SELECT p.id,p.slug,p.scope,p.page_kind,p.authority_state,r.revision,r.data_scope,v.snapshot,pub.proposal_id
      INTO page_value FROM maven.pages p JOIN maven.records r ON r.id=p.id
      JOIN maven.record_versions v ON v.record_id=p.id AND v.revision=r.revision
      JOIN maven.page_publications pub ON pub.page_id=p.id AND pub.revision=r.revision
      WHERE p.slug=request->>'slug';
    IF NOT FOUND OR page_value.page_kind<>'procedure' OR page_value.authority_state<>'active'
       OR page_value.data_scope<>'owner' OR (page_value.scope - 'project')<>'{}'::jsonb
       OR maven.project_scope_key(page_value.scope->>'project') IS DISTINCT FROM home::text
       OR page_value.snapshot->'requires' IS DISTINCT FROM '["maven-secrets"]'::jsonb
       OR NOT maven.instruction_visible(page_value.proposal_id) THEN
        RETURN jsonb_build_object('status','reference_unavailable');
    END IF;
    -- Resolve the entire governing skill/directive dependency graph, not just the
    -- immediate skill. Revoked, unavailable, conflicting or incomplete authority
    -- anywhere in that graph disables execution. Consumption holds the generation
    -- lock while this fresh cloud context is checked.
    governing_context:=maven.memory_context(jsonb_build_object(
        'project_id',home,'skill_slug','maven-secrets','instructions_only',true));
    IF governing_context IS NULL OR governing_context ? 'status' THEN
        RETURN jsonb_build_object('status','dependency_unavailable');
    END IF;
    document:=(page_value.snapshot->>'body_markdown')::jsonb;
    IF jsonb_typeof(document) IS DISTINCT FROM 'object' OR document->'version' IS DISTINCT FROM '1'::jsonb
       OR document->>'kind' IS DISTINCT FROM (CASE WHEN page_value.slug LIKE 'secret-adapter-%'
           THEN 'secret-adapter' ELSE 'secret-grant' END) THEN
        RETURN jsonb_build_object('status','invalid_request');
    END IF;
    RETURN jsonb_build_object('home_id',home,'page_id',page_value.id,'slug',page_value.slug,
        'revision',page_value.revision,'document',document);
EXCEPTION WHEN invalid_text_representation OR invalid_parameter_value OR numeric_value_out_of_range THEN
    RETURN jsonb_build_object('status','invalid_request');
END
$$
-- statement
REVOKE ALL ON FUNCTION maven.project_scope_key(text) FROM PUBLIC
-- statement
-- Existing function ACLs and SECURITY DEFINER owners survive CREATE OR REPLACE.
UPDATE maven.instruction_state SET generation=generation+1 WHERE singleton
