"""Optional local harness integration after setup; never binds a project."""
from __future__ import annotations

import json
import os
from pathlib import Path

from maven_resources import resource_path
from tools.harness import (
    HARNESS_PATHS, SKILLS, _read, apply_integration, integration_plan,
    retired_skill_plan, retire_skills, skill_plan, validate_default_profiles,
    launcher_permission_plan, repair_launcher_permissions,
)
from tools.interaction import Field, OperationPaused
from tools.uninstall.common import UninstallError, read_private, save_private
from tools.uninstall.local import (
    MANIFEST, _lock, record_installation, reinstall_marker,
)


NAMES = {'omp': 'OMP', 'claude': 'Claude Code', 'codex': 'Codex',
         'copilot': 'Copilot', 'gemini': 'Gemini'}


def _ownership(home):
    path = home / MANIFEST
    prior = read_private(path) if path.exists() else {}
    if prior and (prior.get('schema') != 1 or prior.get('home') != str(home)
                  or not prior.get('active', True)):
        raise ValueError('Installer ownership receipt is invalid or inactive; preserved for review.')
    return prior


def _cloud_context(home, outcome):
    return (f'MAVEN home: {home}\nInstance: {outcome["instance_id"]} | '
            f'AWS account: {outcome["account"]} | Region: {outcome["region"]}\n'
            f'Access: routine profile {outcome["profile"]}; prior cloud verification retained. '
            'Harness configuration is local only; no deployment or project binding.')


def _verified(home, outcome):
    from tools.memory.environment import load_environment
    from tools.memory.client import MemoryClientError
    from tools.setup_common import expected_account, SetupPending
    if not (isinstance(outcome, dict) and outcome.get('instance_ready') is True
            and outcome.get('home') == str(home) and isinstance(outcome.get('binding'), dict)
            and all(outcome.get(key) for key in ('instance_id', 'account', 'region', 'profile'))):
        return False
    try:
        values = load_environment(home / '.env')
        return (values == outcome['binding']
                and outcome['account'] == expected_account(values)
                and outcome['instance_id'] == values.get('MAVEN_MEMORY_INSTANCE_ID')
                and outcome['region'] == values.get('MAVEN_AWS_REGION', 'us-east-1')
                and outcome['profile'] == values.get('MAVEN_AWS_PROFILE'))
    except (OSError, ValueError, MemoryClientError, SetupPending):
        return False


def completed_setup_outcome(home, previous):
    """Reuse exact locally pinned readiness; never contact AWS or create a record."""
    outcome = dict(previous.get('outcome') or {})
    if 'binding' not in outcome:
        # Older new-instance setup pinned this exact environment before starter
        # activation. Do not substitute today's settings for missing evidence.
        binding = previous.get('starter_binding')
        if binding is not None:
            outcome['binding'] = binding
    if not _verified(home, outcome):
        raise ValueError('Saved readiness does not match this installation’s current settings, or lacks an exact binding. '
                         'No cloud action was attempted. Restore the original settings or explicitly run '
                         'maven-setup --home ' + str(home) + ' --fresh-operation for a new check/repair.')
    return outcome


def _launcher_owned(home, prior, repair):
    expected = prior.get('owned', {}).get(str(home / 'venv'), {}).get('bin/maven-mcp')
    if expected not in (repair, {**repair, 'mode': 0o755}, {**repair, 'mode': 0o775}):
        raise ValueError('The 0775 launcher does not match its exact installer ownership receipt; preserved. '
                         'Use the reviewed local software update instead of changing an unowned or modified launcher.')


def configure_harnesses(ui, home, *, ca_bundle=None, context=''):
    """Review and apply optional default-profile files without reinstalling MAVEN.

    Returns status, selected, written and reason. A conflict after mutation retains
    the list of completed writes; existing ownership receipts remain authoritative.
    """
    home = Path(home).expanduser().absolute()
    result = {'status': 'skipped', 'selected': [], 'written': [], 'reason': 'Skipped by choice.'}
    ui.set_context(context or (f'MAVEN home: {home}\nAccess: local harness files only. '
                              'Cloud access is not verified by this step.'))
    ui.stage('Connect your coding harness')
    user_home = Path.home()
    # The selection form already supports skip; avoid a redundant configure gate.
    while True:
        fields = [Field(name, label, name in result['selected'], kind='bool', help=(
            f'MCP: {user_home / HARNESS_PATHS[name][0]}\n'
            f'Startup guidance: {user_home / HARNESS_PATHS[name][1]}\n'
            f'Skills: {user_home / HARNESS_PATHS[name][2]}')) for name, label in NAMES.items()]
        values = ui.form('Choose the harnesses you use', fields,
                         description=('Select any installed harnesses, or leave all unchecked to skip. '
                                      'These are your default user-profile locations. Differing MAVEN '
                                      'entries and unrelated files will be preserved. No credentials are copied.'),
                         submit='Review selected / Skip', back=True)
        if values is None:
            return result
        selected = [name for name in NAMES if values[name]]
        result['selected'] = selected
        if not selected:
            return result
        roots = [user_home / HARNESS_PATHS[name][2] for name in selected]
        try:
            validate_default_profiles(selected)
            prior = _ownership(home)
            recorded_launcher = prior.get('owned', {}).get(str(home / 'venv'), {}).get('bin/maven-mcp')
            launcher_repair = launcher_permission_plan(home, recorded=recorded_launcher)
            if launcher_repair is not None:
                _launcher_owned(home, prior, launcher_repair)
            if ca_bundle is None and os.environ.get('AWS_CA_BUNDLE'):
                ca_bundle = Path(os.environ['AWS_CA_BUNDLE']).expanduser().absolute()
            changes = integration_plan(home, selected, ca_bundle=ca_bundle)
            skills = skill_plan(roots)
            retired = retired_skill_plan(home, roots)
        except (OSError, ValueError, UninstallError) as exc:
            result.update(status='conflict', reason=str(exc))
            action = ui.choose('Harness files preserved', [
                ('Retry local harness configuration — no cloud check or new operation', 'retry'),
                ('Continue without harness configuration', 'skip'),
            ], description=f'{exc}\nNo harness files were changed. Cloud setup and approvals are retained.')
            if action == 'retry':
                continue
            return result

        visible = [f'MAVEN home: {home}', 'Harnesses: ' + ', '.join(NAMES[name] for name in selected),
                   'Only these local changes are proposed:']
        if launcher_repair is None and not skills and not retired and all(
                change.before == change.after for change in changes):
            result.update(status='unchanged', reason='Identical local harness files preserved; no writes were needed.')
            return result
        details = []
        if launcher_repair is not None:
            if launcher_repair['mode'] == 0o775:
                visible.append(f'Repair only {home / "venv/bin/maven-mcp"}: mode 0775 → 0755; bytes unchanged.')
            else:
                visible.append(f'Launcher already 0755; correct only its stale 0775 ownership entry: {home / MANIFEST}.')
            details.append('pip inherited a group-writable umask, creating mode 0775. The harness safety '
                           'guard correctly rejects group-writable executables. Remove only that '
                           'launcher’s group-write bit if still present; record its exact 0755 ownership '
                           'without adopting any other runtime changes.\n' + json.dumps(launcher_repair, indent=2))
        for change in changes:
            action = 'Preserve identical content' if change.before == change.after else 'Add MAVEN content'
            visible.append(f'{action}: {change.path}')
            details.append(f'{change.path}\nExact MAVEN entry/block:\n' +
                           json.dumps(change.receipt['value'], indent=2, ensure_ascii=False))
        for target, body in skills:
            visible.append(f'Create shipped skill: {target}')
            details.append(f'{target}\n{body.decode()}')
        for root in roots:
            for name in SKILLS:
                target = root / name / 'SKILL.md'
                if not any(path == target for path, _ in skills):
                    visible.append(f'Preserve identical skill: {target}')
        for target, expected in retired:
            visible.append(f'Remove exact installer-owned legacy skill: {target}')
            details.append(f'Retirement inventory for {target}:\n' + json.dumps(expected, indent=2))
        visible.extend([f'Update selective uninstall ownership: {home / MANIFEST}',
                        'Update the existing local installation discovery registry.',
                        'Unrelated settings remain. No credentials, cloud changes, package reinstall or project binding.',
                        'Close every affected harness session before approving, including any launching this installer. '
                        'The installer cannot verify process liveness; decline or skip if any is running.'])
        approved = ui.review('Review exact local harness changes', '\n'.join(visible),
                             details='\n\n'.join(details), accept='Harnesses stopped — apply local changes', back=True)
        if approved is None:
            continue
        if not approved:
            result['reason'] = 'Local file review declined; no harness files changed.'
            return result
        break
    ui.checkpoint()
    try:
        # Match the installer/uninstaller coordination boundary. No UI pause can
        # interrupt creation between a file write and its ownership receipt.
        with _lock(home / 'runtime/registry.lock'):
            if reinstall_marker(home) is not None:
                raise ValueError('This home requires explicit local reinstallation after uninstall; no harness files changed.')
            prior = _ownership(home)
            source = Path(prior['source']) if prior.get('source') else resource_path('templates').parent
            if skill_plan(roots) != skills or retired_skill_plan(home, roots) != retired:
                raise ValueError('Skill files changed since review; preserved. Review again before writing.')
            for change in changes:
                if _read(change.path) != change.before:
                    raise ValueError(f'Integration changed since review; preserved: {change.path}')
            recorded_launcher = prior.get('owned', {}).get(str(home / 'venv'), {}).get('bin/maven-mcp')
            if launcher_permission_plan(home, recorded=recorded_launcher) != launcher_repair:
                raise ValueError('Launcher changed since review; preserved.')
            if launcher_repair is not None:
                _launcher_owned(home, prior, launcher_repair)
                # Journal only this leaf, not a fresh inventory of the whole venv:
                # unrelated runtime modifications must remain unowned. An
                # interruption before chmod leaves a conservative mismatch that
                # this same exact repair can finish on a local retry.
                prior['owned'][str(home / 'venv')]['bin/maven-mcp'] = {
                    **launcher_repair, 'mode': 0o755}
                save_private(home / MANIFEST, prior)
                if launcher_repair['mode'] == 0o775:
                    repair_launcher_permissions(home, launcher_repair)
                    result['written'].append(str(home / 'venv/bin/maven-mcp'))
                else:
                    result['written'].append(str(home / MANIFEST))
            references = [root / name / 'SKILL.md' for root in roots for name in SKILLS]
            record_installation(home, source, [], references)
            for target, body in skills:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if _read(target) is not None:
                    raise ValueError(f'Skill appeared since review; preserved: {target}')
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                result['written'].append(str(target))
                record_installation(home, source, [target], references)
            for change in changes:
                # Journal before mutation and preserve the original pinned receipt
                # when a subsequent run sees identical, already-owned content.
                record_installation(home, source, [], [], integrations=[change.receipt])
                apply_integration(change)
                if change.before != change.after:
                    result['written'].append(str(change.path))
            for target, expected in retired:
                retire_skills([(target, expected)])
                result['written'].append(str(target))
        result.update(status='written' if result['written'] else 'unchanged', reason='Reviewed local integration is configured.')
    except (OSError, ValueError, UninstallError) as exc:
        result.update(status='conflict', reason=str(exc))
    return result


def show_completion(ui, home, result, *, outcome=None):
    """Show truthful separate readiness states; local-only callers omit outcome."""
    home = Path(home).expanduser().absolute()
    verified = _verified(home, outcome)
    ui.set_context(_cloud_context(home, outcome) if verified else
                   f'MAVEN home: {home}\nCloud access: not verified by this local installation.')
    ui.stage('Next steps')
    cloud = (f'Previously verified cloud access: {outcome["instance_id"]} in account {outcome["account"]} '
             f'({outcome["region"]}), routine profile {outcome["profile"]}. '
             'Exact local settings still match; AWS was not rechecked by this harness step.' if verified else
             'Cloud access: not verified. Local tool/configuration changes do not establish cloud readiness.')
    status = result['status']
    local = {'written': 'Harness configuration: written.',
             'unchanged': 'Harness configuration: identical existing files preserved.',
             'skipped': 'Harness configuration: skipped.',
             'conflict': 'Harness configuration: conflict; differing/unrelated files preserved.'}[status]
    lines = [cloud, local, result.get('reason', '')]
    if result.get('written'):
        lines.append('Completed local changes:\n' + '\n'.join(result['written']))
    if status in ('written', 'unchanged') or result.get('written'):
        lines.append('Restart needed: start a new session of each affected harness to load its MCP configuration, startup guidance and skills.')
    else:
        lines.append('Restart: no new harness configuration was applied in this step.')
    lines.append('Harness MCP connection: NOT YET VERIFIED. A successful cloud check does not prove a harness can connect.')
    if status in ('written', 'unchanged') and result['selected']:
        for name in result['selected']:
            lines.append(f'{NAMES[name]}: open your actual project in a new session and ask '
                         '"Enable MAVEN for this project". Review the proposed project root, '
                         'MAVEN instance and scope before approving.')
    elif result['selected']:
        lines.append('After resolving/skipping the local conflict, configure ' +
                     ', '.join(NAMES[name] for name in result['selected']) +
                     ' before expecting it to connect. Then open the actual project and ask '
                     '"Enable MAVEN for this project"; review the root, instance and scope.')
    else:
        lines.append('When ready, configure your chosen harness, then open the actual project and ask '
                     '"Enable MAVEN for this project"; review the root, instance and scope.')
    lines.append('Project setup: not performed here. No project folder was selected or bound.')
    if ui.form('MAVEN setup result', [], description='\n\n'.join(lines), submit='Finish') is None:
        raise OperationPaused('Completion screen paused; completed setup and local ownership receipts are retained.')
    return result


def finish_setup(ui, home, outcome):
    """Offer optional harness integration only after a verified setup outcome."""
    home = Path(home).expanduser().absolute()
    if not _verified(home, outcome):
        raise ValueError('Harness setup handoff requires a verified setup outcome for this exact MAVEN home.')
    result = configure_harnesses(ui, home, context=_cloud_context(home, outcome))
    return show_completion(ui, home, result, outcome=outcome)
