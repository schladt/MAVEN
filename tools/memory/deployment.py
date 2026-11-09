"""Offline, reviewable CloudFormation packaging; never selects credentials or calls AWS."""

from __future__ import annotations

import base64
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import re
from typing import Any
import uuid
import zlib


from maven_resources import resource_path


_TEMPLATE = resource_path("infra/memory/template.json")
_RUNTIME_FILES = {
    "tools": "tools/__init__.py",
    "tools.memory": "tools/memory/__init__.py",
    "tools.memory.validation": "tools/memory/validation.py",
    "tools.memory.store": "tools/memory/store.py",
    "tools.memory.service": "tools/memory/service.py",
}
_LOCAL_REVIEW_FILES = (
    "tools/memory/admin.py",
    "tools/memory/client.py",
    "tools/memory/cli.py",
    "tools/memory/skills.py",
    "tools/aws/settings.py",
    "tools/aws/secrets.py",
    "tools/memory/deployment.py",
    "tools/memory/environment.py",
    "tools/memory/bootstrap.py",
    "tools/memory/onboarding.py",
    "tools/memory/display.py",
    "tools/memory/boundary.py",
    "tools/setup.py",
    "tools/setup_common.py",
    "tools/interaction.py",
    "tools/tui/__init__.py",
    "tools/tui/app.py",
    "tools/tui/bootstrap.py",
    "tools/project_setup.py",
    "tools/project.py",
    "tools/coordination.py",
    "tools/workflow.py",
    "tools/workspace.py",
)


def _installed_bytes(relative: str) -> bytes:
    package, name = relative.split("/", 1)
    return files(package).joinpath(name).read_bytes()


def _review_hashes() -> dict[str, str]:
    from .admin import MIGRATION_FILES
    paths = [*_LOCAL_REVIEW_FILES,
             *("tools/memory/migrations/" + name for name in MIGRATION_FILES)]
    hashes = {path: _sha256(_installed_bytes(path)) for path in paths}
    for directory in ("config", "templates"):
        root = resource_path(directory)
        for path in sorted(root.rglob("*")):
            if path.is_file():
                relative = path.relative_to(root.parent)
                hashes["maven_resources/" + relative.as_posix()] = _sha256(path.read_bytes())
    return hashes

OUTPUT_CONFIG = {
    "ClusterArn": "cluster_arn",
    "AppSecretArn": "app_secret_arn",
    "AdminSecretArn": "admin_secret_arn",
    "DatabaseName": "database",
    "JournalBucket": "journal_bucket",
    "SkillBucket": "skill_bucket",
    "InstanceId": "instance_id",
    "RecoveryEpoch": "recovery_epoch",
    "Region": "region",
    "ClientRoleArn": "client_role_arn",
    "AdminRoleArn": "admin_role_arn",
    "ApprovalRoleArn": "approval_role_arn",
    "ApiEndpoint": "endpoint",
    "SchemaVersion": "schema_version",
}


def _json(value: Any, *, compact: bool = False) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, allow_nan=False,
        **({"separators": (",", ":")} if compact else {"indent": 2}),
    ) + "\n"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _uuid(value: str | None, name: str) -> str:
    if value is None:
        return str(uuid.uuid4())
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a canonical UUID") from exc
    if str(parsed) != value or parsed.int == 0:
        raise ValueError(f"{name} must be a nonzero canonical UUID")
    return value


def _bundle() -> tuple[str, dict[str, str], dict[str, str]]:
    """Embed actual modules with a normal import loader, preserving relative imports."""
    modules: dict[str, str] = {}
    hashes: dict[str, str] = {}
    for name, relative in _RUNTIME_FILES.items():
        source = _installed_bytes(relative)
        modules[name] = source.decode("utf-8")
        hashes[relative] = _sha256(source)
    packed = base64.b85encode(zlib.compress(_json(modules, compact=True).encode(), 9)).decode("ascii")
    code = (
        "# Generated from the exact reviewed MAVEN runtime modules.\n"
        "import base64\nimport importlib.abc\nimport importlib.util\nimport json\nimport sys\nimport zlib\n"
        f"_SOURCES = json.loads(zlib.decompress(base64.b85decode({packed!r})))\n"
        "class _MemoryModules(importlib.abc.MetaPathFinder, importlib.abc.Loader):\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname in _SOURCES:\n"
        "            return importlib.util.spec_from_loader(fullname, self, "
        "is_package=fullname in ('tools', 'tools.memory'))\n"
        "        return None\n"
        "    def create_module(self, spec):\n"
        "        return None\n"
        "    def exec_module(self, module):\n"
        "        name = module.__name__\n"
        "        filename = '/var/task/' + name.replace('.', '/')\n"
        "        filename += '/__init__.py' if name in ('tools', 'tools.memory') else '.py'\n"
        "        module.__file__ = filename\n"
        "        exec(compile(_SOURCES[name], filename, 'exec'), module.__dict__)\n"
        "sys.meta_path.insert(0, _MemoryModules())\n"
        "from tools.memory.service import handler\n"
    )
    if len(code.encode()) >= 4 * 1024 * 1024:
        raise ValueError("inline runtime exceeds the CloudFormation Lambda ZipFile limit")
    return code, modules, hashes


def _permissions(template: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, resource in template["Resources"].items():
        properties = resource["Properties"]
        if resource["Type"] == "AWS::IAM::Role":
            result[name] = {
                "trust": properties["AssumeRolePolicyDocument"],
                "policies": properties.get("Policies", []),
            }
        elif resource["Type"] in {"AWS::S3::BucketPolicy", "AWS::Lambda::Permission"}:
            result[name] = properties
        elif resource["Type"] == "AWS::ApiGatewayV2::Route":
            result[name] = {key: properties[key] for key in ("RouteKey", "AuthorizationType")}
    return result


def _private_write(path: Path, text: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def build(
    destination: Path,
    *,
    notification_email: str,
    engine_version: str = "18.6",
    instance_name: str = "maven-memory",
    account_id: str | None = None,
    caller_principal_arn: str | None = None,
    instance_id: str | None = None,
    recovery_epoch: str | None = None,
    availability_zones: tuple[str, str] | list[str] | None = None,
) -> dict[str, Any]:
    """Create private artifacts without AWS access, refusing any existing destination.

    Missing account identity or reviewed zones block deployment but not offline review. New UUIDs are
    generated only when omitted and recorded; supplying them reproduces the build.
    Regional engine availability, account authority and pricing are live-review gates,
    never inferred from this package. No generated credential values are available here.
    """
    destination = Path(destination).expanduser()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("memory build destination already exists; refusing overwrite")
    if not isinstance(notification_email, str) or len(notification_email) > 254 or not re.fullmatch(
        r"[^\s@]+@[^\s@]+\.[^\s@]+", notification_email
    ):
        raise ValueError("notification_email must be an explicit email address")
    if not isinstance(engine_version, str) or not re.fullmatch(
        r"18\.(?:[3-9]|[1-9][0-9]+)(?:\.[0-9]+)?", engine_version
    ):
        raise ValueError("engine_version must be a reviewed PostgreSQL 18 patch >=18.3")
    if not isinstance(instance_name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,30}[a-z0-9]", instance_name):
        raise ValueError("instance_name must contain 2..32 lowercase letters, digits or hyphens")
    if account_id is not None and (not isinstance(account_id, str) or not re.fullmatch(r"[0-9]{12}", account_id)):
        raise ValueError("account_id must be an explicitly selected 12-digit AWS account")
    if caller_principal_arn is not None:
        if account_id is None:
            raise ValueError("caller_principal_arn requires an explicitly selected account_id")
        if not isinstance(caller_principal_arn, str) or not re.fullmatch(
            rf"arn:aws:iam::{account_id}:user/[A-Za-z0-9+=,.@_/-]+", caller_principal_arn
        ):
            raise ValueError("caller_principal_arn must identify an exact same-account IAM user")
    if availability_zones is not None:
        if (not isinstance(availability_zones, (tuple, list)) or len(availability_zones) != 2
                or any(not isinstance(zone, str) or not re.fullmatch(r"us-east-1[a-z]", zone)
                       for zone in availability_zones)
                or availability_zones[0] == availability_zones[1]):
            raise ValueError("availability_zones must contain two distinct reviewed us-east-1 zones")
    instance_id = _uuid(instance_id, "instance_id")
    recovery_epoch = _uuid(recovery_epoch, "recovery_epoch")
    code, modules, source_hashes = _bundle()
    source_hashes.update(_review_hashes())
    template_source = _TEMPLATE.read_bytes()
    template = json.loads(template_source)
    template["Resources"]["MemoryFunction"]["Properties"]["Code"] = {"ZipFile": code}
    del template["Parameters"]["RuntimeCode"]
    values = {
        "InstanceName": instance_name,
        "EngineVersion": engine_version,
        "NotificationEmail": notification_email,
        "InstanceId": instance_id,
        "RecoveryEpoch": recovery_epoch,
    }
    if availability_zones is not None:
        values["AvailabilityZones"] = ",".join(availability_zones)
    if account_id is not None:
        values["ExpectedAccountId"] = account_id
    if caller_principal_arn is not None:
        values["CallerPrincipalArn"] = caller_principal_arn
    required_inputs = [name for name, spec in template["Parameters"].items() if name not in values and "Default" not in spec]
    # A compact JSON template stays directly submit-able without an artifact-bucket
    # bootstrap dependency. The repository source and manifest are pretty-printed.
    template_text = _json(template, compact=True)
    if len(template_text.encode()) > 51200:
        raise ValueError("bundled template exceeds the 51,200-byte direct CloudFormation limit; no upload or fake handler was generated")
    parameters = [{"ParameterKey": key, "ParameterValue": value} for key, value in sorted(values.items())]
    configuration = {
        "output_to_config": OUTPUT_CONFIG,
        "integer_config_keys": ["schema_version"],
        "configuration_available": False,
        "instructions": [
            "Do not use this instruction file as a client configuration: no stack outputs exist yet.",
            "After separate owner deployment approval, capture the reviewed stack's Outputs array without fetching secret values.",
            "Map exactly output_to_config, convert schema_version to integer 2, and write a fresh private client/admin/approval configuration from reviewed outputs; never silently update an existing runtime configuration.",
            "Explicitly select the human credential profile; assume ClientRoleArn for ordinary calls, ApprovalRoleArn only for owner challenge/activation or activation lookup, and AdminRoleArn only for approved bootstrap/migration.",
            "Bootstrap or migrate the bundled SQL through the separately approved administrative consumer before ordinary use. Deploy this runtime and migration 012 together before memory-native checkpoints or scoped retrieval. Schema-1 clients must finish pending publication recovery before cutover.",
        ],
    }
    artifacts = {
        "template.json": template_text,
        "parameters.json": _json(parameters),
        "configuration.json": _json(configuration),
        "runtime-index.py": code,
        "runtime-sources.json": _json(modules),
    }
    manifest = {
        "format_version": 1,
        "kind": "offline-memory-foundation-review",
        "deployment_blocked": True,
        "required_inputs": required_inputs,
        "blockers": ([f"Explicit deployment inputs are missing: {', '.join(required_inputs)}."] if required_inputs else []) + [
            "Owner approval of the actual template, parameters, stores, IAM permissions, recipient and costs is required.",
            "Verify selected account/region and current Aurora PostgreSQL patch availability, Data API and zero-ACU support using a separately authorized deployment session.",
            "Deployment, generated-secret handling and bootstrap have not been performed or qualified by this offline build.",
        ],
        "region": "us-east-1",
        "target_guard": template["Metadata"]["TargetGuard"],
        "data_scope": "owner_memory",
        "proposed_stack_name": instance_name,
        "deployment_inputs": values,
        "parameter_definitions": template["Parameters"],
        "capabilities_required": ["CAPABILITY_IAM"],
        "resources": {
            name: {key: value for key, value in resource.items() if key != "Properties"}
            for name, resource in sorted(template["Resources"].items())
        },
        "permissions": _permissions(template),
        "outputs": template["Outputs"],
        "output_to_config": OUTPUT_CONFIG,
        "source_sha256": {"maven_resources/infra/memory/template.json": _sha256(template_source), **source_hashes},
        "runtime_sha256": _sha256(code.encode()),
        "artifact_sha256": {name: _sha256(content.encode()) for name, content in artifacts.items()},
        "packaging": {
            "handler": "index.handler",
            "delegates_to": "tools.memory.service.handler",
            "runtime": "python3.15",
            "architecture": "arm64",
            "method": "inline ZipFile; stdlib import loader for exact zlib/base85-compressed runtime sources",
            "template_bytes": len(template_text.encode()),
            "requires_existing_artifact_bucket": False,
            "dependencies": "stdlib plus boto3/botocore supplied by AWS Lambda Python runtime; no native PostgreSQL adapter",
            "reproducibility": "Same source bytes, inputs (including recorded UUIDs), and Python/zlib toolchain produce identical artifacts; no timestamps or output path embedded.",
        },
        "network": {
            "database": "one private Aurora writer; two AZ subnets, local-only route tables, no TCP ingress",
            "lambda": "outside VPC; AWS-managed outbound networking is not an internet-egress sandbox",
            "api": "public HTTPS endpoint; exactly thirteen POST routes requiring AWS_IAM and route-specific selected-role runtime authorization",
            "absent": ["NAT", "internet gateway", "RDS Proxy", "reader", "VPC endpoints", "bastion", "custom domain", "Lambda VPC attachment"],
        },
        "cost_controls": {
            "monthly_budget_usd": 50,
            "actual_notifications_usd": [25, 50],
            "forecast_notification_usd": 50,
            "budget_filter": template["Resources"]["MemoryBudget"]["Properties"]["Budget"]["CostFilters"],
            "aurora_acu": {"min": 0, "max": 1, "idle_pause_seconds": 300},
            "storage": "Aurora Standard; encrypted native backups retained 30 days",
            "logs_retention_days": 14,
            "lambda_reserved_concurrency": 4,
            "lambda_memory_mib": 512,
            "illustrative_phase6_monthly_usd": {
                "non_acu_subtotal": 6.3964,
                "at_20_percent_awake_and_one_acu": 23.92,
                "at_100_percent_awake_and_one_acu": 94.00,
                "maximum_one_acu_compute_only": 87.60,
                "assumptions": "730h; $0.12/ACU-hour; 20GB live storage; 20GB billable backup; 1M database I/Os; 100k API/Lambda requests at 512MiB/200ms; 500k Data API units; two secrets; illustrative 500k secret calls; 0.1GB S3 with 1k writes and 102k reads.",
                "excluded": "CloudWatch ingestion/storage, applicable transfer/encryption requests, tax, credits and temporary restore clusters; current rates and usage are not verified.",
            },
            "planning_estimate_only": "Phase 6 estimates depend on billed duty cycle, storage, I/O, requests and current public prices; this build neither quotes nor verifies account billing.",
            "limitations": [
                "Budget notifications are not a hard spending cap or automatic shutdown.",
                "Activate the MavenMemoryInstance user-defined cost-allocation tag and verify coverage before relying on the filter.",
                "Shared, untagged and some request charges may not be allocated; billing/notifications lag and forecasts may be unavailable.",
                "Retained Aurora, backups, generated secrets and versioned S3 objects continue to incur charges after stack deletion or replacement.",
                "Auto-pause and maximum ACU do not guarantee actual duty cycle, latency or monthly cost.",
            ],
        },
        "retention": template["Metadata"]["Retention"],
        "bootstrap_boundary": "No control object or SQL readiness is fabricated by CloudFormation. Runtime fails closed until an explicitly selected admin consumer installs reviewed migrations and matching control state.",
    }
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, content in artifacts.items():
        _private_write(destination / name, content)
    _private_write(destination / "manifest.json", _json(manifest))
    return manifest