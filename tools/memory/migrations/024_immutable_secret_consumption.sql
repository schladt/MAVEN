-- Do not narrow ordinary historical secret-reference reads; gate spending only.
CREATE OR REPLACE FUNCTION maven.secret_adapter_consume(request jsonb, actor text) RETURNS jsonb
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
    -- Legacy catalog references remain readable, but unusable or mutable locators
    -- must never consume an authorization. Encrypted objects are write-once names.
    IF NOT maven.secret_ref_valid(invocation->'secret_ref')
       OR (CASE invocation->'secret_ref'->>'provider'
           WHEN 'encrypted-file' THEN false
           WHEN 'ssm' THEN NOT (invocation->'secret_ref'->>'name' ~ ':[1-9][0-9]*$')
           ELSE true END) THEN
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
