-- Project archival preserves history but hides it from default discovery and governed work.
-- This is deliberately separate from memory_visible: Forget/Erase and source visibility
-- remain mandatory even when an explicit historical read/search opts into archived projects.
CREATE FUNCTION maven.project_archived(project uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT EXISTS (SELECT 1 FROM maven.project_lifecycle WHERE project_id=project AND archived)
$$
-- statement
CREATE FUNCTION maven.project_scope_archived(scope_value jsonb) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT CASE WHEN scope_value->>'project' ~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
      THEN maven.project_archived((scope_value->>'project')::uuid) ELSE false END
$$
-- statement
CREATE OR REPLACE FUNCTION maven.memory_read(request jsonb) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT COALESCE((SELECT jsonb_build_object('record_id',r.id,'kind',r.kind,'classification',r.classification,
      'data_scope',r.data_scope,'project_id',v.snapshot->'project_id','revision',v.revision,'data',v.snapshot->'data',
      'tags',v.snapshot->'tags','source',sv.snapshot,'source_id',s.id,'source_revision',sv.revision,
      'project_archived',maven.project_archived(r.id) OR maven.project_archived(m.project_id)
        OR maven.project_archived((v.snapshot->>'project_id')::uuid),
      'content_hash',v.content_hash,'actor_id',v.actor_id,'change_reason',v.change_reason,'recorded_at',v.recorded_at,'operative',false,
      'claim',CASE WHEN NOT request ? 'revision' AND m.claim_session IS NOT NULL THEN
        jsonb_build_object('session_id',m.claim_session,'fence',m.claim_fence,'expires_at',m.claim_expires_at) ELSE NULL END)
    FROM maven.records r JOIN maven.memory_items m ON m.id=r.id
    JOIN maven.record_versions v ON v.record_id=r.id AND v.revision=COALESCE((request->>'revision')::integer,r.revision)
    JOIN maven.record_sources c ON c.record_id=r.id AND c.revision=v.revision
    JOIN maven.records s ON s.id=c.source_id JOIN maven.record_versions sv ON sv.record_id=s.id AND sv.revision=c.source_revision
    WHERE r.id=(request->>'record_id')::uuid AND maven.memory_visible(r.id,v.revision)
      AND (request->'include_archived'='true'::jsonb OR (
        NOT maven.project_archived(r.id) AND NOT maven.project_archived(m.project_id)
        AND NOT maven.project_archived((v.snapshot->>'project_id')::uuid)))
    LIMIT 1),jsonb_build_object('status','record_unavailable'))
$$
-- statement
CREATE OR REPLACE FUNCTION maven.memory_search(request jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    limit_value integer:=LEAST(COALESCE((request->>'limit')::integer,20),20);
    offset_value integer:=COALESCE((request->>'offset')::integer,0);
    include_archived boolean:=COALESCE(request->'include_archived'='true'::jsonb,false);
    patterns text[];
    page_value jsonb;
BEGIN
    IF NOT request ? 'query' THEN
        WITH selected AS (
          SELECT r.id AS record_id,r.revision,r.kind,m.project_id,r.data_scope,
            maven.project_archived(r.id) OR maven.project_archived(m.project_id) AS project_archived,
            maven.memory_title(m.data) AS title,m.data->>'state' AS state,m.tags
          FROM maven.records r JOIN maven.memory_items m ON m.id=r.id
          WHERE maven.memory_visible(r.id)
            AND NOT m.tags ? 'maven-scope-superseded'
            AND (include_archived OR (NOT maven.memory_archived(m.tags,m.data)
              AND NOT maven.project_archived(r.id) AND NOT maven.project_archived(m.project_id)))
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
        maven.project_archived(r.id) OR maven.project_archived(m.project_id) AS project_archived,
        maven.memory_title(m.data) AS title,m.data->>'state' AS state,m.tags,maven.memory_body_text(m.data) AS body
      FROM maven.records r JOIN maven.memory_items m ON m.id=r.id
      WHERE maven.memory_visible(r.id)
        AND NOT m.tags ? 'maven-scope-superseded'
        AND (include_archived OR (NOT maven.memory_archived(m.tags,m.data)
          AND NOT maven.project_archived(r.id) AND NOT maven.project_archived(m.project_id)))
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
        'kind',p.kind,'project_id',p.project_id,'project_archived',p.project_archived,'data_scope',p.data_scope,'title',p.title,'state',p.state,'tags',p.tags,
        'rank',p.rank,'snippet',maven.memory_snippet(p.body,patterns)) ORDER BY p.rank DESC,p.updated_at DESC,p.record_id) FROM page p),'[]'::jsonb),
      'next_after_id',NULL,
      'next_offset',CASE WHEN (SELECT count(*) FROM ordered)>limit_value AND offset_value+limit_value<=1000
        THEN offset_value+limit_value ELSE NULL END) INTO page_value;
    RETURN page_value;
END
$$
-- statement
CREATE OR REPLACE FUNCTION maven.memory_project_index(home uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    WITH projects AS (
      SELECT r.id,m.data->>'name' AS name,false AS archived,
        (SELECT max(a.updated_at) FROM maven.records a JOIN maven.memory_items i ON i.id=a.id
          WHERE i.project_id=r.id AND a.kind IN ('session','checkpoint')) AS last_activity
      FROM maven.records r JOIN maven.memory_items m ON m.id=r.id
      WHERE r.kind='entity' AND r.data_scope='owner' AND m.project_id IS NULL
        AND m.data->>'entity_type'='project' AND maven.memory_visible(r.id)
        AND NOT maven.project_archived(r.id)
    )
    SELECT COALESCE(jsonb_agg(jsonb_build_object('project_id',p.id,'name',p.name,'archived',p.archived,
        'last_activity',p.last_activity,'home',p.id=home)
      ORDER BY p.last_activity DESC NULLS LAST,p.name,p.id),'[]'::jsonb)
    FROM (SELECT * FROM projects ORDER BY last_activity DESC NULLS LAST,name,id LIMIT 100) p
$$
-- statement
CREATE OR REPLACE FUNCTION maven.memory_revisions(request jsonb) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT jsonb_build_object('revisions',COALESCE(jsonb_object_agg(r.id::text,r.revision),'{}'::jsonb))
    FROM maven.records r JOIN maven.memory_items m ON m.id=r.id
    WHERE r.id IN (SELECT ids.value::uuid FROM jsonb_array_elements_text(COALESCE(request->'record_ids','[]'::jsonb)) AS ids(value) LIMIT 100)
      AND maven.memory_visible(r.id)
      AND NOT maven.project_archived(r.id) AND NOT maven.project_archived(m.project_id)
$$
-- statement
-- Receipt lookup is not an alternate historical content/discovery route.
CREATE OR REPLACE FUNCTION maven.memory_lookup(key_id uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT receipt.result || jsonb_build_object('replayed',true)
    FROM maven.memory_receipts receipt JOIN maven.memory_items m ON m.id=receipt.record_id
    JOIN maven.record_versions v ON v.record_id=receipt.record_id AND v.revision=receipt.revision
    WHERE receipt.request_key=key_id AND maven.memory_visible(receipt.record_id,receipt.revision)
      AND NOT maven.project_archived(m.id) AND NOT maven.project_archived(m.project_id)
      AND NOT maven.project_archived((v.snapshot->>'project_id')::uuid)
$$
-- statement
-- All authority discovery (including raw page index/read and proposal lookup) stays inactive.
-- Named legacy scopes are not UUID project identities and retain their existing semantics.
CREATE OR REPLACE FUNCTION maven.instruction_visible(proposal uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT EXISTS (
        SELECT 1 FROM maven.instruction_proposals p
        JOIN maven.records s ON s.id = p.source_id
        LEFT JOIN maven.records r ON r.id = p.page_id
        LEFT JOIN maven.pages page ON page.id = p.page_id
        LEFT JOIN maven.page_publications pub ON pub.proposal_id = p.id
        LEFT JOIN maven.records a ON a.id = pub.approval_source_id
        WHERE p.id = proposal AND s.kind = 'source' AND s.data_scope = p.data_scope
          AND s.visibility_state = 'visible' AND s.archived_at IS NULL
          AND (r.id IS NULL OR (r.kind = 'page' AND r.data_scope = p.data_scope AND r.visibility_state = 'visible' AND r.archived_at IS NULL))
          AND (p.accepted_revision IS NULL OR (a.kind = 'source' AND a.data_scope = p.data_scope AND a.visibility_state = 'visible' AND a.archived_at IS NULL))
          AND NOT maven.project_scope_archived(p.candidate->'scope')
          AND NOT maven.project_scope_archived(page.scope)
    )
$$
-- statement
REVOKE ALL ON FUNCTION maven.project_archived(uuid),maven.project_scope_archived(jsonb) FROM PUBLIC
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
-- The Home/peer enrichment wrapper delegates here before returning any context.
CREATE OR REPLACE FUNCTION maven.memory_project_core(request jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    project uuid:=(request->>'project_id')::uuid;
    requester uuid:=(request->>'session_id')::uuid;
    context_value jsonb;
BEGIN
    IF project IS NULL OR requester IS NULL THEN RETURN jsonb_build_object('status','invalid_request'); END IF;
    IF maven.project_archived(project) THEN RETURN jsonb_build_object('status','project_archived'); END IF;
    WITH sessions AS (
      SELECT r.id,m.data->>'state' AS state,maven.memory_json_object(m.data->>'summary') AS meta,r.created_at
      FROM maven.records r JOIN maven.memory_items m ON m.id=r.id
      WHERE r.kind='session' AND r.data_scope='owner' AND m.project_id=project AND maven.memory_visible(r.id)
    ), checkpoints AS (
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
CREATE OR REPLACE FUNCTION maven.cleanup_candidates() RETURNS jsonb
LANGUAGE sql STABLE SET search_path = pg_catalog AS $$
    WITH items AS (
      SELECT r.id,r.kind,r.data_scope,r.revision,r.updated_at,m.project_id,m.data,m.tags,maven.memory_title(m.data) AS title
      FROM maven.records r JOIN maven.memory_items m ON m.id=r.id
      WHERE r.kind IN ('entity','fact','relationship','task','decision','risk','note','setting')
        AND r.visibility_state='visible' AND maven.memory_visible(r.id)
        AND NOT maven.project_archived(r.id) AND NOT maven.project_archived(m.project_id)
    ), ranked AS (
      SELECT i.*,row_number() OVER (PARTITION BY i.kind,i.data_scope,i.project_id,lower(i.title)
        ORDER BY i.updated_at DESC,i.id DESC) AS recency FROM items i
    ), flagged AS (
      SELECT r.*,array_remove(ARRAY[
        CASE WHEN r.tags ? 'maven-scope-superseded' THEN 'superseded' END,
        CASE WHEN maven.memory_archived(r.tags,r.data) THEN 'archived' END,
        CASE WHEN r.kind='task' AND r.data->>'state' IN ('open','in_progress','blocked')
          AND r.updated_at<transaction_timestamp()-interval '30 days' THEN 'stale_task' END,
        CASE WHEN btrim(r.title)<>'' AND r.recency>1 THEN 'duplicate' END,
        CASE WHEN length(COALESCE(maven.memory_body_text(r.data),''))>20000 THEN 'oversized' END,
        CASE WHEN r.project_id IS NULL AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(r.tags) t WHERE t LIKE 'client:%')
          THEN 'client_universal' END],NULL) AS reasons
      FROM ranked r
    ), listed AS (
      SELECT * FROM flagged WHERE cardinality(reasons)>0 ORDER BY updated_at,id LIMIT 501
    ), page AS (SELECT * FROM listed ORDER BY updated_at,id LIMIT 500)
    SELECT jsonb_build_object('candidates',COALESCE((SELECT jsonb_agg(jsonb_build_object('record_id',p.id,'kind',p.kind,
        'scope',CASE WHEN p.project_id IS NULL THEN 'universal' ELSE 'project' END,'project_id',p.project_id,
        'title',p.title,'revision',p.revision,'updated_at',p.updated_at,'reasons',to_jsonb(p.reasons),
        'suggested',CASE WHEN 'superseded'=ANY(p.reasons) THEN 'erase'
          WHEN p.reasons && ARRAY['archived','duplicate','client_universal'] THEN 'forget' ELSE 'archive' END)
        ORDER BY p.updated_at,p.id) FROM page p),'[]'::jsonb),
      'truncated',(SELECT count(*) FROM listed)>500)
$$
-- statement
-- Governed writes lock instruction_state before project-work. Lifecycle administration
-- must use that same order before changing this table; unchanged replays do not invalidate.
CREATE FUNCTION maven.project_archive_generation() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
BEGIN
    IF (TG_OP='INSERT' AND NEW.archived)
       OR (TG_OP='UPDATE' AND OLD.archived IS DISTINCT FROM NEW.archived)
       OR (TG_OP='DELETE' AND OLD.archived) THEN
        UPDATE maven.instruction_state SET generation=generation+1 WHERE singleton;
    END IF;
    RETURN NULL;
END
$$
-- statement
REVOKE ALL ON FUNCTION maven.project_archive_generation() FROM PUBLIC
-- statement
CREATE TRIGGER project_archive_generation AFTER INSERT OR UPDATE OR DELETE ON maven.project_lifecycle
FOR EACH ROW EXECUTE FUNCTION maven.project_archive_generation()
-- statement
CREATE OR REPLACE FUNCTION maven.instruction_read(request jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    proposal maven.instruction_proposals%ROWTYPE;
    publication maven.page_publications%ROWTYPE;
    version maven.record_versions%ROWTYPE;
    page_record maven.records%ROWTYPE;
    candidate jsonb;
    provenance jsonb;
    offset_value integer := COALESCE((request->>'offset')::integer, 0);
    body text;
    body_length integer;
    operative boolean := false;
    authority text;
BEGIN
    IF request ? 'proposal_id' THEN
        SELECT * INTO proposal FROM maven.instruction_proposals WHERE id = (request->>'proposal_id')::uuid;
        IF NOT FOUND OR NOT maven.instruction_visible(proposal.id) THEN
            RETURN jsonb_build_object('status', 'proposal_unavailable');
        END IF;
        candidate := proposal.candidate;
        authority := 'draft';
    ELSE
        SELECT r.* INTO page_record FROM maven.pages p JOIN maven.records r ON r.id = p.id
        WHERE p.slug = request->>'slug' AND NOT maven.project_scope_archived(p.scope);
        IF NOT FOUND THEN RETURN jsonb_build_object('status', 'page_unavailable'); END IF;
        IF request ? 'expected_revision' AND page_record.revision <> (request->>'expected_revision')::integer THEN
            RETURN jsonb_build_object('status', 'revision_conflict', 'current_revision', page_record.revision);
        END IF;
        SELECT * INTO version FROM maven.record_versions
        WHERE record_id = page_record.id AND revision = COALESCE((request->>'revision')::integer, page_record.revision);
        IF NOT FOUND THEN RETURN jsonb_build_object('status', 'page_unavailable'); END IF;
        SELECT * INTO publication FROM maven.page_publications WHERE page_id = page_record.id AND revision = version.revision;
        SELECT * INTO proposal FROM maven.instruction_proposals WHERE id = publication.proposal_id;
        IF NOT FOUND OR NOT maven.instruction_visible(proposal.id) THEN
            RETURN jsonb_build_object('status', 'page_unavailable');
        END IF;
        candidate := version.snapshot;
        authority := candidate->>'authority_state';
        operative := NOT request ? 'revision' AND authority = 'active';
    END IF;
    body := candidate->>'body_markdown';
    body_length := char_length(body);
    IF offset_value > body_length THEN RETURN jsonb_build_object('status', 'invalid_request'); END IF;
    SELECT jsonb_build_object('source_id', proposal.source_id, 'source_revision', 1, 'source', v.snapshot,
        'actor_id', proposal.actor_id, 'change_reason', proposal.change_reason) INTO provenance
    FROM maven.record_versions v WHERE v.record_id = proposal.source_id AND v.revision = 1;
    IF publication.page_id IS NOT NULL THEN
        SELECT provenance || jsonb_build_object('approval_source_id', publication.approval_source_id,
            'approval_source_revision', 1, 'approval_source', v.snapshot, 'approval_actor_id', publication.actor_id)
        INTO provenance FROM maven.record_versions v WHERE v.record_id = publication.approval_source_id AND v.revision = 1;
    END IF;
    RETURN (candidate - 'body_markdown') || jsonb_build_object(
        'page_id', proposal.page_id, 'proposal_id', proposal.id,
        'revision', COALESCE(version.revision, 0), 'content_hash', proposal.candidate_hash,
        'authority_state', authority, 'operative', operative, 'data_scope', proposal.data_scope,
        'candidate_authority_state', candidate->>'authority_state',
        'body_markdown', substring(body FROM offset_value + 1 FOR 4096), 'offset', offset_value,
        'next_offset', CASE WHEN offset_value + 4096 < body_length THEN offset_value + 4096 ELSE NULL END,
        'total_characters', body_length, 'body_sha256', encode(sha256(convert_to(body, 'UTF8')), 'hex'),
        'provenance', provenance, 'authority_generation', (SELECT generation FROM maven.instruction_state WHERE singleton));
END
$$
-- statement
-- Invalidate authority loaded before this migration when already-archived projects exist.
UPDATE maven.instruction_state SET generation=generation+1
WHERE singleton AND EXISTS (SELECT 1 FROM maven.project_lifecycle WHERE archived)
