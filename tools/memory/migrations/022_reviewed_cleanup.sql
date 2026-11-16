-- Cleanup is a reviewed compare-and-swap, keyed by the original administration operation.
-- Legacy receipts remain evidence, not invented replay authorizations. Only free-text
-- receipt metadata is redacted; identities, selection, approval digest and time stay exact.
DROP TRIGGER immutable_cleanup_receipt ON maven.cleanup_receipts
-- statement
UPDATE maven.cleanup_receipts SET actor = 'sha256:' || encode(sha256(convert_to(actor,'UTF8')),'hex'),
    reason = action
-- statement
CREATE TRIGGER immutable_cleanup_receipt BEFORE UPDATE OR DELETE ON maven.cleanup_receipts
FOR EACH ROW EXECUTE FUNCTION maven.immutable_version()
-- statement
-- Repair content left in legacy facts by earlier erasures without reopening their history.
UPDATE maven.facts SET predicate = 'erased'
WHERE EXISTS (SELECT 1 FROM maven.cleanup_receipts c WHERE c.action='erase' AND facts.id=ANY(c.record_ids))
-- statement
CREATE TABLE maven.cleanup_operations (
    operation_id uuid PRIMARY KEY REFERENCES maven.cleanup_receipts(id),
    request_hash text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    expected_revisions jsonb NOT NULL CHECK (jsonb_typeof(expected_revisions)='object'),
    result jsonb NOT NULL CHECK (jsonb_typeof(result)='object')
)
-- statement
REVOKE ALL ON maven.cleanup_operations FROM PUBLIC
-- statement
CREATE TRIGGER immutable_cleanup_operation BEFORE UPDATE OR DELETE ON maven.cleanup_operations
FOR EACH ROW EXECUTE FUNCTION maven.immutable_version()
-- statement
-- Do not leave an unreviewed destructive overload reachable by an old administrator.
DROP FUNCTION maven.forget_records(uuid[],boolean,text,text,text)
-- statement
DROP FUNCTION maven.erase_records(uuid[],text,text,text)
-- statement
CREATE FUNCTION maven.cleanup_mutate(action_value text, ids uuid[], operation_id uuid,
    expected_revisions jsonb, actor text, reason text, approval_digest text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog AS $$
DECLARE selected uuid[]; target_state text; changed uuid[]; unchanged uuid[]; counts jsonb;
    tombstone jsonb; tombstone_hash text; erased uuid[]; exclusive uuid[]; result_value jsonb;
    request_hash_value text; previous maven.cleanup_operations%ROWTYPE;
BEGIN
    IF action_value IS NULL OR action_value NOT IN ('forget','restore','erase') THEN PERFORM maven.cleanup_refuse('action'); END IF;
    IF operation_id IS NULL THEN PERFORM maven.cleanup_refuse('operation_id'); END IF;
    IF ids IS NULL OR cardinality(ids) NOT BETWEEN 1 AND 200 OR array_position(ids,NULL) IS NOT NULL THEN
        PERFORM maven.cleanup_refuse('record_count');
    END IF;
    selected := ARRAY(SELECT DISTINCT id FROM unnest(ids) AS id ORDER BY id);
    IF jsonb_typeof(expected_revisions) IS DISTINCT FROM 'object' THEN PERFORM maven.cleanup_refuse('expected_revisions'); END IF;
    IF (SELECT count(*) FROM jsonb_each(expected_revisions)) <> cardinality(selected)
       OR EXISTS (SELECT 1 FROM unnest(selected) id WHERE NOT expected_revisions ? id::text)
       OR EXISTS (SELECT 1 FROM jsonb_each(expected_revisions) e
                  WHERE CASE WHEN jsonb_typeof(e.value)='number' AND e.value::text ~ '^[1-9][0-9]*$'
                    AND length(e.value::text)<=10 THEN (e.value::text)::numeric>2147483647 ELSE true END) THEN
        PERFORM maven.cleanup_refuse('expected_revisions');
    END IF;
    IF approval_digest IS NULL OR approval_digest !~ '^[0-9a-f]{64}$' THEN PERFORM maven.cleanup_refuse('approval_digest'); END IF;
    IF actor IS NULL OR length(actor) NOT BETWEEN 1 AND 2048 THEN PERFORM maven.cleanup_refuse('actor'); END IF;
    IF reason IS NULL OR length(btrim(reason)) NOT BETWEEN 1 AND 240 THEN PERFORM maven.cleanup_refuse('reason'); END IF;
    request_hash_value := encode(sha256(convert_to(jsonb_build_object('action',action_value,'record_ids',selected,
        'expected_revisions',expected_revisions,'actor',actor,'reason',reason,'approval_digest',approval_digest)::text,'UTF8')),'hex');
    PERFORM pg_advisory_xact_lock(hashtextextended('cleanup-operation:' || operation_id::text,0));
    SELECT * INTO previous FROM maven.cleanup_operations c WHERE c.operation_id=cleanup_mutate.operation_id;
    IF FOUND THEN
        IF previous.request_hash<>request_hash_value THEN PERFORM maven.cleanup_refuse('operation_conflict'); END IF;
        -- Return the original outcome verbatim, even after Erase/restore or later revisions.
        RETURN previous.result;
    END IF;
    IF EXISTS (SELECT 1 FROM maven.cleanup_receipts WHERE id=operation_id) THEN
        PERFORM maven.cleanup_refuse('operation_conflict');
    END IF;
    selected := maven.cleanup_select(selected,actor,reason,approval_digest);
    -- cleanup_select holds the same advisory/row locks used by memory_write. No review
    -- or fresh snapshot in a separate transaction can substitute for this comparison.
    IF EXISTS (SELECT 1 FROM maven.records r WHERE r.id=ANY(selected)
               AND r.revision<>(expected_revisions->>r.id::text)::integer) THEN
        PERFORM maven.cleanup_refuse('revision_conflict');
    END IF;
    IF action_value<>'erase' THEN
        target_state := CASE WHEN action_value='restore' THEN 'visible' ELSE 'forgotten' END;
        changed := ARRAY(SELECT id FROM maven.records WHERE id=ANY(selected) AND visibility_state<>target_state ORDER BY id);
        unchanged := ARRAY(SELECT id FROM maven.records WHERE id=ANY(selected) AND visibility_state=target_state ORDER BY id);
        SELECT COALESCE(jsonb_object_agg(v.record_id::text,v.versions),'{}'::jsonb) INTO counts
        FROM (SELECT record_id,count(*) AS versions FROM maven.record_versions WHERE record_id=ANY(changed) GROUP BY record_id) v;
    ELSE
        SELECT jsonb_object_agg(s.id::text,(SELECT count(*) FROM maven.record_versions v WHERE v.record_id=s.id)) INTO counts
        FROM unnest(selected) AS s(id);
    END IF;
    INSERT INTO maven.cleanup_receipts(id,action,record_ids,revisions,actor,reason,approval_digest)
    VALUES(operation_id,action_value,selected,counts,'sha256:' || encode(sha256(convert_to(actor,'UTF8')),'hex'),action_value,approval_digest);
    IF action_value<>'erase' THEN
        UPDATE maven.records SET visibility_state=target_state WHERE id=ANY(changed);
        result_value := jsonb_build_object('receipt_id',operation_id,'operation_id',operation_id,'changed',changed,'unchanged',unchanged);
    ELSE
        tombstone := maven.cleanup_tombstone(operation_id);
        tombstone_hash := encode(sha256(convert_to(tombstone::text,'UTF8')),'hex');
        erased := ARRAY(SELECT DISTINCT unnest(record_ids) FROM maven.cleanup_receipts WHERE action='erase');
        exclusive := ARRAY(SELECT DISTINCT c.source_id FROM maven.record_sources c WHERE c.record_id=ANY(selected)
          AND NOT EXISTS (SELECT 1 FROM maven.record_sources o WHERE o.source_id=c.source_id AND NOT o.record_id=ANY(erased))
          AND NOT EXISTS (SELECT 1 FROM maven.instruction_proposals p WHERE p.source_id=c.source_id)
          AND NOT EXISTS (SELECT 1 FROM maven.page_publications p WHERE p.approval_source_id=c.source_id)
          AND NOT EXISTS (SELECT 1 FROM maven.facts f WHERE f.source_id=c.source_id AND NOT f.id=ANY(erased)));
        PERFORM set_config('maven.erase_receipt',operation_id::text,true);
        UPDATE maven.record_versions SET snapshot=tombstone,content_hash=tombstone_hash,change_reason='erased'
        WHERE record_id=ANY(selected) OR record_id=ANY(exclusive);
        UPDATE maven.memory_receipts SET result=maven.cleanup_receipt_result(result)
        WHERE record_id=ANY(selected) AND result<>maven.cleanup_receipt_result(result);
        UPDATE maven.memory_items SET data=tombstone,tags='["erased"]'::jsonb,subject_id=NULL,object_id=NULL,
            session_id=NULL,claim_session=NULL,claim_expires_at=NULL WHERE id=ANY(selected);
        UPDATE maven.facts SET predicate='erased',value='null'::jsonb,value_type='null' WHERE id=ANY(selected);
        UPDATE maven.sources SET excerpt='erased',origin='erased' WHERE id=ANY(exclusive);
        UPDATE maven.records SET visibility_state='forgotten' WHERE id=ANY(selected);
        PERFORM set_config('maven.erase_receipt','',true);
        result_value := jsonb_build_object('receipt_id',operation_id,'operation_id',operation_id,'erased_at',tombstone->'erased_at','tombstone',tombstone,
            'records',(SELECT jsonb_agg(jsonb_build_object('record_id',s.id,'versions',(counts->>s.id::text)::integer) ORDER BY s.id) FROM unnest(selected) AS s(id)));
    END IF;
    INSERT INTO maven.cleanup_operations(operation_id,request_hash,expected_revisions,result)
    VALUES(operation_id,request_hash_value,expected_revisions,result_value);
    RETURN result_value;
END
$$
-- statement
CREATE FUNCTION maven.forget_records(ids uuid[], restore boolean, operation_id uuid, expected_revisions jsonb,
    actor text, reason text, approval_digest text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog AS $$
BEGIN
    IF restore IS NULL THEN PERFORM maven.cleanup_refuse('restore'); END IF;
    RETURN maven.cleanup_mutate(CASE WHEN restore THEN 'restore' ELSE 'forget' END,
        ids,operation_id,expected_revisions,actor,reason,approval_digest);
END
$$
-- statement
CREATE FUNCTION maven.erase_records(ids uuid[], operation_id uuid, expected_revisions jsonb,
    actor text, reason text, approval_digest text) RETURNS jsonb
LANGUAGE sql VOLATILE SET search_path = pg_catalog AS $$
    SELECT maven.cleanup_mutate('erase',ids,operation_id,expected_revisions,actor,reason,approval_digest)
$$
-- statement
REVOKE ALL ON FUNCTION maven.cleanup_mutate(text,uuid[],uuid,jsonb,text,text,text),
    maven.forget_records(uuid[],boolean,uuid,jsonb,text,text,text),
    maven.erase_records(uuid[],uuid,jsonb,text,text,text) FROM PUBLIC
