"""Terminal presenter for the same persisted administration checkpoints as MCP."""
from __future__ import annotations

import json
import os
import shlex
import time

from tools.administration import AdminOperations
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
}
_TRANSIENT_REASONS = {'warming', 'DatabaseResumingException', 'temporarily_unavailable'}
_WAKE_INTERVAL, _WAKE_LIMIT, _WAKE_RETRIES = 5, 600, 3


def _text(value):
    return value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)


def _clock(seconds):
    return f'{seconds // 60}:{seconds % 60:02d}'


def _await_database(ui, controller, stage):
    """Poll the auto-paused database with a visible timer; True once it accepts SQL."""
    ui.message(f'{stage}: the Aurora database is resuming from auto-pause. Checking every '
               f'{_WAKE_INTERVAL}s for up to {_WAKE_LIMIT // 60} minutes; Ctrl+Q pauses safely.')
    start, checks, status = time.monotonic(), 0, 'warming'
    while True:
        elapsed = int(time.monotonic() - start)
        if elapsed >= checks * _WAKE_INTERVAL:
            checks += 1
            status = controller.database_status()
            elapsed = int(time.monotonic() - start)
            if status == 'awake':
                ui.message(f'Database is awake after {_clock(elapsed)} ({checks} checks). Retrying {stage}.')
                return True
            if status not in ('warming', 'temporarily_unavailable'):
                ui.message(f'Database check stopped after {_clock(elapsed)}: {status}.')
                return False
        if elapsed >= _WAKE_LIMIT:
            ui.message(f'Database is still not awake after {_clock(elapsed)}.')
            return False
        ui.progress(f'Waiting for the database to wake — {_clock(elapsed)} elapsed, '
                    f'check {checks}: {status}. Ctrl+Q pauses safely.')
        ui.checkpoint()
        time.sleep(1)


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


def _instructions(result, home, identifier):
    stage = _stage_name(result.get('step', ''))
    handoff = result.get('handoff')
    error = result.get('error') or ('AWS sign-in required.' if handoff else 'External prerequisite required.')
    instructions = [f'Stopped at: {stage} (step "{result.get("step", "unknown")}")', f'Error: {error}']
    if error in _CODE_HELP:
        instructions.append(_CODE_HELP[error])
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
        instructions.extend(['', handoff['reason'], '', 'External command:', shlex.join(handoff['command'])])
        if handoff.get('environment'):
            instructions.extend(['', 'Environment for that external command:', _text(handoff['environment'])])
    for key in ('notice', 'recovery'):
        if result.get(key):
            instructions.extend(['', result[key]])
    if not handoff:
        instructions.extend(['', f'Resume operation retries "{stage}" for operation {identifier}.'])
    instructions.extend([
        '', 'Ctrl+Q pauses safely; the original operation and receipts are retained.',
        '', 'Resume later: ' + shlex.join(['maven-setup', '--home', str(home), '--operation', identifier])])
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


def present(ui, home, *, operation_id=None, mode=None):
    controller = AdminOperations(home)
    parameters = {}
    if mode is not None:
        parameters['mode'] = mode
    result = controller.status(operation_id) if operation_id else controller.begin('setup', parameters)
    identifier = result['operation_id']
    ui.message('Resumable administration operation: ' + identifier)
    ui.message('Pause safely with Ctrl+Q. Resume the same operation: ' +
               shlex.join(['maven-setup', '--home', str(home), '--operation', identifier]))
    wake_retries = 0
    while True:
        ui.checkpoint()
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
            result = controller.respond(identifier, question['interaction_id'], question['revision'], response)
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
            if result.get('error') == 'warming' and wake_retries < _WAKE_RETRIES:
                # A paused database is expected after idle time: wait visibly, then retry the same step.
                wake_retries += 1
                if _await_database(ui, controller, stage):
                    result = controller.advance(identifier)
                    continue
            if not ui.review('Setup stopped — ' + stage, _instructions(result, home, identifier),
                             identity=identifier, accept='Resume operation'):
                raise OperationPaused('Administration paused; evidence retained for ' + identifier)
            ui.checkpoint()
            result = controller.advance(identifier)
        else:
            result = controller.advance(identifier)
