"""Explicitly approved, local Data API bootstrap; never called by Lambda.

bootstrap(config, session) receives an already-assumed administration session.
It returns only identity/status metadata. Provider logging, role denial, wake,
concurrent commits, and maintenance draining still require live qualification.
"""

import hashlib
import json
import logging
import re
from pathlib import Path

from .store import DataAPI, ROLLOUT_SCOPE, control_document, fetch_control, safe_provider_error, sdk_config
from .validation import MemoryError, SCHEMA_VERSION, authorized_actor, canonical, uuid_string

MIGRATION_SEPARATOR = "\n-- statement\n"
MIGRATION_FILES = ("001_foundation.sql", "002_fact_change.sql", "003_current_versions.sql",
                   "004_instructions.sql", "005_owner_instructions.sql", "006_owner_memory.sql",
                   "007_memory_instruction_guards.sql", "008_service_state_lock.sql",
                   "009_named_skill_context.sql", "010_skill_resources.sql",
                   "011_project_lifecycle.sql", "012_agentic_memory.sql", "013_performance.sql",
                   "014_authority_guard.sql", "015_home_context.sql", "016_cleanup.sql",
                   "017_project_archive_visibility.sql")
CLEANUP_LIMIT = 200
_CLEANUP_REFUSAL = re.compile(r"cleanup_refused:([a-z_]{1,40})")


def migrations():
    """Exact bytes are hashed; explicit separators avoid parsing SQL bodies."""
    directory = Path(__file__).with_name("migrations")
    result = []
    for name in MIGRATION_FILES:
        raw = (directory / name).read_bytes()
        result.append((name, hashlib.sha256(raw).hexdigest(),
                       raw.decode("utf-8").split(MIGRATION_SEPARATOR)))
    return result


def _refuse():
    raise MemoryError("bootstrap_refused")


def _config(config):
    for key in ("cluster_arn", "app_secret_arn", "admin_secret_arn", "database",
                "journal_bucket", "instance_id", "recovery_epoch", "region",
                "client_role_arn", "admin_role_arn", "approval_role_arn", "endpoint"):
        if not isinstance(config.get(key), str) or not config[key]:
            _refuse()
    uuid_string(config["instance_id"])
    uuid_string(config["recovery_epoch"])
    if config.get("schema_version") != SCHEMA_VERSION or type(config.get("schema_version")) is not int:
        _refuse()
    if config["region"] != "us-east-1" or config["database"] != "maven":
        _refuse()
    if config["app_secret_arn"] == config["admin_secret_arn"]:
        _refuse()


def _put_control(s3, config, state, *, etag=None, absent=False):
    args = {"Bucket": config["journal_bucket"], "Key": "control.json",
            "Body": canonical(control_document(config, state)).encode("utf-8"),
            "ContentType": "application/json", "ServerSideEncryption": "AES256"}
    if absent:
        args["IfNoneMatch"] = "*"
    elif etag:
        args["IfMatch"] = etag
    else:
        _refuse()
    s3.put_object(**args)


def _control(s3, config):
    try:
        # Without ListBucket, S3 returns 403 for an absent object. Conditional
        # creation needs no listing permission and never replaces existing state.
        _put_control(s3, config, "maintenance", absent=True)
    except Exception as error:
        code = getattr(error, "response", {}).get("Error", {}).get("Code")
        if code not in {"PreconditionFailed", "412"}:
            raise
    return fetch_control(s3, config, allow_maintenance=True, allow_legacy=True)


def _check_logging(db):
    raw = db.scalar("SELECT jsonb_build_object(" 
                    "'statement', current_setting('log_statement'), "
                    "'error_statement', current_setting('log_min_error_statement'), "
                    "'error_verbosity', current_setting('log_error_verbosity'), "
                    "'parameters', current_setting('log_parameter_max_length'), "
                    "'error_parameters', current_setting('log_parameter_max_length_on_error'), "
                    "'audit', COALESCE(current_setting('pgaudit.log', true), 'none'))::text")
    settings = json.loads(raw)
    if settings != {"statement": "none", "error_statement": "panic", "error_verbosity": "terse",
                    "parameters": "0", "error_parameters": "0", "audit": "none"}:
        _refuse()
    if db.scalar("SELECT current_user::text") != "maven_admin":
        _refuse()


def _state(db, tx):
    raw = db.scalar("SELECT jsonb_build_object('instance_id', instance_id, "
                    "'recovery_epoch', recovery_epoch, 'schema_version', schema_version, "
                    "'data_scope', data_scope, 'state', state, "
                    "'app_secret_arn', app_secret_arn, 'app_secret_version', app_secret_version)::text "
                    "FROM maven.service_state WHERE singleton", transaction=tx)
    return json.loads(raw) if raw is not None else None


def _identity(state, config, *, allow_legacy=False):
    if state is None:
        _refuse()
    expected = control_document(config, state["state"])
    if allow_legacy and state.get("data_scope") in {"synthetic", "synthetic_and_owner_instructions"}:
        expected["data_scope"] = state["data_scope"]
    if allow_legacy and state.get("schema_version") == 1:
        expected["schema_version"] = 1
    if any(state.get(key) != value for key, value in expected.items()):
        _refuse()
    if state.get("app_secret_arn") != config["app_secret_arn"] or not state.get("app_secret_version"):
        _refuse()


def _grant_instructions(db, tx):
    db.execute("GRANT SELECT ON maven.instruction_receipts TO maven_app", transaction=tx)
    db.execute("GRANT EXECUTE ON FUNCTION maven.instruction_visible(uuid), "
               "maven.instruction_write(text,jsonb,text,text,text), "
               "maven.instruction_challenge(uuid,text), maven.instruction_index(jsonb), "
               "maven.instruction_read(jsonb), maven.change_fact(jsonb,text,text) TO maven_app", transaction=tx)
    db.execute("GRANT EXECUTE ON FUNCTION maven.lock_service_state(), maven.memory_protocol_allowed(), "
               "maven.memory_write(text,jsonb,text,text), maven.memory_read(jsonb), "
               "maven.memory_search(jsonb), maven.memory_context(jsonb), "
               "maven.memory_lookup(uuid) TO maven_app", transaction=tx)
    # Ordinary memory, page and proposal routes are one dispatcher statement with no explicit
    # transaction for reads, so their timeouts are the application role's session defaults.
    db.execute("GRANT EXECUTE ON FUNCTION maven.memory_service_state(), "
               "maven.memory_request(text,jsonb,text,text,text,integer) TO maven_app", transaction=tx)
    db.execute("ALTER ROLE maven_app SET statement_timeout = '5s'", transaction=tx)
    db.execute("ALTER ROLE maven_app SET lock_timeout = '1s'", transaction=tx)


def _install(db, config, secrets, control, *, upgrade=False):
    bundle = migrations()
    with db.transaction() as tx:
        db.execute("SELECT 1 FROM pg_advisory_xact_lock(1967318421)", transaction=tx)
        exists = db.scalar("SELECT to_regnamespace('maven') IS NOT NULL", transaction=tx)
        if exists:
            if not db.scalar("SELECT to_regclass('maven.schema_migrations') IS NOT NULL", transaction=tx):
                _refuse()
            raw = db.scalar("SELECT COALESCE(jsonb_object_agg(name, sha256), '{}'::jsonb)::text "
                            "FROM maven.schema_migrations", transaction=tx)
            installed = json.loads(raw)
            expected = {name: sha for name, sha, _ in bundle}
            if installed == expected:
                _identity(_state(db, tx), config)
                return
            # Only a byte-identical ordered prefix may advance. No repair,
            # down migration, credential rotation, or implicit upgrade.
            prefix = {name: sha for name, sha, _ in bundle[:len(installed)]}
            if (not upgrade or not installed or installed != prefix
                    or len(installed) >= len(bundle) or control["state"] != "maintenance"):
                _refuse()
            _identity(_state(db, tx), config, allow_legacy=True)
            db.execute("UPDATE maven.service_state SET state = 'maintenance' WHERE singleton", transaction=tx)
            for name, sha, statements in bundle[len(installed):]:
                for statement in statements:
                    db.execute(statement, transaction=tx)
                db.execute("INSERT INTO maven.schema_migrations(name, sha256) VALUES (:name, :sha)",
                           {"name": name, "sha": sha}, tx)
            _grant_instructions(db, tx)
            return
        if upgrade:
            _refuse()
        if control["state"] != "maintenance":
            _refuse()
        if db.scalar("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'maven_app')", transaction=tx):
            _refuse()
        db.execute("CREATE SCHEMA maven AUTHORIZATION maven_admin", transaction=tx)
        db.execute("REVOKE ALL ON SCHEMA maven FROM PUBLIC", transaction=tx)
        db.execute("CREATE TABLE maven.schema_migrations (name text PRIMARY KEY, "
                   "sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'), "
                   "installed_at timestamptz NOT NULL DEFAULT transaction_timestamp())", transaction=tx)
        for name, sha, statements in bundle:
            for statement in statements:
                db.execute(statement, transaction=tx)
            db.execute("INSERT INTO maven.schema_migrations(name, sha256) VALUES (:name, :sha)",
                       {"name": name, "sha": sha}, tx)
        # No literal credential SQL leaves this process. Disable all Python logs
        # for the consumer; cluster logging settings are checked before retrieval.
        secret = secrets.get_secret_value(SecretId=config["app_secret_arn"])
        try:
            credential = json.loads(secret["SecretString"])
            password = credential.get("password")
            if credential.get("username") != "maven_app" or not isinstance(password, str) or len(password) < 32:
                _refuse()
            version = secret["VersionId"]
            db.execute("SELECT 1 FROM maven.bootstrap_app(:password)", {"password": password}, tx)
        finally:
            # This is reference disposal, not a claim of zeroizing Python memory.
            secret = credential = password = None
        db.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC", transaction=tx)
        db.execute("REVOKE ALL ON DATABASE maven FROM PUBLIC", transaction=tx)
        db.execute("GRANT CONNECT ON DATABASE maven TO maven_app", transaction=tx)
        db.execute("GRANT USAGE ON SCHEMA maven TO maven_app", transaction=tx)
        db.execute("GRANT SELECT ON maven.service_state, maven.records, maven.facts, maven.sources, "
                   "maven.record_versions, maven.record_sources, maven.operations TO maven_app", transaction=tx)
        db.execute("GRANT EXECUTE ON FUNCTION maven.change_fact(jsonb, text, text) TO maven_app", transaction=tx)
        _grant_instructions(db, tx)
        db.execute("INSERT INTO maven.service_state(singleton, instance_id, recovery_epoch, schema_version, "
                   "data_scope, state, app_secret_arn, app_secret_version) "
                   "VALUES (true, CAST(:instance AS uuid), CAST(:epoch AS uuid), :schema, :scope, "
                   "'maintenance', :secret, :version)",
                   {"instance": config["instance_id"], "epoch": config["recovery_epoch"],
                    "secret": config["app_secret_arn"], "version": version, "scope": ROLLOUT_SCOPE,
                    "schema": SCHEMA_VERSION}, tx)


def _verify_application(db, config):
    with db.transaction() as tx:
        _identity(_state(db, tx), config)
        allowed = db.scalar("SELECT current_user = 'maven_app' "
                            "AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication "
                            "AND NOT EXISTS (SELECT 1 FROM pg_auth_members WHERE member = pg_roles.oid) "
                            "AND NOT has_database_privilege(current_user, 'maven', 'CREATE') "
                            "AND NOT has_database_privilege(current_user, 'maven', 'TEMP') "
                            "AND NOT has_schema_privilege(current_user, 'maven', 'CREATE') "
                            "AND NOT has_schema_privilege(current_user, 'public', 'CREATE') "
                            "AND NOT has_function_privilege(current_user, 'maven.bootstrap_app(text)', 'EXECUTE') "
                            "AND has_function_privilege(current_user, 'maven.change_fact(jsonb,text,text)', 'EXECUTE') "
                            "AND NOT has_table_privilege(current_user, 'maven.records', 'INSERT,UPDATE,DELETE') "
                            "AND NOT has_table_privilege(current_user, 'maven.record_versions', 'INSERT,UPDATE,DELETE') "
                            "AND NOT has_table_privilege(current_user, 'maven.service_state', 'INSERT,UPDATE,DELETE') "
                            "AND has_table_privilege(current_user, 'maven.record_versions', 'SELECT') "
                            "AND NOT has_table_privilege(current_user, 'maven.pages', 'INSERT,UPDATE,DELETE') "
                            "AND NOT has_table_privilege(current_user, 'maven.instruction_proposals', 'INSERT,UPDATE,DELETE') "
                            "AND NOT has_function_privilege(current_user, 'maven.change_fact_foundation(jsonb,text,text)', 'EXECUTE') "
                            "AND NOT has_function_privilege(current_user, 'maven.forget_records(uuid[],boolean,text,text,text)', 'EXECUTE') "
                            "AND NOT has_function_privilege(current_user, 'maven.erase_records(uuid[],text,text,text)', 'EXECUTE') "
                            "AND NOT has_table_privilege(current_user, 'maven.cleanup_receipts', 'SELECT,INSERT,UPDATE,DELETE') "
                            "AND has_function_privilege(current_user, 'maven.instruction_read(jsonb)', 'EXECUTE') "
                            "FROM pg_roles WHERE rolname = current_user", transaction=tx)
        if allowed is not True:
            _refuse()


def _run(config, session, *, upgrade=False):
    """Run only reviewed initialization or ordered migration under maintenance."""
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        _config(config)
        kwargs = {"region_name": config["region"], "config": sdk_config()}
        identity = session.client("sts", **kwargs).get_caller_identity()
        authorized_actor({"requestContext": {"authorizer": {"iam": {"userArn": identity["Arn"]}}}},
                         config["admin_role_arn"])
        data = session.client("rds-data", **kwargs)
        s3 = session.client("s3", **kwargs)
        db = DataAPI(data, config, admin=True)
        _check_logging(db)
        control, etag = _control(s3, config)
        if control.get("uninstall"):
            _refuse()
        if upgrade and (control["state"] == "ready" or control["data_scope"] != ROLLOUT_SCOPE
                        or control["schema_version"] != SCHEMA_VERSION):
            _put_control(s3, config, "maintenance", etag=etag)
            control, _ = fetch_control(s3, config, allow_maintenance=True)
            if control["state"] != "maintenance":
                _refuse()
        _install(db, config, session.client("secretsmanager", **kwargs), control, upgrade=upgrade)
        _verify_application(DataAPI(data, config), config)
        control, etag = fetch_control(s3, config, allow_maintenance=True)
        if control.get("uninstall"):
            _refuse()
        with db.transaction() as tx:
            db.execute("SELECT 1 FROM pg_advisory_xact_lock(1967318421)", transaction=tx)
            _identity(_state(db, tx), config)
            db.execute("UPDATE maven.service_state SET state = 'ready' WHERE singleton", transaction=tx)
        if control["state"] == "maintenance":
            _put_control(s3, config, "ready", etag=etag)
        verified, _ = fetch_control(s3, config)
        if verified != control_document(config, "ready"):
            _refuse()
        return {"status": "ready", "instance_id": config["instance_id"],
                "recovery_epoch": config["recovery_epoch"], "schema_version": SCHEMA_VERSION,
                "data_scope": ROLLOUT_SCOPE}
    except MemoryError:
        raise
    except Exception as error:
        raise safe_provider_error(error) from None
    finally:
        logging.disable(previous_logging)


def bootstrap(config, session):
    """Initialize or repeat the exact installed bundle, never implicitly upgrade."""
    return _run(config, session)


def migrate(config, session):
    """Apply a reviewed ordered migration prefix without reading a password."""
    return _run(config, session, upgrade=True)


def _cleanup_ids(record_ids, *, allow_empty=False):
    if not isinstance(record_ids, (list, tuple)) or len(record_ids) > CLEANUP_LIMIT:
        raise MemoryError("cleanup_refused", 409, detail="record_count")
    if not record_ids and not allow_empty:
        raise MemoryError("cleanup_refused", 409, detail="record_count")
    return json.dumps([uuid_string(value, name="record_id") for value in record_ids])


def _cleanup_approval(actor, reason, approval_digest):
    if not isinstance(actor, str) or not 1 <= len(actor) <= 2048:
        raise MemoryError("cleanup_refused", 409, detail="actor")
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 240:
        raise MemoryError("cleanup_refused", 409, detail="reason")
    if not isinstance(approval_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", approval_digest):
        raise MemoryError("cleanup_refused", 409, detail="approval_digest")
    return {"actor": actor, "reason": reason, "digest": approval_digest}


def _cleanup_error(error):
    response = getattr(error, "response", {})
    message = response.get("Error", {}).get("Message", "")
    match = (_CLEANUP_REFUSAL.search(message)
             if response.get("Error", {}).get("Code") == "DatabaseErrorException" else None)
    if match:
        return MemoryError("cleanup_refused", 409, detail=match.group(1))
    return safe_provider_error(error)


def _cleanup(config, session, expression, values=None):
    """One clean-up statement under the administration role; agents' memory tools never reach it."""
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        _config(config)
        kwargs = {"region_name": config["region"], "config": sdk_config()}
        identity = session.client("sts", **kwargs).get_caller_identity()
        authorized_actor({"requestContext": {"authorizer": {"iam": {"userArn": identity["Arn"]}}}},
                         config["admin_role_arn"])
        db = DataAPI(session.client("rds-data", **kwargs), config, admin=True)
        _check_logging(db)
        with db.transaction() as tx:
            _identity(_state(db, tx), config)
            raw = db.json_scalar(expression, values, tx)
        return json.loads(raw)
    except MemoryError:
        raise
    except Exception as error:
        raise _cleanup_error(error) from None
    finally:
        logging.disable(previous_logging)


_ID_ARRAY = "ARRAY(SELECT jsonb_array_elements_text(CAST(:ids AS jsonb))::uuid)"


def cleanup_candidates(config, session):
    """Read-only review list: {candidates: [...at most 500], truncated}."""
    return _cleanup(config, session, "maven.cleanup_candidates()")


def cleanup_describe(config, session, record_ids):
    """Exact review entries in request order; unknown ids carry found: false."""
    return _cleanup(config, session, f"maven.cleanup_describe({_ID_ARRAY})",
                    {"ids": _cleanup_ids(record_ids, allow_empty=True)})


def cleanup_erased_ids(config, session):
    """Every record id ever erased, so each machine can sweep its local caches."""
    return _cleanup(config, session, "maven.cleanup_erased_ids()")["record_ids"]


def forget_records(config, session, record_ids, *, restore=False, actor, reason, approval_digest):
    """Owner-approved Forget (or restore): visibility only, with one immutable receipt."""
    if type(restore) is not bool:
        raise MemoryError("cleanup_refused", 409, detail="restore")
    values = {"ids": _cleanup_ids(record_ids), "restore": restore,
              **_cleanup_approval(actor, reason, approval_digest)}
    return _cleanup(config, session, f"maven.forget_records({_ID_ARRAY}, :restore, :actor, :reason, :digest)", values)


def erase_records(config, session, record_ids, *, actor, reason, approval_digest):
    """Owner-approved Erase: every version and the current data become one fixed tombstone."""
    values = {"ids": _cleanup_ids(record_ids), **_cleanup_approval(actor, reason, approval_digest)}
    return _cleanup(config, session, f"maven.erase_records({_ID_ARRAY}, :actor, :reason, :digest)", values)
