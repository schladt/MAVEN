-- One dispatcher statement per request, ranked retrieval and server-side startup context.
-- Applied migrations stay unchanged; client protocol and this runtime ship together.
ALTER TABLE maven.service_state DROP CONSTRAINT service_state_minimum_client_protocol_check
-- statement
UPDATE maven.service_state SET minimum_client_protocol=4 WHERE singleton
-- statement
ALTER TABLE maven.service_state ALTER COLUMN minimum_client_protocol SET DEFAULT 4
-- statement
ALTER TABLE maven.service_state ADD CONSTRAINT service_state_minimum_client_protocol_check CHECK (minimum_client_protocol=4)
-- statement
-- The same row lock as lock_service_state(), plus the instruction generation read in that transaction.
CREATE FUNCTION maven.memory_service_state() RETURNS jsonb
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT jsonb_build_object('service_state',maven.lock_service_state(),
      'authority_generation',(SELECT generation FROM maven.instruction_state WHERE singleton))
$$
-- statement
CREATE FUNCTION maven.memory_archived(tags_value jsonb, payload jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS $$
    SELECT tags_value ? 'archived' OR COALESCE(payload->>'state','')='archived'
$$
-- statement
CREATE FUNCTION maven.memory_title(payload jsonb) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS $$
    SELECT COALESCE(payload->>'title',payload->>'name',payload->>'key',payload->>'predicate','')
$$
-- statement
-- Prose first (body, description, summary), then every other string value except the one presented as the title.
CREATE FUNCTION maven.memory_body_text(payload jsonb) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS $$
    SELECT concat_ws(E'\n',payload->>'body_markdown',payload->>'description',payload->>'summary',
      (SELECT string_agg(item #>> '{}',E'\n')
       FROM jsonb_path_query(payload - 'body_markdown' - 'description' - 'summary' - CASE WHEN payload ? 'title' THEN 'title'
           WHEN payload ? 'name' THEN 'name' WHEN payload ? 'key' THEN 'key' WHEN payload ? 'predicate' THEN 'predicate' ELSE '' END,
         'strict $.** ? (@.type() == "string")') AS item))
$$
-- statement
-- Whitespace tokens (at most 8), each matched case-insensitively where a word starts.
-- Every non-word character is escaped, so a token is always literal text.
CREATE FUNCTION maven.memory_search_patterns(query text) RETURNS text[]
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS $$
    SELECT COALESCE(array_agg('(^|[^[:alnum:]_])' || regexp_replace(token,'([^[:alnum:]_])','\\\1','g') ORDER BY position),
      ARRAY[]::text[])
    FROM (SELECT token,position FROM regexp_split_to_table(btrim(COALESCE(query,'')),'\s+') WITH ORDINALITY AS t(token,position)
      WHERE token<>'' ORDER BY position LIMIT 8) tokens
$$
-- statement
CREATE FUNCTION maven.memory_snippet(body text, patterns text[]) RETURNS text
LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog AS $$
DECLARE pattern text; first_match integer; position_value integer;
BEGIN
    IF body IS NULL OR body='' THEN RETURN NULL; END IF;
    FOREACH pattern IN ARRAY patterns LOOP
        IF body ~* pattern THEN
            -- The leftmost match is replaced by its leading boundary, leaving exactly the text before the token.
            position_value:=char_length(regexp_replace(body,pattern || '.*$','\1','i'))+1;
            IF first_match IS NULL OR position_value<first_match THEN first_match:=position_value; END IF;
        END IF;
    END LOOP;
    IF first_match IS NULL THEN RETURN NULL; END IF;
    RETURN btrim(regexp_replace(substring(body FROM greatest(1,first_match-80) FOR 240),'\s+',' ','g'));
END
$$
-- statement
CREATE OR REPLACE FUNCTION maven.memory_search(request jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    limit_value integer:=LEAST(COALESCE((request->>'limit')::integer,20),20);
    offset_value integer:=COALESCE((request->>'offset')::integer,0);
    include_archived boolean:=COALESCE((request->>'include_archived')::boolean,false);
    patterns text[];
    page_value jsonb;
BEGIN
    IF NOT request ? 'query' THEN
        WITH selected AS (
          SELECT r.id AS record_id,r.revision,r.kind,m.project_id,r.data_scope,
            maven.memory_title(m.data) AS title,m.data->>'state' AS state,m.tags
          FROM maven.records r JOIN maven.memory_items m ON m.id=r.id
          WHERE maven.memory_visible(r.id)
            AND NOT m.tags ? 'maven-scope-superseded'
            AND (include_archived OR NOT maven.memory_archived(m.tags,m.data))
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
            AND (NOT request ? 'after_id' OR r.id>(request->>'after_id')::uuid)
          ORDER BY r.id LIMIT limit_value+1
        ), page AS (SELECT * FROM selected ORDER BY record_id LIMIT limit_value)
        SELECT jsonb_build_object('records',COALESCE((SELECT jsonb_agg(to_jsonb(p) || '{"snippet":null}'::jsonb ORDER BY record_id) FROM page p),'[]'::jsonb),
          'next_after_id',CASE WHEN (SELECT count(*) FROM selected)>limit_value
            THEN (SELECT record_id FROM page ORDER BY record_id DESC LIMIT 1) ELSE NULL END,
          'next_offset',NULL) INTO page_value;
        RETURN page_value;
    END IF;
    patterns:=maven.memory_search_patterns(request->>'query');
    IF cardinality(patterns)=0 THEN RETURN jsonb_build_object('status','invalid_request'); END IF;
    WITH candidates AS (
      SELECT r.id AS record_id,r.revision,r.kind,m.project_id,r.data_scope,r.updated_at,
        maven.memory_title(m.data) AS title,m.data->>'state' AS state,m.tags,maven.memory_body_text(m.data) AS body
      FROM maven.records r JOIN maven.memory_items m ON m.id=r.id
      WHERE maven.memory_visible(r.id)
        AND NOT m.tags ? 'maven-scope-superseded'
        AND (include_archived OR NOT maven.memory_archived(m.tags,m.data))
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
    ), scored AS (
      -- Every token must match somewhere; rank sums title 3, tag 2 and body 1 per token.
      SELECT c.*,s.rank FROM candidates c CROSS JOIN LATERAL (
        SELECT sum(x.score)::integer AS rank,bool_and(x.score>0) AS matched FROM (
          SELECT (CASE WHEN c.title ~* pattern THEN 3 ELSE 0 END)
            + (CASE WHEN EXISTS (SELECT 1 FROM jsonb_array_elements_text(c.tags) tag WHERE tag ~* pattern) THEN 2 ELSE 0 END)
            + (CASE WHEN COALESCE(c.body ~* pattern,false) THEN 1 ELSE 0 END) AS score
          FROM unnest(patterns) AS pattern) x) s
      WHERE s.matched
    ), ordered AS (
      SELECT * FROM scored ORDER BY rank DESC,updated_at DESC,record_id LIMIT limit_value+1 OFFSET offset_value
    ), page AS (SELECT * FROM ordered ORDER BY rank DESC,updated_at DESC,record_id LIMIT limit_value)
    SELECT jsonb_build_object('records',COALESCE((SELECT jsonb_agg(jsonb_build_object('record_id',p.record_id,'revision',p.revision,
        'kind',p.kind,'project_id',p.project_id,'data_scope',p.data_scope,'title',p.title,'state',p.state,'tags',p.tags,
        'rank',p.rank,'snippet',maven.memory_snippet(p.body,patterns)) ORDER BY p.rank DESC,p.updated_at DESC,p.record_id) FROM page p),'[]'::jsonb),
      'next_after_id',NULL,
      'next_offset',CASE WHEN (SELECT count(*) FROM ordered)>limit_value AND offset_value+limit_value<=1000
        THEN offset_value+limit_value ELSE NULL END) INTO page_value;
    RETURN page_value;
END
$$
-- statement
CREATE OR REPLACE FUNCTION maven.memory_context(request jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    pages_value jsonb; tasks_value jsonb; row_value record; dependency text; dependency_page record;
    selected_ids uuid[] := ARRAY[]::uuid[]; queue uuid[]; current_id uuid; index_value integer:=1;
    inline_value boolean;
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
    -- Exact approved bodies travel inline while small; larger sets use bounded page reads.
    SELECT COALESCE(sum(octet_length(v.snapshot->>'body_markdown')),0)<=98304 INTO inline_value
      FROM maven.records r JOIN maven.record_versions v ON v.record_id=r.id AND v.revision=r.revision WHERE r.id=ANY(selected_ids);
    SELECT COALESCE(jsonb_agg(jsonb_build_object('page_id',p.id,'slug',p.slug,'page_kind',p.page_kind,'title',p.title,
      'scope',p.scope,'revision',r.revision,'content_hash',v.content_hash,'data_scope',r.data_scope)
      || CASE WHEN v.snapshot ? 'requires' THEN jsonb_build_object('requires',v.snapshot->'requires') ELSE '{}'::jsonb END
      || CASE WHEN v.snapshot ? 'bundle' THEN jsonb_build_object('bundle',v.snapshot->'bundle') ELSE '{}'::jsonb END
      || CASE WHEN inline_value THEN jsonb_build_object('body_markdown',v.snapshot->>'body_markdown',
           'body_sha256',encode(sha256(convert_to(v.snapshot->>'body_markdown','UTF8')),'hex')) ELSE '{}'::jsonb END
      ORDER BY p.slug COLLATE "C"),'[]'::jsonb)
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
    RETURN jsonb_build_object('pages',pages_value,'bodies_inline',inline_value,
      'authority_generation',(SELECT generation FROM maven.instruction_state WHERE singleton),
      'tasks',tasks_value->'records','next_task_after_id',tasks_value->'next_after_id',
      'minimum_client_protocol',(SELECT minimum_client_protocol FROM maven.service_state WHERE singleton));
END
$$
-- statement
-- Session metadata is client-authored JSON text; malformed text is treated as absent, never fatal.
CREATE FUNCTION maven.memory_json_object(value text) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog AS $$
BEGIN
    IF value IS NULL OR left(btrim(value),1)<>'{' THEN RETURN NULL; END IF;
    RETURN value::jsonb;
EXCEPTION WHEN invalid_text_representation THEN
    RETURN NULL;
END
$$
-- statement
CREATE FUNCTION maven.memory_project_context(request jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    project uuid:=(request->>'project_id')::uuid;
    requester uuid:=(request->>'session_id')::uuid;
    context_value jsonb;
BEGIN
    IF project IS NULL OR requester IS NULL THEN RETURN jsonb_build_object('status','invalid_request'); END IF;
    WITH sessions AS (
      SELECT r.id,m.data->>'state' AS state,maven.memory_json_object(m.data->>'summary') AS meta,r.created_at
      FROM maven.records r JOIN maven.memory_items m ON m.id=r.id
      WHERE r.kind='session' AND r.data_scope='owner' AND m.project_id=project AND maven.memory_visible(r.id)
    ), checkpoints AS (
      -- Memory checkpoints carry the handover; Git checkpoints keep it in their session's summarized metadata.
      SELECT r.id AS record_id,r.revision,r.updated_at AS recorded_at,s.id AS session_id,s.state AS session_state,
        CASE WHEN m.data ? 'cutoff' THEN m.data->>'summary' ELSE s.meta->>'summary' END AS summary
      FROM maven.records r JOIN maven.memory_items m ON m.id=r.id JOIN sessions s ON s.id=m.session_id
      WHERE r.kind='checkpoint' AND r.data_scope='owner' AND m.project_id=project
        AND m.data->>'state' IN ('saved','committed') AND NOT m.tags ? 'maven-scope-superseded' AND maven.memory_visible(r.id)
    ), latest AS (
      SELECT * FROM checkpoints ORDER BY recorded_at DESC,record_id DESC LIMIT 1
    ), peers AS (
      SELECT * FROM sessions WHERE state='active' AND id<>requester ORDER BY created_at DESC,id LIMIT 20
    ), work AS (
      SELECT r.id,r.revision,r.kind,r.updated_at,m.project_id,m.data,m.tags
      FROM maven.records r JOIN maven.memory_items m ON m.id=r.id
      WHERE r.kind IN ('task','risk','decision') AND r.data_scope='owner' AND (m.project_id IS NULL OR m.project_id=project)
        AND NOT m.tags ? 'maven-scope-superseded' AND NOT maven.memory_archived(m.tags,m.data) AND maven.memory_visible(r.id)
    ), tasks AS (
      SELECT * FROM work WHERE kind='task' AND data->>'state' IN ('open','in_progress','blocked') ORDER BY updated_at DESC,id LIMIT 50
    ), blockers AS (
      SELECT * FROM work WHERE kind='risk' AND data->>'state'='open' ORDER BY updated_at DESC,id LIMIT 50
    ), preferences AS (
      SELECT * FROM work WHERE kind='decision' AND project_id IS NULL AND data->>'state'='accepted' ORDER BY updated_at DESC,id LIMIT 20
    )
    SELECT jsonb_build_object(
      'latest_handover',(SELECT jsonb_build_object('session_id',l.session_id,'checkpoint_record_id',l.record_id,'revision',l.revision,
        'recorded_at',l.recorded_at,'session_state',l.session_state,'summary',l.summary) FROM latest l),
      'active_peers',COALESCE((SELECT jsonb_agg(jsonb_build_object('session_id',p.id,'harness',p.meta->>'harness','started_at',p.created_at,
          'latest_checkpoint_at',c.recorded_at,'latest_summary',c.summary) ORDER BY p.created_at DESC,p.id)
        FROM peers p LEFT JOIN LATERAL (SELECT k.recorded_at,k.summary FROM checkpoints k WHERE k.session_id=p.id
          ORDER BY k.recorded_at DESC,k.record_id DESC LIMIT 1) c ON true),'[]'::jsonb),
      'tasks',COALESCE((SELECT jsonb_agg(jsonb_build_object('record_id',t.id,'revision',t.revision,'title',t.data->>'title',
          'state',t.data->>'state','tags',t.tags) ORDER BY t.updated_at DESC,t.id) FROM tasks t),'[]'::jsonb),
      'blockers',COALESCE((SELECT jsonb_agg(jsonb_build_object('record_id',b.id,'revision',b.revision,'title',b.data->>'title',
          'state',b.data->>'state','tags',b.tags) ORDER BY b.updated_at DESC,b.id) FROM blockers b),'[]'::jsonb),
      'owner_preferences',COALESCE((SELECT jsonb_agg(jsonb_build_object('record_id',d.id,'revision',d.revision,'title',d.data->>'title',
          'body_markdown',left(d.data->>'body_markdown',1000)) ORDER BY d.updated_at DESC,d.id) FROM preferences d),'[]'::jsonb))
    INTO context_value;
    RETURN context_value;
END
$$
-- statement
CREATE FUNCTION maven.memory_revisions(request jsonb) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT jsonb_build_object('revisions',COALESCE(jsonb_object_agg(r.id::text,r.revision),'{}'::jsonb))
    FROM maven.records r JOIN maven.memory_items m ON m.id=r.id
    WHERE r.id IN (SELECT ids.value::uuid FROM jsonb_array_elements_text(COALESCE(request->'record_ids','[]'::jsonb)) AS ids(value) LIMIT 100)
      AND maven.memory_visible(r.id)
$$
-- statement
-- One statement per request: service-state lock, protocol gate and route under one snapshot boundary.
-- The caller compares service_state with its control document before trusting result.
CREATE FUNCTION maven.memory_request(route text, request jsonb, request_hash text, candidate_hash text, actor text, client_protocol integer) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog SET lock_timeout = '1s' AS $$
DECLARE envelope jsonb; result jsonb;
BEGIN
    envelope:=maven.memory_service_state();
    IF envelope->'service_state'->>'state' IS DISTINCT FROM 'ready' THEN
        RETURN envelope || jsonb_build_object('result',NULL);
    END IF;
    IF client_protocol IS NULL OR client_protocol<1 OR jsonb_typeof(request) IS DISTINCT FROM 'object' THEN
        RETURN envelope || jsonb_build_object('result',jsonb_build_object('status','invalid_request'));
    END IF;
    PERFORM set_config('maven.client_protocol',client_protocol::text,true);
    IF NOT maven.memory_protocol_allowed() THEN
        RETURN envelope || jsonb_build_object('result',jsonb_build_object('status','client_incompatible'));
    END IF;
    IF route='/v1/memory/write' THEN result:=maven.memory_write('write',request,request_hash,actor);
    ELSIF route='/v1/memory/claim' THEN result:=maven.memory_write('claim',request,request_hash,actor);
    ELSIF route='/v1/memory/read' THEN result:=maven.memory_read(request);
    ELSIF route='/v1/memory/search' THEN result:=maven.memory_search(request);
    ELSIF route='/v1/memory/context' THEN result:=maven.memory_context(request);
    ELSIF route='/v1/memory/project_context' THEN result:=maven.memory_project_context(request);
    ELSIF route='/v1/memory/revisions' THEN result:=maven.memory_revisions(request);
    ELSIF route='/v1/proposals/create' THEN result:=maven.instruction_write('create',request,request_hash,candidate_hash,actor);
    ELSIF route='/v1/proposals/resolve' THEN result:=maven.instruction_write('resolve',request,request_hash,candidate_hash,actor);
    ELSIF route='/v1/proposals/challenge' THEN result:=maven.instruction_challenge((request->>'proposal_id')::uuid,actor);
    ELSIF route='/v1/pages/index' THEN result:=maven.instruction_index(request);
    ELSIF route='/v1/pages/read' THEN result:=maven.instruction_read(request);
    ELSE result:=jsonb_build_object('status','invalid_request');
    END IF;
    -- A route reporting its own generation (context, pages, publication) is authoritative for this response.
    RETURN jsonb_build_object('service_state',envelope->'service_state',
      'authority_generation',CASE WHEN jsonb_typeof(result->'authority_generation')='number' THEN result->'authority_generation'
        ELSE (SELECT to_jsonb(generation) FROM maven.instruction_state WHERE singleton) END,
      'result',result);
END
$$
-- statement
REVOKE ALL ON FUNCTION maven.memory_service_state(),maven.memory_archived(jsonb,jsonb),maven.memory_title(jsonb),
    maven.memory_body_text(jsonb),maven.memory_search_patterns(text),maven.memory_snippet(text,text[]),
    maven.memory_json_object(text),maven.memory_project_context(jsonb),maven.memory_revisions(jsonb),
    maven.memory_request(text,jsonb,text,text,text,integer) FROM PUBLIC
