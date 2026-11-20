"""Mode-gated secret operations. Tool arguments/results never carry credential values."""
from __future__ import annotations

from functools import wraps
from typing import Any, Literal

from tools.aws.secret_store import DEFAULT_STREAM_LIMIT, SecretStore
from tools.aws.secrets import StoreError
from tools.memory.boundary import BoundaryError
from tools.memory.environment import load_environment
from tools.memory.validation import source as validate_source
from tools.secret_catalog import SecretCatalog


def register_secret_tools(server, app, guarded):
    def selected(function):
        @wraps(function)
        def invoke(**kwargs):
            with app.local(kwargs['cwd'], use_home=kwargs.get('use_home', False)):
                return function(**kwargs)
        return invoke

    @server.tool()
    @guarded
    @selected
    def maven_secrets(cwd: str, action: Literal['list', 'lookup', 'store_and_catalog', 'retry_catalog', 'resolve_catalog'],
                      name: str | None = None, domain: str | None = None, service: str | None = None,
                      cursor: str | None = None, limit: int = 20, source_path: str | None = None,
                      provider: Literal['ssm', 'encrypted-file'] = 'ssm', replace: bool = False,
                      operation_id: str | None = None, source: dict[str, str] | None = None,
                      max_bytes: int = DEFAULT_STREAM_LIMIT, expected_revision: int | None = None,
                      use_home: bool = False) -> dict[str, Any]:
        """Use the explicitly configured secret store in a selected project or Home.

        list returns paged names/metadata for SSM and encrypted files, never values. domain filters a dotted
        namespace, not a substring. lookup returns only the exact Home secret: setting, without a Home session
        or general memory search. store_and_catalog is an explicit owner command: read an owner-private input
        file (source_path), store it, then write only that secret's Home catalog setting. Never pass the value
        through tool arguments, argv or environment. source records the real owner's request. replace=true
        rotates an existing catalog entry; files use a new immutable object, SSM refs pin the stored version.
        SSM storage and catalog CAS are separate; neither is a provider credential rotation. If storage succeeds
        but catalog fails, retry_catalog with the original operation_id: it never re-uploads. A definitive
        revision conflict returns the current record for review; resolve_catalog requires its expected_revision
        and actual owner source before repointing to the original receipt. Preserve unknown outcomes.
        Low-level maven-secrets CLI commands never write Home. No memory session/capture is started."""
        try:
            if action == 'list':
                return {'status': 'ready', **SecretStore(load_environment(app.env)).list(domain, cursor, limit)}
            if action == 'store_and_catalog':
                if source is None or not source_path:
                    raise StoreError('explicit storage requires the actual owner request and a private input path')
                validate_source(source)
            controller = SecretCatalog(app.home, client=app.unbound_client())
            if action == 'lookup':
                return {'status': 'ready', 'home_id': controller.home_id, 'record': controller.lookup(name)}
            if action == 'retry_catalog':
                return controller.retry_catalog(operation_id)
            if action == 'resolve_catalog':
                return controller.resolve_catalog(operation_id, expected_revision=expected_revision, source=source)
            return controller.store_and_catalog(
                name, source_path=source_path, provider=provider, replace=replace, operation_id=operation_id,
                domain=domain, service=service, source=source, max_bytes=max_bytes)
        except StoreError as error:
            raise BoundaryError(str(error)) from None

    @server.tool()
    @guarded
    @selected
    def maven_secret_adapter(
        action: Literal['policy', 'draft', 'review', 'approve', 'prepare', 'grant_document', 'execute'],
        parameters: dict[str, Any], cwd: str, use_home: bool = False,
    ) -> dict[str, Any]:
        """Use owner-defined secret consumers; never return raw program output or secret values.

        policy: slug. draft: slug, title, document, owner_source; optional page_id,
        expected_revision, revoke. review: proposal_id. approve: proposal_id,
        challenge_id, candidate_hash, expected_revision, owner_source, approved.
        prepare: adapter, secret_name, purpose, operation, destination, parameters.
        grant_document: invocation, mode (standing|single-use), expires_at.
        execute: invocation, grant, grant_revision. Drafts and grant documents are
        not approvals. Show the exact review and collect actual owner approval.
        Publication failed reports a definitive rejection; outcome_unknown is not a confirmed draft.
        Preserve/reconcile the exact original request in either case; never invent new IDs.
        Execution is fail-closed and never replayed after ambiguous consumption.
        Selected project or explicit Home only; no memory capture starts.
        """
        from ..secret_adapters import SecretAdapters
        from ..memory.adapter_contracts import AdapterError
        from ..memory.environment import configured_client

        approval_client = None
        try:
            catalog = SecretCatalog(app.home, client=app.unbound_client())
            if action in ('review', 'approve'):
                approval_client = configured_client(app.home / '.env', approval=True)
            controller = SecretAdapters(catalog.home_id, client=app.unbound_client(),
                                        approval_client=approval_client,
                                        catalog=catalog, store=catalog.store)
            if action not in ('policy', 'draft', 'review', 'approve', 'prepare', 'grant_document', 'execute'):
                raise BoundaryError('invalid_adapter_action')
            return getattr(controller, action)(**parameters)
        except AdapterError as error:
            actions = {
                'invocation_consumed': 'This invocation has already consumed authorization. Do not replay it. '
                                       'For another attempt, prepare a new invocation and obtain any required fresh owner approval.',
                'adapter_not_authorized': 'Authorization was denied for this exact invocation. Check grant expiry, '
                                          'approved bindings and selected-role permissions. Obtain fresh owner approval if required; do not retry automatically.',
                'unsupported_secret_provider': 'This consumer supports only pinned SSM and encrypted-file references. '
                                               'Explicitly migrate the catalog to a supported store and approve a new grant; existing grants are not reinterpreted.',
                'secret_reference_migration_required': 'Explicitly replace the legacy SSM catalog entry using store_and_catalog '
                                                       'to publish a verified numeric version, then prepare and approve a new grant. Labels/latest are not immutable.',
                'adapter_store_mismatch': 'Select the exact catalog store or explicitly migrate the catalog and obtain a new grant. '
                                          'No authorization was consumed.',
            }
            if error.code in actions:
                return {'status': 'blocked', 'error': error.code, 'next_action': actions[error.code]}
            raise BoundaryError(str(error)) from None
        except StoreError as error:
            raise BoundaryError(str(error)) from None
        except TypeError:
            raise BoundaryError('invalid_adapter_arguments') from None
        finally:
            if approval_client is not None:
                approval_client.close()
