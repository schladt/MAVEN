-- Signed daily delivery discloses only opaque retired identities; it cannot create
-- markers or read the content of an archived, unavailable, or unknown project.
CREATE FUNCTION maven.redaction_delivery(request jsonb) RETURNS json
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE
    state jsonb := maven.lock_service_state(); p uuid; result json; n bigint; token text;
    items json; more boolean; cursor_kind text; cursor_id uuid;
    uuid_pattern constant text := '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$';
BEGIN
    IF state->>'state' IS DISTINCT FROM 'ready' OR state->>'instance_id' IS DISTINCT FROM request->>'instance_id'
        OR state->>'recovery_epoch' IS DISTINCT FROM request->>'recovery_epoch' THEN
        result := json_build_object('status','context_stale');
    ELSIF jsonb_typeof(request) IS DISTINCT FROM 'object' OR coalesce(request->>'project_id','') !~ uuid_pattern
       OR request - ARRAY['instance_id','recovery_epoch','project_id','bundle_hashes','after_kind','after_id','expected_token'] <> '{}'::jsonb
       OR ((request ?| ARRAY['after_kind','after_id','expected_token']) AND
           (NOT request ?& ARRAY['after_kind','after_id','expected_token'] OR request ? 'bundle_hashes'
            OR coalesce(request->>'after_id','') !~ uuid_pattern OR coalesce(request->>'expected_token','') !~ '^[0-9a-f]{64}$')) THEN
        result := json_build_object('status','invalid_request');
    ELSIF request ? 'bundle_hashes' THEN
        IF jsonb_typeof(request->'bundle_hashes') IS DISTINCT FROM 'array' OR jsonb_array_length(request->'bundle_hashes') NOT BETWEEN 1 AND 50
           OR EXISTS (SELECT FROM jsonb_array_elements_text(request->'bundle_hashes') s WHERE coalesce(s,'') !~ '^[a-f0-9]{64}$') THEN
            result := json_build_object('status','invalid_request');
        ELSE
            SELECT json_build_object('retired',COALESCE(json_agg(i.ord-1 ORDER BY i.ord),'[]'::json)) INTO result
            FROM jsonb_array_elements_text(request->'bundle_hashes') WITH ORDINALITY i(sha,ord)
            WHERE EXISTS (SELECT FROM maven.redaction_markers m WHERE m.kind='skill_bundle' AND m.id=maven.redaction_bundle_id(i.sha));
        END IF;
    ELSE
        p := (request->>'project_id')::uuid;
        cursor_kind := request->>'after_kind'; cursor_id := (request->>'after_id')::uuid;
        -- One statement snapshot for both count and page. The permanent set only grows.
        WITH relevant AS NOT MATERIALIZED (
            SELECT kind COLLATE "C" AS kind,id FROM maven.redaction_markers
            WHERE CASE WHEN kind='project_record' THEN project_id=p WHEN kind='project' THEN id=p ELSE true END
        ), selected AS MATERIALIZED (
            SELECT * FROM relevant WHERE cursor_kind IS NULL OR (kind,id)>(cursor_kind COLLATE "C",cursor_id)
            ORDER BY kind,id LIMIT 65
        ), page AS (SELECT * FROM selected ORDER BY kind,id LIMIT 64)
        SELECT (SELECT count(*) FROM relevant), (SELECT count(*)>64 FROM selected),
            COALESCE(json_agg(CASE WHEN kind='project_record' THEN json_build_object('kind',kind,'id',id,'project_id',p)
                                  ELSE json_build_object('kind',kind,'id',id) END ORDER BY kind,id),'[]'::json)
        INTO n,more,items FROM page;
        token := encode(sha256(convert_to(concat_ws(':',p,state->>'instance_id',state->>'recovery_epoch',n),'UTF8')),'hex');
        IF request ? 'expected_token' AND token IS DISTINCT FROM request->>'expected_token' THEN
            result := json_build_object('status','view_conflict');
        ELSE
            result := json_build_object('token',token,'total',n,'more',more,'targets',items);
        END IF;
    END IF;
    RETURN json_build_object('service_state',state,'authority_generation',0,'result',result);
END
$$
-- statement
REVOKE ALL ON FUNCTION maven.redaction_delivery(jsonb) FROM PUBLIC
