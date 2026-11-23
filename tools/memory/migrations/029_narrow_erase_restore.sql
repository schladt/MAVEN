-- Historical erase is NOT redaction of the dependency graph. These identities
-- cover exactly the rows/tombstones already authorized, even on an older backup.
CREATE TABLE maven.narrow_absent_ids(kind text NOT NULL, project uuid NOT NULL, id uuid NOT NULL,
    PRIMARY KEY(kind,project,id), CHECK(kind IN ('record','project_record')))
-- statement
CREATE FUNCTION maven.narrow_absent_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF TG_TABLE_NAME='records' THEN
        IF EXISTS(SELECT FROM maven.narrow_absent_ids WHERE kind='record' AND id=NEW.id) THEN
            PERFORM maven.cleanup_refuse('record_erased'); END IF;
    ELSIF NEW.item->>'kind' IS DISTINCT FROM 'erased' AND EXISTS(SELECT FROM maven.narrow_absent_ids
            WHERE kind='project_record' AND project=NEW.project_id AND id=NEW.id) THEN
        PERFORM maven.cleanup_refuse('record_erased');
    END IF;
    RETURN NEW;
END $$
-- statement
CREATE TRIGGER narrow_absent_identity BEFORE INSERT ON maven.records FOR EACH ROW EXECUTE FUNCTION maven.narrow_absent_guard()
-- statement
CREATE TRIGGER narrow_absent_project_record BEFORE INSERT OR UPDATE ON maven.reconcile_records FOR EACH ROW EXECUTE FUNCTION maven.narrow_absent_guard()
-- statement
CREATE FUNCTION maven.narrow_legacy_manifest(op uuid) RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog AS $$
    SELECT jsonb_build_object('job', c.id, 'records', (SELECT jsonb_agg(id ORDER BY id) FROM unnest(c.record_ids) id),
        'sources', (SELECT coalesce(jsonb_agg(s.id ORDER BY s.id),'[]'::jsonb) FROM maven.sources s WHERE EXISTS
        (SELECT FROM maven.record_versions v WHERE v.record_id=s.id AND v.snapshot->>'receipt_id'=c.id::text AND v.snapshot->>'erased'='true')))
    FROM maven.cleanup_receipts c WHERE c.id=op AND c.action='erase'
$$
-- statement
CREATE FUNCTION maven.narrow_replay_project(p uuid,r uuid,apply boolean) RETURNS boolean LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF apply THEN
        IF (SELECT state FROM maven.service_state WHERE singleton) IS DISTINCT FROM 'maintenance' THEN RETURN false; END IF;
        INSERT INTO maven.narrow_absent_ids VALUES('project_record',p,r) ON CONFLICT DO NOTHING;
        UPDATE maven.reconcile_records SET wire=maven.redaction_wire(r),item=maven.redaction_item(r)
            WHERE project_id=p AND id=r AND item->>'kind'<>'erased';
        IF FOUND THEN UPDATE maven.reconcile_projects SET version=gen_random_uuid() WHERE project_id=p; END IF;
    END IF;
    RETURN NOT EXISTS(SELECT FROM maven.reconcile_records WHERE project_id=p AND id=r AND item->>'kind' IS DISTINCT FROM 'erased')
        AND (EXISTS(SELECT FROM maven.reconcile_records WHERE project_id=p AND id=r) OR EXISTS
            (SELECT FROM maven.narrow_absent_ids WHERE kind='project_record' AND project=p AND id=r));
END $$
-- statement
CREATE FUNCTION maven.narrow_replay_legacy(manifest jsonb,apply boolean) RETURNS boolean LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE missing uuid[]; ids uuid[]; src uuid[]; redo uuid; versions jsonb; erased_sources jsonb;
BEGIN
    ids:=ARRAY(SELECT value::uuid FROM jsonb_array_elements_text(manifest->'records'));
    src:=ARRAY(SELECT value::uuid FROM jsonb_array_elements_text(manifest->'sources'));
    IF apply THEN
        IF (SELECT state FROM maven.service_state WHERE singleton) IS DISTINCT FROM 'maintenance' THEN RETURN false; END IF;
        INSERT INTO maven.narrow_absent_ids SELECT 'record','00000000-0000-0000-0000-000000000000'::uuid,i
            FROM unnest(ids||src) i WHERE NOT EXISTS(SELECT FROM maven.records WHERE id=i) ON CONFLICT DO NOTHING;
        SELECT array_agg(r.id ORDER BY r.id), jsonb_object_agg(r.id::text,r.revision) INTO missing,versions FROM maven.records r
            WHERE r.id=ANY(ids) AND NOT EXISTS(SELECT FROM maven.cleanup_receipts c WHERE c.action='erase' AND r.id=ANY(c.record_ids));
        IF missing IS NOT NULL THEN
            -- Use the existing restricted operation, never the broader redaction closure or an immutability bypass.
            redo:=gen_random_uuid(); PERFORM maven.erase_records(missing,redo,versions,'restore','erase',repeat('0',64));
            erased_sources:=maven.narrow_legacy_manifest(redo)->'sources';
            IF NOT erased_sources <@ (manifest->'sources') THEN PERFORM maven.cleanup_refuse('restore_scope_changed'); END IF;
        END IF;
    END IF;
    RETURN NOT EXISTS(SELECT FROM maven.record_versions WHERE record_id=ANY(ids||src) AND snapshot->>'erased' IS DISTINCT FROM 'true')
        AND NOT EXISTS(SELECT FROM maven.records WHERE id=ANY(ids) AND visibility_state<>'forgotten')
        AND NOT EXISTS(SELECT FROM maven.sources WHERE id=ANY(src) AND (origin<>'erased' OR excerpt<>'erased'))
        AND NOT EXISTS(SELECT FROM unnest(ids||src) i WHERE NOT EXISTS(SELECT FROM maven.records WHERE id=i)
            AND NOT EXISTS(SELECT FROM maven.narrow_absent_ids WHERE kind='record' AND id=i));
END $$
-- statement
REVOKE ALL ON maven.narrow_absent_ids FROM PUBLIC
-- statement
REVOKE ALL ON FUNCTION maven.narrow_absent_guard(),maven.narrow_legacy_manifest(uuid),
    maven.narrow_replay_project(uuid,uuid,boolean),maven.narrow_replay_legacy(jsonb,boolean) FROM PUBLIC
