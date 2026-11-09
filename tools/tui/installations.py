"""Read-only installation selection before entering the removal dashboard."""
from pathlib import Path

from tools.interaction import Field
from tools.uninstall.common import UninstallError, checked_path
from tools.uninstall.local import discover_homes


def _directory(path):
    path = checked_path(path)
    if not path.is_dir():
        raise UninstallError('Select an installation directory, not a file.')
    return path


def _browse(ui):
    current = Path.home()
    while True:
        ui.stage('Find installation folder')
        try:
            directories = sorted((path for path in current.iterdir()
                                  if not path.is_symlink() and path.is_dir()),
                                 key=lambda path: path.name.casefold())
        except OSError:
            directories = []
            ui.message('Cannot list this folder. Enter an accessible path or go back; no files were changed.')
        choices = [('Use this folder — ' + str(current), 'select'),
                   ('Enter an exact folder path', 'path')]
        if current.parent != current and current.parent != Path(current.anchor):
            choices.append(('Parent folder', 'parent'))
        choices.extend((path.name + '/', str(index)) for index, path in enumerate(directories))
        choices.append(('Back to detected installations', 'back'))
        action = ui.choose('Browse for a MAVEN installation', choices,
                           description=f'Current folder: {current}\n'
                           'Browse directories only, including hidden folders. Symlinks are not followed. '
                           'Choose the private installation home (usually containing installation.json, .env and venv), '
                           'not a project or source checkout. No removal is authorized here.')
        if action == 'back':
            return None
        try:
            if action == 'select':
                return _directory(current)
            if action == 'path':
                answer = ui.form('Find an installation folder', [
                    Field('path', 'Installation folder', str(current),
                          help='Enter an existing absolute path or ~/path. Files and symlink paths are rejected.')],
                    description='This only opens the folder for browsing; it does not remove anything.', back=True)
                if answer is None:
                    continue
                current = _directory(answer['path'])
            elif action == 'parent':
                current = _directory(current.parent)
            else:
                current = _directory(directories[int(action)])
        except (OSError, UninstallError) as error:
            ui.review('Folder cannot be selected', str(error) + '\nNo files were changed.', accept='Keep browsing')


def choose_removal_home(ui, *, extra=()):
    """Discover only known homes; browsing elsewhere requires an explicit choice."""
    while True:
        ui.stage('Find MAVEN installations')
        ui.set_context('Uninstall • Local discovery only • No removal or AWS access approved')
        description = ('Checks ~/.maven, MAVEN_HOME, the installer-selected home, and the installer registry '
                       '(~/.maven-installations/installations.json). No recursive disk scan or AWS login.\n'
                       'These are candidate homes, not proof that their files belong to MAVEN. '
                       'Project, machine and cloud removal remain separate choices.')
        try:
            homes = discover_homes(extra)
        except (OSError, UninstallError) as error:
            homes = []
            description += '\nAutomatic discovery needs attention: ' + str(error)
        if not homes:
            description += '\nNo installations could be listed. Use Advanced to locate a custom installation.'
        choices = [(home, str(index)) for index, home in enumerate(homes)]
        choices.extend([('Advanced — browse or enter an installation path', 'advanced'),
                        ('Back to main menu', 'back')])
        selected = ui.choose('Choose an installation to uninstall', choices, description=description)
        if selected == 'back':
            return None
        try:
            home = _browse(ui) if selected == 'advanced' else _directory(homes[int(selected)])
            if home is None:
                continue
            approved = ui.review('Open removal options for this installation?',
                                 f'Installation home: {home}\n\n'
                                 'Next: choose project, machine, cloud, or a combination. No scope is preselected. '
                                 'Exact inventory and removal approvals are still required. '
                                 'Opening the dashboard does not sign in to AWS or delete files.',
                                 accept='Open uninstall', back=True)
            if approved:
                return home
        except (OSError, UninstallError) as error:
            ui.review('Installation cannot be selected', str(error) + '\nNothing was removed.', accept='Choose another')
