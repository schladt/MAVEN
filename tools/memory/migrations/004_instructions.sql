ALTER TABLE maven.record_sources DROP CONSTRAINT source_matches_fact_revision
-- statement
CREATE FUNCTION maven.check_fact_citation() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF NEW.revision <> NEW.source_revision AND EXISTS (
        SELECT 1 FROM maven.records WHERE id = NEW.record_id AND kind = 'fact'
    ) THEN
        RAISE EXCEPTION 'fact source revision mismatch' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$$
-- statement
REVOKE ALL ON FUNCTION maven.check_fact_citation() FROM PUBLIC
-- statement
CREATE TRIGGER fact_citation_revision BEFORE INSERT ON maven.record_sources
FOR EACH ROW EXECUTE FUNCTION maven.check_fact_citation()
-- statement
ALTER TABLE maven.records DROP CONSTRAINT records_kind_check
-- statement
ALTER TABLE maven.records ADD CONSTRAINT records_kind_check CHECK (kind IN ('fact', 'source', 'page'))
-- statement
CREATE TABLE maven.instruction_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0)
)
-- statement
INSERT INTO maven.instruction_state(singleton) VALUES (true)
-- statement
CREATE TABLE maven.pages (
    id uuid PRIMARY KEY,
    kind text NOT NULL DEFAULT 'page' CHECK (kind = 'page'),
    slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND length(slug) <= 80),
    page_kind text NOT NULL CHECK (page_kind IN ('skill', 'procedure')),
    title text NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 160),
    scope jsonb NOT NULL CHECK (jsonb_typeof(scope) = 'object'),
    authority_state text NOT NULL CHECK (authority_state IN ('active', 'revoked')),
    FOREIGN KEY (id, kind) REFERENCES maven.records(id, kind)
)
-- statement
CREATE TABLE maven.instruction_proposals (
    id uuid PRIMARY KEY,
    page_id uuid NOT NULL,
    base_revision integer NOT NULL CHECK (base_revision >= 0),
    candidate jsonb NOT NULL CHECK (jsonb_typeof(candidate) = 'object'),
    candidate_hash text NOT NULL CHECK (candidate_hash ~ '^[0-9a-f]{64}$'),
    source_id uuid NOT NULL REFERENCES maven.sources(id),
    source_revision integer NOT NULL DEFAULT 1 CHECK (source_revision = 1),
    change_reason text NOT NULL CHECK (length(btrim(change_reason)) BETWEEN 1 AND 240),
    actor_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    accepted_revision integer,
    FOREIGN KEY (source_id, source_revision) REFERENCES maven.record_versions(record_id, revision),
    FOREIGN KEY (page_id, accepted_revision) REFERENCES maven.record_versions(record_id, revision),
    CHECK (accepted_revision IS NULL OR accepted_revision = base_revision + 1)
)
-- statement
CREATE TABLE maven.instruction_challenges (
    id uuid PRIMARY KEY,
    proposal_id uuid NOT NULL REFERENCES maven.instruction_proposals(id),
    candidate_hash text NOT NULL,
    base_revision integer NOT NULL,
    actor_id text NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp()
)
-- statement
CREATE TABLE maven.page_publications (
    page_id uuid NOT NULL,
    revision integer NOT NULL,
    proposal_id uuid NOT NULL UNIQUE REFERENCES maven.instruction_proposals(id),
    challenge_id uuid NOT NULL UNIQUE REFERENCES maven.instruction_challenges(id),
    approval_source_id uuid NOT NULL REFERENCES maven.sources(id),
    approval_source_revision integer NOT NULL DEFAULT 1 CHECK (approval_source_revision = 1),
    actor_id text NOT NULL,
    PRIMARY KEY(page_id, revision),
    FOREIGN KEY(page_id, revision) REFERENCES maven.record_versions(record_id, revision),
    FOREIGN KEY(approval_source_id, approval_source_revision) REFERENCES maven.record_versions(record_id, revision)
)
-- statement
CREATE TABLE maven.instruction_receipts (
    request_key uuid PRIMARY KEY,
    request_hash text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    proposal_id uuid NOT NULL REFERENCES maven.instruction_proposals(id),
    result jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT transaction_timestamp()
)
-- statement
CREATE TRIGGER immutable_instruction_receipt BEFORE UPDATE OR DELETE ON maven.instruction_receipts
FOR EACH ROW EXECUTE FUNCTION maven.immutable_version()
-- statement
CREATE TRIGGER immutable_publication BEFORE UPDATE OR DELETE ON maven.page_publications
FOR EACH ROW EXECUTE FUNCTION maven.immutable_version()
-- statement
CREATE TRIGGER immutable_challenge BEFORE UPDATE OR DELETE ON maven.instruction_challenges
FOR EACH ROW EXECUTE FUNCTION maven.immutable_version()
-- statement
CREATE FUNCTION maven.guard_proposal() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF TG_OP = 'DELETE' OR OLD.accepted_revision IS NOT NULL
       OR NEW.accepted_revision IS NULL
       OR (to_jsonb(OLD) - 'accepted_revision') <> (to_jsonb(NEW) - 'accepted_revision') THEN
        RAISE EXCEPTION 'immutable proposal' USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END
$$
-- statement
CREATE TRIGGER immutable_proposal BEFORE UPDATE OR DELETE ON maven.instruction_proposals
FOR EACH ROW EXECUTE FUNCTION maven.guard_proposal()
-- statement
CREATE FUNCTION maven.instruction_visible(proposal uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT EXISTS (
        SELECT 1 FROM maven.instruction_proposals p
        JOIN maven.records s ON s.id = p.source_id
        LEFT JOIN maven.records r ON r.id = p.page_id
        LEFT JOIN maven.page_publications pub ON pub.proposal_id = p.id
        LEFT JOIN maven.records a ON a.id = pub.approval_source_id
        WHERE p.id = proposal AND s.visibility_state = 'visible' AND s.archived_at IS NULL
          AND (r.id IS NULL OR (r.kind = 'page' AND r.visibility_state = 'visible' AND r.archived_at IS NULL))
          AND (p.accepted_revision IS NULL OR (a.visibility_state = 'visible' AND a.archived_at IS NULL))
    )
$$
-- statement
CREATE FUNCTION maven.instruction_source(evidence jsonb, actor text, reason text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE source_id uuid := gen_random_uuid();
BEGIN
    INSERT INTO maven.records(id, kind, revision) VALUES (source_id, 'source', 1);
    INSERT INTO maven.sources(id, origin, excerpt) VALUES (source_id, evidence->>'origin', evidence->>'excerpt');
    INSERT INTO maven.record_versions(record_id, revision, snapshot, content_hash, actor_id, change_reason)
    VALUES (source_id, 1, evidence, encode(sha256(convert_to(evidence::text, 'UTF8')), 'hex'), actor, reason);
    RETURN source_id;
END
$$
-- statement
CREATE FUNCTION maven.instruction_write(action text, request jsonb, canonical_hash text, candidate_hash_value text, actor text) RETURNS jsonb
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
BEGIN
    IF action NOT IN ('create', 'resolve') OR canonical_hash IS NULL OR canonical_hash !~ '^[0-9a-f]{64}$'
       OR key_id IS NULL OR proposal_id IS NULL OR actor IS NULL THEN
        RAISE EXCEPTION 'invalid request' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('request:' || key_id::text, 0));
    IF EXISTS (SELECT 1 FROM maven.operations WHERE request_key = key_id) THEN
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
           OR request->>'data_scope' IS DISTINCT FROM 'synthetic'
           OR candidate_hash_value IS NULL OR candidate_hash_value !~ '^[0-9a-f]{64}$'
           OR request->'candidate'->>'page_kind' NOT IN ('skill', 'procedure')
           OR request->'candidate'->>'authority_state' NOT IN ('active', 'revoked')
           OR octet_length(request->'candidate'->>'body_markdown') > 131072 THEN
            RAISE EXCEPTION 'invalid request' USING ERRCODE = '22023';
        END IF;
        target_id := (request->>'page_id')::uuid;
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
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('record:' || target_id::text, 0));
    SELECT * INTO page_record FROM maven.records WHERE id = target_id FOR UPDATE;
    IF FOUND AND (page_record.kind <> 'page' OR page_record.visibility_state <> 'visible' OR page_record.archived_at IS NOT NULL) THEN
        RETURN jsonb_build_object('status', 'page_unavailable');
    END IF;
    IF COALESCE(page_record.revision, 0) <> (request->>'expected_revision')::integer THEN
        RETURN jsonb_build_object('status', 'revision_conflict', 'current_revision', COALESCE(page_record.revision, 0));
    END IF;
    IF action = 'create' THEN
        evidence_id := maven.instruction_source(request->'source', actor, request->>'change_reason');
        INSERT INTO maven.instruction_proposals(id, page_id, base_revision, candidate, candidate_hash, source_id, change_reason, actor_id)
        VALUES (proposal_id, target_id, (request->>'expected_revision')::integer, request->'candidate', candidate_hash_value,
                evidence_id, request->>'change_reason', actor);
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
        evidence_id := maven.instruction_source(request->'approval_source', actor, 'Owner-approved instruction publication');
        IF page_record.id IS NULL THEN
            INSERT INTO maven.records(id, kind, revision) VALUES (target_id, 'page', next_revision);
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
CREATE FUNCTION maven.instruction_challenge(proposal_id uuid, actor text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    proposal maven.instruction_proposals%ROWTYPE;
    challenge_id uuid := gen_random_uuid();
    expiry timestamptz := clock_timestamp() + interval '10 minutes';
    current_revision integer;
BEGIN
    SELECT * INTO proposal FROM maven.instruction_proposals WHERE id = proposal_id;
    IF NOT FOUND OR NOT maven.instruction_visible(proposal_id) THEN
        RETURN jsonb_build_object('status', 'proposal_unavailable');
    END IF;
    IF proposal.accepted_revision IS NOT NULL THEN RETURN jsonb_build_object('status', 'proposal_conflict'); END IF;
    SELECT revision INTO current_revision FROM maven.records WHERE id = proposal.page_id;
    IF COALESCE(current_revision, 0) <> proposal.base_revision THEN
        RETURN jsonb_build_object('status', 'revision_conflict', 'current_revision', COALESCE(current_revision, 0));
    END IF;
    INSERT INTO maven.instruction_challenges(id, proposal_id, candidate_hash, base_revision, actor_id, expires_at)
    VALUES (challenge_id, proposal_id, proposal.candidate_hash, proposal.base_revision, actor, expiry);
    RETURN jsonb_build_object('challenge_id', challenge_id, 'proposal_id', proposal_id, 'page_id', proposal.page_id,
        'candidate_hash', proposal.candidate_hash, 'expected_revision', proposal.base_revision, 'expires_at', expiry);
END
$$
-- statement
CREATE FUNCTION maven.instruction_index(request jsonb) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    WITH selected AS (
        SELECT p.id AS page_id, p.slug, p.page_kind, p.title, p.scope, r.revision, v.content_hash
        FROM maven.pages p JOIN maven.records r ON r.id = p.id
        JOIN maven.record_versions v ON v.record_id = p.id AND v.revision = r.revision
        JOIN maven.page_publications pub ON pub.page_id = p.id AND pub.revision = r.revision
        WHERE p.authority_state = 'active' AND maven.instruction_visible(pub.proposal_id)
          AND (NOT request ? 'page_kind' OR p.page_kind = request->>'page_kind')
          AND (NOT request ? 'after_slug' OR p.slug COLLATE "C" > (request->>'after_slug') COLLATE "C")
        ORDER BY p.slug COLLATE "C" LIMIT COALESCE((request->>'limit')::integer, 10) + 1
    ), page AS (
        SELECT * FROM selected ORDER BY slug COLLATE "C" LIMIT COALESCE((request->>'limit')::integer, 10)
    )
    SELECT jsonb_build_object('pages', COALESCE((SELECT jsonb_agg(to_jsonb(p) ORDER BY slug COLLATE "C") FROM page p), '[]'::jsonb),
        'next_after_slug', CASE WHEN (SELECT count(*) FROM selected) > COALESCE((request->>'limit')::integer, 10)
            THEN (SELECT slug FROM page ORDER BY slug COLLATE "C" DESC LIMIT 1) ELSE NULL END,
        'authority_generation', (SELECT generation FROM maven.instruction_state WHERE singleton))
$$
-- statement
CREATE FUNCTION maven.instruction_read(request jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    proposal maven.instruction_proposals%ROWTYPE;
    publication maven.page_publications%ROWTYPE;
    version maven.record_versions%ROWTYPE;
    page_record maven.records%ROWTYPE;
    candidate jsonb;
    provenance jsonb;
    offset_value integer := COALESCE((request->>'offset')::integer, 0);
    body text;
    body_length integer;
    operative boolean := false;
    authority text;
BEGIN
    IF request ? 'proposal_id' THEN
        SELECT * INTO proposal FROM maven.instruction_proposals WHERE id = (request->>'proposal_id')::uuid;
        IF NOT FOUND OR NOT maven.instruction_visible(proposal.id) THEN
            RETURN jsonb_build_object('status', 'proposal_unavailable');
        END IF;
        candidate := proposal.candidate;
        authority := 'draft';
    ELSE
        SELECT r.* INTO page_record FROM maven.pages p JOIN maven.records r ON r.id = p.id WHERE p.slug = request->>'slug';
        IF NOT FOUND THEN RETURN jsonb_build_object('status', 'page_unavailable'); END IF;
        IF request ? 'expected_revision' AND page_record.revision <> (request->>'expected_revision')::integer THEN
            RETURN jsonb_build_object('status', 'revision_conflict', 'current_revision', page_record.revision);
        END IF;
        SELECT * INTO version FROM maven.record_versions
        WHERE record_id = page_record.id AND revision = COALESCE((request->>'revision')::integer, page_record.revision);
        IF NOT FOUND THEN RETURN jsonb_build_object('status', 'page_unavailable'); END IF;
        SELECT * INTO publication FROM maven.page_publications WHERE page_id = page_record.id AND revision = version.revision;
        SELECT * INTO proposal FROM maven.instruction_proposals WHERE id = publication.proposal_id;
        IF NOT FOUND OR NOT maven.instruction_visible(proposal.id) THEN
            RETURN jsonb_build_object('status', 'page_unavailable');
        END IF;
        candidate := version.snapshot;
        authority := candidate->>'authority_state';
        operative := NOT request ? 'revision' AND authority = 'active';
    END IF;
    body := candidate->>'body_markdown';
    body_length := char_length(body);
    IF offset_value > body_length THEN RETURN jsonb_build_object('status', 'invalid_request'); END IF;
    SELECT jsonb_build_object('source_id', proposal.source_id, 'source_revision', 1, 'source', v.snapshot,
        'actor_id', proposal.actor_id, 'change_reason', proposal.change_reason) INTO provenance
    FROM maven.record_versions v WHERE v.record_id = proposal.source_id AND v.revision = 1;
    IF publication.page_id IS NOT NULL THEN
        SELECT provenance || jsonb_build_object('approval_source_id', publication.approval_source_id,
            'approval_source_revision', 1, 'approval_source', v.snapshot, 'approval_actor_id', publication.actor_id)
        INTO provenance FROM maven.record_versions v WHERE v.record_id = publication.approval_source_id AND v.revision = 1;
    END IF;
    RETURN (candidate - 'body_markdown') || jsonb_build_object(
        'page_id', proposal.page_id, 'proposal_id', proposal.id,
        'revision', COALESCE(version.revision, 0), 'content_hash', proposal.candidate_hash,
        'authority_state', authority, 'operative', operative, 'data_scope', 'synthetic',
        'candidate_authority_state', candidate->>'authority_state',
        'body_markdown', substring(body FROM offset_value + 1 FOR 4096), 'offset', offset_value,
        'next_offset', CASE WHEN offset_value + 4096 < body_length THEN offset_value + 4096 ELSE NULL END,
        'total_characters', body_length, 'body_sha256', encode(sha256(convert_to(body, 'UTF8')), 'hex'),
        'provenance', provenance, 'authority_generation', (SELECT generation FROM maven.instruction_state WHERE singleton));
END
$$
-- statement
ALTER FUNCTION maven.change_fact(jsonb, text, text) RENAME TO change_fact_foundation
-- statement
CREATE FUNCTION maven.change_fact(request jsonb, canonical_hash text, actor text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('request:' || (request->>'request_key'), 0));
    IF EXISTS (SELECT 1 FROM maven.instruction_receipts WHERE request_key = (request->>'request_key')::uuid) THEN
        RETURN jsonb_build_object('status', 'idempotency_conflict');
    END IF;
    RETURN maven.change_fact_foundation(request, canonical_hash, actor);
END
$$
-- statement
REVOKE ALL ON FUNCTION maven.guard_proposal(), maven.instruction_visible(uuid), maven.instruction_source(jsonb,text,text),
    maven.instruction_write(text,jsonb,text,text,text), maven.instruction_challenge(uuid,text),
    maven.instruction_index(jsonb), maven.instruction_read(jsonb), maven.change_fact(jsonb,text,text) FROM PUBLIC
-- statement
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'maven_app') THEN
        REVOKE ALL ON FUNCTION maven.change_fact_foundation(jsonb,text,text) FROM maven_app;
    END IF;
END $$
