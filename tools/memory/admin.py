"""Explicitly approved, local Data API bootstrap; never called by Lambda.

bootstrap(config, session) receives an already-assumed administration session.
It returns only identity/status metadata. Provider logging, role denial, wake,
concurrent commits, and maintenance draining still require live qualification.
"""

import hashlib
import json
import logging
import re
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .store import DataAPI, ROLLOUT_SCOPE, control_document, fetch_control, safe_provider_error, sdk_config
from . import erase_fence, redaction_ledger, redaction_objects
from .validation import MemoryError, SCHEMA_VERSION, authorized_actor, canonical, digest, uuid_string

MIGRATION_SEPARATOR = "\n-- statement\n"
MIGRATION_FILES = ("001_foundation.sql", "002_fact_change.sql", "003_current_versions.sql",
                   "004_instructions.sql", "005_owner_instructions.sql", "006_owner_memory.sql",
                   "007_memory_instruction_guards.sql", "008_service_state_lock.sql",
                   "009_named_skill_context.sql", "010_skill_resources.sql",
                   "011_project_lifecycle.sql", "012_agentic_memory.sql", "013_performance.sql",
                   "014_authority_guard.sql", "015_home_context.sql", "016_cleanup.sql",
                   "017_project_archive_visibility.sql", "018_skill_discovery.sql",
                   "019_secret_catalog.sql", "020_secret_adapter_grants.sql",
                   "021_secret_adapter_scope_guard.sql", "022_reviewed_cleanup.sql",
                   "023_retired_work_visibility.sql", "024_immutable_secret_consumption.sql",
                   "025_project_reconciliation.sql", "026_reviewed_redaction.sql",
                   "027_redaction_delivery.sql", "028_redaction_outbox.sql", "029_narrow_erase_restore.sql",
                   "030_reconcile_taxonomy.sql")
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


def _put_control(s3, config, state, *, etag=None, absent=False, bound=False):
    args = {"Bucket": config["journal_bucket"], "Key": "control.json",
            "Body": canonical(control_document(config, state, redaction_bound=bound)).encode("utf-8"),
            "ContentType": "application/json", "ServerSideEncryption": "AES256"}
    if absent:
        args["IfNoneMatch"] = "*"
    elif etag:
        args["IfMatch"] = etag
    else:
        _refuse()
    s3.put_object(**args)


def _control(s3, config, *, existing=False):
    if existing:
        # A legacy DB or restored snapshot must not manufacture a missing fence.
        return fetch_control(s3, config, allow_maintenance=True, allow_legacy=True)
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
               "maven.instruction_read(jsonb) TO maven_app", transaction=tx)
    db.execute("GRANT EXECUTE ON FUNCTION maven.lock_service_state(), maven.memory_protocol_allowed(), "
               "maven.memory_read(jsonb), "
               "maven.memory_search(jsonb), maven.memory_context(jsonb), "
               "maven.memory_lookup(uuid) TO maven_app", transaction=tx)
    # Ordinary memory, page and proposal routes are one dispatcher statement with no explicit
    # transaction for reads, so their timeouts are the application role's session defaults.
    db.execute("GRANT EXECUTE ON FUNCTION maven.memory_service_state(), "
               "maven.memory_request(text,jsonb,text,text,text,integer), "
               "maven.reconcile_request(text,jsonb), maven.reconcile_taxonomy(jsonb), "
               "maven.redaction_delivery(jsonb) TO maven_app", transaction=tx)
    db.execute("REVOKE EXECUTE ON FUNCTION maven.memory_request_before_reconcile(text,jsonb,text,text,text,integer), "
               "maven.memory_write(text,jsonb,text,text), maven.change_fact(jsonb,text,text) FROM maven_app", transaction=tx)
    db.execute("ALTER ROLE maven_app SET statement_timeout = '5s'", transaction=tx)
    db.execute("ALTER ROLE maven_app SET lock_timeout = '1s'", transaction=tx)


def _apply_migration(db, tx, name, sha, statements):
    try:
        for position, statement in enumerate(statements, 1):
            db.execute(statement, transaction=tx)
        position = "registration"
        db.execute("INSERT INTO maven.schema_migrations(name, sha256) VALUES (:name, :sha)",
                   {"name": name, "sha": sha}, tx)
    except Exception as error:
        provider = getattr(error, "response", {}).get("Error", {})
        # Never return provider prose, SQL, or parameters: only the SQLSTATE token.
        match = re.search(r"\bSQLState:\s*([A-Z0-9]{5})\b", provider.get("Message", ""))
        sqlstate = match.group(1) if match else "unavailable"
        status = "migration_rejected" if sqlstate.startswith("42") else "migration_failed"
        raise MemoryError(status, detail=f"Migration: {name}; statement: {position}; SQLSTATE: {sqlstate}") from None


def _install(db, config, secrets, control, *, ledger_gate, upgrade=False):
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
                ledger_gate(tx, False)
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
                _apply_migration(db, tx, name, sha, statements)
            _grant_instructions(db, tx)
            ledger_gate(tx, "026_reviewed_redaction.sql" not in installed)
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
            _apply_migration(db, tx, name, sha, statements)
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
        _grant_instructions(db, tx)
        db.execute("INSERT INTO maven.service_state(singleton, instance_id, recovery_epoch, schema_version, "
                   "data_scope, state, app_secret_arn, app_secret_version) "
                   "VALUES (true, CAST(:instance AS uuid), CAST(:epoch AS uuid), :schema, :scope, "
                   "'maintenance', :secret, :version)",
                   {"instance": config["instance_id"], "epoch": config["recovery_epoch"],
                    "secret": config["app_secret_arn"], "version": version, "scope": ROLLOUT_SCOPE,
                    "schema": SCHEMA_VERSION}, tx)
        ledger_gate(tx, True)


def _verify_application(db, config, *, pre_login=False):
    with db.transaction() as tx:
        _identity(_state(db, tx), config)
        identity = ("current_user = 'maven_admin' AND NOT rolcanlogin" if pre_login else "current_user = 'maven_app'")
        allowed = db.scalar("SELECT " + identity + " "
                            "AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication "
                            "AND NOT EXISTS (SELECT 1 FROM pg_auth_members WHERE member = pg_roles.oid) "
                            "AND NOT has_database_privilege(rolname, 'maven', 'CREATE') "
                            "AND NOT has_database_privilege(rolname, 'maven', 'TEMP') "
                            "AND NOT has_schema_privilege(rolname, 'maven', 'CREATE') "
                            "AND NOT has_schema_privilege(rolname, 'public', 'CREATE') "
                            "AND NOT has_function_privilege(rolname, 'maven.bootstrap_app(text)', 'EXECUTE') "
                            "AND has_function_privilege(rolname, 'maven.reconcile_request(text,jsonb)', 'EXECUTE') "
                            "AND has_function_privilege(rolname, 'maven.reconcile_taxonomy(jsonb)', 'EXECUTE') "
                            "AND has_function_privilege(rolname, 'maven.redaction_delivery(jsonb)', 'EXECUTE') "
                            "AND NOT EXISTS (SELECT FROM pg_proc p WHERE p.pronamespace='maven'::regnamespace "
                            "AND (starts_with(p.proname,'redaction_') OR starts_with(p.proname,'narrow_')) "
                            "AND p.oid <> 'maven.redaction_delivery(jsonb)'::regprocedure "
                            "AND has_function_privilege(rolname,p.oid,'EXECUTE')) "
                            "AND NOT EXISTS (SELECT FROM pg_class c WHERE c.relnamespace='maven'::regnamespace "
                            "AND (starts_with(c.relname,'redaction_') OR starts_with(c.relname,'narrow_')) AND c.relkind IN ('r','v') "
                            "AND (has_table_privilege(rolname,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES') "
                            "OR has_any_column_privilege(rolname,c.oid,'SELECT,INSERT,UPDATE,REFERENCES'))) "
                            "AND NOT has_function_privilege(rolname, 'maven.change_fact(jsonb,text,text)', 'EXECUTE') "
                            "AND NOT has_function_privilege(rolname, 'maven.memory_write(text,jsonb,text,text)', 'EXECUTE') "
                            "AND NOT has_function_privilege(rolname, 'maven.memory_request_before_reconcile(text,jsonb,text,text,text,integer)', 'EXECUTE') "
                            "AND NOT has_table_privilege(rolname, 'maven.reconcile_records', 'SELECT,INSERT,UPDATE,DELETE') "
                            "AND NOT has_table_privilege(rolname, 'maven.reconcile_projects', 'SELECT,INSERT,UPDATE,DELETE') "
                            "AND NOT has_table_privilege(rolname, 'maven.records', 'INSERT,UPDATE,DELETE') "
                            "AND NOT has_table_privilege(rolname, 'maven.record_versions', 'INSERT,UPDATE,DELETE') "
                            "AND NOT has_table_privilege(rolname, 'maven.service_state', 'INSERT,UPDATE,DELETE') "
                            "AND has_table_privilege(rolname, 'maven.record_versions', 'SELECT') "
                            "AND NOT has_table_privilege(rolname, 'maven.pages', 'INSERT,UPDATE,DELETE') "
                            "AND NOT has_table_privilege(rolname, 'maven.instruction_proposals', 'INSERT,UPDATE,DELETE') "
                            "AND NOT has_function_privilege(rolname, 'maven.change_fact_foundation(jsonb,text,text)', 'EXECUTE') "
                            "AND NOT has_function_privilege(rolname, 'maven.forget_records(uuid[],boolean,uuid,jsonb,text,text,text)', 'EXECUTE') "
                            "AND NOT has_function_privilege(rolname, 'maven.erase_records(uuid[],uuid,jsonb,text,text,text)', 'EXECUTE') "
                            "AND NOT has_function_privilege(rolname, 'maven.cleanup_mutate(text,uuid[],uuid,jsonb,text,text,text)', 'EXECUTE') "
                            "AND NOT has_table_privilege(rolname, 'maven.cleanup_receipts', 'SELECT,INSERT,UPDATE,DELETE') "
                            "AND NOT has_table_privilege(rolname, 'maven.cleanup_operations', 'SELECT,INSERT,UPDATE,DELETE') "
                            "AND has_function_privilege(rolname, 'maven.instruction_read(jsonb)', 'EXECUTE') "
                            "FROM pg_roles WHERE rolname = 'maven_app'", transaction=tx)
        if allowed is not True:
            _refuse()


def _run(config, session, *, upgrade=False):
    """Run only reviewed initialization or ordered migration under maintenance."""
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    db = None
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
        control, etag = _control(s3, config, existing=upgrade)
        if control.get("uninstall"):
            _refuse()
        if upgrade and (control["state"] == "ready" or control["data_scope"] != ROLLOUT_SCOPE
                        or control["schema_version"] != SCHEMA_VERSION):
            _put_control(s3, config, "maintenance", etag=etag, bound=control.get("redaction_bound", False))
            control, _ = fetch_control(s3, config, allow_maintenance=True)
            if control["state"] != "maintenance":
                _refuse()
        rds = session.client("rds", **kwargs)
        physical = redaction_ledger.cluster_identity(rds, config)

        def ledger_gate(tx, can_seed):
            if not can_seed:
                redaction_ledger.verify(db, tx, s3, config, physical)
                return
            if control.get("redaction_bound") or control["state"] != "maintenance":
                _refuse()
            if upgrade:
                # A restored legacy cluster has a new physical creation time. Its SQL
                # installation predates that physical cluster; it cannot adopt a new ledger.
                clusters = rds.describe_db_clusters(DBClusterIdentifier=config["cluster_arn"])["DBClusters"]
                if len(clusters) != 1 or clusters[0]["DBClusterArn"] != config["cluster_arn"]:
                    _refuse()
                created = clusters[0]["ClusterCreateTime"].isoformat()
                if db.scalar("SELECT CAST(:created AS timestamptz) <= min(installed_at) FROM maven.schema_migrations",
                             {"created": created}, tx) is not True:
                    _refuse()
            redaction_ledger.genesis(db, tx, s3, config, physical)
            erase_fence.initialize(db, tx, s3, config)

        _install(db, config, session.client("secretsmanager", **kwargs), control, ledger_gate=ledger_gate, upgrade=upgrade)
        _verify_application(DataAPI(data, config), config)
        control, etag = fetch_control(s3, config, allow_maintenance=True)
        if control.get("uninstall"):
            _refuse()
        with db.transaction() as tx:
            db.execute("SELECT 1 FROM pg_advisory_xact_lock(1967318421)", transaction=tx)
            _identity(_state(db, tx), config)
            redaction_ledger.verify(db, tx, s3, config, physical)
            db.execute("UPDATE maven.service_state SET state = 'ready' WHERE singleton", transaction=tx)
        if control["state"] == "maintenance" or not control.get("redaction_bound"):
            _put_control(s3, config, "ready", etag=etag, bound=True)
        verified, _ = fetch_control(s3, config)
        if verified != control_document(config, "ready", redaction_bound=True):
            _refuse()
        return {"status": "ready", "instance_id": config["instance_id"],
                "recovery_epoch": config["recovery_epoch"], "schema_version": SCHEMA_VERSION,
                "data_scope": ROLLOUT_SCOPE}
    except MemoryError as error:
        if error.status in ("migration_rejected", "migration_failed"):
            confirmed = db is not None and db.rollback_confirmed is True
            status = error.status if confirmed else "migration_outcome_unknown"
            detail = error.metadata["detail"] + ("; rollback confirmed" if confirmed else "; rollback unconfirmed")
            raise MemoryError(status, 422 if confirmed else 503, detail=detail) from None
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


def _cleanup_review(record_ids, operation_id, expected_revisions):
    ids = _cleanup_ids(record_ids)
    operation_id = uuid_string(operation_id, name="operation_id")
    if (not isinstance(expected_revisions, dict) or set(expected_revisions) != set(record_ids)
            or any(type(revision) is not int or not 1 <= revision <= 2147483647
                   for revision in expected_revisions.values())):
        raise MemoryError("cleanup_refused", 409, detail="expected_revisions")
    return {"ids": ids, "operation_id": operation_id,
            "expected_revisions": canonical(expected_revisions)}


def _cleanup_error(error):
    response = getattr(error, "response", {})
    message = response.get("Error", {}).get("Message", "")
    match = (_CLEANUP_REFUSAL.search(message)
             if response.get("Error", {}).get("Code") == "DatabaseErrorException" else None)
    if match:
        return MemoryError("cleanup_refused", 409, detail=match.group(1))
    return safe_provider_error(error)


@contextmanager
def _admin_transaction(config, session, *, job=None, review_digest=None):
    """Pin the live physical database and erasure head while using admin privileges."""
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        _config(config)
        kwargs = {"region_name": config["region"], "config": sdk_config()}
        with ExitStack() as clients:
            def client(name):
                item = session.client(name, **kwargs)
                clients.callback(item.close)
                return item
            identity = client("sts").get_caller_identity()
            authorized_actor({"requestContext": {"authorizer": {"iam": {"userArn": identity["Arn"]}}}},
                             config["admin_role_arn"])
            s3 = client("s3")
            control, _ = fetch_control(s3, config)
            if control.get("redaction_bound") is not True:
                raise MemoryError("redaction_ledger_unverified", 409)
            physical = redaction_ledger.cluster_identity(client("rds"), config)
            db = DataAPI(client("rds-data"), config, admin=True)
            _check_logging(db)
            with db.transaction() as tx:
                redaction_ledger.verify(db, tx, s3, config, physical, job=job, plan_digest=review_digest)
                state = _state(db, tx)
                _identity(state, config)
                if state["state"] != "ready":
                    raise MemoryError("context_stale", 409)
                yield db, tx, s3, physical
    except redaction_objects.ObjectRedactionError as error:
        raise MemoryError("redaction_scope_changed", 409, detail=error.code) from None
    except MemoryError:
        raise
    except Exception as error:
        raise _cleanup_error(error) from None
    finally:
        logging.disable(previous_logging)


def _cleanup(config, session, expression, values=None, *, reviewed=False, erase_job=None):
    with _admin_transaction(config, session) as (db, tx, s3, physical):
        if reviewed and db.scalar(
                "SELECT to_regprocedure('maven.forget_records(uuid[],boolean,uuid,jsonb,text,text,text)') IS NOT NULL "
                "AND to_regprocedure('maven.erase_records(uuid[],uuid,jsonb,text,text,text)') IS NOT NULL",
                transaction=tx) is not True:
            raise MemoryError("cleanup_upgrade_required", 409)
        result = json.loads(db.json_scalar(expression, values, tx))
        if erase_job:
            erase_fence.record_legacy_erase(db, tx, s3, config, erase_job)
            if fetch_control(s3, config)[0].get("redaction_bound") is not True:
                raise MemoryError("context_stale", 409)
            redaction_ledger.verify(db, tx, s3, config, physical)
        return result


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


def forget_records(config, session, record_ids, *, operation_id, expected_revisions,
                   restore=False, actor, reason, approval_digest):
    """Replayable reviewed Forget/restore; the original UUID and revision manifest are required."""
    if type(restore) is not bool:
        raise MemoryError("cleanup_refused", 409, detail="restore")
    values = {**_cleanup_review(record_ids, operation_id, expected_revisions), "restore": restore,
              **_cleanup_approval(actor, reason, approval_digest)}
    return _cleanup(config, session,
                    f"maven.forget_records({_ID_ARRAY}, :restore, CAST(:operation_id AS uuid), "
                    "CAST(:expected_revisions AS jsonb), :actor, :reason, :digest)", values, reviewed=True)


def erase_records(config, session, record_ids, *, operation_id, expected_revisions,
                  actor, reason, approval_digest):
    """Replayable reviewed Erase; never substitute a new operation or a fresh revision snapshot."""
    values = {**_cleanup_review(record_ids, operation_id, expected_revisions),
              **_cleanup_approval(actor, reason, approval_digest)}
    return _cleanup(config, session,
                    f"maven.erase_records({_ID_ARRAY}, CAST(:operation_id AS uuid), "
                    "CAST(:expected_revisions AS jsonb), :actor, :reason, :digest)", values, reviewed=True,
                    erase_job=operation_id)


def _salt(db, tx):
    return uuid_string(db.scalar("SELECT maven.redaction_salt()::text", transaction=tx))


def redaction_salt(config, session):
    """Internal input for the separately authorized secret provider. Never a review field."""
    with _admin_transaction(config, session) as (db, tx, _, _):
        return _salt(db, tx)


def redaction_candidates(config, session, after=None):
    """Content-free keyset discovery, not an approval or stable snapshot."""
    def key(target):
        return target["kind"], target.get("project_id", ""), target["id"]

    cursor = key(after) if after else ("", "", "")
    with _admin_transaction(config, session) as (db, tx, _, _):
        rows = _sql_json(db, tx, """(SELECT coalesce(jsonb_agg(target ORDER BY k,p,i),'[]'::jsonb) FROM (
            SELECT DISTINCT target,(target->>'kind') COLLATE "C" AS k,
                coalesce(target->>'project_id','') COLLATE "C" AS p,(target->>'id') COLLATE "C" AS i
            FROM maven.redaction_versions
            WHERE ((target->>'kind') COLLATE "C",coalesce(target->>'project_id','') COLLATE "C",
                   (target->>'id') COLLATE "C") > (:kind,:project,:id) ORDER BY k,p,i LIMIT 101
        ) page)""", kind=cursor[0], project=cursor[1], id=cursor[2])
        for target in redaction_objects.candidates(config, session, _salt(db, tx)):
            if key(target) > cursor:
                rows.append(target)
        by_key = {key(target): target for target in rows}
        page = [by_key[k] for k in sorted(by_key)[:100]]
        return {"candidates": page, "next_after": page[-1] if len(by_key) > len(page) else None}


def _sql_json(db, tx, expression, **values):
    return json.loads(db.json_scalar(expression, values, tx))


def _redaction_review(db, tx, config, session, targets):
    value = _sql_json(db, tx, "maven.redaction_review(CAST(:targets AS jsonb))", targets=canonical(targets))
    objects = [item for item in value["permanent_targets"] if item["kind"] in {"skill_bundle", "recovery_object"}]
    keys = (_sql_json(db, tx, "maven.redaction_bundle_keys(CAST(:ids AS jsonb))", ids=canonical(value["bundles"]))
            if value["bundles"] else {})
    salt = _salt(db, tx)
    plan = redaction_objects.review(config, session, salt, objects, missing_keys=keys)
    return {**value, "objects": plan, "instance_id": config["instance_id"], "recovery_epoch": config["recovery_epoch"]}, keys


def review_redaction(config, session, targets):
    with _admin_transaction(config, session) as (db, tx, _, _):
        review, _ = _redaction_review(db, tx, config, session, targets)
        return review


def _outbox(db, tx, job):
    return _sql_json(db, tx, "COALESCE((SELECT jsonb_build_object('digest',review_digest,'plan',object_plan) "
                     "FROM maven.redaction_outbox WHERE job_id=CAST(:job AS uuid)), 'null'::jsonb)", job=job)


def redaction_pending(config, session):
    with _admin_transaction(config, session) as (db, tx, _, _):
        return _sql_json(db, tx, "(SELECT coalesce(jsonb_agg(jsonb_build_object('job_id',job_id, "
                         "'objects',(SELECT jsonb_agg(jsonb_build_object('kind',o->'kind','id',o->'id')) "
                         "FROM jsonb_array_elements(object_plan->'objects') o)) ORDER BY job_id),'[]'::jsonb) "
                         "FROM maven.redaction_outbox WHERE NOT verified)")


def review_redaction_objects(config, session, job_id):
    with _admin_transaction(config, session) as (db, tx, _, _):
        entry = _outbox(db, tx, uuid_string(job_id))
        if entry is None:
            raise MemoryError("record_unavailable", 404)
        return entry["plan"]


def finish_redaction_objects(config, session, job_id):
    """Only the already authorized pins may be acted on, even after a process crash."""
    job = uuid_string(job_id)
    try:
        with _admin_transaction(config, session) as (db, tx, _, _):
            entry = _outbox(db, tx, job)
            if entry is None:
                raise MemoryError("record_unavailable", 404)
            try:
                result = redaction_objects.apply(config, session, _salt(db, tx), entry["plan"])
            except redaction_objects.ObjectRedactionError as error:
                return {"status": "blocked", "code": error.code}
            db.execute("UPDATE maven.redaction_outbox SET verified=true WHERE job_id=CAST(:job AS uuid) AND NOT verified",
                       {"job": job}, tx)
        return result
    except MemoryError as error:
        if error.status == "record_unavailable":
            raise
        # The SQL redaction is already committed; do not claim the provider phase completed.
        return {"status": "blocked", "code": "provider_unverified"}


def apply_redaction(config, session, *, operation_id, review):
    job = uuid_string(operation_id)
    if not isinstance(review, dict) or review.get("instance_id") != config["instance_id"] or review.get("recovery_epoch") != config["recovery_epoch"]:
        raise MemoryError("redaction_scope_changed", 409)
    fingerprint = digest(review)
    with _admin_transaction(config, session, job=job, review_digest=fingerprint) as (db, tx, s3, physical):
        entry = _outbox(db, tx, job)
        if entry is not None:
            if entry["digest"] != fingerprint:
                raise MemoryError("redaction_scope_changed", 409)
        else:
            verified, keys = _redaction_review(db, tx, config, session, review.get("targets"))
            if verified != review:
                raise MemoryError("redaction_scope_changed", 409)
            values = {"job": job, "targets": canonical(verified["targets"]), "plan": verified["plan_digest"]}
            result = _sql_json(db, tx, "maven.redaction_apply(CAST(:job AS uuid),CAST(:targets AS jsonb),:plan)", **values)
            if (result["job_id"] != job or result["live_data_redacted"] is not True
                    or sorted(result["pending_bundles"]) != sorted(verified["bundles"])):
                raise MemoryError("redaction_scope_changed", 409)
            redaction_objects.prepare_missing(config, session, _salt(db, tx), verified["objects"], keys)
            db.execute("INSERT INTO maven.redaction_outbox(job_id,review_digest,object_plan,verified) "
                       "VALUES(CAST(:job AS uuid),:fingerprint,CAST(:objects AS jsonb),:empty)",
                       {"job": job, "fingerprint": fingerprint, "objects": canonical(verified["objects"]),
                        "empty": not verified["objects"]["objects"]}, tx)
            # Database CAS succeeded under lock. Journal the exact approved intent before commit;
            # a lost SQL commit reply cannot leave a restorable pre-erasure snapshot unprotected.
            redaction_ledger.record_applied(db, tx, s3, config, physical, job, verified["permanent_targets"],
                                            fingerprint, object_plan=verified["objects"])
    objects = finish_redaction_objects(config, session, job)
    return {"job_id": job, "live_data_redacted": True, "objects": objects,
            "backup_retention_pending": True, "offline_copies_unverified": True}


def redaction_restore_review(config, session):
    from . import redaction_recovery
    return redaction_recovery.review(config, session)


def redaction_restore_apply(config, session, review):
    from . import redaction_recovery
    return redaction_recovery.apply(config, session, review)
