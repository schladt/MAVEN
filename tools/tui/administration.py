"""Terminal presenter for the same persisted administration checkpoints as MCP."""
from __future__ import annotations

import json
import os
import shlex

from tools.administration import AdminOperations
from tools.interaction import Field, OperationPaused


_STAGE_NAMES = {
    'start': 'Choose setup', 'selection': 'Choose setup',
    'identity': 'Choose everyday AWS access', 'identity_inspect': 'Check everyday AWS identity',
    'identity_accept': 'Confirm everyday AWS identity',
    'deploy_identity': 'Choose deployment access', 'deploy_identity_inspect': 'Check deployment identity',
    'deploy_identity_accept': 'Confirm deployment identity',
    'connect_review': 'Review existing instance', 'connect': 'Connect existing instance',
    'provision': 'Choose new instance settings', 'prepare': 'Prepare deployment review',
    'plan': 'Prepare change set', 'apply_review': 'Review billable deployment', 'apply': 'Deploy approved changes',
    'initialize_review': 'Review database initialization', 'initialize': 'Initialize approved database',
    'migration_review': 'Review database upgrade', 'migrate': 'Apply approved database upgrade',
    'migration_reconnect': 'Reconnect upgraded instance', 'migration_stack': 'Find upgraded instance',
    'upgrade_choice': 'Choose database upgrade', 'verify': 'Verify instance readiness',
    'starter_prepare_review': 'Review starter content preparation', 'starter_prepare': 'Prepare starter content',
    'starter_review': 'Review starter proposal', 'starter_submit': 'Submit approved starter proposal',
    'starter_challenge': 'Request starter approval challenge', 'starter_approve': 'Record starter approval',
    'starter_activate': 'Activate approved starter content', 'starter_renew': 'Renew approval challenge',
    'done': 'Setup complete',
}


def _text(value):
    return value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)


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
    lines.append('Saved: ' + stage + ' | Receipts: ' + str(len(result.get('receipts', []))) +
                 ' | Ctrl+Q pauses')
    return '\n'.join(lines)


def _instructions(result, home, identifier):
    instructions = [result.get('error', 'External prerequisite required.')]
    handoff = result.get('handoff')
    if handoff:
        instructions.extend([handoff['reason'], '', 'External command:', shlex.join(handoff['command'])])
        if handoff.get('environment'):
            instructions.extend(['', 'Environment for that external command:', _text(handoff['environment'])])
    for key in ('notice', 'recovery'):
        if result.get(key):
            instructions.extend(['', result[key]])
    instructions.extend([
        '', 'Ctrl+Q pauses safely; the original operation and receipts are retained.',
        'Complete the prerequisite externally. Resume only after it is corrected.',
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
        status = ('Sign-in command exited with status ' + str(login.returncode) +
                  '. AWS identity is not verified until the next account/principal inspection.')
        ui.message(status)
        if login.returncode == 0 and ui.review('Sign-in returned — check the actual AWS identity', status,
                                             identity=identifier, accept='Check identity', details=instructions):
            return controller.advance(identifier)


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
    while True:
        ui.checkpoint()
        presentation = result.get('presentation') or {}
        stage = _STAGE_NAMES.get(result['step'], result['step'].replace('_', ' ').capitalize())
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
                response = ui.form(question['title'], fields, description=description, details=details, back=can_back)
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
        elif result['state'] in ('completed', 'cancelled'):
            outcome = result.get('outcome', result['state'])
            ui.message(_text(outcome))
            if result['state'] == 'completed' and result.get('kind') == 'setup':
                from tools.tui.harness import finish_setup
                finish_setup(ui, home, outcome)
            return 0 if result['state'] == 'completed' else 1
        elif result['state'] in ('blocked', 'uncertain'):
            if result.get('handoff', {}).get('type') == 'aws_login':
                result = _aws_login(ui, controller, result, home, identifier)
                continue
            if not ui.review('External prerequisite — pause or resume', _instructions(result, home, identifier),
                             identity=identifier, accept='Resume operation'):
                raise OperationPaused('Administration paused; evidence retained for ' + identifier)
            ui.checkpoint()
            result = controller.advance(identifier)
        else:
            result = controller.advance(identifier)
