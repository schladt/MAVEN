"""Offline private-settings boundaries for reusable external integrations."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools.aws import secrets, settings
from tools.memory.environment import MemoryClientError


VALUES = {
    "MAVEN_AWS_REGION": "us-west-2",
    "MAVEN_SECRETS_PARAMETER_PREFIX": "/synthetic/secrets/",
    "MAVEN_SECRETS_FILE_KEY_PREFIX": "/synthetic/file-keys/",
    "MAVEN_SECRETS_OBJECT_PREFIX": "synthetic/files/",
    "MAVEN_SECRETS_BUCKET": "synthetic-private-files",
    "MAVEN_SECRETS_KEY_ID": "0123456789abcdef",
}


class IntegrationSettingsTests(unittest.TestCase):
    def test_loads_only_owner_private_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("MAVEN_SECRETS_BUCKET=synthetic-private-files\n", encoding="utf-8")
            path.chmod(0o600)
            with patch.object(settings, "ENVIRONMENT", path):
                self.assertEqual(settings.load_settings(), {
                    "MAVEN_SECRETS_BUCKET": "synthetic-private-files"})
                path.chmod(0o644)
                with self.assertRaises(MemoryClientError):
                    settings.load_settings()
                path.unlink()
                with self.assertRaises(MemoryClientError):
                    settings.load_settings()

    def test_explicit_configured_store_and_namespaces(self):
        with patch.object(secrets, "load_settings", return_value=VALUES):
            self.assertEqual(secrets.file_config(), ("synthetic-private-files", "0123456789abcdef"))
            self.assertEqual(secrets.parameter_name("example"), "/synthetic/secrets/example")
            self.assertEqual(settings.prefix(VALUES, "MAVEN_SECRETS_FILE_KEY_PREFIX"), "/synthetic/file-keys/")
            self.assertEqual(settings.prefix(VALUES, "MAVEN_SECRETS_OBJECT_PREFIX", parameter=False), "synthetic/files/")

    def test_missing_store_and_namespace_never_use_old_defaults(self):
        with patch.object(secrets, "load_settings", return_value={}):
            with self.assertRaises(secrets.StoreError):
                secrets.file_config()
            with self.assertRaises(secrets.StoreError):
                secrets.parameter_name("example")
            with patch.object(secrets, "selected_aws_session") as session:
                with self.assertRaises(secrets.StoreError):
                    secrets.aws_session()
                session.assert_not_called()

    def test_aws_session_uses_shared_identity_verification(self):
        session = Mock()
        with patch.object(secrets, "load_settings", return_value=VALUES), \
                patch.object(secrets, "selected_aws_session", return_value=session) as selected:
            self.assertIs(secrets.aws_session(), session)
            selected.assert_called_once_with(VALUES, region="us-west-2")
        with patch.object(secrets, "load_settings", return_value=VALUES), \
                patch.object(secrets, "selected_aws_session", side_effect=MemoryClientError("identity_mismatch")):
            with self.assertRaises(secrets.StoreError) as failure:
                secrets.aws_session()
            self.assertNotIn("identity_mismatch", str(failure.exception))

    def test_rejects_invalid_resource_configuration(self):
        for value in ("", "relative/", "/bad/../", "/missing-trailing-slash", "/bad space/"):
            with self.subTest(prefix=value):
                with self.assertRaises(MemoryClientError):
                    settings.prefix({"prefix": value}, "prefix")
        for key, value in (("MAVEN_SECRETS_BUCKET", "private/bucket"),
                           ("MAVEN_SECRETS_KEY_ID", "not-a-key-id")):
            with self.subTest(key=key), patch.object(secrets, "load_settings", return_value=dict(VALUES, **{key: value})):
                with self.assertRaises(secrets.StoreError):
                    secrets.file_config()


class SelectedLoginCacheTests(unittest.TestCase):
    def test_concurrent_login_providers_and_refresh_keep_the_selected_cache(self):
        """The real resolver/cache run concurrently; only the network fetcher is replaced."""
        from concurrent.futures import ThreadPoolExecutor
        from datetime import datetime, timedelta, timezone
        import os
        from threading import Barrier
        from types import MappingProxyType
        from botocore.utils import JSONFileCache, LoginTokenLoader
        from tools.memory.environment import login_session

        barrier = Barrier(2, timeout=5)

        class Fetcher:
            def __init__(self, session_name, token_loader, **kwargs):
                self.name, self.loader = session_name, token_loader

            def load_cached_credentials(self):
                barrier.wait()
                return self.loader.load_token(self.name)

            def refresh_credentials(self):
                barrier.wait()
                token = self.loader.load_token(self.name)
                token['access_key'] += '-refreshed'
                self.loader.save_token(self.name, token)
                return token

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            config = root / 'config'
            config.write_text('[profile daily]\nlogin_session = same-login\n'
                              '[profile maintenance]\nlogin_session = same-login\n')
            credentials = root / 'credentials'
            credentials.write_text('')
            values = {'MAVEN_AWS_REGION': 'us-west-2'}
            for prefix, profile in [('MAVEN_AWS_', 'daily'), ('MAVEN_DEPLOY_', 'maintenance')]:
                cache = root / profile
                cache.mkdir(mode=0o700)
                values.update({prefix + 'PROFILE': profile, prefix + 'LOGIN_CACHE': str(cache)})
                LoginTokenLoader(JSONFileCache(str(cache))).save_token('same-login', {
                    'access_key': 'synthetic-' + profile, 'secret_key': 'synthetic-secret',
                    'token': 'synthetic-token', 'expiry_time': '2099-01-01T00:00:00+00:00',
                    'account_id': '123456789012'})
            ambient = str(root / 'ambient-must-not-be-used')
            environment = {'AWS_CONFIG_FILE': str(config), 'AWS_SHARED_CREDENTIALS_FILE': str(credentials),
                           'AWS_LOGIN_CACHE_DIRECTORY': ambient, 'AWS_PROFILE': 'wrong-ambient',
                           'AWS_ACCESS_KEY_ID': 'synthetic-ambient', 'AWS_SECRET_ACCESS_KEY': 'synthetic-ambient'}
            with patch.dict(os.environ, environment), \
                    patch('botocore.credentials.LoginCredentialFetcher', Fetcher), \
                    patch('botocore.credentials.EC', object()), \
                    patch('botocore.session.Session.create_client', side_effect=AssertionError('No AWS calls')):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    pending = [pool.submit(login_session, MappingProxyType(values), deploy=deploy)
                               for deploy in (False, True)]
                    sessions = [future.result(timeout=10) for future in pending]
                    self.assertEqual(os.environ['AWS_LOGIN_CACHE_DIRECTORY'], ambient)
                    for session, profile in zip(sessions, ('daily', 'maintenance')):
                        self.assertEqual(session.profile_name, profile)
                        self.assertEqual(session.region_name, 'us-west-2')
                        self.assertEqual(session.get_credentials().method, 'login')
                        self.assertEqual(session.get_credentials().get_frozen_credentials().access_key,
                                         'synthetic-' + profile)
                        session.get_credentials()._expiry_time = datetime.now(timezone.utc) - timedelta(seconds=1)
                    pending = [pool.submit(session.get_credentials().get_frozen_credentials)
                               for session in sessions]
                    renewed = [future.result(timeout=10) for future in pending]
                self.assertEqual([item.access_key for item in renewed],
                                 ['synthetic-daily-refreshed', 'synthetic-maintenance-refreshed'])
                self.assertEqual(os.environ['AWS_LOGIN_CACHE_DIRECTORY'], ambient)
            for profile in ('daily', 'maintenance'):
                saved = LoginTokenLoader(JSONFileCache(str(root / profile))).load_token('same-login')
                self.assertEqual(saved['access_key'], 'synthetic-' + profile + '-refreshed')
            self.assertFalse(Path(ambient).exists())

    def test_login_only_resolution_never_uses_static_or_ambient_credentials(self):
        import os
        from tools.memory.environment import login_session
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            config = root / 'config'
            config.write_text('[profile static]\nregion = us-west-2\n')
            credentials = root / 'credentials'
            credentials.write_text('[static]\naws_access_key_id = synthetic-static\n'
                                   'aws_secret_access_key = synthetic-static\n')
            values = {'MAVEN_AWS_PROFILE': 'static', 'MAVEN_AWS_LOGIN_CACHE': str(root / 'cache')}
            with patch.dict(os.environ, {'AWS_CONFIG_FILE': str(config),
                                         'AWS_SHARED_CREDENTIALS_FILE': str(credentials),
                                         'AWS_ACCESS_KEY_ID': 'synthetic-ambient',
                                         'AWS_SECRET_ACCESS_KEY': 'synthetic-ambient'}), \
                    patch('botocore.session.Session.create_client') as network:
                with self.assertRaisesRegex(MemoryClientError, 'selected_profile_requires_login'):
                    login_session(values)
                network.assert_not_called()
                config.write_text('[profile static]\nlogin_session = synthetic-missing\n')
                with patch('botocore.credentials.EC', object()), \
                        self.assertRaisesRegex(MemoryClientError, 'selected_login_unavailable_or_expired'):
                    login_session(values)
                network.assert_not_called()

    def test_cache_selection_validation_does_not_rewrite_settings_or_environment(self):
        import os
        from tools.memory.environment import login_session
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            target = root / 'target'
            target.mkdir(mode=0o700)
            link = root / 'link'
            link.symlink_to(target, target_is_directory=True)
            public = root / 'public'
            public.mkdir(mode=0o755)
            public.chmod(0o755)
            for cache, code in [('relative', 'selected_login_configuration_unavailable'),
                                (str(link), 'login_cache_must_be_owner_private'),
                                (str(public), 'login_cache_must_be_owner_private')]:
                values = {'MAVEN_AWS_PROFILE': 'selected', 'MAVEN_AWS_LOGIN_CACHE': cache}
                original = dict(values)
                with self.subTest(cache=cache), patch.dict(os.environ, {'AWS_LOGIN_CACHE_DIRECTORY': 'untouched'}), \
                        patch('tools.memory.environment.boto3.Session') as sdk:
                    with self.assertRaisesRegex(MemoryClientError, code):
                        login_session(values)
                    sdk.assert_not_called()
                    self.assertEqual(values, original)
                    self.assertEqual(os.environ['AWS_LOGIN_CACHE_DIRECTORY'], 'untouched')

    def test_initial_identity_inspection_uses_unbound_selected_login(self):
        from tools.setup_common import inspect_login_identity
        identity = {'Arn': 'arn:aws:iam::123456789012:user/selected', 'Account': '123456789012'}
        session = Mock()
        session.client.return_value.get_caller_identity.return_value = identity
        values = {'MAVEN_AWS_PROFILE': 'selected', 'MAVEN_AWS_REGION': 'us-west-2',
                  'MAVEN_AWS_LOGIN_CACHE': '/synthetic/cache'}
        with patch('tools.memory.environment.login_session', return_value=session) as selected:
            self.assertEqual(inspect_login_identity(values), identity)
        selected.assert_called_once_with(values, deploy=False, region='us-west-2')
        session.client.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
