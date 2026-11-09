"""Terminal presenter for the same persisted administration checkpoints as MCP."""
from __future__ import annotations

import json
import shlex

from tools.administration import AdminOperations
from tools.interaction import Field, OperationPaused


def present(ui, home, *, operation_id=None, project_root=None):
    controller = AdminOperations(home)
    result = (controller.status(operation_id) if operation_id else
              controller.begin('setup', {'project_root': str(project_root)} if project_root else {}))
    identifier = result['operation_id']
    ui.message('Resumable administration operation: ' + identifier)
    while True:
        ui.checkpoint()
        ui.stage(result['step'])
        question = result['pending_interaction']
        if question:
            if question['kind'] == 'form':
                fields = [Field(**{**field, 'choices': tuple(tuple(choice) for choice in field['choices'])})
                          for field in question['fields']]
                response = ui.form(question['title'], fields,
                                   description='Choices are saved privately. Never enter credentials.')
                if response is None:
                    raise OperationPaused('Administration paused; resume operation ' + identifier)
            else:
                approved = ui.review(question['title'], json.dumps(question['review'], indent=2, ensure_ascii=False),
                                     identity=question['review_digest'])
                response = {'approved': approved, 'review_digest': question['review_digest']}
                if approved:
                    evidence = ui.form('Record actual owner approval', [
                        Field('excerpt', 'Your approval statement for this exact review', kind='multiline')],
                        description='This is a terminal response, not a harness/chat approval.')
                    if evidence is None:
                        raise OperationPaused('Approval not recorded; resume operation ' + identifier)
                    response['source'] = {'origin': 'owner MAVEN administration TUI', 'excerpt': evidence['excerpt']}
                    confirmations = question['confirmations']
                    if confirmations:
                        values = ui.form('Separate destructive confirmations', [
                            Field(key, 'Enter exact instance for ' + key) for key in confirmations])
                        if values is None:
                            raise OperationPaused('Destructive confirmations not recorded.')
                        response['confirmations'] = values
                result = controller.respond(identifier, question['interaction_id'], question['revision'], response)
                if not approved:
                    raise OperationPaused('Approval declined; original review retained for ' + identifier)
                continue
            result = controller.respond(identifier, question['interaction_id'], question['revision'], response)
        elif result['state'] in ('completed', 'cancelled'):
            ui.message(json.dumps(result.get('outcome', result['state']), ensure_ascii=False, indent=2))
            return 0 if result['state'] == 'completed' else 1
        elif result['state'] in ('blocked', 'uncertain'):
            instructions = [result.get('error', 'External prerequisite required.')]
            if result.get('handoff'):
                handoff = result['handoff']
                instructions.extend([handoff['reason'], '', 'External command:',
                                     shlex.join(handoff['command'])])
                if handoff.get('environment'):
                    instructions.extend(['', 'Environment for that external command:',
                                         json.dumps(handoff['environment'], indent=2, ensure_ascii=False)])
            for key in ('notice', 'recovery'):
                if result.get(key):
                    instructions.extend(['', result[key]])
            instructions.extend([
                '', 'Decline or Ctrl+Q to pause safely; the original operation and receipts are retained.',
                'Complete the prerequisite externally. Resume now only after it is corrected.',
                '', 'Resume later: ' + shlex.join(['maven-setup', '--home', str(home), '--operation', identifier])])
            if not ui.review('External prerequisite — pause or resume', '\n'.join(instructions),
                             identity=identifier, accept='Resume operation'):
                raise OperationPaused('Administration paused; evidence retained for ' + identifier)
            ui.checkpoint()
            result = controller.advance(identifier)
        else:
            result = controller.advance(identifier)
