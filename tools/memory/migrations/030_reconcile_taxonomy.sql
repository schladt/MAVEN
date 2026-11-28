-- Read just the facts needed to validate an apply, before its transaction commits.
-- The ordinary application role never receives SELECT on project records. Reuse
-- the existing project visibility, lock, service-state and token checks, including
-- when a caller invokes this helper directly. No history or record content leaves it.
CREATE FUNCTION maven.reconcile_taxonomy(request jsonb) RETURNS json
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    answer jsonb; p uuid; missing boolean; linked boolean := false;
BEGIN
    answer := maven.reconcile_request('prior', request || jsonb_build_object('ids','[]'::jsonb))::jsonb;
    IF answer->'result' ? 'status' THEN RETURN answer::json; END IF;
    p := (request->>'project_id')::uuid;
    SELECT EXISTS (SELECT FROM jsonb_array_elements_text(request->'task_ids') AS t(id)
                   WHERE NOT EXISTS (SELECT FROM maven.reconcile_records r
                                     WHERE r.project_id=p AND r.id=t.id::uuid AND r.item->>'kind'='task')) INTO missing;
    IF jsonb_array_length(request->'retyped_tasks') > 0 THEN
        SELECT EXISTS (SELECT FROM maven.reconcile_records r WHERE r.project_id=p AND r.item->>'kind'='plan'
                       AND (request->'retyped_tasks') ? (r.wire::jsonb #>> '{record,data,task_id}')) INTO linked;
    END IF;
    RETURN jsonb_set(answer,'{result}',jsonb_build_object('missing_task',missing,
        'linked_task',linked))::json;
END
$$
-- statement
REVOKE ALL ON FUNCTION maven.reconcile_taxonomy(jsonb) FROM PUBLIC
