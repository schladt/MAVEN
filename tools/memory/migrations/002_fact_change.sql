CREATE FUNCTION maven.change_fact(request jsonb, canonical_hash text, actor text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    key_id uuid := (request->>'request_key')::uuid;
    fact_id uuid := (request->>'record_id')::uuid;
    receipt maven.operations%ROWTYPE;
    current_record maven.records%ROWTYPE;
    evidence_id uuid;
    next_revision integer;
    fact_snapshot jsonb := request->'fact';
    source_snapshot jsonb := request->'source';
    reason text := request->>'change_reason';
BEGIN
    IF request->>'classification' IS DISTINCT FROM 'Sensitive'
       OR request->>'data_scope' IS DISTINCT FROM 'synthetic'
       OR COALESCE(request->>'action', '') NOT IN ('create_fact', 'correct_fact')
       OR canonical_hash IS NULL OR canonical_hash !~ '^[0-9a-f]{64}$'
       OR key_id IS NULL OR fact_id IS NULL
       OR (request->>'action' = 'create_fact' AND request ? 'expected_revision')
       OR (request->>'action' = 'correct_fact' AND
           (jsonb_typeof(request->'expected_revision') IS DISTINCT FROM 'number'
            OR (request->>'expected_revision') !~ '^[1-9][0-9]*$')) THEN
        RAISE EXCEPTION 'invalid request' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('request:' || key_id::text, 0));
    SELECT * INTO receipt FROM maven.operations WHERE request_key = key_id;
    IF FOUND THEN
        IF receipt.request_hash <> canonical_hash THEN
            RETURN jsonb_build_object('status', 'idempotency_conflict');
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM maven.records r JOIN maven.facts f ON f.id = r.id
            JOIN maven.records s ON s.id = f.source_id
            WHERE r.id = receipt.record_id AND r.visibility_state = 'visible'
              AND r.archived_at IS NULL AND s.visibility_state = 'visible'
              AND s.archived_at IS NULL
        ) THEN
            RETURN jsonb_build_object('status', 'record_unavailable');
        END IF;
        RETURN jsonb_build_object('record_id', receipt.record_id, 'revision', receipt.revision,
                                  'request_key', key_id, 'replayed', true);
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('record:' || fact_id::text, 0));
    SELECT * INTO current_record FROM maven.records WHERE id = fact_id FOR UPDATE;
    IF request->>'action' = 'create_fact' THEN
        IF FOUND THEN
            IF current_record.kind <> 'fact' OR current_record.visibility_state <> 'visible'
               OR current_record.archived_at IS NOT NULL THEN
                RETURN jsonb_build_object('status', 'record_unavailable');
            END IF;
            RETURN jsonb_build_object('status', 'revision_conflict', 'current_revision', current_record.revision);
        END IF;
        next_revision := 1;
        evidence_id := gen_random_uuid();
        INSERT INTO maven.records(id, kind, revision) VALUES (fact_id, 'fact', 1), (evidence_id, 'source', 1);
        INSERT INTO maven.sources(id, origin, excerpt)
        VALUES (evidence_id, source_snapshot->>'origin', source_snapshot->>'excerpt');
        INSERT INTO maven.facts(id, predicate, value_type, value, source_id)
        VALUES (fact_id, fact_snapshot->>'predicate', fact_snapshot->>'value_type', fact_snapshot->'value', evidence_id);
    ELSE
        IF NOT FOUND OR current_record.kind <> 'fact' OR current_record.visibility_state <> 'visible'
           OR current_record.archived_at IS NOT NULL THEN
            RETURN jsonb_build_object('status', 'record_unavailable');
        END IF;
        SELECT f.source_id INTO evidence_id FROM maven.facts f
        JOIN maven.records s ON s.id = f.source_id
        WHERE f.id = fact_id AND s.visibility_state = 'visible' AND s.archived_at IS NULL
        FOR UPDATE OF s;
        IF NOT FOUND THEN
            RETURN jsonb_build_object('status', 'record_unavailable');
        END IF;
        IF current_record.revision <> (request->>'expected_revision')::integer THEN
            RETURN jsonb_build_object('status', 'revision_conflict', 'current_revision', current_record.revision);
        END IF;
        next_revision := current_record.revision + 1;
        UPDATE maven.records SET revision = next_revision, updated_at = transaction_timestamp()
        WHERE id IN (fact_id, evidence_id);
        UPDATE maven.facts SET predicate = fact_snapshot->>'predicate',
            value_type = fact_snapshot->>'value_type', value = fact_snapshot->'value' WHERE id = fact_id;
        UPDATE maven.sources SET origin = source_snapshot->>'origin', excerpt = source_snapshot->>'excerpt',
            captured_at = transaction_timestamp() WHERE id = evidence_id;
    END IF;
    INSERT INTO maven.record_versions(record_id, revision, snapshot, content_hash, actor_id, change_reason)
    VALUES (fact_id, next_revision, fact_snapshot, encode(sha256(convert_to(fact_snapshot::text, 'UTF8')), 'hex'), actor, reason),
           (evidence_id, next_revision, source_snapshot, encode(sha256(convert_to(source_snapshot::text, 'UTF8')), 'hex'), actor, reason);
    INSERT INTO maven.record_sources(record_id, revision, source_id, source_revision)
    VALUES (fact_id, next_revision, evidence_id, next_revision);
    INSERT INTO maven.operations(request_key, request_hash, record_id, revision, operation_kind)
    VALUES (key_id, canonical_hash, fact_id, next_revision, request->>'action');
    RETURN jsonb_build_object('record_id', fact_id, 'revision', next_revision,
                              'request_key', key_id, 'replayed', false);
END
$$
-- statement
REVOKE ALL ON FUNCTION maven.change_fact(jsonb, text, text) FROM PUBLIC
