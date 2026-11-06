-- Bundle metadata is optional and never rewrites historical candidates or hashes.
CREATE FUNCTION maven.skill_bundle_valid(candidate jsonb) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog AS $$
DECLARE bundle jsonb;
BEGIN
    IF NOT candidate ? 'bundle' THEN RETURN true; END IF;
    bundle:=candidate->'bundle';
    IF candidate->>'page_kind' IS DISTINCT FROM 'skill'
       OR jsonb_typeof(bundle) IS DISTINCT FROM 'object' THEN RETURN false; END IF;
    IF NOT bundle ?& ARRAY['version','sha256','size_bytes','manifest_sha256','load_policy']
       OR bundle - ARRAY['version','sha256','size_bytes','manifest_sha256','load_policy'] <> '{}'::jsonb THEN
        RETURN false;
    END IF;
    IF jsonb_typeof(bundle->'version') IS DISTINCT FROM 'string'
       OR (bundle->>'version') COLLATE "C" !~ '^[A-Za-z0-9._-]{1,80}$'
       OR jsonb_typeof(bundle->'sha256') IS DISTINCT FROM 'string'
       OR (bundle->>'sha256') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR jsonb_typeof(bundle->'manifest_sha256') IS DISTINCT FROM 'string'
       OR (bundle->>'manifest_sha256') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR jsonb_typeof(bundle->'load_policy') IS DISTINCT FROM 'string'
       OR bundle->>'load_policy' NOT IN ('always','on-demand')
       OR jsonb_typeof(bundle->'size_bytes') IS DISTINCT FROM 'number' THEN RETURN false; END IF;
    -- JSON's lexical integer form rejects booleans and fractional metadata.
    IF bundle->>'size_bytes' !~ '^[0-9]+$' THEN RETURN false; END IF;
    RETURN (bundle->>'size_bytes')::numeric BETWEEN 1 AND 268435456;
END
$$
-- statement
CREATE FUNCTION maven.guard_skill_bundle() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE candidate_value jsonb;
BEGIN
    IF TG_TABLE_NAME='instruction_proposals' THEN
        candidate_value:=NEW.candidate;
    ELSE
        SELECT candidate INTO candidate_value FROM maven.instruction_proposals WHERE id=NEW.proposal_id;
    END IF;
    IF NOT maven.skill_bundle_valid(candidate_value) THEN
        RAISE EXCEPTION 'invalid skill bundle' USING ERRCODE='22023';
    END IF;
    RETURN NEW;
END
$$
-- statement
CREATE TRIGGER valid_proposal_skill_bundle BEFORE INSERT OR UPDATE ON maven.instruction_proposals
FOR EACH ROW EXECUTE FUNCTION maven.guard_skill_bundle()
-- statement
CREATE TRIGGER valid_publication_skill_bundle BEFORE INSERT ON maven.page_publications
FOR EACH ROW EXECUTE FUNCTION maven.guard_skill_bundle()
-- statement
REVOKE ALL ON FUNCTION maven.skill_bundle_valid(jsonb),maven.guard_skill_bundle() FROM PUBLIC
-- statement
CREATE OR REPLACE FUNCTION maven.instruction_index(request jsonb) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    WITH selected AS (
      SELECT p.slug, jsonb_build_object('page_id',p.id,'slug',p.slug,'page_kind',p.page_kind,
        'title',p.title,'scope',p.scope,'revision',r.revision,'content_hash',v.content_hash,'data_scope',r.data_scope)
        || CASE WHEN v.snapshot ? 'requires' THEN jsonb_build_object('requires',v.snapshot->'requires') ELSE '{}'::jsonb END
        || CASE WHEN v.snapshot ? 'bundle' THEN jsonb_build_object('bundle',v.snapshot->'bundle') ELSE '{}'::jsonb END AS metadata
      FROM maven.pages p JOIN maven.records r ON r.id=p.id
      JOIN maven.record_versions v ON v.record_id=r.id AND v.revision=r.revision
      JOIN maven.page_publications pub ON pub.page_id=r.id AND pub.revision=r.revision
      WHERE p.authority_state='active' AND maven.instruction_visible(pub.proposal_id)
        AND (NOT request ? 'page_kind' OR p.page_kind=request->>'page_kind')
        AND (NOT request ? 'after_slug' OR p.slug COLLATE "C" > (request->>'after_slug') COLLATE "C")
      ORDER BY p.slug COLLATE "C" LIMIT LEAST(COALESCE((request->>'limit')::integer,10),10)+1
    ), page AS (
      SELECT * FROM selected ORDER BY slug COLLATE "C" LIMIT LEAST(COALESCE((request->>'limit')::integer,10),10)
    )
    SELECT jsonb_build_object('pages',COALESCE((SELECT jsonb_agg(metadata ORDER BY slug COLLATE "C") FROM page),'[]'::jsonb),
      'next_after_slug',CASE WHEN (SELECT count(*) FROM selected)>LEAST(COALESCE((request->>'limit')::integer,10),10)
        THEN (SELECT slug FROM page ORDER BY slug COLLATE "C" DESC LIMIT 1) ELSE NULL END,
      'authority_generation',(SELECT generation FROM maven.instruction_state WHERE singleton))
$$
-- statement
-- A named skill is an explicit root, not a replacement for caller selectors.
-- Existing approval, scope, dependency and bounded-context checks still apply.
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
