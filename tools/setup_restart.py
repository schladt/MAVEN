"""Explicit local setup restart; archives evidence without undoing cloud effects."""
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

from tools.administration import AdminOperations, _digest, _source
from tools.setup_common import SetupPending, atomic_private, private_directory, read_private, save_json, setup_lock
from tools.uninstall.local import _lock, _quiet


@contextmanager
def _locked(home):
    controller = AdminOperations(home)
    controller._prepare()
    with setup_lock(controller.directory):
        directory = controller.home / 'runtime/setup'
        private_directory(directory)
        with setup_lock(directory), _lock(controller.home / 'runtime/registry.lock'):
            _quiet(controller.home)
            # Local preservation does not authorize using a connection. Read legacy locators
            # for the review without forcing their migration or constructing AWS clients.
            from tools.environment_migrations import inspect
            env = controller.home / '.env'
            values = inspect(env)[0] if env.exists() or env.is_symlink() else {}
            state_path = directory / 'state.json'
            state = read_private(state_path) if state_path.exists() else {'schema': 1}
            if state.get('schema') != 1:
                raise SetupPending('Unsupported setup state; preserve its original evidence.')
            yield controller, SimpleNamespace(home=controller.home, env=env, directory=directory,
                                              values=values, state_path=state_path, state=state)


def _snapshot(controller, wizard, identifier):
    operation = controller._load(identifier)
    if (operation['kind'] != 'setup' or operation['state'] in ('completed', 'cancelled', 'abandoned')
            or wizard.state.get('administration_operation') != identifier):
        raise SetupPending('Only the current unfinished setup can be abandoned. Preserve other operation receipts.')
    return {'operation': operation, 'setup': wizard.state,
            'environment': wizard.env.read_text() if wizard.env.exists() else None}


def _retained_integrations(wizard, operation):
    from tools.secret_setup import integration_settings
    retained = integration_settings(wizard.values)
    if operation.get('secrets_resources'):
        # Submitted resources retain their exact locators even before local binding.
        retained.update(operation['secrets_candidate']['settings'])
    return retained


def _review(controller, wizard, snapshot):
    operation = snapshot['operation']
    return {'home': str(controller.home), 'operation_id': operation['operation_id'],
            'saved_step': operation['step'], 'saved_state': operation['state'],
            'receipt_steps': [receipt['step'] for receipt in operation['receipts']],
            'submitted_requests': snapshot['setup'].get('submitted_requests', []),
            'account': wizard.values.get('MAVEN_ACCOUNT_ID', 'Unverified saved account selectors'),
            'stack': wizard.values.get('MAVEN_STACK_NAME'),
            'instance_id': wizard.values.get('MAVEN_MEMORY_INSTANCE_ID'),
            'snapshot_digest': _digest(snapshot),
            'effect': 'Archive the old operation, setup state and private .env; start fresh setup choices while preserving all existing connection and AWS selections as unverified. No sign-in or cloud effect occurs during this local cutover.',
            'retained_integrations': _retained_integrations(wizard, operation),
            'secrets_resources': operation.get('secrets_resources', {}),
            'retained': 'Installed software, AWS sign-in caches, original request artifacts, approvals, receipts, independently retained secrets resources and all other cloud resources remain.',
            'warning': 'This does not cancel submitted AWS work, roll back resources, refund charges or reconcile unknown outcomes. Existing resources may still incur charges. Retained settings are not live readiness evidence; subsequent cloud use must revalidate the selected identity and authority.'}


def review_restart(home, identifier):
    with _locked(home) as (controller, wizard):
        controller._path(identifier)  # Validate before constructing the archive path.
        archive = controller.directory / ('restart-' + identifier + '.json')
        if archive.exists() or archive.is_symlink():
            return read_private(archive, maximum=64 * 1024 * 1024)['review']
        return _review(controller, wizard, _snapshot(controller, wizard, identifier))


def restart(home, identifier, *, confirm_review, source):
    """Resume an exact approved local cutover; never replay a cloud request."""
    _source(source)
    with _locked(home) as (controller, wizard):
        controller._path(identifier)
        archive = controller.directory / ('restart-' + identifier + '.json')
        if archive.exists() or archive.is_symlink():
            journal = read_private(archive, maximum=64 * 1024 * 1024)
            if journal['review']['home'] != str(controller.home) or journal['review']['operation_id'] != identifier:
                raise SetupPending('Restart archive belongs to another operation; preserve it.')
            if _digest(journal['review']) != confirm_review:
                raise SetupPending('Restart approval does not match the archived review.')
            if journal['completed']:
                return controller.status(journal['replacement']['operation_id'])
        else:
            snapshot = _snapshot(controller, wizard, identifier)
            review = _review(controller, wizard, snapshot)
            if _digest(review) != confirm_review:
                raise SetupPending('Setup or private settings changed since review. Review abandonment again; nothing was reset.')
            # This operation only archives local evidence. Preserve the exact settings bytes;
            # authentication belongs to the subsequent selected cloud operation.
            retained_environment = snapshot['environment'] or ''
            successor = str(uuid4())
            replacement = {'schema': 1, 'operation_id': successor, 'kind': 'setup',
                           'home': str(controller.home), 'revision': 0, 'state': 'pending', 'step': 'start',
                           'parameters': {}, 'answers': {}, 'approvals': [], 'receipts': [],
                           'pending_interaction': None, 'live_state_checked': False,
                           'restarted_from': identifier}
            journal = {'schema': 1, 'review': review, 'source': source, 'original': snapshot,
                       'retained_environment': retained_environment,
                       'replacement': replacement,
                       'setup': {'schema': 1, 'administration_operation': successor,
                                 'administration_artifacts': successor}, 'completed': False}
            # Persist complete recovery inputs before changing any active state.
            save_json(archive, journal)
        abandoned = {**journal['original']['operation'], 'state': 'abandoned', 'pending_interaction': None,
                     'revision': journal['original']['operation']['revision'] + 1,
                     'outcome': {'status': 'abandoned',
                                 'replacement_operation': journal['replacement']['operation_id'],
                                 'archive': str(archive), 'cloud_rollback': False}}
        current = controller._load(identifier)
        environment = wizard.env.read_text() if wizard.env.exists() else None
        if (current not in (journal['original']['operation'], abandoned)
                or wizard.state not in (journal['original']['setup'], journal['setup'])
                or environment not in (journal['original']['environment'], journal.get('retained_environment', ''))):
            raise SetupPending('State changed during restart recovery. Preserve the archive; no newer state was overwritten.')
        successor_path = controller._path(journal['replacement']['operation_id'])
        if successor_path.exists() and controller._load(journal['replacement']['operation_id']) != journal['replacement']:
            raise SetupPending('Replacement operation already advanced; do not overwrite it.')
        save_json(controller._path(identifier), abandoned)
        atomic_private(wizard.env, journal.get('retained_environment', ''))
        save_json(successor_path, journal['replacement'])
        save_json(wizard.state_path, journal['setup'])
        journal['completed'] = True
        save_json(archive, journal)
        return controller.status(journal['replacement']['operation_id'])
