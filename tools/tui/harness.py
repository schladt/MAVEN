"""Optional local harness integration after setup; never binds a project."""
from __future__ import annotations

import json
import os
from pathlib import Path

from maven_resources import resource_path
from tools.harness import (
    HARNESS_PATHS, SKILL_FILES, TURN_LIMITS, _read, apply_reviewed_integration, integration_plan, installed_targets,
    skill_cutover_plan, retire_skills, validate_default_profiles,
    launcher_permission_plan, repair_launcher_permissions,
)
from tools.interaction import Field, OperationPaused
from tools.uninstall.common import UninstallError, read_private, save_private
from tools.uninstall.local import (
    MANIFEST, _lock, record_installation, reinstall_marker,
)
from tools.update import install_skill_bundle, pending_owned_skills


NAMES = {'omp': 'OMP', 'claude': 'Claude Code', 'codex': 'Codex',
         'copilot': 'Copilot', 'gemini': 'Gemini'}
STATUSLINE_LABEL = "Show the active MAVEN project in Claude Code's status line"


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
    if not (isinstance(outcome, dict) and outcome.get('instance_ready') is True
            and outcome.get('home') == str(home) and isinstance(outcome.get('binding'), dict)
            and all(outcome.get(key) for key in ('instance_id', 'account', 'region', 'profile'))):
        return False
    from tools.memory.environment import load_environment
    from tools.memory.client import MemoryClientError
    from tools.setup_common import expected_account, SetupPending
    try:
        values = load_environment(home / '.env')
        # Deployment credentials do not attest the daily memory connection.
        def memory_binding(settings):
            return {key: value for key, value in settings.items() if not key.startswith('MAVEN_DEPLOY_')}

        return (memory_binding(values) == memory_binding(outcome['binding'])
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
    """Review and apply optional recorded or explicitly selected local profile files.

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
    recorded = installed_targets(home)
    destinations = recorded['paths']
    statusline = False
    while True:
        fields = [Field(name, label, name in result['selected'], kind='bool', help=(
            f'MCP: {destinations.get(name, (user_home / HARNESS_PATHS[name][0],))[0]}\n'
            f'Startup guidance: {destinations.get(name, (None, user_home / HARNESS_PATHS[name][1]))[1]}\n'
            f'Skills: {", ".join(map(str, recorded["skill_roots"])) if name in destinations else user_home / HARNESS_PATHS[name][2]}')) for name, label in NAMES.items()]
        fields.append(Field('statusline', STATUSLINE_LABEL, statusline, kind='bool', required=False, help=(
            'Applies only when Claude Code is selected. Adds an owned statusLine command to '
            f'{user_home / ".claude/settings.json"}; an existing different status line is preserved.')))
        values = ui.form('Choose the harnesses you use', fields,
                         description=('Select any installed harnesses, or leave all unchecked to skip. '
                                      'Recorded destinations are retained; new selections use default profiles. Differing MAVEN '
                                      'entries and unrelated files will be preserved. Includes native local turn hooks '
                                      'when the selected profile supports them. No credentials are copied.'),
                         submit='Review selected / Skip', back=True)
        if values is None:
            return result
        selected = [name for name in NAMES if values[name]]
        result['selected'] = selected
        statusline = bool(values.get('statusline')) and 'claude' in selected
        if not selected:
            return result
        conflicts = [{'target': path, 'status': 'conflict',
                      'reason': 'Legacy custom destination is ambiguous; select explicit paths in the installer.'}
                     for path in recorded['unresolved']]
        try:
            available = []
            for name in selected:
                try:
                    if name not in destinations:
                        if recorded['unresolved']:
                            raise ValueError('An existing custom destination is ambiguous; use explicit installer paths '
                                             'instead of adding guessed defaults.')
                        validate_default_profiles([name])
                    available.append(name)
                except ValueError as exc:
                    conflicts.append({'target': name, 'status': 'conflict', 'reason': str(exc)})
            if available and set(destinations).issubset(available):
                roots = list(recorded['skill_roots'])
            else:
                # A custom skill directory in older receipts has no harness tag.
                # Do not update unselected profiles by guessing its owner.
                roots = [p for p in recorded['skill_roots'] if any(
                    p == user_home / HARNESS_PATHS[name][2] for name in available)]
                if any(name in destinations for name in available):
                    known_defaults = {user_home / paths[2] for paths in HARNESS_PATHS.values()}
                    conflicts.extend({'target': str(root), 'status': 'conflict',
                                      'reason': 'Custom skill root has no recorded harness association; '
                                                'select all recorded harnesses or its explicit installer path.'}
                                     for root in recorded['skill_roots'] if root not in known_defaults)
            roots.extend(user_home / HARNESS_PATHS[name][2] for name in available if name not in destinations)
            roots = list(dict.fromkeys(roots))
            if available and not roots:
                conflicts.append({'target': ', '.join(available), 'component': 'skill', 'status': 'conflict',
                                  'reason': 'No canonical skill root is in scope; select an explicit --skill-dir.'})
            prior = _ownership(home)
            recorded_launcher = prior.get('owned', {}).get(str(home / 'venv'), {}).get('bin/maven-mcp')
            launcher_repair = launcher_permission_plan(home, recorded=recorded_launcher)
            if launcher_repair is not None:
                _launcher_owned(home, prior, launcher_repair)
            if ca_bundle is None and os.environ.get('AWS_CA_BUNDLE'):
                ca_bundle = Path(os.environ['AWS_CA_BUNDLE']).expanduser().absolute()
            profile_options = dict(ca_bundle=ca_bundle, statusline=statusline and 'claude' in available,
                                   paths={name: destinations[name] for name in available if name in destinations})
            changes = integration_plan(home, available, **profile_options)
            skills, retired, ready_roots = skill_cutover_plan(home, roots, conflicts=conflicts)
            pending_targets = [root / name for root in ready_roots for name in SKILL_FILES]
            pending_skills = pending_owned_skills(home, pending_targets)
            conflicts.extend({'target': str(change.path), 'status': 'conflict',
                              'component': change.receipt.get('target'), 'harness': change.receipt.get('harness'),
                              'reason': change.receipt['conflict']} for change in changes
                             if change.receipt.get('conflict'))
            result['targets'] = list(conflicts)
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
        if not conflicts and not pending_skills and launcher_repair is None and not skills and not retired and all(
                change.before == change.after and not change.receipt.get('retired') for change in changes):
            result.update(status='unchanged', reason='Identical local harness files preserved; no writes were needed.')
            return result
        details = []
        visible.extend('Conflict, preserved: ' + item['target'] + ' — ' + item['reason'] for item in conflicts)
        for pending in pending_skills:
            visible.append((pending.get('disposition') or 'Resume exact journaled local file installation')
                           + ': ' + pending['intent']['target'])
            details.append('Original local installation intent:\n' + json.dumps(pending, indent=2))
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
            if change.receipt.get('conflict'):
                visible.append('Conflict, not changed: ' + change.receipt['conflict'])
                continue
            action = ('Remove exact legacy hook' if change.receipt.get('retired') else
                      'Preserve identical content' if change.before == change.after else 'Write MAVEN content')
            visible.append(f'{action} ({change.receipt.get("target", "legacy hook")}): {change.path}')
            details.append(f'{change.path}\nExact MAVEN entry/block:\n' +
                           json.dumps(change.receipt['value'], indent=2, ensure_ascii=False))
        for target, body, expected in skills:
            visible.append(('Update installer-owned skill: ' if expected else 'Create shipped skill: ') + str(target))
            details.append(f'{target}\n{body.decode()}')
        for root in ready_roots:
            for name in SKILL_FILES:
                target = root / name
                if not any(path == target for path, _, _ in skills):
                    visible.append(f'Preserve identical skill: {target}')
        for target, expected in retired:
            visible.append(f'Remove exact installer-owned legacy skill: {target}')
            details.append(f'Retirement inventory for {target}:\n' + json.dumps(expected, indent=2))
        visible.extend(f'{NAMES[name]}: {TURN_LIMITS[name]}' for name in available)
        visible.extend([f'Update selective uninstall ownership: {home / MANIFEST}',
                        'Update the existing local installation discovery registry.',
                        'Unrelated settings remain. No credentials, cloud changes, package reinstall or project binding.',
                        'Running harness sessions do not block this change: they load it only after a restart. '
                        'A harness that saves its own configuration file while running can overwrite the MAVEN entry; '
                        'after restarting it, confirm MAVEN is listed.'])
        approved = ui.review('Review exact local harness changes', '\n'.join(visible),
                             details='\n\n'.join(details), accept='Apply local changes', back=True)
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
            if integration_plan(home, available, **profile_options) != changes:
                raise ValueError('Harness settings or ownership changed after review; no harness files changed.')
            if pending_owned_skills(home, pending_targets) != pending_skills:
                raise ValueError('Pending local installation intents changed since review; review the original evidence again.')
            checked = set()
            for change in changes:
                if change.receipt.get('conflict'):
                    continue
                # Retiring hooks may follow an MCP/statusLine change to the same JSON file.
                if change.path not in checked and _read(change.path) != change.before:
                    raise ValueError(f'Integration changed since review; preserved: {change.path}')
                checked.add(change.path)
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
            install_skill_bundle(home, source, ready_roots, skills, retired)
            record_installation(home, source, [], [], skill_roots=roots)
            result['written'].extend(str(target) for target, _, _ in skills)
            result['targets'].extend({'target': pending['intent']['target'],
                                      'status': pending.get('disposition', 'reconciled')} for pending in pending_skills)
            result['written'].extend(pending['path'] for pending in pending_skills)
            for change in changes:
                if change.receipt.get('conflict'):
                    continue
                apply_reviewed_integration(home, source, change)
                if change.before != change.after:
                    result['written'].append(str(change.path))
                result['targets'].append({'target': str(change.path),
                                          'component': change.receipt.get('target'), 'harness': change.receipt.get('harness'),
                                          'status': 'written' if change.before != change.after else 'unchanged'})
            for target, expected in retired:
                retire_skills(home, [(target, expected)])
                result['written'].append(str(target))
        result.update(status=('partial' if result['written'] else 'conflict') if conflicts else
                      ('written' if result['written'] else 'unchanged'),
                      reason='Safe reviewed targets configured; conflicting targets preserved.' if conflicts
                      else 'Reviewed local integration is configured.')
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
             'partial': 'Harness configuration: safe changes applied; conflicting files preserved.',
             'conflict': 'Harness configuration: conflict; differing/unrelated files preserved.'}[status]
    lines = [cloud, local, result.get('reason', '')]
    if result.get('targets'):
        lines.append('Per-target outcomes:\n' + '\n'.join(
            item['status'] + ': ' + (item['component'] + ' — ' if item.get('component') else '') + item['target'] +
            (' — ' + item['reason'] if item.get('reason') else '') for item in result['targets']))
    if result.get('written'):
        lines.append('Completed local changes:\n' + '\n'.join(result['written']))
    if status in ('written', 'unchanged') or result.get('written'):
        lines.append('Restart affected harnesses to load the MCP configuration, local project orientation and skills.')
    else:
        lines.append('Restart: no new harness configuration was applied in this step.')
    lines.append('Harness MCP connection: NOT YET VERIFIED. A successful cloud check does not prove a harness can connect.')
    if result.get('selected') and status != 'skipped':
        lines.append('Native hook execution: NOT VERIFIED.\n' + '\n'.join(
            f'{NAMES[name]}: {TURN_LIMITS[name]}' for name in result['selected']))
    if status in ('written', 'unchanged') and result['selected']:
        for name in result['selected']:
            lines.append(f'{NAMES[name]}: open an enabled project to read its local orientation brief. '
                         'For a new project, ask "Enable MAVEN for this project" and review the '
                         'proposed root, instance and scope before approving.')
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


def offer_home(ui, home):
    """Optional MAVEN Home step; Skip changes nothing and never blocks completion."""
    from tools.home import load_home
    if load_home(home) is not None:
        return {'status': 'present'}
    choice = ui.choose('Set up MAVEN Home (optional)', [
        ('Set up MAVEN Home — choose a separate project for personal memory', 'home'),
        ('Skip — nothing changes; set it up later from the installer menu', 'skip'),
    ], description='MAVEN Home remains separate from the current folder. Select it explicitly to use its memory. '
                   'Every change is shown in an exact review before it is applied.')
    if choice != 'home':
        return {'status': 'skipped'}
    from tools.tui.administration import present
    from tools.setup_common import SetupPending
    try:
        code = present(ui, home, kind='home', parameters={'action': 'enable'})
    except (OperationPaused, SetupPending, ValueError, OSError) as exc:
        ui.message(f'MAVEN Home was not finished: {exc}. Setup is complete; resume Home later from the installer menu.')
        return {'status': 'paused'}
    return {'status': 'completed' if code == 0 else 'stopped'}


def finish_setup(ui, home, outcome):
    """Offer optional harness integration and MAVEN Home only after a verified setup outcome."""
    home = Path(home).expanduser().absolute()
    if not _verified(home, outcome):
        raise ValueError('Harness setup handoff requires a verified setup outcome for this exact MAVEN home.')
    result = configure_harnesses(ui, home, context=_cloud_context(home, outcome))
    result = show_completion(ui, home, result, outcome=outcome)
    offer_home(ui, home)
    return result
