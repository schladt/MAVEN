"""Terminal presenter for the same persisted administration checkpoints as MCP."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sys
import time

from tools.administration import AdminInputError, AdminOperations
from tools.interaction import Field, OperationPaused


_STAGE_NAMES = {
    'start': 'Choose setup', 'selection': 'Choose setup',
    'identity': 'Choose everyday AWS access', 'identity_inspect': 'Check everyday AWS identity',
    'identity_accept': 'Confirm everyday AWS identity',
    'deploy_identity': 'Choose maintenance access', 'deploy_identity_inspect': 'Check maintenance identity',
    'deploy_identity_accept': 'Confirm maintenance identity',
    'discover': 'Find MAVEN stacks in AWS', 'discovery_select': 'Select discovered instance',
    'connect_review': 'Review existing instance', 'connect': 'Connect existing instance',
    'provision': 'Choose new instance settings', 'prepare': 'Prepare deployment review',
    'plan': 'Prepare change set', 'apply_review': 'Review billable deployment', 'apply': 'Deploy approved changes',
    'initialize_review': 'Review database initialization', 'initialize': 'Initialize approved database',
    'migration_review': 'Review database upgrade', 'migrate': 'Apply approved database upgrade',
    'migration_reconnect': 'Reconnect upgraded instance', 'migration_stack': 'Find upgraded instance',
    'upgrade_choice': 'Choose database upgrade', 'verify': 'Verify instance readiness',
    'starter_prepare_review': 'Prepare starter instructions', 'starter_prepare': 'Prepare starter instructions',
    'starter_review': 'Install starter instructions', 'starter_submit': 'Upload approved starter instructions',
    'starter_challenge': 'Prepare approved activation',
    'update_local': 'Check local software', 'update_local_choice': 'Update local software',
    'update_local_wait': 'Update local software', 'update_schema': 'Check database schema',
    'update_service': 'Check service stack', 'update_service_apply': 'Update service stack',
    'update_authority': 'Check instructions', 'update_authority_choice': 'Update instructions',
    'starter_activate': 'Activate approved starter instructions', 'starter_renew': 'Renew activation challenge',
    'done': 'Setup complete',
}


# Plain-language meaning of the bounded error and check-reason codes setup records.
_CODE_HELP = {
    'warming': 'The Aurora database is resuming from auto-pause (scaled to zero) and is not accepting '
               'SQL yet. Setup waits up to 10 minutes for it automatically; this screen means that wait '
               'ended first (see the event log). Resume to check again.',
    'DatabaseResumingException': 'AWS reported the Aurora database is still resuming from auto-pause.',
    'temporarily_unavailable': 'The database or MAVEN service did not answer in time. Resume re-checks '
                               'this same operation before repeating anything.',
    'migration_rejected': 'The database rejected the migration SQL or its permissions. The transaction '
                          'was rolled back. Correct the reported cause before resuming; this is not a service outage.',
    'migration_failed': 'The migration transaction was rolled back. Inspect the diagnostic below and '
                        'correct the cause before retrying this same operation.',
    'migration_outcome_unknown': 'A migration statement failed, but rollback could not be confirmed. '
                                 'Inspect the database state before resuming this same operation.',
    'selected_login_or_role_unavailable': 'The selected AWS login could not assume the MAVEN role this step '
                                          'needs. The sign-in may have expired, or the role in the saved '
                                          'settings may not exist or may not trust this user.',
    'selected_login_unavailable_or_expired': 'The AWS sign-in for the selected profile is missing or expired. '
                                             'Sign in again with that profile.',
    'unexpected_login_identity': 'The AWS sign-in belongs to a different principal than the one confirmed '
                                 'for this setup. Sign in as the confirmed principal or change the selection.',
    'authentication_required': 'The MAVEN service did not accept the request credentials. Sign in again.',
    'not_authorized': 'AWS or the MAVEN service denied this request for the selected identity or role.',
    'context_stale': 'The instance changed while this step ran (for example maintenance or a new '
                     'authority version). Resume re-reads the current state.',
    'client_incompatible': 'This MAVEN software version does not match the deployed service version.',
    'invalid_configuration': 'The saved connection settings are incomplete or do not belong together. '
                             'Check the instance, roles and endpoint in the saved settings.',
    'instance_confirmation_mismatch': 'The confirmed instance does not match the saved connection settings.',
    'transport_unavailable': 'The selected service could not be reached. Check connectivity and the trusted CA '
                             'configuration; this is not evidence that AWS sign-in expired.',
    'selected_login_configuration_unavailable': 'The selected named AWS profile, region or cache configuration '
                                                'is unavailable. Correct that selection; do not switch identities implicitly.',
    'selected_profile_requires_login': 'The selected profile uses an unsupported credential method. Select a '
                                      'named AWS login profile; static credentials are not accepted.',
    'login_cache_must_be_owner_private': 'The selected login cache must be an owner-private directory. Correct '
                                        'its permissions without changing the selected profile or identity.',
    'login_cache_unavailable': 'The selected login cache cannot be read. Restore access to that exact cache.',
    'invalid_login_cache': 'The selected login cache path is invalid. Choose an absolute private cache directory.',
    'invalid_role_credentials': 'AWS returned invalid role credentials. No identity or readiness was verified; '
                                'inspect the selected role configuration before resuming the same operation.',
}
_TRANSIENT_REASONS = {'warming', 'DatabaseResumingException', 'temporarily_unavailable'}
_WAKE_INTERVAL, _WAKE_LIMIT = 5, 600


def _text(value):
    return value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)


def _clock(seconds):
    return f'{seconds // 60}:{seconds % 60:02d}'


def _await_database(ui, controller, stage):
    """Share bounded read-only warming progression; never retry an administration effect."""
    from tools.memory.client import MemoryClientError, wait_for_read_only
    ui.message(f'{stage}: the Aurora database is resuming from auto-pause. Checking every '
               f'{_WAKE_INTERVAL}s for up to {_WAKE_LIMIT // 60} minutes; Ctrl+Q pauses safely.')
    start = time.monotonic()

    def probe():
        status = controller.database_status()
        if status == 'warming':
            raise MemoryClientError('warming')
        return status

    def warming():
        ui.progress(f'Waiting for the database to wake — {_clock(int(time.monotonic() - start))} '
                    'elapsed. Ctrl+Q pauses safely.')

    try:
        status = wait_for_read_only(probe, budget=_WAKE_LIMIT, interval=_WAKE_INTERVAL,
                                   checkpoint=ui.checkpoint, on_warming=warming)
    except MemoryClientError as error:
        status = error.code
    ui.message('Database probe: ' + status + '. The retained operation has not been replayed.')
    return status


def _stage_name(step):
    return _STAGE_NAMES.get(step, step.replace('_', ' ').capitalize())


def _context(result, stage):
    presentation = result.get('presentation') or {}
    lines = ['Task: ' + presentation.get('title', stage)]
    if presentation.get('purpose'):
        lines.append('Access: ' + presentation['purpose'])
    account, profile = presentation.get('account'), presentation.get('profile')
    if account or profile:
        lines.append('AWS: ' + str(account or 'not yet inspected') +
                     ' | Profile: ' + str(profile or 'not selected') +
                     (' | inspected' if presentation.get('verified') else ' | not verified'))
    if presentation.get('principal'):
        lines.append(('Principal: ' if presentation.get('verified') else 'Expected principal: ') +
                     str(presentation['principal']))
    if presentation.get('target'):
        lines.append('Exact target: ' + str(presentation['target']))
    progress = result.get('starter_progress')
    if result.get('step', '').startswith('starter') and progress:
        lines.append(f'Starter instructions: {progress["completed"]}/{progress["total"]} completed | '
                     f'Page {progress["position"]}: {progress["title"]}')
    lines.append('Saved: ' + stage + ' | Receipts: ' + str(len(result.get('receipts', []))) +
                 ' | Ctrl+Q pauses')
    return '\n'.join(lines)


def _resume_command(result, home, identifier):
    program = (['maven-setup'] if result.get('kind', 'setup') == 'setup'
               else [sys.executable, '-m', 'tools.tui.administration'])
    return shlex.join([*program, '--home', str(home), '--operation', identifier])


def _instructions(result, home, identifier):
    stage = _stage_name(result.get('step', ''))
    handoff = result.get('handoff')
    prerequisite = ('AWS sign-in required.' if handoff and handoff.get('type') == 'aws_login'
                    else 'Local software update required.' if handoff and handoff.get('type') == 'local_update'
                    else 'External prerequisite required.')
    error = result.get('error') or prerequisite
    instructions = [f'Stopped at: {stage} (step "{result.get("step", "unknown")}")', f'Error: {error}']
    if error in _CODE_HELP:
        instructions.append(_CODE_HELP[error])
    if result.get('error_detail'):
        instructions.append(result['error_detail'])
    # The sign-in screen offers its own choices; Resume applies only to the stopped-step screen.
    if not handoff and result.get('state') == 'uncertain':
        instructions.append('State: uncertain. The request may have reached AWS, so its outcome is not known. '
                            'Resume reconciles this same operation; do not start a new one.')
    elif not handoff and result.get('state') == 'blocked':
        instructions.append('State: blocked. The step did not complete. Resume retries it for this same '
                            'operation with the approvals already given.')
    checked = next((receipt for receipt in reversed(result.get('receipts', []))
                    if isinstance(receipt.get('result'), dict) and receipt['result'].get('checks')), None)
    if checked:
        instructions.extend(['', f'Latest checks (recorded at step "{checked["step"]}"):'])
        for check in checked['result']['checks']:
            line = f'  - {check.get("check")}: {check.get("status")}'
            if check.get('reason'):
                line += f' ({check["reason"]})'
                if check['reason'] in _CODE_HELP and check['reason'] != error:
                    line += ' — ' + _CODE_HELP[check['reason']]
            instructions.append(line)
        # The recorded remediation is IAM advice; it does not apply to a database that is only waking up.
        failures = [check for check in checked['result']['checks'] if check.get('status') != 'Passed']
        if (checked['result'].get('remediation')
                and any(check.get('reason') not in _TRANSIENT_REASONS for check in failures)):
            instructions.append('Suggested fix: ' + checked['result']['remediation'])
    if handoff:
        command = ['env', *(key + '=' + value for key, value in handoff.get('environment', {}).items()),
                   *handoff['command']]
        instructions.extend(['', handoff['reason'], '', 'External command:', shlex.join(command)])
    if result.get('probe_status') and result['probe_status'] != 'awake':
        status = result['probe_status']
        instructions.extend(['', 'Latest read-only database probe: ' + status,
                             _CODE_HELP.get(status, 'The optional SQL probe is unavailable; the original operation is retained.')])
        probe_handoff = result.get('probe_handoff')
        if probe_handoff:
            command = ['env', *(key + '=' + value for key, value in probe_handoff.get('environment', {}).items()),
                       *probe_handoff['command']]
            instructions.extend(['Probe authentication (selected everyday access):', shlex.join(command)])
    for key in ('notice', 'recovery'):
        if result.get(key):
            instructions.extend(['', result[key]])
    if not handoff:
        instructions.extend(['', f'Resume operation retries "{stage}" for operation {identifier}.'])
    instructions.extend([
        '', 'Ctrl+Q pauses safely; the original operation and receipts are retained.',
        '', 'Resume later: ' + _resume_command(result, home, identifier)])
    return '\n'.join(instructions)


def _aws_login(ui, controller, result, home, identifier):
    """Only this typed handoff may launch authentication, after an explicit choice."""
    handoff = result['handoff']
    instructions = _instructions(result, home, identifier)
    status = 'Signing in does not approve deployment or verify the AWS identity. The next check inspects the actual account and principal.'
    while True:
        choices = [('Sign in with the selected AWS profile', 'signin'),
                   ('Show manual sign-in instructions', 'manual')]
        if 'back' in result.get('next_actions', []):
            choices.append(('Back to change the AWS selection', 'back'))
        choices.append(('Pause and keep this operation', 'pause'))
        choice = ui.choose('Sign in to AWS', choices, description=status + '\n\n' + instructions)
        if choice == 'pause':
            raise OperationPaused('AWS sign-in paused; resume operation ' + identifier)
        if choice == 'back':
            return controller.back(identifier)
        if choice == 'manual':
            if ui.review('Sign in outside MAVEN, then check identity', instructions,
                         identity=identifier, accept='Check identity',
                         description='Use the exact selected profile and environment below. No AWS identity has been verified by this screen.'):
                return controller.advance(identifier)
            continue
        try:
            login = ui.external(handoff['command'], env={**os.environ, **handoff.get('environment', {})}, check=False)
        except OSError as error:
            status = ('Sign-in could not start (' + type(error).__name__ + '). Use the manual instructions or change the selection. AWS identity is not verified.')
            ui.message(status)
            continue
        if login.returncode == 0:
            # A clean sign-in exit goes straight to the actual account/principal inspection.
            ui.message('Sign-in command exited with status 0; checking the actual AWS identity.')
            return controller.advance(identifier)
        status = ('Sign-in command exited with status ' + str(login.returncode) +
                  '. AWS identity is not verified. Sign in again, use the manual instructions, '
                  'or change the selection.')
        ui.message(status)


def present(ui, home, *, operation_id=None, mode=None, kind='setup', parameters=None):
    controller = AdminOperations(home)
    parameters = dict(parameters or {})
    if mode is not None:
        parameters['mode'] = mode
    result = controller.status(operation_id) if operation_id else controller.begin(kind, parameters)
    identifier = result['operation_id']
    ui.message('Resumable administration operation: ' + identifier)
    ui.message('Pause safely with Ctrl+Q. Resume the same operation: ' +
               _resume_command({**result, 'kind': result.get('kind', kind)}, home, identifier))
    warmed = set()
    input_error = None
    while True:
        ui.checkpoint()
        from tools.environment_migrations import prerequisite
        pending = prerequisite(controller, result)
        if pending:
            result = {**result, 'prerequisite_operation': pending['operation_id']}
        if result.get('prerequisite_operation'):
            outcome = present(ui, home, operation_id=result['prerequisite_operation'])
            if outcome:
                return outcome
            result = controller.status(identifier)
            continue
        presentation = result.get('presentation') or {}
        stage = _stage_name(result['step'])
        ui.stage(stage)
        ui.set_context(_context(result, stage))
        question = result['pending_interaction']
        can_back = 'back' in result.get('next_actions', [])
        if (question and result.get('kind') == 'setup' and
                'respond' not in result.get('next_actions', ['respond']) and
                'advance' in result.get('next_actions', [])):
            result = controller.advance(identifier)
            continue
        if question:
            description = question.get('description') or presentation.get('description') or 'Review this saved checkpoint before continuing. Choices are saved privately; never enter credentials.'
            if input_error:
                description = 'Not accepted: ' + input_error + '\n\n' + description
                input_error = None
            details = _text(question.get('details') or presentation.get('details') or '')
            if question['kind'] == 'form':
                fields = [Field(**{**field, 'choices': tuple(tuple(choice) for choice in field.get('choices', ()))})
                          for field in question['fields']]
                response = ui.form(question['title'], fields, description=description, details=details,
                                   back=can_back, submit=question.get('submit', 'Continue'))
                if response is None:
                    if can_back:
                        result = controller.back(identifier)
                        continue
                    raise OperationPaused('Administration paused; resume operation ' + identifier)
            else:
                artifact = _text(question['review'])
                # Old checkpoints without a target-aware summary retain the full
                # artifact in the main view. Metadata never replaces the bound review.
                summary = question.get('description') or artifact
                technical = (details + '\n\n' if details else '') + 'Complete bound review:\n' + artifact
                approved = ui.review(question['title'], summary, details=technical,
                                     identity=question['review_digest'], identity_label='Review digest', back=can_back)
                if approved is None and can_back:
                    result = controller.back(identifier)
                    continue
                if not approved:
                    # Declining is not an answer or a mutation of the saved review.
                    raise OperationPaused('Approval declined; original review retained for ' + identifier)
                response = {'approved': True, 'review_digest': question['review_digest']}
                # Record the observed terminal action, not an invented owner quote
                # or a claim that a harness/chat supplied the approval.
                response['source'] = {
                    'origin': 'owner MAVEN administration TUI',
                    'excerpt': 'Owner selected Approve for ' + question['title'] +
                               ' / review digest ' + question['review_digest'],
                }
                confirmations = question.get('confirmations', {})
                if confirmations:
                    values = ui.form('Separate destructive confirmations', [
                        Field(key, 'Enter exact instance for ' + key,
                              help='Exact destructive target: ' + str(target)) for key, target in confirmations.items()],
                        description='These separate confirmations authorize only the exact destructive targets listed. No action has been submitted.',
                        details=_text(question['review']), back=True)
                    if values is None:
                        continue
                    response['confirmations'] = values
            try:
                result = controller.respond(identifier, question['interaction_id'], question['revision'], response)
            except AdminInputError as error:
                # The saved question is unchanged; show why and ask it again.
                ui.message('Not accepted: ' + str(error))
                input_error = str(error)
                result = controller.status(identifier)
        elif result['state'] in ('completed', 'cancelled', 'abandoned'):
            outcome = result.get('outcome', result['state'])
            ui.message(_text(outcome))
            if result['state'] == 'completed' and result.get('kind') == 'setup':
                from tools.tui.harness import finish_setup
                return finish_setup(ui, home, outcome)
            return 0 if result['state'] == 'completed' else 1
        elif result['state'] in ('blocked', 'uncertain'):
            if result.get('handoff', {}).get('type') == 'aws_login':
                result = _aws_login(ui, controller, result, home, identifier)
                continue
            if result.get('error') == 'warming' and result['step'] not in warmed:
                warmed.add(result['step'])
                status = _await_database(ui, controller, stage)
                result = {**result, 'probe_status': status}
                if status == 'awake' and result.get('read_only_retry') is True:
                    result = controller.advance(identifier)
                    continue
                from tools.memory.client import AUTHENTICATION_CODES, MemoryClientError
                if status in AUTHENTICATION_CODES:
                    from tools.memory.environment import authentication_handoff, load_environment
                    result['probe_handoff'] = authentication_handoff(
                        MemoryClientError(status), load_environment(Path(home) / '.env'), deploy=False)
            if not ui.review('Administration stopped — ' + stage, _instructions(result, home, identifier),
                             identity=identifier, accept='Resume operation'):
                raise OperationPaused('Administration paused; evidence retained for ' + identifier)
            ui.checkpoint()
            result = controller.advance(identifier)
        else:
            result = controller.advance(identifier)


def main(argv=None):
    """Terminal entry for MAVEN Home: enable, change or turn off, or resume one exact operation."""
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('MAVEN_HOME', str(Path.home() / '.maven'))))
    parser.add_argument('--action', choices=('enable', 'disable'), default='enable')
    parser.add_argument('--operation', help='Resume the exact persisted administration operation UUID')
    args = parser.parse_args(argv)
    home = args.home.expanduser().absolute()
    os.environ['MAVEN_HOME'] = str(home)
    from tools.tui import run_workflow
    return run_workflow('MAVEN Home', ['Choose the Home folder', 'Review', 'Apply'],
                        lambda ui: present(ui, home, operation_id=args.operation, kind='home',
                                           parameters={'action': args.action}))


if __name__ == '__main__':
    sys.exit(main())
