CREATE TABLE maven.service_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    instance_id uuid NOT NULL,
    recovery_epoch uuid NOT NULL,
    schema_version integer NOT NULL CHECK (schema_version = 1),
    data_scope text NOT NULL CHECK (data_scope = 'synthetic'),
    state text NOT NULL CHECK (state IN ('maintenance', 'ready')),
    app_secret_arn text NOT NULL,
    app_secret_version text NOT NULL
)
-- statement
CREATE TABLE maven.records (
    id uuid PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('fact', 'source')),
    revision integer NOT NULL CHECK (revision > 0),
    classification text NOT NULL DEFAULT 'Sensitive' CHECK (classification = 'Sensitive'),
    data_scope text NOT NULL DEFAULT 'synthetic' CHECK (data_scope = 'synthetic'),
    visibility_state text NOT NULL DEFAULT 'visible' CHECK (visibility_state IN ('visible', 'blocked', 'forgotten')),
    archived_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    UNIQUE (id, kind)
)
-- statement
CREATE TABLE maven.sources (
    id uuid PRIMARY KEY,
    kind text NOT NULL DEFAULT 'source' CHECK (kind = 'source'),
    source_kind text NOT NULL DEFAULT 'synthetic' CHECK (source_kind = 'synthetic'),
    origin text NOT NULL CHECK (length(btrim(origin)) BETWEEN 1 AND 160),
    excerpt text NOT NULL CHECK (length(excerpt) <= 2000),
    captured_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    FOREIGN KEY (id, kind) REFERENCES maven.records (id, kind)
)
-- statement
CREATE TABLE maven.facts (
    id uuid PRIMARY KEY,
    kind text NOT NULL DEFAULT 'fact' CHECK (kind = 'fact'),
    subject_id uuid CHECK (subject_id IS NULL),
    predicate text NOT NULL CHECK (length(btrim(predicate)) BETWEEN 1 AND 80),
    value_type text NOT NULL CHECK (value_type IN ('string', 'number', 'boolean', 'object', 'array', 'null')),
    value jsonb NOT NULL,
    source_id uuid NOT NULL UNIQUE REFERENCES maven.sources (id),
    CHECK (jsonb_typeof(value) = value_type),
    FOREIGN KEY (id, kind) REFERENCES maven.records (id, kind)
)
-- statement
CREATE TABLE maven.record_versions (
    record_id uuid NOT NULL REFERENCES maven.records (id),
    revision integer NOT NULL CHECK (revision > 0),
    snapshot_schema_version integer NOT NULL DEFAULT 1 CHECK (snapshot_schema_version = 1),
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot) = 'object'),
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    actor_id text NOT NULL CHECK (length(actor_id) BETWEEN 1 AND 2048),
    change_reason text NOT NULL CHECK (length(btrim(change_reason)) BETWEEN 1 AND 240),
    PRIMARY KEY (record_id, revision)
)
-- statement
CREATE TABLE maven.record_sources (
    record_id uuid NOT NULL,
    revision integer NOT NULL,
    source_id uuid NOT NULL REFERENCES maven.sources (id),
    source_revision integer NOT NULL,
    role text NOT NULL DEFAULT 'evidence' CHECK (role = 'evidence'),
    PRIMARY KEY (record_id, revision, source_id, source_revision, role),
    FOREIGN KEY (record_id, revision) REFERENCES maven.record_versions (record_id, revision),
    FOREIGN KEY (source_id, source_revision) REFERENCES maven.record_versions (record_id, revision)
)
-- statement
CREATE INDEX record_sources_source ON maven.record_sources (source_id, source_revision)
-- statement
CREATE TABLE maven.operations (
    request_key uuid PRIMARY KEY,
    request_hash text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    record_id uuid NOT NULL,
    revision integer NOT NULL,
    operation_kind text NOT NULL CHECK (operation_kind IN ('create_fact', 'correct_fact')),
    recorded_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    FOREIGN KEY (record_id, revision) REFERENCES maven.record_versions (record_id, revision),
    UNIQUE (record_id, revision)
)
-- statement
CREATE FUNCTION maven.immutable_version() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    RAISE EXCEPTION 'immutable history' USING ERRCODE = '42501';
END
$$
-- statement
CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON maven.record_versions
FOR EACH ROW EXECUTE FUNCTION maven.immutable_version()
-- statement
CREATE TRIGGER immutable_receipt BEFORE UPDATE OR DELETE ON maven.operations
FOR EACH ROW EXECUTE FUNCTION maven.immutable_version()
-- statement
CREATE TRIGGER immutable_citation BEFORE UPDATE OR DELETE ON maven.record_sources
FOR EACH ROW EXECUTE FUNCTION maven.immutable_version()
-- statement
CREATE FUNCTION maven.bootstrap_app(password_value text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
BEGIN
    IF session_user <> 'maven_admin' OR length(password_value) < 32 THEN
        RAISE EXCEPTION 'bootstrap refused' USING ERRCODE = '42501';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'maven_app') THEN
        RAISE EXCEPTION 'existing login refused' USING ERRCODE = '42501';
    END IF;
    EXECUTE format('CREATE ROLE maven_app LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION', password_value);
END
$$
-- statement
REVOKE ALL ON FUNCTION maven.bootstrap_app(text) FROM PUBLIC
-- statement
REVOKE ALL ON FUNCTION maven.immutable_version() FROM PUBLIC
