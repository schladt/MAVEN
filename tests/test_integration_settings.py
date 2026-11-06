"""Offline private-settings boundaries for reusable external integrations."""

import builtins
from contextlib import redirect_stdout
from io import StringIO
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
            self.assertEqual(secrets._prefix("MAVEN_SECRETS_FILE_KEY_PREFIX"), "/synthetic/file-keys/")
            self.assertEqual(secrets._prefix("MAVEN_SECRETS_OBJECT_PREFIX", parameter=False), "synthetic/files/")

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

    def test_google_help_does_not_import_optional_runtime(self):
        from tools.google import cli
        original = builtins.__import__

        def deny_runtime(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "tools.google" and "client" in (fromlist or ()):
                raise AssertionError("help imported optional Google runtime")
            return original(name, globals, locals, fromlist, level)

        with patch("builtins.__import__", side_effect=deny_runtime), redirect_stdout(StringIO()):
            with self.assertRaises(SystemExit) as result:
                cli.main(["--help"])
        self.assertEqual(result.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
