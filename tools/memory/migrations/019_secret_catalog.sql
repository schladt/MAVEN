-- Typed encrypted-file references and explicit one-setting Home catalog operations.
CREATE FUNCTION maven.secret_ref_valid(member jsonb) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog AS $$
BEGIN
    IF jsonb_typeof(member) IS DISTINCT FROM 'object' OR NOT member ?& ARRAY['provider','name']
      OR jsonb_typeof(member->'provider') IS DISTINCT FROM 'string'
      OR jsonb_typeof(member->'name') IS DISTINCT FROM 'string'
      OR length(member->>'name') NOT BETWEEN 1 AND 512 THEN RETURN false; END IF;
    IF member->>'provider' IN ('ssm','secretsmanager') THEN
      RETURN member - ARRAY['provider','name'] = '{}'::jsonb
        AND member->>'name' ~ '^[A-Za-z0-9_./:@+=-]+$';
    END IF;
    IF member->>'provider' IS DISTINCT FROM 'encrypted-file'
      OR NOT member ?& ARRAY['store_id','bucket','key_id']
      OR member - ARRAY['provider','name','store_id','bucket','key_id','version_id'] <> '{}'::jsonb
      OR jsonb_typeof(member->'store_id') IS DISTINCT FROM 'string'
      OR member->>'store_id' !~ '^[0-9a-f]{64}$'
      OR jsonb_typeof(member->'bucket') IS DISTINCT FROM 'string'
      OR member->>'bucket' !~ '^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$'
      OR jsonb_typeof(member->'key_id') IS DISTINCT FROM 'string'
      OR member->>'key_id' !~ '^[0-9a-f]{16}$'
      OR member->>'name' !~ '^([A-Za-z0-9_.-]+/)+[a-z0-9][a-z0-9_.-]{0,63}$'
      OR EXISTS (SELECT 1 FROM unnest(string_to_array(member->>'name','/')) part WHERE part IN ('.','..'))
      THEN RETURN false; END IF;
    IF member ? 'version_id' AND (jsonb_typeof(member->'version_id') IS DISTINCT FROM 'string'
      OR length(member->>'version_id') NOT BETWEEN 1 AND 1024
      OR member->>'version_id' !~ '^[A-Za-z0-9_.+/=-]+$') THEN RETURN false; END IF;
    RETURN true;
END
$$
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
      IF payload ? 'secret_ref' AND maven.secret_ref_valid(payload->'secret_ref') IS DISTINCT FROM true THEN RETURN false; END IF;
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
CREATE FUNCTION maven.secret_lookup(request jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE home uuid:=(request->>'project_id')::uuid; target uuid; result jsonb;
BEGIN
    IF request->>'name' IS NULL OR request->>'name' !~ '^[a-z0-9][a-z0-9_.-]{0,63}$' THEN
      RETURN jsonb_build_object('status','invalid_request'); END IF;
    IF NOT EXISTS (SELECT 1 FROM maven.records r JOIN maven.memory_items m ON m.id=r.id
      WHERE r.id=home AND r.kind='entity' AND r.data_scope='owner' AND m.data->>'entity_type'='project'
      AND maven.memory_visible(r.id) AND NOT maven.project_archived(r.id)) THEN
      RETURN jsonb_build_object('status','reference_unavailable'); END IF;
    SELECT r.id INTO target FROM maven.records r JOIN maven.memory_items m ON m.id=r.id
      WHERE r.kind='setting' AND r.data_scope='owner' AND m.project_id=home
      AND m.data->>'key'='secret:' || (request->>'name') AND m.data->>'scope'='project'
      AND maven.memory_visible(r.id) AND NOT maven.memory_archived(m.tags,m.data)
      AND NOT m.tags ? 'maven-scope-superseded';
    IF target IS NOT NULL THEN
      result:=maven.memory_read(jsonb_build_object('record_id',target) ||
        CASE WHEN request ? 'revision' THEN jsonb_build_object('revision',request->'revision') ELSE '{}'::jsonb END);
      IF result ? 'status' OR result->'data'->>'key' IS DISTINCT FROM 'secret:' || (request->>'name')
        OR result->>'project_id' IS DISTINCT FROM home::text THEN result:=NULL; END IF;
    END IF;
    RETURN jsonb_build_object('record',result);
END
$$
-- statement
CREATE FUNCTION maven.secret_catalog_write(request jsonb, request_hash text, actor text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE payload jsonb:=request->'data'; meta jsonb:=payload->'value'; ref jsonb:=payload->'secret_ref';
BEGIN
    IF request->>'kind' IS DISTINCT FROM 'setting' OR NOT request ? 'project_id'
      OR request->>'data_scope' IS DISTINCT FROM 'owner' OR NOT request ? 'authority_generation'
      OR payload->>'scope' IS DISTINCT FROM 'project' OR payload->>'key' IS NULL
      OR payload->>'key' !~ '^secret:[a-z0-9][a-z0-9_.-]{0,63}$'
      OR maven.secret_ref_valid(ref) IS DISTINCT FROM true
      OR jsonb_typeof(meta) IS DISTINCT FROM 'object'
      OR NOT meta ?& ARRAY['store_id','provider','storage_operation','storage_name']
      OR meta - ARRAY['store_id','provider','storage_operation','storage_name','version','domain','service','previous_metadata'] <> '{}'::jsonb
      OR jsonb_typeof(meta->'store_id') IS DISTINCT FROM 'string' OR meta->>'store_id' !~ '^[0-9a-f]{64}$'
      OR meta->>'provider' IS DISTINCT FROM ref->>'provider' OR meta->>'provider' NOT IN ('ssm','encrypted-file')
      OR jsonb_typeof(meta->'storage_operation') IS DISTINCT FROM 'string'
      OR meta->>'storage_operation' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      OR jsonb_typeof(meta->'storage_name') IS DISTINCT FROM 'string' OR meta->>'storage_name' !~ '^[a-z0-9][a-z0-9_.-]{0,63}$'
      THEN RETURN jsonb_build_object('status','invalid_request'); END IF;
    IF EXISTS (SELECT 1 FROM unnest(ARRAY['domain','service']) field WHERE meta ? field
      AND (jsonb_typeof(meta->field) IS DISTINCT FROM 'string' OR meta->>field !~ '^[a-z0-9][a-z0-9_.-]{0,63}$'))
      THEN RETURN jsonb_build_object('status','invalid_request'); END IF;
    IF meta->>'provider'='ssm' THEN
      IF jsonb_typeof(meta->'version') IS DISTINCT FROM 'number' OR meta->>'version' !~ '^[1-9][0-9]{0,9}$'
        OR (meta->>'version')::bigint>2147483647 OR payload->>'key' IS DISTINCT FROM 'secret:' || (meta->>'storage_name')
        OR right(ref->>'name',length('/' || (meta->>'storage_name') || ':' || (meta->>'version')))
          IS DISTINCT FROM '/' || (meta->>'storage_name') || ':' || (meta->>'version') THEN
        RETURN jsonb_build_object('status','invalid_request'); END IF;
    ELSIF ref->>'store_id' IS DISTINCT FROM meta->>'store_id'
      OR right(ref->>'name',length('/' || (meta->>'storage_name'))) IS DISTINCT FROM '/' || (meta->>'storage_name') THEN
      RETURN jsonb_build_object('status','invalid_request');
    END IF;
    RETURN maven.memory_write('write',request,request_hash,actor);
END
$$
-- statement
REVOKE ALL ON FUNCTION maven.secret_ref_valid(jsonb),maven.secret_lookup(jsonb),maven.secret_catalog_write(jsonb,text,text) FROM PUBLIC
-- statement
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
    IF route IN ('/v1/memory/write','/v1/memory/claim','/v1/memory/secret_catalog') AND request ? 'authority_generation' THEN
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
    ELSIF route='/v1/memory/secret_lookup' THEN result:=maven.secret_lookup(request);
    ELSIF route='/v1/memory/secret_catalog' THEN result:=maven.secret_catalog_write(request,request_hash,actor);
    ELSIF route='/v1/memory/secret_adapter_policy' THEN result:=maven.secret_adapter_policy(request);
    ELSIF route='/v1/memory/secret_adapter_consume' THEN result:=maven.secret_adapter_consume(request,actor);
    ELSIF route='/v1/proposals/create' THEN result:=maven.instruction_write('create',request,request_hash,candidate_hash,actor);
    ELSIF route='/v1/proposals/resolve' THEN result:=maven.instruction_write('resolve',request,request_hash,candidate_hash,actor);
    ELSIF route='/v1/proposals/challenge' THEN result:=maven.instruction_challenge((request->>'proposal_id')::uuid,actor);
    ELSIF route='/v1/pages/index' THEN result:=maven.instruction_index(request);
    ELSIF route='/v1/pages/read' THEN result:=maven.instruction_read(request);
    ELSE result:=jsonb_build_object('status','invalid_request');
    END IF;
    RETURN jsonb_build_object('service_state',envelope->'service_state',
      'authority_generation',CASE WHEN jsonb_typeof(result->'authority_generation')='number' THEN result->'authority_generation'
        ELSE (SELECT to_jsonb(generation) FROM maven.instruction_state WHERE singleton) END,
      'result',result);
END
$$
