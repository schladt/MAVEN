"""Interactive owner-selected cloud and project setup; run from the installed venv."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from uuid import uuid4


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
        profile = self.values.get(prefix + 'PROFILE')
        if profile is None:
            profiles = [p for p in boto3.Session().available_profiles if re.fullmatch(r'[A-Za-z0-9_.@+-]{1,128}', p)]
            print('Configured AWS profiles: ' + (', '.join(profiles) or '(none)'))
            print('Select a named profile; no ambient/default identity or privilege fallback is used.')
            profile = ask(f'AWS {purpose} profile name')
        if not re.fullmatch(r'[A-Za-z0-9_.@+-]{1,128}', profile):
            raise SetupPending('Invalid profile name. Configure a named AWS CLI login profile, then resume.')
        cache_setting = self.values.get(prefix + 'LOGIN_CACHE')
        if cache_setting is None:
            default_cache = self.home / 'runtime' / ('aws-deploy-login-cache' if deploy else 'aws-login-cache')
            cache_setting = ask(f'Private {purpose} login-cache folder (Enter creates the default)', str(default_cache))
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
                print(f'Login unavailable for selected profile {profile}. Configure it with your owner-approved AWS login flow.')
                print('If a VPN/proxy requires a CA, source your installation activate file or set AWS_CA_BUNDLE to your trusted PEM bundle; TLS stays enabled.')
                command = ['aws', 'login', '--profile', profile, '--region', region]
                print('Owner login command: ' + shlex.join(command))
                print('AWS sign-in may also offer AWS Agent Toolkit skills and an AWS MCP connection.')
                print('Those are optional AWS integrations, separate from MAVEN; declining them does not block MAVEN setup.')
                if attempt or not confirm('Run that interactive login now, using this terminal?'):
                    raise SetupPending('Complete the selected profile login, then resume this same setup.') from None
                try:
                    result = subprocess.run(command, check=False)
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
        print(f'Actual {purpose} principal: {arn}\nAccount: {identity.get("Account", "unknown")}\nRegion: {region}')
        approve(f'Confirm this explicitly selected {purpose} identity.', arn)
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
                print('How would you like to connect to your existing MAVEN instance?')
                print('  1. Import settings from another installation’s .env file (usually ~/.maven/.env)')
                print('  2. Find the existing deployment in AWS (CloudFormation stack)')
                method = {'1': 'import', '2': 'stack'}.get(ask('Choose 1 or 2', '1'))
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
                print('Imported connection and identity selections. Login caches are selected locally; credentials are never copied.')
            elif method != 'stack':
                raise SetupPending('Choose 1 or 2 when resuming.')
            else:
                self.state['existing_source'] = 'stack'
                self.save()
        self.region()
        self.identity()
        if not self.values.get('MAVEN_MEMORY_INSTANCE_ID'):
            session, _ = self.identity(deploy=True)
            print('This separately selected identity is used only for CloudFormation metadata; existing mode never deploys or initializes.')
            cloud = session.client('cloudformation', verify=transport_ca_bundle())
            try:
                stack = self.values.get('MAVEN_STACK_NAME')
                if not stack:
                    if confirm('List existing CloudFormation stacks with this selected metadata identity?'):
                        for page in cloud.get_paginator('list_stacks').paginate(StackStatusFilter=['CREATE_COMPLETE', 'UPDATE_COMPLETE']):
                            for item in page['StackSummaries']:
                                print(item['StackName'])
                    stack = ask('Exact existing CloudFormation stack name or ARN')
                    self.settings({'MAVEN_STACK_NAME': stack})
                result = cloud.describe_stacks(StackName=stack)['Stacks']
                if len(result) != 1:
                    raise SetupPending('Select exactly one existing stack.')
                config = bootstrap._outputs(result[0])
                approve(f'Bind existing stack {stack}, instance {config["instance_id"]}, recovery epoch {config["recovery_epoch"]}?', config['instance_id'])
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
            for key, prompt, default in (
                ('MAVEN_STACK_NAME', 'New CloudFormation stack name (must not be an existing stack)', None),
                ('MAVEN_INSTANCE_NAME', 'Instance name (2–32 lowercase letters/digits/hyphens, starting with a letter)', None),
                ('MAVEN_NOTIFICATION_EMAIL', 'Notification email (confirm the AWS subscription when it arrives)', None),
                ('MAVEN_AVAILABILITY_ZONES', 'Two distinct reviewed us-east-1 AZs, comma-separated', None),
                ('MAVEN_ENGINE_VERSION', 'Reviewed Aurora PostgreSQL 18 patch >=18.3 available in this account/region', '18.6'),
            ):
                updates[key] = self.values.get(key) if has_package else ask(prompt, self.values.get(key) or default)
                if key == 'MAVEN_AVAILABILITY_ZONES' and not has_package:
                    updates[key] = ','.join(zone.strip() for zone in updates[key].split(','))
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
        manifest, _, _ = bootstrap._package(package)
        if manifest['deployment_inputs']['InstanceId'] != self.state['instance_id']:
            raise SetupPending('Prepared instance identity changed; preserve and inspect the exact package.')
        review = self.directory / 'change-set.json'
        if not self.state.get('applied'):
            print(f'Review all real deployment artifacts: {package}')
            print('This creates billable Aurora, networking, API, storage and logging resources. Review IAM, notification recipient, live engine/AZ availability and current AWS prices. Retained databases, snapshots and buckets may continue to incur costs after stack deletion. Nothing is free or automatically cleaned up.')
            if not review.exists():
                approve('Authorize creation/reconciliation of one CREATE change set for this exact package, NOT its execution.', 'PLAN ' + self.values['MAVEN_STACK_NAME'])
                bootstrap.plan(self.values, package, review, change_type='CREATE')
            document = read_private(review)
            digest = document['review_sha256']
            print(f'Actual CloudFormation review: {review}\nReview digest: {digest}')
            approve('Read the complete change-set review and package before authorizing execution of this exact digest. A resumed unknown outcome reconciles this same change set.', digest)
            bootstrap.apply(self.values, package, review, self.env, confirm_review=digest)
            self.values = load_environment(self.env)
            self.state['applied'] = True
            self.save()
        config = memory_config(self.values)
        if config['instance_id'] != self.state['instance_id']:
            raise SetupPending('Connected instance differs from the prepared deployment; refusing initialization.')
        if not self.state.get('initialized'):
            approve('Initialize only this newly deployed database through the configured, separately assumed admin role. A resume repeats only the exact installed SQL bundle, never an upgrade.', 'INIT ' + config['instance_id'])
            self.state['initialization_pending'] = True
            self.save()
            bootstrap.initialize(self.values, confirm_instance=config['instance_id'])
            self.state['initialized'] = True
            self.save()
        self.starter()

    def starter(self):
        from tools.memory import onboarding
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
            print(f'Exact starter candidate: {draft_path}\nCandidate digest: {candidate_hash}')
            approve('Review the complete candidate body, scope and dependencies. Authorize proposing this exact candidate (not activation).', 'PROPOSE ' + candidate_hash)
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
            approve('Authorize activation of this exact candidate through the separate approval role. This makes its instructions operative in this new instance.', 'ACTIVATE ' + candidate_hash)
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
                                            approval_origin='owner terminal setup',
                                            approval_excerpt='Owner typed ACTIVATE ' + candidate_hash + ' after reviewing the complete candidate.')
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
        print('MAVEN guided configuration: private settings, explicit AWS identity, reviewed cloud actions, then your selected project. Never enter access keys, tokens or passwords here. Ctrl-C pauses safely.')
        mode = self.state.get('mode')
        if mode is None:
            if self.values.get('MAVEN_MEMORY_INSTANCE_ID'):
                mode = 'existing'
                print('An existing connection is present; preserving it. Redeployment is disabled.')
            else:
                mode = ask('Connect existing infrastructure or create a new instance? (existing/new)', 'existing')
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
        generation = self.verify_authority()
        root = None
        if confirm('Configure a project now? (No: instance ready; choose the actual project later in OMP)'):
            root, generation = self.project()
            self.state['ready_project'] = str(root)
            self.save()
            print(f'\nReady: routine cloud access, complete applicable authority generation {generation}, and the cloud/local project were verified.')
            print(f'Project selection: {root / ".maven" / "project.json"}')
        else:
            print('\ninstance ready, project not configured in this run')
            if self.state.get('project') and not self.state.get('ready_project'):
                print('The original interrupted project receipt is preserved; resume its exact root before creating another project.')
        print(f'Home: {self.home}\nPrivate settings: {self.env}\nVirtual environment: {sys.prefix}')
        print('For a new shell (never source .env):')
        print('export MAVEN_HOME=' + shlex.quote(str(self.home)))
        print('export PATH=' + shlex.quote(str(Path(sys.executable).parent)) + ':"$PATH"')
        if (self.home / 'activate').exists():
            print('Or load the installer environment, including any explicitly selected CA:')
            print('source ' + shlex.quote(str(self.home / 'activate')))
        print('Open the actual project directory in OMP and invoke /skill:maven-start-work.')
        if root:
            print('Verify selection: maven-project show --root ' + shlex.quote(str(root)))
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('MAVEN_HOME', str(Path.home() / '.maven'))))
    parser.add_argument('--project-root', type=Path, help='Explicit project suggestion; still requires confirmation')
    args = parser.parse_args(argv)
    home = args.home.expanduser().absolute()
    os.environ['MAVEN_HOME'] = str(home)
    # The entrypoint runs only after install.py has built the isolated environment.
    from botocore.exceptions import BotoCoreError, ClientError
    from tools.memory.client import MemoryClientError
    from tools.project import ProjectError
    try:
        private_directory(home)
        private_directory(home / 'runtime')
        private_directory(home / 'runtime' / 'setup')
        with setup_lock(home / 'runtime' / 'setup'):
            return Wizard(home, args.project_root).run()
    except (EOFError, KeyboardInterrupt):
        print('\nSetup paused; readiness was not declared.', file=sys.stderr)
    except SetupPending as exc:
        print('Setup pending: ' + str(exc), file=sys.stderr)
    except MemoryClientError as exc:
        print('Setup pending: ' + exc.code, file=sys.stderr)
        if exc.request_key:
            print('Preserved request key: ' + exc.request_key, file=sys.stderr)
        print('Check the selected profile/role permissions, service availability and exact saved review. Unknown outcomes must use these same artifacts and request keys; never delete them or switch homes to retry the operation.', file=sys.stderr)
    except (BotoCoreError, ClientError):
        print('Setup pending: selected AWS operation failed. Check the selected profile login, permissions, region and trusted CA; no fallback identity was used.', file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError, ProjectError):
        print('Setup pending: invalid or unavailable local settings/artifacts/project. Preserve existing files, check their paths and private permissions, and resume. No readiness was declared.', file=sys.stderr)
    resume = [str(Path(sys.executable).parent / 'maven-setup'), '--home', str(home)]
    if args.project_root is not None:
        resume.extend(['--project-root', str(args.project_root)])
    print('Resume: ' + shlex.join(resume), file=sys.stderr)
    print('Preserved setup artifacts: ' + str(home / 'runtime' / 'setup'), file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
