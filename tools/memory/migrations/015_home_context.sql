-- MAVEN Home startup index: the Home project's context lists the owner's projects (identity,
-- name, archived state, last activity) without summaries, tasks or notes. Other projects'
-- context is unchanged. Every project's active peers also carry their machine and last
-- activity, and are marked stale after 24 hours without activity, so a session left open on
-- another machine is recognizable. The existing body is kept as memory_project_core.
ALTER FUNCTION maven.memory_project_context(jsonb) RENAME TO memory_project_core
-- statement
CREATE FUNCTION maven.memory_project_index(home uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    WITH projects AS (
      SELECT r.id,m.data->>'name' AS name,COALESCE(l.archived,false) AS archived,
        (SELECT max(a.updated_at) FROM maven.records a JOIN maven.memory_items i ON i.id=a.id
          WHERE i.project_id=r.id AND a.kind IN ('session','checkpoint')) AS last_activity
      FROM maven.records r JOIN maven.memory_items m ON m.id=r.id
      LEFT JOIN maven.project_lifecycle l ON l.project_id=r.id
      WHERE r.kind='entity' AND r.data_scope='owner' AND m.project_id IS NULL
        AND m.data->>'entity_type'='project' AND maven.memory_visible(r.id)
    )
    SELECT COALESCE(jsonb_agg(jsonb_build_object('project_id',p.id,'name',p.name,'archived',p.archived,
        'last_activity',p.last_activity,'home',p.id=home)
      ORDER BY p.last_activity DESC NULLS LAST,p.name,p.id),'[]'::jsonb)
    FROM (SELECT * FROM projects ORDER BY last_activity DESC NULLS LAST,name,id LIMIT 100) p
$$
-- statement
CREATE FUNCTION maven.memory_project_context(request jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    context_value jsonb:=maven.memory_project_core(request);
    project uuid;
BEGIN
    IF context_value ? 'status' THEN RETURN context_value; END IF;
    project:=(request->>'project_id')::uuid;
    context_value:=jsonb_set(context_value,'{active_peers}',COALESCE((
      SELECT jsonb_agg(p.peer || jsonb_build_object('machine',maven.memory_json_object(m.data->>'summary')->>'machine',
          'last_activity',activity.at,'stale',activity.at<clock_timestamp()-interval '24 hours',
          'revision',r.revision) ORDER BY p.ordinality)
      FROM jsonb_array_elements(context_value->'active_peers') WITH ORDINALITY p(peer,ordinality)
      JOIN maven.records r ON r.id=(p.peer->>'session_id')::uuid
      JOIN maven.memory_items m ON m.id=r.id
      CROSS JOIN LATERAL (SELECT GREATEST(r.updated_at,(SELECT max(c.updated_at) FROM maven.records c
          JOIN maven.memory_items ci ON ci.id=c.id WHERE c.kind='checkpoint' AND ci.session_id=r.id)) AS at) activity),'[]'::jsonb));
    IF EXISTS (SELECT 1 FROM maven.records r JOIN maven.memory_items m ON m.id=r.id
               WHERE r.id=project AND r.kind='entity' AND m.project_id IS NULL
                 AND m.data->>'entity_type'='project' AND m.tags ? 'maven-home' AND maven.memory_visible(r.id)) THEN
        context_value:=context_value || jsonb_build_object('project_index',maven.memory_project_index(project));
    END IF;
    RETURN context_value;
END
$$
-- statement
REVOKE ALL ON FUNCTION maven.memory_project_core(jsonb),maven.memory_project_index(uuid),
    maven.memory_project_context(jsonb) FROM PUBLIC
