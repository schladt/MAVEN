"""Offline tests of deployment credential routing; no AWS clients or login caches are opened."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

from tools.memory import environment as env
from tools.memory.client import MemoryClientError
from tools.setup_common import inspect_login_identity, permission_preflight, verify_deployment_access


ACCOUNT = '123456789012'
CHROME = 'google-chrome --incognito --new-window %s'
ROLE = f'arn:aws:iam::{ACCOUNT}:role/operations/releases'
SOURCE = f'arn:aws:iam::{ACCOUNT}:user/developer'


def denied(action, code='AccessDenied'):
    return ClientError({'Error': {'Code': code, 'Message': 'private provider detail'}}, action)


class DeploymentCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.values = {'MAVEN_DEPLOY_MODE': 'role', 'MAVEN_DEPLOY_ROLE_ARN': ROLE,
                       'MAVEN_AWS_EXPECTED_PRINCIPAL': SOURCE, 'MAVEN_AWS_PROFILE': 'developer',
                       'MAVEN_AWS_LOGIN_CACHE': '/private/daily', 'MAVEN_AWS_REGION': 'us-east-1'}
        self.source, self.elevated = Mock(), Mock()
        self.daily, self.assumed, self.cloud, self.iam = Mock(), Mock(), Mock(), Mock()
        self.source.client.return_value = self.daily
        self.elevated.client.side_effect = lambda name, **kw: {
            'sts': self.assumed, 'cloudformation': self.cloud, 'iam': self.iam}[name]
        self.daily.get_caller_identity.return_value = {'Account': ACCOUNT, 'Arn': SOURCE}
        self.assumed.get_caller_identity.return_value = {
            'Account': ACCOUNT, 'Arn': f'arn:aws:sts::{ACCOUNT}:assumed-role/releases/changing-session',
            'UserId': 'AROAEXAMPLE:changing-session'}
        self.daily.assume_role.return_value = {'Credentials': {
            'AccessKeyId': 'synthetic', 'SecretAccessKey': 'synthetic', 'SessionToken': 'synthetic',
            'Expiration': self.now + timedelta(minutes=15)}}
        self.login = self.enterContext(patch.object(env, 'login_session', return_value=self.source))
        self.session = self.enterContext(patch.object(env.boto3, 'Session', return_value=self.elevated))
        self.enterContext(patch.object(env, '_deployment_time', side_effect=lambda: self.now))
        self.enterContext(patch.object(env, 'transport_ca_bundle', return_value=True))
        self.enterContext(patch.object(env, 'private_browser', return_value=CHROME))

    def test_verified_source_and_role_are_used_for_read_without_deploy_profile(self):
        for identity in [verify_deployment_access(self.values), inspect_login_identity(self.values, deploy=True)]:
            self.assertEqual(identity, {'Account': ACCOUNT, 'Arn': ROLE, 'UserId': 'AROAEXAMPLE'})
        self.login.assert_called_with(self.values, deploy=False, region='us-east-1')
        self.daily.assume_role.assert_called_with(RoleArn=ROLE, RoleSessionName='maven-deployment', DurationSeconds=900)
        self.cloud.describe_account_limits.assert_called_once_with()
        self.cloud.close.assert_called_once_with()
        self.assertEqual(self.assumed.get_caller_identity.call_count, 2)
        self.assertEqual(self.daily.get_caller_identity.call_count, 2)

    def test_expiry_never_refreshes_or_falls_back(self):
        env.selected_aws_session(self.values, deploy=True)
        credentials = self.session.call_args.kwargs['botocore_session'].get_credentials()
        self.assertEqual(credentials.get_frozen_credentials().access_key, 'synthetic')
        self.now += timedelta(seconds=901)
        with self.assertRaisesRegex(MemoryClientError, '^deployment_session_expired$'):
            credentials.get_frozen_credentials()
        self.daily.assume_role.assert_called_once()
        self.login.assert_called_once()

    def test_wrong_source_account_or_root_never_attempts_elevation(self):
        for changes in ({'MAVEN_AWS_EXPECTED_PRINCIPAL': f'arn:aws:iam::{ACCOUNT}:root'},
                        {'MAVEN_ACCOUNT_ID': '210987654321'},
                        {'MAVEN_DEPLOY_ROLE_ARN': ROLE.replace(ACCOUNT, '210987654321')},
                        {'MAVEN_DEPLOY_ROLE_ARN': ROLE.replace(':role/', ':user/')}):
            with self.subTest(changes=changes), self.assertRaises(MemoryClientError):
                env.selected_aws_session({**self.values, **changes}, deploy=True)
        self.login.assert_not_called()
        for identity in ({'Arn': SOURCE, 'Account': '210987654321'},
                         {'Arn': f'arn:aws:iam::{ACCOUNT}:root', 'Account': ACCOUNT}):
            self.daily.get_caller_identity.return_value = identity
            with self.assertRaisesRegex(MemoryClientError, '^unexpected_login_identity$'):
                env.selected_aws_session(self.values, deploy=True)
        self.daily.assume_role.assert_not_called()

    def test_wrong_resulting_account_or_role_never_reaches_cloudformation(self):
        actual = self.assumed.get_caller_identity.return_value
        for changes in ({'Account': '210987654321'}, {'Arn': actual['Arn'].replace('releases', 'unrelated')},
                        {'Arn': actual['Arn'].replace(ACCOUNT, '210987654321')}, {'Arn': ROLE}):
            self.assumed.get_caller_identity.return_value = {**actual, **changes}
            with self.subTest(changes=changes), self.assertRaisesRegex(MemoryClientError, '^unexpected_login_identity$'):
                verify_deployment_access(self.values)
        self.cloud.describe_account_limits.assert_not_called()

    def test_only_denied_elevation_or_expiry_offers_fresh_mfa_source_login(self):
        for method, exc, code, handoff in (
                (self.daily.get_caller_identity, denied('GetCallerIdentity'), 'not_authorized', False),
                (self.daily.assume_role, denied('AssumeRole'), 'deployment_elevation_denied', True),
                (self.daily.assume_role, denied('AssumeRole', 'ExpiredToken'), 'selected_login_unavailable_or_expired', True),
                (self.cloud.describe_account_limits, denied('DescribeAccountLimits'), 'not_authorized', False)):
            method.side_effect = exc
            with self.subTest(code=code), self.assertRaisesRegex(MemoryClientError, '^' + code + '$') as raised:
                verify_deployment_access(self.values)
            method.side_effect = None
            result = env.authentication_handoff(raised.exception, self.values, deploy=True)
            if handoff:
                self.assertEqual(result['command'], ['aws', 'login', '--profile', 'developer', '--region', 'us-east-1'])
                self.assertEqual(result['environment'], {'AWS_LOGIN_CACHE_DIRECTORY': '/private/daily', 'BROWSER': CHROME})
                self.assertTrue(result['fresh_mfa'])
                self.assertIn('private browser window', result['note'])
            else:
                self.assertIsNone(result)
        # Only elevation raises this code; a stage that did not say "deploy" still gets the handoff.
        result = env.authentication_handoff(MemoryClientError('deployment_elevation_denied'), self.values)
        self.assertEqual(result['environment']['BROWSER'], CHROME)
        self.assertEqual(result['purpose'], 'Fresh MFA for deployment role ' + ROLE)
        self.assertIn('fresh MFA', result['reconnect'])
        # Daily-use sign-in keeps reusing the browser session; only elevation needs fresh MFA.
        daily = env.authentication_handoff(MemoryClientError('selected_login_unavailable_or_expired'), self.values)
        self.assertEqual(daily['environment'], {'AWS_LOGIN_CACHE_DIRECTORY': '/private/daily'})
        self.assertNotIn('fresh_mfa', daily)

    def test_printed_role_sign_in_falls_back_to_a_plain_url(self):
        selected = env.sign_in(self.values, deploy=True)
        self.assertEqual(selected['command'], "AWS_LOGIN_CACHE_DIRECTORY=/private/daily BROWSER='google-chrome "
                         "--incognito --new-window %s' aws login --profile developer --region us-east-1")
        with patch.object(env, 'private_browser', return_value=None):
            selected = env.sign_in(self.values, deploy=True)
        self.assertEqual(selected['argv'][-1], '--remote')
        self.assertEqual(selected['environment'], {'AWS_LOGIN_CACHE_DIRECTORY': '/private/daily'})
        self.assertIn('private/incognito', selected['note'])
        with self.assertRaises(ValueError):
            env.fresh_sign_in(selected, 'sh -c unsafe %s')

    def test_preflight_simulates_exact_role_not_source_or_guessed_path(self):
        self.iam.simulate_principal_policy.side_effect = lambda **kw: {'EvaluationResults': [
            {'EvalActionName': name, 'EvalDecision': 'allowed'} for name in kw['ActionNames']]}
        self.assertEqual(permission_preflight(self.values, 'infrastructure')['status'], 'Passed')
        self.assertEqual(self.iam.simulate_principal_policy.call_args.kwargs['PolicySourceArn'], ROLE)
        self.cloud.describe_account_limits.assert_called_once_with()

    def test_legacy_profile_and_deferred_never_assume_a_role(self):
        values = {'MAVEN_DEPLOY_EXPECTED_PRINCIPAL': SOURCE, 'MAVEN_DEPLOY_PROFILE': 'maintenance'}
        for mode in ({}, {'MAVEN_DEPLOY_MODE': 'profile'}):
            selected = {**values, **mode}
            self.assertIs(env.selected_aws_session(selected, deploy=True), self.source)
            self.login.assert_called_with(selected, deploy=True, region='us-east-1')
        self.login.reset_mock()
        for mode in ('deferred', 'misspelled'):
            selected = {**self.values, **values, 'MAVEN_DEPLOY_MODE': mode}
            with self.assertRaises(MemoryClientError):
                env.selected_aws_session(selected, deploy=True)
            self.assertIsNone(env.authentication_handoff(MemoryClientError('authentication_required'), selected, deploy=True))
        self.login.assert_not_called()
        self.daily.assume_role.assert_not_called()


class PrivateBrowserTests(unittest.TestCase):
    """Default-browser detection for fresh-MFA sign-in, without launching anything."""

    def detect(self, platform, environ, **patches):
        with patch.object(env.sys, 'platform', platform), patch.dict(env.os.environ, environ, clear=True), \
                patch.object(env.subprocess, 'run', **patches.get('run', {})) as run, \
                patch.object(env.shutil, 'which', return_value='/usr/bin/found'):
            return env.private_browser(), run

    def test_linux_uses_the_default_browsers_private_window(self):
        for desktop, expected in (('google-chrome.desktop\n', CHROME),
                                  ('firefox_firefox.desktop', 'firefox --private-window %s'),
                                  ('microsoft-edge.desktop', 'microsoft-edge --inprivate --new-window %s'),
                                  ('unknown.desktop', None)):
            with self.subTest(desktop=desktop):
                browser, run = self.detect('linux', {'DISPLAY': ':0'},
                                           run={'return_value': Mock(stdout=desktop)})
                self.assertEqual(browser, expected)
                self.assertEqual(run.call_args.args[0], ['xdg-settings', 'get', 'default-web-browser'])
                if expected:
                    self.assertIn(expected, env.PRIVATE_BROWSERS)

    def test_headless_ssh_or_failed_detection_uses_the_plain_url(self):
        for platform, environ, run in (('linux', {}, {}), ('linux', {'DISPLAY': ':0', 'SSH_CONNECTION': 'x'}, {}),
                                       ('darwin', {'SSH_TTY': '/dev/ttys001'}, {}),
                                       ('linux', {'WAYLAND_DISPLAY': 'w'}, {'side_effect': OSError})):
            with self.subTest(platform=platform, environ=environ):
                browser, launched = self.detect(platform, environ, run=run)
                self.assertIsNone(browser)
                if not run:
                    launched.assert_not_called()

    def test_macos_reads_the_https_handler(self):
        import plistlib
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / 'Library/Preferences/com.apple.LaunchServices'
            path.mkdir(parents=True)
            for bundle, expected in (('com.google.chrome', "/usr/bin/open -na 'Google Chrome' --args --incognito "
                                                           "--new-window %s"),
                                     ('com.apple.safari', None)):
                (path / 'com.apple.launchservices.secure.plist').write_bytes(plistlib.dumps({'LSHandlers': [
                    {'LSHandlerURLScheme': 'mailto', 'LSHandlerRoleAll': 'com.apple.mail'},
                    {'LSHandlerURLScheme': 'https', 'LSHandlerRoleAll': bundle}]}))
                with self.subTest(bundle=bundle):
                    browser, run = self.detect('darwin', {'HOME': home})
                    self.assertEqual(browser, expected)
                    run.assert_not_called()
                    if expected:
                        self.assertIn(expected, env.PRIVATE_BROWSERS)
