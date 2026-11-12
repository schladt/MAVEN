-- Owner-approved memory clean-up through the administration role only: review candidates,
-- Forget/restore (visibility) and Erase (content tombstones). Receipts are append-only and
-- content-free. History stays immutable except for the tombstone rewrite inside erase_records.
CREATE TABLE maven.cleanup_receipts (
    id uuid PRIMARY KEY,
    action text NOT NULL CHECK (action IN ('forget', 'restore', 'erase')),
    record_ids uuid[] NOT NULL CHECK (cardinality(record_ids) BETWEEN 1 AND 200 AND array_position(record_ids, NULL) IS NULL),
    revisions jsonb NOT NULL CHECK (jsonb_typeof(revisions) = 'object'),
    actor text NOT NULL CHECK (length(actor) BETWEEN 1 AND 2048),
    reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 240),
    approval_digest text NOT NULL CHECK (approval_digest ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp()
)
-- statement
REVOKE ALL ON maven.cleanup_receipts FROM PUBLIC
-- statement
CREATE TRIGGER immutable_cleanup_receipt BEFORE UPDATE OR DELETE ON maven.cleanup_receipts
FOR EACH ROW EXECUTE FUNCTION maven.immutable_version()
-- statement
-- The erase receipt authorizing this transaction: the transaction-local maven.erase_receipt must
-- name an erase receipt written in this same transaction (its created_at is this transaction's
-- start), so a receipt id from an earlier transaction never reopens history.
CREATE FUNCTION maven.cleanup_erase_receipt() RETURNS uuid
LANGUAGE plpgsql STABLE SET search_path = pg_catalog AS $$
DECLARE value text := current_setting('maven.erase_receipt', true);
BEGIN
    IF value IS NULL OR value !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RETURN NULL; END IF;
    RETURN (SELECT id FROM maven.cleanup_receipts
            WHERE id = value::uuid AND action = 'erase' AND created_at = transaction_timestamp());
END
$$
-- statement
-- Fixed, content-free tombstone; erased_at is UTC so it never depends on the session time zone.
CREATE FUNCTION maven.cleanup_tombstone(receipt uuid) RETURNS jsonb
LANGUAGE sql STABLE SET search_path = pg_catalog AS $$
    SELECT jsonb_build_object('erased', true,
        'erased_at', to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'), 'receipt_id', id)
    FROM maven.cleanup_receipts WHERE id = receipt AND action = 'erase'
$$
-- statement
CREATE FUNCTION maven.cleanup_receipt_result(result jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS $$
    SELECT COALESCE(jsonb_object_agg(key, value), '{}'::jsonb) FROM jsonb_each(result)
    WHERE key IN ('record_id', 'revision', 'request_key', 'replayed')
$$
-- statement
-- DELETE is never allowed. An UPDATE is allowed only for the exact tombstone rewrite of an erased
-- record's history (or of a source cited by it) and of its write receipts, inside erase_records.
CREATE OR REPLACE FUNCTION maven.immutable_version() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE receipt uuid; erased uuid[];
BEGIN
    IF TG_OP = 'UPDATE' AND TG_TABLE_SCHEMA = 'maven' AND TG_TABLE_NAME IN ('record_versions', 'memory_receipts') THEN
        receipt := maven.cleanup_erase_receipt();
        IF receipt IS NOT NULL THEN
            SELECT record_ids INTO erased FROM maven.cleanup_receipts WHERE id = receipt;
            IF TG_TABLE_NAME = 'record_versions' THEN
                IF NEW.record_id = OLD.record_id AND NEW.revision = OLD.revision
                   AND NEW.snapshot_schema_version = OLD.snapshot_schema_version
                   AND NEW.recorded_at = OLD.recorded_at AND NEW.actor_id = OLD.actor_id
                   AND NEW.change_reason = 'erased' AND NEW.snapshot = maven.cleanup_tombstone(receipt)
                   AND NEW.content_hash = encode(sha256(convert_to(NEW.snapshot::text, 'UTF8')), 'hex')
                   AND (OLD.record_id = ANY(erased) OR EXISTS (SELECT 1 FROM maven.record_sources c
                        WHERE c.source_id = OLD.record_id AND c.record_id = ANY(erased))) THEN
                    RETURN NEW;
                END IF;
            ELSIF NEW.request_key = OLD.request_key AND NEW.request_hash = OLD.request_hash
                  AND NEW.action = OLD.action AND NEW.record_id = OLD.record_id AND NEW.revision = OLD.revision
                  AND OLD.record_id = ANY(erased) AND NEW.result = maven.cleanup_receipt_result(OLD.result) THEN
                RETURN NEW;
            END IF;
        END IF;
    END IF;
    RAISE EXCEPTION 'immutable history' USING ERRCODE = '42501';
END
$$
-- statement
-- The structured-data guard additionally accepts only the exact erase tombstone, inside erase_records.
CREATE OR REPLACE FUNCTION maven.guard_memory_item() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE kind_value text; receipt uuid;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.data ? 'erased' THEN
        receipt := maven.cleanup_erase_receipt();
        IF receipt IS NOT NULL AND NEW.data = maven.cleanup_tombstone(receipt) AND NEW.tags = '["erased"]'::jsonb
           AND NEW.id = OLD.id AND NEW.project_id IS NOT DISTINCT FROM OLD.project_id
           AND NEW.subject_id IS NULL AND NEW.object_id IS NULL AND NEW.session_id IS NULL
           AND NEW.claim_session IS NULL AND NEW.claim_expires_at IS NULL AND NEW.claim_fence = OLD.claim_fence
           AND EXISTS (SELECT 1 FROM maven.cleanup_receipts WHERE id = receipt AND NEW.id = ANY(record_ids)) THEN
            RETURN NEW;
        END IF;
    END IF;
    SELECT kind INTO kind_value FROM maven.records WHERE id=NEW.id;
    IF NOT maven.memory_data_valid(kind_value,NEW.data,NEW.project_id,NEW.tags)
      OR NEW.subject_id IS DISTINCT FROM (NEW.data->>'subject_id')::uuid
      OR NEW.object_id IS DISTINCT FROM (NEW.data->>'object_id')::uuid
      OR NEW.session_id IS DISTINCT FROM (NEW.data->>'session_id')::uuid
      OR (kind_value<>'task' AND (NEW.claim_session IS NOT NULL OR NEW.claim_fence<>0)) THEN
        RAISE EXCEPTION 'invalid memory record' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END
$$
-- statement
CREATE FUNCTION maven.cleanup_refuse(reason text) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    RAISE EXCEPTION 'cleanup_refused:%', reason USING ERRCODE = '22023';
END
$$
-- statement
-- Validates one approved selection, serializes it with memory writes (same record advisory lock,
-- then row locks in id order) and refuses anything that is not an ordinary, unerased memory record.
CREATE FUNCTION maven.cleanup_select(ids uuid[], actor text, reason text, approval_digest text) RETURNS uuid[]
LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog AS $$
DECLARE selected uuid[]; target uuid;
BEGIN
    IF ids IS NULL OR cardinality(ids) NOT BETWEEN 1 AND 200 OR array_position(ids, NULL) IS NOT NULL THEN
        PERFORM maven.cleanup_refuse('record_count');
    END IF;
    IF approval_digest IS NULL OR approval_digest !~ '^[0-9a-f]{64}$' THEN PERFORM maven.cleanup_refuse('approval_digest'); END IF;
    IF actor IS NULL OR length(actor) NOT BETWEEN 1 AND 2048 THEN PERFORM maven.cleanup_refuse('actor'); END IF;
    IF reason IS NULL OR length(btrim(reason)) NOT BETWEEN 1 AND 240 THEN PERFORM maven.cleanup_refuse('reason'); END IF;
    selected := ARRAY(SELECT DISTINCT id FROM unnest(ids) AS id ORDER BY id);
    FOREACH target IN ARRAY selected LOOP
        PERFORM pg_advisory_xact_lock(hashtextextended('record:' || target::text, 0));
    END LOOP;
    PERFORM 1 FROM maven.records WHERE id = ANY(selected) ORDER BY id FOR UPDATE;
    IF (SELECT count(*) FROM maven.records WHERE id = ANY(selected)) <> cardinality(selected) THEN
        PERFORM maven.cleanup_refuse('record_unavailable');
    END IF;
    IF EXISTS (SELECT 1 FROM maven.records WHERE id = ANY(selected) AND kind IN ('session', 'checkpoint', 'operation')) THEN
        PERFORM maven.cleanup_refuse('lifecycle_kind');
    END IF;
    IF EXISTS (SELECT 1 FROM maven.records WHERE id = ANY(selected) AND kind = 'page') THEN
        PERFORM maven.cleanup_refuse('instruction_page');
    END IF;
    IF EXISTS (SELECT 1 FROM maven.records WHERE id = ANY(selected)
               AND kind NOT IN ('entity', 'fact', 'relationship', 'task', 'decision', 'risk', 'note', 'setting')) THEN
        PERFORM maven.cleanup_refuse('unsupported_kind');
    END IF;
    IF EXISTS (SELECT 1 FROM maven.cleanup_receipts WHERE action = 'erase' AND record_ids && selected) THEN
        PERFORM maven.cleanup_refuse('record_erased');
    END IF;
    IF EXISTS (SELECT 1 FROM maven.records WHERE id = ANY(selected) AND visibility_state = 'blocked') THEN
        PERFORM maven.cleanup_refuse('record_blocked');
    END IF;
    RETURN selected;
END
$$
-- statement
CREATE FUNCTION maven.cleanup_candidates() RETURNS jsonb
LANGUAGE sql STABLE SET search_path = pg_catalog AS $$
    WITH items AS (
      SELECT r.id,r.kind,r.data_scope,r.revision,r.updated_at,m.project_id,m.data,m.tags,maven.memory_title(m.data) AS title
      FROM maven.records r JOIN maven.memory_items m ON m.id=r.id
      WHERE r.kind IN ('entity','fact','relationship','task','decision','risk','note','setting')
        AND r.visibility_state='visible' AND maven.memory_visible(r.id)
    ), ranked AS (
      SELECT i.*,row_number() OVER (PARTITION BY i.kind,i.data_scope,i.project_id,lower(i.title)
        ORDER BY i.updated_at DESC,i.id DESC) AS recency FROM items i
    ), flagged AS (
      SELECT r.*,array_remove(ARRAY[
        CASE WHEN r.tags ? 'maven-scope-superseded' THEN 'superseded' END,
        CASE WHEN maven.memory_archived(r.tags,r.data) THEN 'archived' END,
        CASE WHEN r.kind='task' AND r.data->>'state' IN ('open','in_progress','blocked')
          AND r.updated_at<transaction_timestamp()-interval '30 days' THEN 'stale_task' END,
        CASE WHEN btrim(r.title)<>'' AND r.recency>1 THEN 'duplicate' END,
        CASE WHEN length(COALESCE(maven.memory_body_text(r.data),''))>20000 THEN 'oversized' END,
        CASE WHEN r.project_id IS NULL AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(r.tags) t WHERE t LIKE 'client:%')
          THEN 'client_universal' END],NULL) AS reasons
      FROM ranked r
    ), listed AS (
      SELECT * FROM flagged WHERE cardinality(reasons)>0 ORDER BY updated_at,id LIMIT 501
    ), page AS (SELECT * FROM listed ORDER BY updated_at,id LIMIT 500)
    SELECT jsonb_build_object('candidates',COALESCE((SELECT jsonb_agg(jsonb_build_object('record_id',p.id,'kind',p.kind,
        'scope',CASE WHEN p.project_id IS NULL THEN 'universal' ELSE 'project' END,'project_id',p.project_id,
        'title',p.title,'revision',p.revision,'updated_at',p.updated_at,'reasons',to_jsonb(p.reasons),
        'suggested',CASE WHEN 'superseded'=ANY(p.reasons) THEN 'erase'
          WHEN p.reasons && ARRAY['archived','duplicate','client_universal'] THEN 'forget' ELSE 'archive' END)
        ORDER BY p.updated_at,p.id) FROM page p),'[]'::jsonb),
      'truncated',(SELECT count(*) FROM listed)>500)
$$
-- statement
CREATE FUNCTION maven.cleanup_describe(ids uuid[]) RETURNS jsonb
LANGUAGE plpgsql STABLE SET search_path = pg_catalog AS $$
BEGIN
    IF ids IS NULL OR cardinality(ids) > 200 THEN PERFORM maven.cleanup_refuse('record_count'); END IF;
    RETURN COALESCE((SELECT jsonb_agg(x.entry ORDER BY x.position) FROM (
      SELECT u.position,CASE WHEN r.id IS NULL THEN jsonb_build_object('record_id',u.id,'found',false)
        ELSE jsonb_build_object('record_id',r.id,'found',true,'kind',r.kind,
          'scope',CASE WHEN m.id IS NULL THEN NULL WHEN m.project_id IS NULL THEN 'universal' ELSE 'project' END,
          'project_id',m.project_id,'title',CASE WHEN m.id IS NULL THEN COALESCE(p.title,'') ELSE maven.memory_title(m.data) END,
          'revision',r.revision,
          'visibility_state',r.visibility_state,
          'archived',r.archived_at IS NOT NULL OR COALESCE(maven.memory_archived(m.tags,m.data),false),
          'superseded',COALESCE(m.tags ? 'maven-scope-superseded',false),
          'erased',EXISTS (SELECT 1 FROM maven.cleanup_receipts c WHERE c.action='erase' AND r.id=ANY(c.record_ids)),
          'versions',(SELECT count(*) FROM maven.record_versions v WHERE v.record_id=r.id)) END AS entry
      FROM (SELECT DISTINCT ON (id) id,position FROM unnest(ids) WITH ORDINALITY AS u(id,position)
            WHERE id IS NOT NULL ORDER BY id,position) u
      LEFT JOIN maven.records r ON r.id=u.id
      LEFT JOIN maven.memory_items m ON m.id=r.id
      LEFT JOIN maven.pages p ON p.id=r.id) x),'[]'::jsonb);
END
$$
-- statement
CREATE FUNCTION maven.cleanup_erased_ids() RETURNS jsonb
LANGUAGE sql STABLE SET search_path = pg_catalog AS $$
    SELECT jsonb_build_object('record_ids',COALESCE(jsonb_agg(e.id ORDER BY e.id),'[]'::jsonb))
    FROM (SELECT DISTINCT unnest(record_ids) AS id FROM maven.cleanup_receipts WHERE action='erase') e
$$
-- statement
CREATE FUNCTION maven.forget_records(ids uuid[], restore boolean, actor text, reason text, approval_digest text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog AS $$
DECLARE selected uuid[]; target_state text; changed uuid[]; unchanged uuid[]; receipt uuid := gen_random_uuid(); counts jsonb;
BEGIN
    IF restore IS NULL THEN PERFORM maven.cleanup_refuse('restore'); END IF;
    selected := maven.cleanup_select(ids, actor, reason, approval_digest);
    target_state := CASE WHEN restore THEN 'visible' ELSE 'forgotten' END;
    changed := ARRAY(SELECT id FROM maven.records WHERE id = ANY(selected) AND visibility_state <> target_state ORDER BY id);
    unchanged := ARRAY(SELECT id FROM maven.records WHERE id = ANY(selected) AND visibility_state = target_state ORDER BY id);
    SELECT COALESCE(jsonb_object_agg(v.record_id::text, v.versions), '{}'::jsonb) INTO counts
    FROM (SELECT record_id, count(*) AS versions FROM maven.record_versions WHERE record_id = ANY(changed) GROUP BY record_id) v;
    INSERT INTO maven.cleanup_receipts(id, action, record_ids, revisions, actor, reason, approval_digest)
    VALUES (receipt, CASE WHEN restore THEN 'restore' ELSE 'forget' END, selected, counts, actor, reason, approval_digest);
    UPDATE maven.records SET visibility_state = target_state WHERE id = ANY(changed);
    RETURN jsonb_build_object('receipt_id', receipt, 'changed', to_jsonb(changed), 'unchanged', to_jsonb(unchanged));
END
$$
-- statement
-- A source is blanked only when every citation, proposal, publication and legacy fact that uses it
-- belongs to an erased record; shared sources keep their content.
CREATE FUNCTION maven.erase_records(ids uuid[], actor text, reason text, approval_digest text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog AS $$
DECLARE
    selected uuid[]; receipt uuid := gen_random_uuid(); counts jsonb; tombstone jsonb; tombstone_hash text;
    erased uuid[]; exclusive uuid[];
BEGIN
    selected := maven.cleanup_select(ids, actor, reason, approval_digest);
    SELECT jsonb_object_agg(s.id::text, (SELECT count(*) FROM maven.record_versions v WHERE v.record_id = s.id)) INTO counts
    FROM unnest(selected) AS s(id);
    INSERT INTO maven.cleanup_receipts(id, action, record_ids, revisions, actor, reason, approval_digest)
    VALUES (receipt, 'erase', selected, counts, actor, reason, approval_digest);
    tombstone := maven.cleanup_tombstone(receipt);
    tombstone_hash := encode(sha256(convert_to(tombstone::text, 'UTF8')), 'hex');
    erased := ARRAY(SELECT DISTINCT unnest(record_ids) FROM maven.cleanup_receipts WHERE action = 'erase');
    exclusive := ARRAY(SELECT DISTINCT c.source_id FROM maven.record_sources c WHERE c.record_id = ANY(selected)
      AND NOT EXISTS (SELECT 1 FROM maven.record_sources o WHERE o.source_id = c.source_id AND NOT o.record_id = ANY(erased))
      AND NOT EXISTS (SELECT 1 FROM maven.instruction_proposals p WHERE p.source_id = c.source_id)
      AND NOT EXISTS (SELECT 1 FROM maven.page_publications p WHERE p.approval_source_id = c.source_id)
      AND NOT EXISTS (SELECT 1 FROM maven.facts f WHERE f.source_id = c.source_id AND NOT f.id = ANY(erased)));
    PERFORM set_config('maven.erase_receipt', receipt::text, true);
    UPDATE maven.record_versions SET snapshot = tombstone, content_hash = tombstone_hash, change_reason = 'erased'
    WHERE record_id = ANY(selected) OR record_id = ANY(exclusive);
    UPDATE maven.memory_receipts SET result = maven.cleanup_receipt_result(result)
    WHERE record_id = ANY(selected) AND result <> maven.cleanup_receipt_result(result);
    UPDATE maven.memory_items SET data = tombstone, tags = '["erased"]'::jsonb, subject_id = NULL, object_id = NULL,
      session_id = NULL, claim_session = NULL, claim_expires_at = NULL
    WHERE id = ANY(selected);
    UPDATE maven.facts SET value = 'null'::jsonb, value_type = 'null' WHERE id = ANY(selected);
    UPDATE maven.sources SET excerpt = 'erased', origin = 'erased' WHERE id = ANY(exclusive);
    UPDATE maven.records SET visibility_state = 'forgotten' WHERE id = ANY(selected);
    PERFORM set_config('maven.erase_receipt', '', true);
    RETURN jsonb_build_object('receipt_id', receipt, 'erased_at', tombstone->'erased_at', 'tombstone', tombstone,
      'records', (SELECT jsonb_agg(jsonb_build_object('record_id', s.id, 'versions', (counts->>s.id::text)::integer) ORDER BY s.id)
                  FROM unnest(selected) AS s(id)));
END
$$
-- statement
REVOKE ALL ON FUNCTION maven.cleanup_erase_receipt(), maven.cleanup_tombstone(uuid), maven.cleanup_receipt_result(jsonb),
    maven.cleanup_refuse(text), maven.cleanup_select(uuid[],text,text,text), maven.cleanup_candidates(),
    maven.cleanup_describe(uuid[]), maven.cleanup_erased_ids(), maven.forget_records(uuid[],boolean,text,text,text),
    maven.erase_records(uuid[],text,text,text) FROM PUBLIC
