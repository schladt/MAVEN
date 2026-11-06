-- Preserve existing candidates verbatim; only approved publication can establish authority.
CREATE OR REPLACE FUNCTION maven.instruction_write(action text, request jsonb, canonical_hash text, candidate_hash_value text, actor text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    key_id uuid := (request->>'request_key')::uuid;
    proposal_id uuid := (request->>'proposal_id')::uuid;
    proposal maven.instruction_proposals%ROWTYPE;
    receipt maven.instruction_receipts%ROWTYPE;
    challenge maven.instruction_challenges%ROWTYPE;
    page_record maven.records%ROWTYPE;
    target_id uuid;
    evidence_id uuid;
    result jsonb;
    next_revision integer;
    generation_value bigint;
    scope_value text;
BEGIN
    IF NOT maven.memory_protocol_allowed() THEN RETURN jsonb_build_object('status','client_incompatible'); END IF;
    IF NOT maven.memory_safe_json(request) THEN RETURN jsonb_build_object('status','invalid_request'); END IF;
    IF action NOT IN ('create', 'resolve') OR canonical_hash IS NULL OR canonical_hash !~ '^[0-9a-f]{64}$'
       OR key_id IS NULL OR proposal_id IS NULL OR actor IS NULL THEN
        RAISE EXCEPTION 'invalid request' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('request:' || key_id::text, 0));
    IF EXISTS (SELECT 1 FROM maven.operations WHERE request_key = key_id)
       OR EXISTS (SELECT 1 FROM maven.memory_receipts WHERE request_key = key_id) THEN
        RETURN jsonb_build_object('status', 'idempotency_conflict');
    END IF;
    SELECT * INTO receipt FROM maven.instruction_receipts WHERE request_key = key_id;
    IF FOUND THEN
        IF receipt.request_hash <> canonical_hash THEN
            RETURN jsonb_build_object('status', 'idempotency_conflict');
        END IF;
        IF NOT maven.instruction_visible(receipt.proposal_id) THEN
            RETURN jsonb_build_object('status', 'proposal_unavailable');
        END IF;
        RETURN receipt.result || jsonb_build_object('replayed', true);
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('proposal:' || proposal_id::text, 0));
    SELECT * INTO proposal FROM maven.instruction_proposals WHERE id = proposal_id FOR UPDATE;
    IF action = 'create' THEN
        IF FOUND THEN RETURN jsonb_build_object('status', 'proposal_conflict'); END IF;
        IF request->>'classification' IS DISTINCT FROM 'Sensitive'
           OR COALESCE(request->>'data_scope', '') NOT IN ('synthetic', 'owner')
           OR candidate_hash_value IS NULL OR candidate_hash_value !~ '^[0-9a-f]{64}$'
           OR request->'candidate'->>'page_kind' NOT IN ('skill', 'procedure', 'directive')
           OR request->'candidate'->>'authority_state' NOT IN ('active', 'revoked')
           OR octet_length(request->'candidate'->>'body_markdown') > 131072 THEN
            RAISE EXCEPTION 'invalid request' USING ERRCODE = '22023';
        END IF;
        target_id := (request->>'page_id')::uuid;
        scope_value := request->>'data_scope';
    ELSE
        IF NOT FOUND OR NOT maven.instruction_visible(proposal_id) THEN
            RETURN jsonb_build_object('status', 'proposal_unavailable');
        END IF;
        IF proposal.accepted_revision IS NOT NULL THEN RETURN jsonb_build_object('status', 'proposal_conflict'); END IF;
        SELECT * INTO challenge FROM maven.instruction_challenges WHERE id = (request->>'challenge_id')::uuid;
        IF NOT FOUND OR challenge.proposal_id <> proposal_id OR challenge.expires_at <= clock_timestamp()
           OR challenge.candidate_hash <> request->>'candidate_hash'
           OR challenge.candidate_hash <> proposal.candidate_hash
           OR challenge.base_revision <> (request->>'expected_revision')::integer
           OR challenge.base_revision <> proposal.base_revision THEN
            RETURN jsonb_build_object('status', 'approval_invalid');
        END IF;
        target_id := proposal.page_id;
        scope_value := proposal.data_scope;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('record:' || target_id::text, 0));
    SELECT * INTO page_record FROM maven.records WHERE id = target_id FOR UPDATE;
    IF FOUND AND (page_record.kind <> 'page' OR page_record.visibility_state <> 'visible' OR page_record.archived_at IS NOT NULL) THEN
        RETURN jsonb_build_object('status', 'page_unavailable');
    END IF;
    IF page_record.id IS NOT NULL AND page_record.data_scope <> scope_value THEN
        RETURN jsonb_build_object('status', 'proposal_conflict');
    END IF;
    IF COALESCE(page_record.revision, 0) <> (request->>'expected_revision')::integer THEN
        RETURN jsonb_build_object('status', 'revision_conflict', 'current_revision', COALESCE(page_record.revision, 0));
    END IF;
    IF action = 'create' THEN
        evidence_id := maven.instruction_source(request->'source', actor, request->>'change_reason', scope_value);
        INSERT INTO maven.instruction_proposals(id, page_id, base_revision, candidate, candidate_hash, source_id, change_reason, actor_id, data_scope)
        VALUES (proposal_id, target_id, (request->>'expected_revision')::integer, request->'candidate', candidate_hash_value,
                evidence_id, request->>'change_reason', actor, scope_value);
        result := jsonb_build_object('proposal_id', proposal_id, 'page_id', target_id, 'candidate_hash', candidate_hash_value,
                                     'expected_revision', (request->>'expected_revision')::integer, 'state', 'pending');
    ELSE
        -- Serialize publication/slug checks and generation increments, not owner think time.
        SELECT generation INTO generation_value FROM maven.instruction_state WHERE singleton FOR UPDATE;
        IF EXISTS (SELECT 1 FROM maven.pages WHERE slug = proposal.candidate->>'slug' AND id <> target_id)
           OR EXISTS (SELECT 1 FROM maven.pages WHERE id = target_id AND
                (slug <> proposal.candidate->>'slug' OR page_kind <> proposal.candidate->>'page_kind')) THEN
            RETURN jsonb_build_object('status', 'proposal_conflict');
        END IF;
        next_revision := proposal.base_revision + 1;
        evidence_id := maven.instruction_source(request->'approval_source', actor, 'Owner-approved instruction publication', scope_value);
        IF page_record.id IS NULL THEN
            INSERT INTO maven.records(id, kind, revision, data_scope) VALUES (target_id, 'page', next_revision, scope_value);
            INSERT INTO maven.pages(id, slug, page_kind, title, scope, authority_state)
            VALUES (target_id, proposal.candidate->>'slug', proposal.candidate->>'page_kind', proposal.candidate->>'title',
                    proposal.candidate->'scope', proposal.candidate->>'authority_state');
        ELSE
            UPDATE maven.records SET revision = next_revision, updated_at = transaction_timestamp() WHERE id = target_id;
            UPDATE maven.pages SET title = proposal.candidate->>'title', scope = proposal.candidate->'scope',
                authority_state = proposal.candidate->>'authority_state' WHERE id = target_id;
        END IF;
        INSERT INTO maven.record_versions(record_id, revision, snapshot, content_hash, actor_id, change_reason)
        VALUES (target_id, next_revision, proposal.candidate, proposal.candidate_hash, actor, proposal.change_reason);
        INSERT INTO maven.record_sources(record_id, revision, source_id, source_revision)
        VALUES (target_id, next_revision, proposal.source_id, 1), (target_id, next_revision, evidence_id, 1);
        INSERT INTO maven.page_publications(page_id, revision, proposal_id, challenge_id, approval_source_id, actor_id)
        VALUES (target_id, next_revision, proposal_id, challenge.id, evidence_id, actor);
        UPDATE maven.instruction_proposals SET accepted_revision = next_revision WHERE id = proposal_id;
        UPDATE maven.instruction_state SET generation = generation + 1 WHERE singleton RETURNING generation INTO generation_value;
        result := jsonb_build_object('page_id', target_id, 'revision', next_revision, 'proposal_id', proposal_id,
                                     'authority_generation', generation_value);
    END IF;
    result := result || jsonb_build_object('request_key', key_id, 'replayed', false);
    INSERT INTO maven.instruction_receipts(request_key, request_hash, proposal_id, result)
    VALUES (key_id, canonical_hash, proposal_id, result);
    RETURN result;
END
$$
-- statement
CREATE OR REPLACE FUNCTION maven.change_fact(request jsonb, canonical_hash text, actor text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
BEGIN
    IF NOT maven.memory_protocol_allowed() THEN RETURN jsonb_build_object('status','client_incompatible'); END IF;
    IF NOT maven.memory_safe_json(request) THEN RETURN jsonb_build_object('status','invalid_request'); END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('request:' || (request->>'request_key'), 0));
    IF EXISTS (SELECT 1 FROM maven.instruction_receipts WHERE request_key=(request->>'request_key')::uuid)
       OR EXISTS (SELECT 1 FROM maven.memory_receipts WHERE request_key=(request->>'request_key')::uuid) THEN
        RETURN jsonb_build_object('status','idempotency_conflict');
    END IF;
    RETURN maven.change_fact_foundation(request,canonical_hash,actor);
END
$$
-- statement
CREATE OR REPLACE FUNCTION maven.instruction_index(request jsonb) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    WITH selected AS (
      SELECT p.slug, jsonb_build_object('page_id',p.id,'slug',p.slug,'page_kind',p.page_kind,
        'title',p.title,'scope',p.scope,'revision',r.revision,'content_hash',v.content_hash,'data_scope',r.data_scope)
        || CASE WHEN v.snapshot ? 'requires' THEN jsonb_build_object('requires',v.snapshot->'requires') ELSE '{}'::jsonb END AS metadata
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
