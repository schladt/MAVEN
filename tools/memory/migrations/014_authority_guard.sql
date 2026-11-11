-- Governed writes carry the authority generation they were made under. A mismatch commits
-- nothing (context_stale); the generation row stays share-locked until commit, so a concurrent
-- publication waits for the write instead of slipping between the check and the mutation.
-- An exact replay of an already committed request still returns its original receipt.
CREATE OR REPLACE FUNCTION maven.memory_request(route text, request jsonb, request_hash text, candidate_hash text, actor text, client_protocol integer) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog SET lock_timeout = '1s' AS $$
DECLARE envelope jsonb; result jsonb; current_generation bigint;
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
    IF route IN ('/v1/memory/write','/v1/memory/claim') AND request ? 'authority_generation' THEN
        IF jsonb_typeof(request->'authority_generation') IS DISTINCT FROM 'number'
           OR COALESCE(request->>'request_key','') !~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN
            RETURN envelope || jsonb_build_object('result',jsonb_build_object('status','invalid_request'));
        END IF;
        SELECT generation INTO current_generation FROM maven.instruction_state WHERE singleton FOR SHARE;
        IF current_generation::numeric <> (request->>'authority_generation')::numeric AND NOT EXISTS (
            SELECT 1 FROM maven.memory_receipts WHERE request_key=(request->>'request_key')::uuid) THEN
            RETURN jsonb_build_object('service_state',envelope->'service_state',
              'authority_generation',current_generation,'result',jsonb_build_object('status','context_stale'));
        END IF;
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
