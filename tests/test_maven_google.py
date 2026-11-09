"""Focused account isolation and Gmail behavior for the interactive Google helper."""
import base64
from contextlib import nullcontext, redirect_stdout
import json
from io import StringIO
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    from tools.google import client as google
except ModuleNotFoundError as error:
    if error.name and (error.name == "requests" or error.name.startswith(("google", "google_auth_oauthlib"))):
        raise unittest.SkipTest("optional Google dependencies are not installed") from error
    raise
from tools.google import cli as google_cli


class Parameters:
    def __init__(self):
        self.items = {}
        self.reads = []
        self.writes = []

    def get_parameter(self, *, Name, WithDecryption):
        assert WithDecryption is True
        self.reads.append(Name)
        if Name not in self.items:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "ParameterNotFound", "Message": "missing"}}, "GetParameter")
        return {"Parameter": self.items[Name]}

    def put_parameter(self, *, Name, Value, Type, KeyId, Tier, Overwrite):
        assert Type == "SecureString" and Tier == "Standard" and KeyId == "alias/aws/ssm"
        if Name in self.items and not Overwrite:
            raise AssertionError("unexpected overwrite")
        self.items[Name] = {"Type": Type, "Value": Value,
                            "Version": self.items.get(Name, {}).get("Version", 0) + 1}
        self.writes.append(Name)


CLIENT = {"installed": {"client_id": "synthetic-id", "client_secret": "synthetic-secret",
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "redirect_uris": ["http://localhost"]}}


class GoogleBoundaryTests(unittest.TestCase):
    def setUp(self):
        settings = patch.object(google, "load_settings", return_value={
            "MAVEN_GOOGLE_PARAMETER_PREFIX": "/synthetic/secrets/",
            "MAVEN_AWS_REGION": "us-west-2",
        })
        settings.start()
        self.addCleanup(settings.stop)

    def test_namespace_requires_private_configuration(self):
        self.assertEqual(google._name("work", "client"), "/synthetic/secrets/google-work-client")
        with patch.object(google, "load_settings", return_value={}):
            with self.assertRaises(google.GoogleError):
                google._name("work", "client")

    def test_ssm_requires_configured_identity_without_fallback(self):
        with patch.object(google, "selected_aws_session") as session:
            google._ssm()
            session.assert_called_once_with(google.load_settings(), region="us-west-2")
        with patch.object(google, "load_settings", return_value={}), \
                patch.object(google, "selected_aws_session") as session:
            with self.assertRaises(google.GoogleError):
                google._ssm()
            session.assert_not_called()

    def test_import_isolated_and_rejects_duplicate_without_overwrite(self):
        store = Parameters()
        with tempfile.TemporaryDirectory() as directory:
            client_file = Path(directory) / "client.json"
            client_file.write_text(json.dumps(CLIENT))
            client_file.chmod(0o644)  # Existing work Desktop client is owner-owned and readable.
            with patch.object(google, "_ssm", return_value=store), patch.object(google, "_lock", return_value=nullcontext()):
                self.assertEqual(google.import_client("work", client_file, "work@example.com"), "work@example.com")
                first = store.items[google._name("work", "client")]["Value"]
                with self.assertRaisesRegex(google.GoogleError, "already imported"):
                    google.import_client("work", client_file, "work@example.com")
        self.assertEqual(store.items[google._name("work", "client")]["Value"], first)
        self.assertNotIn(google._name("personal", "client"), store.items)
        self.assertEqual(store.writes, [google._name("work", "client")])

    def test_wrong_identity_cannot_replace_existing_refresh_credential(self):
        store = Parameters()
        store.items[google._name("personal", "client")] = {
            "Type": "SecureString", "Value": json.dumps({**CLIENT, "expected_email": "personal@example.com"}), "Version": 1}
        prior = json.dumps({"expected_email": "personal@example.com", "refresh_token": "prior",
                            "scopes": list(google.SCOPES)})
        store.items[google._name("personal", "refresh")] = {
            "Type": "SecureString", "Value": prior, "Version": 1}

        class Flow:
            def __init__(self):
                self.oauth2session = type("Session", (), {"token": {"scope": list(google.SCOPES)}})()

            @staticmethod
            def from_client_config(*args, **kwargs):
                return Flow()

            def run_local_server(self, **kwargs):
                return type("Cred", (), {"granted_scopes": list(google.SCOPES),
                                         "refresh_token": "replacement"})()

        with patch.object(google, "_ssm", return_value=store), \
             patch.object(google, "_lock", return_value=nullcontext()), \
             patch.object(google, "InstalledAppFlow", Flow), \
             patch.object(google, "_identity", side_effect=google.GoogleError("wrong account")):
            with self.assertRaisesRegex(google.GoogleError, "wrong account"):
                google.authorize("personal", reauthorize=True)
        self.assertEqual(store.items[google._name("personal", "refresh")]["Value"], prior)
        self.assertNotIn(google._name("work", "refresh"), store.items)
        self.assertEqual(store.writes, [])

    def test_identity_failure_reports_only_http_status(self):
        class Response:
            status_code = 403
            text = "private API response and credential"

        class Session:
            def __init__(self, _credentials):
                pass

            def get(self, url, *, params, timeout):
                return Response()

        with patch.object(google, "AuthorizedSession", Session):
            with self.assertRaises(google.GoogleError) as failure:
                google._identity(object(), "personal@example.com")
        self.assertEqual(str(failure.exception), "Google Drive identity verification failed (HTTP 403)")
        self.assertNotIn("private API response", str(failure.exception))


    def test_inbox_summaries_and_private_download_preserve_existing_mail(self):
        mail = b"From: synthetic@example.com\r\nSubject: hello\r\n\r\nbody"
        encoded = base64.urlsafe_b64encode(mail).decode().rstrip("=")
        calls = []

        def fake_get(_credentials, url, *, params=None):
            calls.append((url, params))
            if url == google.GMAIL_MESSAGES:
                return {"messages": [{"id": "abc123"}]}
            if params["format"] == "metadata":
                return {"id": "abc123", "threadId": "thread1", "labelIds": ["INBOX", "UNREAD"],
                        "snippet": "summary\ntext", "payload": {"headers": [
                            {"name": "From", "value": "synthetic@example.com"},
                            {"name": "Subject", "value": "=?utf-8?q?Hello?="},
                            {"name": "Date", "value": "Fri, 01 Nov 2026 10:00:00 +0000"}]}}
            return {"id": "abc123", "raw": encoded}

        with patch.object(google, "_credentials", return_value=(object(), "personal@example.com")), \
             patch.object(google, "_gmail_get", side_effect=fake_get):
            summaries = google.gmail_list("personal", "from:synthetic@example.com", 1)
            self.assertEqual(summaries["messages"][0]["subject"], "Hello")
            self.assertEqual(summaries["messages"][0]["snippet"], "summary text")
            self.assertTrue(summaries["messages"][0]["unread"])
            with tempfile.TemporaryDirectory() as directory:
                Path(directory).chmod(0o700)
                first = google.gmail_download("personal", ["abc123"], directory)
                self.assertEqual(first["saved"][0]["status"], "downloaded")
                target = Path(directory) / "abc123.eml"
                self.assertEqual(target.read_bytes(), mail)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
                again = google.gmail_download("personal", [], directory,
                                               query="from:synthetic@example.com", maximum=1)
                self.assertEqual(again["saved"][0]["status"], "exists")
                self.assertEqual(target.read_bytes(), mail)
        formats = [params.get("format") for _, params in calls]
        self.assertEqual(formats.count("raw"), 1)
        self.assertEqual(formats.count("metadata"), 1)

    def test_cli_download_exact_id_and_query_save_private_mail(self):
        mail = b"From: synthetic@example.com\r\nSubject: hello\r\n\r\nbody"
        encoded = base64.urlsafe_b64encode(mail).decode().rstrip("=")

        for selection in (["abc123"], ["--query", "from:synthetic@example.com", "--max", "1"]):
            with self.subTest(selection=selection), tempfile.TemporaryDirectory() as directory:
                Path(directory).chmod(0o700)
                calls = []

                def fake_get(_credentials, url, *, params=None):
                    calls.append((url, params))
                    if url == google.GMAIL_MESSAGES:
                        return {"messages": [{"id": "abc123"}]}
                    return {"id": "abc123", "raw": encoded}

                argv = ["maven-google", "--account", "personal", "gmail", "download",
                        *selection, "--out", directory, "--json"]
                with patch.object(sys, "argv", argv), \
                     patch.object(google, "_credentials", return_value=(object(), "personal@example.com")), \
                     patch.object(google, "_gmail_get", side_effect=fake_get), \
                     redirect_stdout(StringIO()) as output:
                    self.assertEqual(google_cli.main(), 0)

                saved = json.loads(output.getvalue())["saved"][0]
                self.assertEqual(saved["status"], "downloaded")
                target = Path(directory) / "abc123.eml"
                self.assertEqual(target.read_bytes(), mail)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
                if selection[0] == "--query":
                    self.assertEqual(calls[0][1]["maxResults"], 1)
                    self.assertEqual(calls[0][1]["q"], "from:synthetic@example.com")
                else:
                    self.assertEqual(len(calls), 1)

    def test_empty_gmail_query_returns_no_messages_without_masking_other_errors(self):
        class Session:
            status = 204

            def __init__(self, _credentials):
                pass

            def get(self, url, *, params, timeout):
                class Response:
                    status_code = Session.status

                    def json(self):
                        raise AssertionError("no response body for HTTP 204")

                return Response()

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(google, "_credentials", return_value=(object(), "personal@example.com")), \
             patch.object(google, "AuthorizedSession", Session):
            Path(directory).chmod(0o700)
            for action, expected_key in (("list", "messages"), ("download", "saved")):
                argv = ["maven-google", "--account", "personal", "gmail", action,
                        "--query", "from:unmatched@example.com", "--max", "1", "--json"]
                if action == "download":
                    argv += ["--out", directory]
                with patch.object(sys, "argv", argv), redirect_stdout(StringIO()) as output:
                    self.assertEqual(google_cli.main(), 0)
                self.assertEqual(json.loads(output.getvalue())[expected_key], [])
            self.assertEqual(list(Path(directory).iterdir()), [])
            with self.assertRaisesRegex(google.GoogleError, "read request failed"):
                google.gmail_message("personal", "abc123")
            Session.status = 403
            with self.assertRaisesRegex(google.GoogleError, "read request failed"):
                google.gmail_list("personal", "from:unmatched@example.com", 1)

    def test_all_mailbox_requires_explicit_query_without_inbox_filter(self):
        calls = []

        def fake_get(_credentials, url, *, params=None):
            calls.append((url, params))
            if url == google.GMAIL_MESSAGES:
                return {"messages": [{"id": "sent123"}]}
            return {"id": "sent123", "threadId": "thread1", "labelIds": ["SENT"],
                    "payload": {"headers": []}}

        with patch.object(google, "_credentials", return_value=(object(), "work@example.com")), \
             patch.object(google, "_gmail_get", side_effect=fake_get):
            with self.assertRaisesRegex(google.GoogleError, "explicit query"):
                google.gmail_list("work", mailbox="all")
            result = google.gmail_list("work", "from:work@example.com", 1, mailbox="all")
        self.assertEqual(result["messages"][0]["id"], "sent123")
        self.assertEqual(calls[0][1]["q"], "from:work@example.com")
        self.assertTrue(calls[0][1]["includeSpamTrash"])
        self.assertNotIn("labelIds", calls[0][1])

    def test_send_refuses_mismatch_and_non_private_mime_before_api(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "mail.eml"
            source.write_bytes(b"From: work@example.com\r\nTo: intended@example.com\r\n"
                               b"Subject: hi\r\n\r\nowner supplied body")
            source.chmod(0o600)
            with patch.object(google, "_credentials", return_value=(object(), "work@example.com")), \
                 patch.object(google, "_gmail_write") as write:
                with self.assertRaisesRegex(google.GoogleError, "recipients differ"):
                    google.gmail_send("work", source, ["wrong@example.com"])
                source.write_bytes(b"From: personal@example.com\r\nTo: intended@example.com\r\n\r\nbody")
                with self.assertRaisesRegex(google.GoogleError, "selected account"):
                    google.gmail_send("work", source, ["intended@example.com"])
                source.chmod(0o644)
                with self.assertRaisesRegex(google.GoogleError, "private owner-owned"):
                    google.gmail_send("work", source, ["intended@example.com"])
                write.assert_not_called()

    def test_send_verifies_sent_and_never_retries_ambiguous_failure(self):
        mail = b"From: work@example.com\r\nTo: intended@example.com\r\n\r\nprivate body"
        calls = []

        class Response:
            status_code = 200

            def __init__(self, data):
                self.data = data

            def json(self):
                return self.data

        class Session:
            def __init__(self, credentials):
                self.credentials = credentials

            def request(self, method, url, *, json, timeout):
                calls.append(("write", method, url, json))
                self.assert_body(json)
                return Response({"id": "sent123", "threadId": "thread1"})

            def assert_body(self, body):
                assert base64.urlsafe_b64decode(body["raw"]) == mail

            def get(self, url, *, params, timeout):
                calls.append(("verify", url, params))
                return Response({"id": "sent123", "threadId": "thread1",
                                 "labelIds": ["SENT"]})

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "mail.eml"
            source.write_bytes(mail)
            source.chmod(0o600)
            with patch.object(google, "_credentials", return_value=(object(), "work@example.com")), \
                 patch.object(google, "_lock", return_value=nullcontext()), \
                 patch.object(google, "LOCKS", Path(directory)), \
                 patch.object(google, "AuthorizedSession", Session):
                self.assertEqual(google.gmail_send("work", source, ["intended@example.com"]),
                                 {"id": "sent123", "thread_id": "thread1"})
                with self.assertRaisesRegex(google.GoogleError, "already attempted"):
                    google.gmail_send("work", source, ["intended@example.com"])
        self.assertEqual([call[0] for call in calls], ["write", "verify"])

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "mail.eml"
            source.write_bytes(mail)
            source.chmod(0o600)
            with patch.object(google, "_credentials", return_value=(object(), "work@example.com")), \
                 patch.object(google, "_lock", return_value=nullcontext()), \
                 patch.object(google, "LOCKS", Path(directory)), \
                 patch.object(google, "_gmail_write",
                              side_effect=google.GoogleError("Gmail write request failed")) as write:
                with self.assertRaisesRegex(google.GoogleError, "request failed"):
                    google.gmail_send("work", source, ["intended@example.com"])
                with self.assertRaisesRegex(google.GoogleError, "already attempted"):
                    google.gmail_send("work", source, ["intended@example.com"])
                self.assertEqual(write.call_count, 1)

    def test_draft_create_update_send_checks_identity_and_exact_draft(self):
        raw = b"From: personal@example.com\r\nTo: target@example.com\r\n\r\nbody"
        calls = []

        def fake_write(_credentials, method, url, body, **kwargs):
            calls.append((method, url, body))
            if url == google.GMAIL_DRAFTS + "/send":
                return {"id": "message2", "threadId": "thread1"}
            return {"id": "draft1", "message": {"id": "message1"}}

        def fake_get(_credentials, url, *, params=None):
            if url == google.GMAIL_DRAFTS + "/draft1":
                return {"id": "draft1", "message": {"raw": base64.urlsafe_b64encode(raw).decode()}}
            return {"id": "message2", "threadId": "thread1", "labelIds": ["SENT"]}

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "draft.eml"
            source.write_bytes(raw)
            source.chmod(0o600)
            with patch.object(google, "_credentials", return_value=(object(), "personal@example.com")), \
                 patch.object(google, "_lock", return_value=nullcontext()), \
                 patch.object(google, "LOCKS", Path(directory)), \
                 patch.object(google, "_gmail_write", side_effect=fake_write), \
                 patch.object(google, "_gmail_get", side_effect=fake_get):
                self.assertEqual(google.gmail_draft_create("personal", source, ["target@example.com"])["id"],
                                 "draft1")
                self.assertEqual(google.gmail_draft_update("personal", "draft1", source,
                                                           ["target@example.com"])["id"], "draft1")
                self.assertEqual(google.gmail_draft_send("personal", "draft1",
                                                         ["target@example.com"])["id"], "message2")
                with self.assertRaisesRegex(google.GoogleError, "already attempted"):
                    google.gmail_draft_send("personal", "draft1", ["target@example.com"])
        self.assertEqual([call[1] for call in calls],
                         [google.GMAIL_DRAFTS, google.GMAIL_DRAFTS + "/draft1",
                          google.GMAIL_DRAFTS + "/send"])

    def test_draft_send_uses_validated_mime_after_concurrent_replacement(self):
        approved = (b"From: personal@example.com\r\nTo: target@example.com\r\n"
                    b"Subject: approved\r\n\r\napproved body")
        replacement = (b"From: personal@example.com\r\nTo: other@example.com\r\n"
                       b"Subject: replaced\r\n\r\nunapproved body")
        drafts = {"draft1": approved}
        sent = []

        def fake_get(_credentials, url, *, params=None):
            if url == google.GMAIL_DRAFTS + "/draft1":
                snapshot = drafts["draft1"]
                drafts["draft1"] = replacement
                return {"id": "draft1", "message": {
                    "raw": base64.urlsafe_b64encode(snapshot).decode("ascii")}}
            self.assertEqual(url, google.GMAIL_MESSAGES + "/message2")
            return {"id": "message2", "threadId": "thread1", "labelIds": ["SENT"]}

        def fake_write(_credentials, method, url, body, **kwargs):
            self.assertEqual((method, url), ("POST", google.GMAIL_DRAFTS + "/send"))
            self.assertEqual(body["id"], "draft1")
            self.assertEqual(drafts["draft1"], replacement)
            encoded = body.get("message", {}).get("raw")
            if encoded is not None:
                drafts[body["id"]] = base64.urlsafe_b64decode(encoded)
            sent.append(drafts.pop(body["id"]))
            return {"id": "message2", "threadId": "thread1"}

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(google, "_credentials", return_value=(object(), "personal@example.com")), \
                 patch.object(google, "_lock", return_value=nullcontext()), \
                 patch.object(google, "LOCKS", Path(directory)), \
                 patch.object(google, "_gmail_write", side_effect=fake_write), \
                 patch.object(google, "_gmail_get", side_effect=fake_get):
                self.assertEqual(
                    google.gmail_draft_send("personal", "draft1", ["target@example.com"]),
                    {"id": "message2", "thread_id": "thread1"})
        self.assertEqual(sent, [approved])

    def test_label_modification_and_trash_are_distinct_exact_operations(self):
        calls = []

        def fake_write(_credentials, method, url, body):
            calls.append((method, url, body))
            return {"id": "msg1", "threadId": "thread1", "labelIds": ["INBOX", "Label_1"]}

        with patch.object(google, "_credentials", return_value=(object(), "work@example.com")), \
             patch.object(google, "_gmail_write", side_effect=fake_write):
            with self.assertRaisesRegex(google.GoogleError, "dedicated workflow"):
                google.gmail_modify("work", "msg1", add_labels=["TRASH"])
            with self.assertRaisesRegex(google.GoogleError, "nonconflicting"):
                google.gmail_modify("work", "msg1", add_labels=["INBOX"], remove_labels=["INBOX"])
            self.assertFalse(google.gmail_modify("work", "msg1", add_labels=["Label_1"],
                                                 remove_labels=["UNREAD"])["unread"])
            google.gmail_trash("work", "msg1")
            google.gmail_untrash("work", "msg1")
        self.assertEqual(calls, [
            ("POST", google.GMAIL_MESSAGES + "/msg1/modify",
             {"addLabelIds": ["Label_1"], "removeLabelIds": ["UNREAD"]}),
            ("POST", google.GMAIL_MESSAGES + "/msg1/trash", {}),
            ("POST", google.GMAIL_MESSAGES + "/msg1/untrash", {})])

    def test_gmail_api_failure_does_not_expose_response_body(self):
        class Response:
            status_code = 403
            text = "private message body and OAuth credential"

        class Session:
            def __init__(self, _credentials):
                pass

            def request(self, *args, **kwargs):
                return Response()

        with patch.object(google, "AuthorizedSession", Session):
            with self.assertRaises(google.GoogleError) as failure:
                google._gmail_write(object(), "POST", google.GMAIL_MESSAGES + "/send",
                                    {"raw": "private message body"})
        self.assertNotIn("private message body", str(failure.exception))
        self.assertNotIn("OAuth credential", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
