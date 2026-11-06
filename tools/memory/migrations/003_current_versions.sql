ALTER TABLE maven.records ADD CONSTRAINT records_current_version
FOREIGN KEY (id, revision) REFERENCES maven.record_versions (record_id, revision)
DEFERRABLE INITIALLY DEFERRED
-- statement
ALTER TABLE maven.operations ADD CONSTRAINT operation_revision_kind
CHECK ((operation_kind = 'create_fact' AND revision = 1)
    OR (operation_kind = 'correct_fact' AND revision > 1))
-- statement
ALTER TABLE maven.record_sources ADD CONSTRAINT source_matches_fact_revision
CHECK (revision = source_revision)
