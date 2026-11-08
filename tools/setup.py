"""Interactive owner-selected cloud and project setup; run from the installed venv."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import sys
from uuid import uuid4

from tools.interaction import Field, OperationPaused, checkpoint, message, require, stage


from tools.setup_common import (
    SetupPending, approve, ask, atomic_private, confirm, private_directory,
    read_private, save_json, setup_lock, verify_authority,
)


class Wizard:
    def __init__(self, home, project_root):
        from tools.memory.environment import load_environment
        self.home = home
        self.env = home / '.env'
        self.directory = home / 'runtime' / 'setup'
        private_directory(home)
        private_directory(home / 'runtime')
        private_directory(self.directory)
        self.state_path = self.directory / 'state.json'
        self.state = read_private(self.state_path) if self.state_path.exists() else {'schema': 1}
        if self.state.get('schema') != 1:
            raise SetupPending('Unsupported setup state; preserve the artifacts and use the matching installed version.')
        self.values = load_environment(self.env) if self.env.exists() or self.env.is_symlink() else {}
        self.project_root = project_root

    def save(self):
        save_json(self.state_path, self.state)

    def before_request(self, client, request_key):
        """Reconcile an explicitly resumed write before retrying its original request."""
        checkpoint()
        from tools.memory.client import MemoryClientError
        submitted = self.state.setdefault('submitted_requests', [])
        if request_key in submitted:
            try:
                client.invoke('/v1/operations/lookup', {'request_key': request_key})
            except MemoryClientError as exc:
                if exc.code != 'operation_unavailable':
                    raise
        else:
            submitted.append(request_key)
            self.save()

    def settings(self, updates):
        from tools.memory.environment import load_environment, ENV_KEYS
        if set(updates) - ENV_KEYS:
            raise SetupPending('Unsupported private setting; nothing was written.')
        if self.env.exists() and load_environment(self.env) != self.values:
            raise SetupPending('Private settings changed during setup; rerun without discarding pending artifacts.')
        values = {**self.values, **updates}
        if any(not isinstance(v, str) or not v or any(ord(c) < 32 for c in v) for v in values.values()):
            raise SetupPending('Settings must be nonempty literal text, never credentials.')
        atomic_private(self.env, ''.join(k + '=' + json.dumps(v) + '\n' for k, v in sorted(values.items())))
        self.values = values

    def identity(self, *, deploy=False):
        import boto3
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
        from tools.memory.environment import selected_aws_session, transport_ca_bundle
        prefix = 'MAVEN_DEPLOY_' if deploy else 'MAVEN_AWS_'
        purpose = 'metadata/deployment' if deploy else 'routine'
        stage('AWS identities')
        profile = self.values.get(prefix + 'PROFILE')
        cache_setting = self.values.get(prefix + 'LOGIN_CACHE')
        if profile is None or cache_setting is None:
            profiles = ([p for p in boto3.Session().available_profiles if re.fullmatch(r'[A-Za-z0-9_.@+-]{1,128}', p)]
                        if profile is None else [profile])
            default_cache = self.home / 'runtime' / ('aws-deploy-login-cache' if deploy else 'aws-login-cache')
            fields = []
            if profile is None:
                fields.append(Field('profile', f'Named AWS {purpose} profile'))
            if cache_setting is None:
                fields.append(Field('cache', f'Private {purpose} login-cache folder', str(default_cache)))
            values = require().form(f'Select {purpose} identity', fields,
                                    description='Configured profiles: ' + (', '.join(profiles) or '(none)') +
                                    '\nNo ambient identity or privilege fallback. Never enter credentials.')
            if values is None:
                raise SetupPending('Identity selection paused.')
            profile = profile or values['profile']
            cache_setting = cache_setting or values['cache']
        if not re.fullmatch(r'[A-Za-z0-9_.@+-]{1,128}', profile):
            raise SetupPending('Invalid profile name. Configure a named AWS CLI login profile, then resume.')
        cache = Path(cache_setting).expanduser()
        if not cache.is_absolute():
            raise SetupPending('The selected login cache must be an absolute private directory.')
        other = self.values.get(('MAVEN_AWS_' if deploy else 'MAVEN_DEPLOY_') + 'LOGIN_CACHE')
        if other and cache == Path(other).expanduser():
            raise SetupPending('Routine and metadata/deployment login caches must be separate.')
        private_directory(cache)
        self.settings({prefix + 'PROFILE': profile, prefix + 'LOGIN_CACHE': str(cache)})
        os.environ['AWS_LOGIN_CACHE_DIRECTORY'] = str(cache)
        region = self.values['MAVEN_AWS_REGION']
        for attempt in range(2):
            try:
                session = boto3.Session(profile_name=profile, region_name=region)
                credentials = session.get_credentials()
                if credentials is None:
                    raise NoCredentialsError()
                if credentials.method != 'login':
                    raise SetupPending('Use a named AWS CLI aws login profile for guided setup, not saved static keys '
                                       'or an implicit credential chain. Select a login-only profile in the private settings and resume.')
                sts = session.client('sts', verify=transport_ca_bundle(), config=Config(
                    connect_timeout=3, read_timeout=10, retries={'total_max_attempts': 1}))
                try:
                    identity = sts.get_caller_identity()
                finally:
                    sts.close()
                break
            except (BotoCoreError, ClientError):
                message(f'Login unavailable for selected profile {profile}. Configure it with your owner-approved AWS login flow.')
                message('If a VPN/proxy requires a CA, source your installation activate file or set AWS_CA_BUNDLE to your trusted PEM bundle; TLS stays enabled.')
                command = ['aws', 'login', '--profile', profile, '--region', region]
                message('Owner login command: ' + shlex.join(command))
                message('AWS sign-in may also offer AWS Agent Toolkit skills and an AWS MCP connection.')
                message('Those are optional AWS integrations, separate from MAVEN; declining them does not block MAVEN setup.')
                if attempt or not confirm('Run that interactive login now, using this terminal?'):
                    raise SetupPending('Complete the selected profile login, then resume this same setup.') from None
                try:
                    result = require().external(command, check=False)
                except FileNotFoundError:
                    raise SetupPending('Install AWS CLI v2 with aws login support, then run the displayed login command.') from None
                if result.returncode:
                    raise SetupPending('Login did not succeed. Complete your normal AWS login and resume.')
        arn = identity.get('Arn', '')
        if not deploy and arn.endswith(':root'):
            raise SetupPending('Root cannot be used for routine access. Select a named non-root profile in the private settings and resume.')
        expected = self.values.get(prefix + 'EXPECTED_PRINCIPAL')
        if expected and arn != expected:
            raise SetupPending('The selected login does not match the saved expected principal. Log in as that principal; settings and pending requests are preserved.')
        message(f'Actual {purpose} principal: {arn}\nAccount: {identity.get("Account", "unknown")}\nRegion: {region}')
        approve(f'Confirm this explicitly selected {purpose} identity.', arn,
                body=f'Principal: {arn}\nAccount: {identity.get("Account", "unknown")}\nRegion: {region}\nProfile: {profile}\nLogin cache: {cache}')
        self.settings({prefix + 'EXPECTED_PRINCIPAL': arn})
        return selected_aws_session(self.values, deploy=deploy, region=region), identity

    def region(self):
        region = self.values.get('MAVEN_AWS_REGION') or self.values.get('MAVEN_MEMORY_REGION')
        if not region:
            region = ask('AWS region (the reviewed deployment currently supports us-east-1)', 'us-east-1')
        if region != 'us-east-1':
            raise SetupPending('This runtime/template supports us-east-1 only; select compatible infrastructure or a reviewed regional implementation.')
        self.settings({'MAVEN_AWS_REGION': region})

    def existing(self):
        from tools.memory import bootstrap
        from tools.memory.environment import load_environment, memory_config, transport_ca_bundle
        if not self.values.get('MAVEN_MEMORY_INSTANCE_ID'):
            method = self.state.get('existing_source')
            if method is None:
                method = require().choose('Connect existing instance', (
                    ('Import private settings', 'import'),
                    ('Select an existing CloudFormation stack', 'stack')),
                    description='Existing mode never deploys, initializes, or replaces authority.')
            if method == 'import':
                source = Path(ask('Absolute path to the existing owner-private .env (0600)')).expanduser()
                if not source.is_absolute():
                    raise SetupPending('Select an absolute path to the existing private settings.')
                imported = load_environment(source)
                # Connection locators are portable; another machine's cache paths are not.
                imported.pop('MAVEN_AWS_LOGIN_CACHE', None)
                imported.pop('MAVEN_DEPLOY_LOGIN_CACHE', None)
                config = memory_config(imported)
                approve(f'Bind imported instance {config["instance_id"]} in {config["region"]}? No deployment or initialization will run.', config['instance_id'])
                conflicts = [key for key in self.values if key in imported and self.values[key] != imported[key]]
                if conflicts:
                    raise SetupPending('Imported settings conflict with saved selections (' + ', '.join(sorted(conflicts)) + '). Use the matching installation home; nothing was overwritten.')
                self.settings(imported)
                message('Imported connection and identity selections. Login caches are selected locally; credentials are never copied.')
            elif method != 'stack':
                raise SetupPending('Choose 1 or 2 when resuming.')
            else:
                self.state['existing_source'] = 'stack'
                self.save()
        self.region()
        self.identity()
        if not self.values.get('MAVEN_MEMORY_INSTANCE_ID'):
            session, _ = self.identity(deploy=True)
            message('This separately selected identity is used only for CloudFormation metadata; existing mode never deploys or initializes.')
            cloud = session.client('cloudformation', verify=transport_ca_bundle())
            try:
                stack = self.values.get('MAVEN_STACK_NAME')
                if not stack:
                    if confirm('List existing CloudFormation stacks with this selected metadata identity?'):
                        stacks = []
                        for page in cloud.get_paginator('list_stacks').paginate(StackStatusFilter=['CREATE_COMPLETE', 'UPDATE_COMPLETE']):
                            stacks.extend((item['StackName'], item['StackId']) for item in page['StackSummaries'])
                        stack = require().choose('Select the exact existing stack',
                                                 tuple(stacks) + (('Enter another exact name or ARN', ''),))
                    if not stack:
                        stack = ask('Exact existing CloudFormation stack name or ARN')
                    self.settings({'MAVEN_STACK_NAME': stack})
                result = cloud.describe_stacks(StackName=stack)['Stacks']
                if len(result) != 1:
                    raise SetupPending('Select exactly one existing stack.')
                config = bootstrap._outputs(result[0])
                approve(f'Bind existing stack {stack} without deployment or initialization?', config['instance_id'],
                        body=json.dumps({'stack': stack, 'connection': config}, indent=2))
                bootstrap.connect(self.values, self.env, confirm_instance=config['instance_id'], cloud=cloud)
                self.values = load_environment(self.env)
            finally:
                cloud.close()
        else:
            config = memory_config(self.values)
            approve('Preserve and verify the existing connection without deployment, initialization, or authority replacement.', config['instance_id'])

    def fresh(self):
        from tools.memory import bootstrap
        from tools.memory.environment import load_environment, memory_config
        self.region()
        _, routine = self.identity()
        _, deployment = self.identity(deploy=True)
        if not self.state.get('prepared'):
            if self.values.get('MAVEN_MEMORY_INSTANCE_ID'):
                raise SetupPending('Existing connection settings cannot be redeployed. Use existing-instance mode in a separate setup home.')
            caller = routine['Arn']
            account = routine['Account']
            if not re.fullmatch(rf'arn:aws:iam::{account}:user/[A-Za-z0-9+=,.@_/-]+', caller):
                raise SetupPending('The reviewed fresh-instance template requires an exact same-account IAM user as its routine caller principal. Ask your owner to provision that named login; assumed-role/root substitution is not supported. Existing compatible deployments can instead be connected in another home.')
            if deployment['Account'] != account:
                raise SetupPending('Deployment and routine callers must select the same AWS account.')
            package = self.directory / 'package'
            has_package = package.exists()
            updates = {'MAVEN_ACCOUNT_ID': account, 'MAVEN_CALLER_PRINCIPAL_ARN': caller}
            specifications = (
                ('MAVEN_STACK_NAME', 'New CloudFormation stack name', ''),
                ('MAVEN_INSTANCE_NAME', 'Instance name (2–32 lowercase letters/digits/hyphens)', ''),
                ('MAVEN_NOTIFICATION_EMAIL', 'Notification email', ''),
                ('MAVEN_AVAILABILITY_ZONES', 'Two distinct reviewed us-east-1 AZs, comma-separated', ''),
                ('MAVEN_ENGINE_VERSION', 'Reviewed Aurora PostgreSQL 18 patch >=18.3', '18.6'),
            )
            stage('Deployment review')
            if has_package:
                updates.update({key: self.values.get(key) for key, _, _ in specifications})
            else:
                fields = [Field(key, label, self.values.get(key) or default)
                          for key, label, default in specifications]
                selected = require().form('New instance provisioning', fields,
                                          description='Select actual account/region-supported AZs and engine patch. Confirm the notification subscription when it arrives.')
                if selected is None:
                    raise SetupPending('Provisioning selection paused.')
                updates.update(selected)
                updates['MAVEN_AVAILABILITY_ZONES'] = ','.join(
                    zone.strip() for zone in updates['MAVEN_AVAILABILITY_ZONES'].split(','))
            updates['MAVEN_INSTANCE_ID'] = self.values.get('MAVEN_INSTANCE_ID') or str(uuid4())
            updates['MAVEN_RECOVERY_EPOCH'] = self.values.get('MAVEN_RECOVERY_EPOCH') or str(uuid4())
            self.settings(updates)
            if package.exists():
                from tools.memory.client import MemoryClientError
                private_directory(package)
                try:
                    bootstrap._package(package)
                except (MemoryClientError, OSError, KeyError, TypeError, ValueError):
                    if any((self.directory / name).exists() for name in ('change-set.json', 'change-set.json.pending')):
                        raise SetupPending('An incomplete package has a cloud review/pending request. Restore the exact original artifacts; never rebuild or create another change set.')
                    archived = self.directory / ('package.incomplete.' + str(uuid4()))
                    approve(f'Offline preparation was interrupted before any cloud plan. Preserve {package} as {archived} and rebuild with the same instance/recovery IDs?', 'REBUILD OFFLINE')
                    package.rename(archived)
            if not package.exists():
                from tools.memory.client import MemoryClientError
                try:
                    bootstrap.prepare(self.values, package)
                except MemoryClientError as exc:
                    if exc.code != 'installation_settings_invalid':
                        raise
                    raise SetupPending('Offline deployment settings were rejected. Resume to correct the instance '
                                       'name, notification email, two distinct AZs or reviewed engine patch. '
                                       'No change set was created; keep the same private home.') from None
            manifest, _, _ = bootstrap._package(package)
            self.state.update(prepared=True, instance_id=manifest['deployment_inputs']['InstanceId'])
            self.save()
        package = self.directory / 'package'
        manifest, template, parameters = bootstrap._package(package)
        if manifest['deployment_inputs']['InstanceId'] != self.state['instance_id']:
            raise SetupPending('Prepared instance identity changed; preserve and inspect the exact package.')
        stage('Deployment review')
        package_body = self.package_review(package, manifest, template, parameters)
        review = self.directory / 'change-set.json'
        if not self.state.get('applied'):
            message(f'Review all real deployment artifacts: {package}')
            message('This creates billable Aurora, networking, API, storage and logging resources. Review IAM, notification recipient, live engine/AZ availability and current AWS prices. Retained databases, snapshots and buckets may continue to incur costs after stack deletion. Nothing is free or automatically cleaned up.')
            if not review.exists():
                approve('Create/reconcile one CREATE change set, NOT execute it.', self.values['MAVEN_STACK_NAME'],
                        body=package_body)
                bootstrap.plan(self.values, package, review, change_type='CREATE')
            document = read_private(review)
            digest = document['review_sha256']
            message(f'Actual CloudFormation review: {review}\nReview digest: {digest}')
            approve('Execute this exact reviewed change set? Unknown outcomes reconcile the same change set.', digest,
                    body=package_body + '\n\nComplete CloudFormation review\n' + json.dumps(document, indent=2))
            bootstrap.apply(self.values, package, review, self.env, confirm_review=digest)
            self.values = load_environment(self.env)
            self.state['applied'] = True
            self.save()
        config = memory_config(self.values)
        if config['instance_id'] != self.state['instance_id']:
            raise SetupPending('Connected instance differs from the prepared deployment; refusing initialization.')
        if not self.state.get('initialized'):
            stage('Initialize')
            approve('Initialize only this newly deployed database through the separate admin role. Resume repeats the exact SQL bundle, never an upgrade.',
                    config['instance_id'], body=package_body, destructive=True)
            self.state['initialization_pending'] = True
            self.save()
            bootstrap.initialize(self.values, confirm_instance=config['instance_id'])
            self.state['initialized'] = True
            self.save()
        self.starter()

    @staticmethod
    def package_review(package, manifest, template, parameters):
        sections = [
            'Billable Aurora, networking, API, storage and logging resources. Review IAM, notification recipient, live engine/AZ availability and current AWS prices. Retained databases, snapshots and buckets may continue to cost money after stack deletion. No automatic cleanup or cloud rollback.',
            'Package: ' + str(package),
            'Manifest and artifact hashes\n' + json.dumps(manifest, indent=2),
            'CloudFormation template\n' + template,
            'Parameters\n' + json.dumps(parameters, indent=2),
        ]
        for name in manifest.get('artifact_sha256', {}):
            if Path(name).name != name:
                raise SetupPending('Invalid deployment artifact path.')
            if name not in ('template.json', 'parameters.json'):
                sections.append(name + '\n' + (package / name).read_text(encoding='utf-8'))
        return '\n\n'.join(sections)

    def starter(self):
        from tools.memory import onboarding
        stage('Starter authority')
        from tools.memory.environment import configured_client
        from tools.memory.validation import digest, validate_create, validate_resolve
        directory = self.directory / 'starter'
        if directory.exists() and not (directory / 'manifest.json').exists():
            if self.state.get('starter_submitted'):
                raise SetupPending('Starter manifest is missing after submission. Restore the original private artifacts; new proposal/request IDs must not replace unknown outcomes.')
            private_directory(directory)
            archived = self.directory / ('starter.incomplete.' + str(uuid4()))
            approve(f'Offline starter preparation was interrupted before submission. Preserve {directory} as {archived} and prepare a fresh, unsubmitted review set?', 'REBUILD OFFLINE')
            directory.rename(archived)
        if not directory.exists():
            if not confirm('Prepare generic starter authority for this new instance (offline, not yet active)?'):
                raise SetupPending('Fresh-instance starter authority remains unconfigured.')
            onboarding.prepare(directory, source_origin='owner terminal setup',
                               source_excerpt='Owner requested the generic starter proposals for the newly deployed instance.')
        manifest = read_private(directory / 'manifest.json')
        completed = self.state.setdefault('starter_completed', [])
        if all(page['slug'] in completed for page in manifest['pages']):
            return
        if not confirm('This is the newly deployed instance and no active harness is using it. Continue reviewing its starter authority?'):
            raise SetupPending('Starter activation paused; no active harness authority is changed.')
        for page in manifest['pages']:
            slug = page['slug']
            if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', slug) or page['file'] != slug + '.json':
                raise SetupPending('Invalid starter artifact path; preserve the package for inspection.')
            draft_path = directory / page['file']
            draft = validate_create(read_private(draft_path))
            candidate_hash = digest(draft['candidate'])
            if (candidate_hash != page['candidate_hash'] or draft['proposal_id'] != page['proposal_id']
                    or draft['page_id'] != page['page_id'] or draft['request_key'] != page['request_key']):
                raise SetupPending('Starter candidate changed; refusing to submit or activate it.')
            if slug in completed:
                continue
            candidate_body = json.dumps(draft, ensure_ascii=False, indent=2)
            approve('Propose this exact starter candidate (not activation). Review its complete body, scope and dependencies.',
                    candidate_hash, body=candidate_body)
            self.state['starter_submitted'] = True
            self.save()
            client = configured_client(self.env, values=self.values)
            try:
                self.before_request(client, draft['request_key'])
                client.invoke('/v1/proposals/create', draft)
                actual = client.read_full({'proposal_id': draft['proposal_id']})['data']
                if actual.get('content_hash') != candidate_hash:
                    raise SetupPending('Cloud proposal does not match the reviewed candidate.')
            finally:
                client.close()
            approve('Activate this exact candidate through the separate approval role? Its instructions become operative in this new instance.',
                    candidate_hash, body=candidate_body)
            selection = self.state.setdefault('starter_approvals', {})
            name = selection.get(slug, slug + '.approval.json')
            if Path(name).name != name:
                raise SetupPending('Invalid saved approval artifact path.')
            approval_path = directory / name
            approval_client = configured_client(self.env, approval=True, values=self.values)
            try:
                from tools.memory.client import MemoryClientError
                for attempt in range(2):
                    if not approval_path.exists():
                        challenge = approval_client.invoke('/v1/proposals/challenge', {'proposal_id': draft['proposal_id']})
                        challenge_path = approval_path.with_suffix('.challenge.json')
                        save_json(challenge_path, challenge)
                        onboarding.approval(draft_path, challenge_path, approval_path,
                                            approval_origin='owner MAVEN setup TUI',
                                            approval_excerpt='Owner approved activation using the review button after viewing the complete candidate, scope and dependencies. Candidate SHA256: ' + candidate_hash)
                    selection[slug] = approval_path.name
                    self.save()
                    request = validate_resolve(read_private(approval_path))
                    if request['candidate_hash'] != candidate_hash or request['proposal_id'] != draft['proposal_id']:
                        raise SetupPending('Saved activation does not match this exact candidate.')
                    try:
                        # Unknown outcomes always keep the same request, even after expiry.
                        self.before_request(approval_client, request['request_key'])
                        approval_client.invoke('/v1/proposals/resolve', request)
                        break
                    except MemoryClientError as exc:
                        if exc.code != 'approval_invalid' or attempt:
                            raise
                        approve('The service definitively rejected this approval (for example, an expired challenge). Preserve it and authorize a new challenge for the SAME exact candidate? Unknown outcomes are never renewed.', 'RENEW ' + candidate_hash)
                        approval_path = directory / (slug + '.approval.' + str(uuid4()) + '.json')
                        # Save the next artifact name before creating it; never lose an approval request.
                        selection[slug] = approval_path.name
                        self.save()
            finally:
                approval_client.close()
            completed.append(slug)
            self.save()

    def verify_authority(self, project_id=None):
        return verify_authority(self.env, project_id, values=self.values)

    def project(self):
        from tools.project_setup import setup_project
        stage('Project selection')
        saved = self.state.get('project')
        if saved and not self.state.get('ready_project'):
            root = Path(saved['root'])
            approve(f'Resume the original pending project at {root}? Its exact IDs and receipts will be preserved.', str(root))
        else:
            root = Path(ask('Actual project directory (not the tool installation checkout)',
                            str(self.project_root) if self.project_root else None)).expanduser()
        result = setup_project(argparse.Namespace(
            root=root, env=self.env, non_interactive=False, create=False, project_id=None,
            name=None, source_origin=None, source_excerpt=None, no_git=False,
            remote=None, accepted_url=None, branch=None, check=[],
        ))
        return Path(result['project']['root']), result['authority_generation']

    def run(self):
        stage('Instance selection')
        checkpoint()
        message('MAVEN guided configuration: private settings, explicit AWS identity, reviewed cloud actions, then your selected project. Never enter access keys, tokens or passwords here. Quit pauses at a safe boundary.')
        mode = self.state.get('mode')
        if mode is None:
            if self.values.get('MAVEN_MEMORY_INSTANCE_ID'):
                mode = 'existing'
                message('An existing connection is present; preserving it. Redeployment is disabled.')
            else:
                mode = require().choose('Select instance setup', (
                    ('Connect existing infrastructure', 'existing'),
                    ('Provision a new instance', 'new')))
            if mode not in ('existing', 'new'):
                raise SetupPending('Choose existing or new when resuming.')
            self.state['mode'] = mode
            self.save()
        if mode == 'existing':
            self.existing()
        elif mode == 'new':
            self.fresh()
        else:
            raise SetupPending('Invalid saved setup mode; preserve and inspect the state.')
        stage('Verify readiness')
        checkpoint()
        generation = self.verify_authority()
        root = None
        if confirm('Configure a project now? (No: instance ready; choose the actual project later in OMP)'):
            root, generation = self.project()
            stage('Verify readiness')
            self.state['ready_project'] = str(root)
            self.save()
            message(f'\nReady: routine cloud access, complete applicable authority generation {generation}, and the cloud/local project were verified.')
            message(f'Project selection: {root / ".maven" / "project.json"}')
        else:
            message('\ninstance ready, project not configured in this run')
            if self.state.get('project') and not self.state.get('ready_project'):
                message('The original interrupted project receipt is preserved; resume its exact root before creating another project.')
        message(f'Home: {self.home}\nPrivate settings: {self.env}\nVirtual environment: {sys.prefix}')
        message('For a new shell (never source .env):')
        message('export MAVEN_HOME=' + shlex.quote(str(self.home)))
        message('export PATH=' + shlex.quote(str(Path(sys.executable).parent)) + ':"$PATH"')
        if (self.home / 'activate').exists():
            message('Or load the installer environment, including any explicitly selected CA:')
            message('source ' + shlex.quote(str(self.home / 'activate')))
        message('Open the actual project directory in OMP and invoke /skill:maven-start-work.')
        if root:
            message('Verify selection: maven-project show --root ' + shlex.quote(str(root)))
        checkpoint()
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('MAVEN_HOME', str(Path.home() / '.maven'))))
    parser.add_argument('--project-root', type=Path, help='Explicit project suggestion; still requires confirmation')
    args = parser.parse_args(argv)
    home = args.home.expanduser().absolute()
    os.environ['MAVEN_HOME'] = str(home)
    from tools.tui import run_workflow
    return run_workflow('MAVEN instance setup', [
        'Instance selection', 'AWS identities', 'Deployment review', 'Initialize',
        'Starter authority', 'Project selection', 'Project binding', 'Verify readiness',
    ], lambda ui: _run(args, home))


def _run(args, home):
    # All SDK operations stay on the workflow worker under the durable setup lock.
    from botocore.exceptions import BotoCoreError, ClientError
    from tools.memory.client import MemoryClientError
    from tools.project import ProjectError
    paused = False
    try:
        private_directory(home)
        private_directory(home / 'runtime')
        private_directory(home / 'runtime' / 'setup')
        with setup_lock(home / 'runtime' / 'setup'):
            return Wizard(home, args.project_root).run()
    except (OperationPaused, EOFError, KeyboardInterrupt):
        paused = True
        message('Setup paused at a safe boundary; readiness was not declared.')
    except SetupPending as exc:
        message('Setup pending: ' + str(exc))
    except MemoryClientError as exc:
        message('Setup pending: ' + exc.code)
        if exc.request_key:
            message('Preserved request key: ' + exc.request_key)
        message('Check the selected profile/role permissions, service availability and exact saved review. Unknown outcomes must use these same artifacts and request keys; never delete them or switch homes to retry the operation.')
    except (BotoCoreError, ClientError):
        message('Setup pending: selected AWS operation failed. Check the selected profile login, permissions, region and trusted CA; no fallback identity was used.')
    except (OSError, ValueError, KeyError, TypeError, ProjectError):
        message('Setup pending: invalid or unavailable local settings/artifacts/project. Preserve existing files, check their paths and private permissions, and resume. No readiness was declared.')
    resume = [str(Path(sys.executable).parent / 'maven-setup'), '--home', str(home)]
    if args.project_root is not None:
        resume.extend(['--project-root', str(args.project_root)])
    message('Resume: ' + shlex.join(resume))
    message('Preserved setup artifacts: ' + str(home / 'runtime' / 'setup'))
    if paused:
        raise OperationPaused('Setup paused; resume the same home with the preserved artifacts.')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
