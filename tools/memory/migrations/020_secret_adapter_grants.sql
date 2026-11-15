-- Adapter definitions AND grants are approved Home-scoped procedure publications.
-- Only spent attempt markers live here. No local cache or memory setting grants access.
-- Integration adds branches to migration 019's memory_request; this migration never
-- replaces that dispatcher or removes its catalog routes.
CREATE TABLE maven.secret_adapter_consumptions (
    invocation_id uuid PRIMARY KEY,
    invocation_digest text NOT NULL CHECK (invocation_digest ~ '^[0-9a-f]{64}$'),
    home_id uuid NOT NULL REFERENCES maven.records(id),
    adapter_page_id uuid NOT NULL REFERENCES maven.pages(id),
    adapter_revision integer NOT NULL CHECK (adapter_revision > 0),
    grant_page_id uuid NOT NULL REFERENCES maven.pages(id),
    grant_revision integer NOT NULL CHECK (grant_revision > 0),
    single_use boolean NOT NULL,
    actor_id text NOT NULL,
    consumed_at timestamptz NOT NULL DEFAULT clock_timestamp()
)
-- statement
CREATE UNIQUE INDEX secret_adapter_grant_once
ON maven.secret_adapter_consumptions(grant_page_id,grant_revision) WHERE single_use
-- statement
CREATE TRIGGER immutable_secret_adapter_consumption BEFORE UPDATE OR DELETE
ON maven.secret_adapter_consumptions FOR EACH ROW EXECUTE FUNCTION maven.immutable_version()
-- statement
REVOKE ALL ON maven.secret_adapter_consumptions FROM PUBLIC
-- statement
CREATE FUNCTION maven.guard_secret_adapter_home() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE candidate jsonb:=NEW.candidate; home uuid;
BEGIN
    IF candidate->>'slug' NOT LIKE 'secret-adapter-%' AND candidate->>'slug' NOT LIKE 'secret-grant-%' THEN
        RETURN NEW;
    END IF;
    IF NEW.data_scope<>'owner' OR candidate->>'page_kind' IS DISTINCT FROM 'procedure'
       OR candidate->'requires' IS DISTINCT FROM '["maven-secrets"]'::jsonb
       OR jsonb_typeof(candidate->'scope') IS DISTINCT FROM 'object'
       OR candidate->'scope' - 'project' <> '{}'::jsonb OR NOT candidate->'scope' ? 'project' THEN
        RAISE EXCEPTION 'invalid secret procedure authority' USING ERRCODE='23514';
    END IF;
    home:=(candidate->'scope'->>'project')::uuid;
    IF NOT EXISTS (SELECT 1 FROM maven.records r JOIN maven.memory_items m ON m.id=r.id
        WHERE r.id=home AND r.kind='entity' AND r.data_scope='owner' AND m.project_id IS NULL
          AND m.data->>'entity_type'='project' AND maven.memory_visible(r.id))
       OR maven.project_archived(home) THEN
        RAISE EXCEPTION 'invalid secret procedure Home' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END
$$
-- statement
REVOKE ALL ON FUNCTION maven.guard_secret_adapter_home() FROM PUBLIC
-- statement
CREATE TRIGGER secret_adapter_home BEFORE INSERT ON maven.instruction_proposals
FOR EACH ROW EXECUTE FUNCTION maven.guard_secret_adapter_home()
-- statement
CREATE FUNCTION maven.secret_adapter_canonical(value jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE STRICT SET search_path = pg_catalog AS $$
DECLARE result text;
BEGIN
    CASE jsonb_typeof(value)
    WHEN 'object' THEN
        SELECT '{' || COALESCE(string_agg(to_jsonb(key)::text || ':' || maven.secret_adapter_canonical(item),
                    ',' ORDER BY key COLLATE "C"),'') || '}' INTO result FROM jsonb_each(value) AS e(key,item);
    WHEN 'array' THEN
        SELECT '[' || COALESCE(string_agg(maven.secret_adapter_canonical(item),',' ORDER BY position),'') || ']'
          INTO result FROM jsonb_array_elements(value) WITH ORDINALITY AS e(item,position);
    ELSE result:=value::text;
    END CASE;
    RETURN result;
END
$$
-- statement
REVOKE ALL ON FUNCTION maven.secret_adapter_canonical(jsonb) FROM PUBLIC
-- statement
CREATE FUNCTION maven.secret_adapter_policy(request jsonb) RETURNS jsonb
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
       OR page_value.data_scope<>'owner' OR page_value.scope<>jsonb_build_object('project',home::text)
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
       OR document->>'kind' IS DISTINCT FROM CASE WHEN page_value.slug LIKE 'secret-adapter-%'
           THEN 'secret-adapter' ELSE 'secret-grant' END THEN
        RETURN jsonb_build_object('status','invalid_request');
    END IF;
    RETURN jsonb_build_object('home_id',home,'page_id',page_value.id,'slug',page_value.slug,
        'revision',page_value.revision,'document',document);
EXCEPTION WHEN invalid_text_representation OR invalid_parameter_value OR numeric_value_out_of_range THEN
    RETURN jsonb_build_object('status','invalid_request');
END
$$
-- statement
REVOKE ALL ON FUNCTION maven.secret_adapter_policy(jsonb) FROM PUBLIC
-- statement
CREATE FUNCTION maven.secret_adapter_consume(request jsonb, actor text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE invocation jsonb; definition jsonb; permission jsonb; grant_value jsonb;
        catalog jsonb; digest_value text; name text; expiry timestamptz; inserted uuid;
BEGIN
    IF jsonb_typeof(request) IS DISTINCT FROM 'object'
       OR request - ARRAY['invocation','invocation_digest','grant','grant_revision'] <> '{}'::jsonb
       OR NOT request ?& ARRAY['invocation','invocation_digest','grant','grant_revision']
       OR actor IS NULL OR COALESCE(request->>'grant','') !~ '^secret-grant-[a-z0-9]+(-[a-z0-9]+)*$'
       OR length(request->>'grant')>80 THEN
        RETURN jsonb_build_object('status','invalid_request');
    END IF;
    invocation:=request->'invocation';
    IF jsonb_typeof(invocation) IS DISTINCT FROM 'object'
       OR invocation - ARRAY['home_id','invocation_id','adapter','adapter_revision','secret_name','secret_ref',
          'purpose','operation','destination','parameters','catalog_record_id','catalog_revision','store_id'] <> '{}'::jsonb
       OR NOT invocation ?& ARRAY['home_id','invocation_id','adapter','adapter_revision','secret_name','secret_ref',
          'purpose','operation','destination','parameters','catalog_record_id','catalog_revision','store_id'] THEN
        RETURN jsonb_build_object('status','invalid_request');
    END IF;
    digest_value:=encode(sha256(convert_to(maven.secret_adapter_canonical(invocation),'UTF8')),'hex');
    IF digest_value IS DISTINCT FROM request->>'invocation_digest' THEN
        RETURN jsonb_build_object('status','invalid_request');
    END IF;
    -- A publication or revocation cannot commit between validation and consumption.
    PERFORM generation FROM maven.instruction_state WHERE singleton FOR SHARE;
    definition:=maven.secret_adapter_policy(jsonb_build_object('home_id',invocation->>'home_id','slug',invocation->>'adapter'));
    permission:=maven.secret_adapter_policy(jsonb_build_object('home_id',invocation->>'home_id','slug',request->>'grant'));
    IF definition ? 'status' OR permission ? 'status' THEN
        RETURN jsonb_build_object('status','reference_unavailable');
    END IF;
    IF definition->'revision' IS DISTINCT FROM invocation->'adapter_revision'
       OR permission->'revision' IS DISTINCT FROM request->'grant_revision' THEN
        RETURN jsonb_build_object('status','revision_conflict');
    END IF;
    grant_value:=permission->'document';
    IF grant_value->>'mode' NOT IN ('standing','single-use') OR grant_value->>'mode' IS NULL THEN
        RETURN jsonb_build_object('status','invalid_request');
    END IF;
    FOREACH name IN ARRAY ARRAY['adapter','adapter_revision','secret_name','secret_ref','purpose','operation',
                                'destination','parameters','catalog_record_id','catalog_revision','store_id'] LOOP
        IF grant_value->name IS DISTINCT FROM invocation->name THEN
            RETURN jsonb_build_object('status','not_authorized');
        END IF;
    END LOOP;
    expiry:=(grant_value->>'expires_at')::timestamptz;
    IF expiry IS NULL OR NOT isfinite(expiry) OR expiry<=clock_timestamp()
       OR (grant_value->>'mode'='single-use' AND grant_value->>'invocation_digest' IS DISTINCT FROM digest_value) THEN
        RETURN jsonb_build_object('status','not_authorized');
    END IF;
    -- Lock the catalog revision before resolving it; rotation/retargeting cannot race
    -- the exact comparison. The client also checks it again before fetching bytes.
    PERFORM id FROM maven.records WHERE id=(invocation->>'catalog_record_id')::uuid FOR SHARE;
    catalog:=maven.secret_lookup(jsonb_build_object('project_id',invocation->>'home_id','name',invocation->>'secret_name'))->'record';
    IF catalog IS NULL OR catalog='null'::jsonb OR catalog->>'record_id' IS DISTINCT FROM invocation->>'catalog_record_id'
       OR catalog->'revision' IS DISTINCT FROM invocation->'catalog_revision'
       OR catalog->'data'->'secret_ref' IS DISTINCT FROM invocation->'secret_ref'
       OR (catalog->'data'->'value' ? 'store_id'
           AND catalog->'data'->'value'->>'store_id' IS DISTINCT FROM invocation->>'store_id') THEN
        RETURN jsonb_build_object('status','reference_unavailable');
    END IF;
    INSERT INTO maven.secret_adapter_consumptions(invocation_id,invocation_digest,home_id,adapter_page_id,
        adapter_revision,grant_page_id,grant_revision,single_use,actor_id)
      VALUES((invocation->>'invocation_id')::uuid,digest_value,(invocation->>'home_id')::uuid,
        (definition->>'page_id')::uuid,(definition->>'revision')::integer,
        (permission->>'page_id')::uuid,(permission->>'revision')::integer,grant_value->>'mode'='single-use',actor)
      ON CONFLICT DO NOTHING RETURNING invocation_id INTO inserted;
    IF inserted IS NULL THEN
        -- Deliberately no successful replay receipt: a lost response spends permission.
        RETURN jsonb_build_object('status','claim_conflict');
    END IF;
    RETURN jsonb_build_object('consumed',true,'invocation_id',inserted,'invocation_digest',digest_value);
EXCEPTION WHEN invalid_text_representation OR invalid_datetime_format OR datetime_field_overflow
               OR invalid_parameter_value OR numeric_value_out_of_range THEN
    RETURN jsonb_build_object('status','invalid_request');
END
$$
-- statement
REVOKE ALL ON FUNCTION maven.secret_adapter_consume(jsonb,text) FROM PUBLIC
