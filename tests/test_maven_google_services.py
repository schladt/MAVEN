"""Offline, exact-target contracts for the directed Workspace service layer."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from tools.google import services
    from tools.google.client import GoogleError
except ModuleNotFoundError as error:
    if error.name and (error.name == "requests" or error.name.startswith(("google", "google_auth_oauthlib"))):
        raise unittest.SkipTest("optional Google dependencies are not installed") from error
    raise


class Request:
    def __init__(self, result):
        self.result = result

    def execute(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Resource:
    def __init__(self, fake, api, path):
        self.fake, self.api, self.path = fake, api, path

    def __getattr__(self, name):
        if name in ("files", "permissions", "documents", "spreadsheets", "values",
                    "presentations", "events", "freebusy"):
            return lambda: Resource(self.fake, self.api, self.path + (name,))

        def call(**kwargs):
            self.fake.calls.append((self.api, self.path, name, kwargs))
            result = self.fake.results.get((self.api, self.path, name), {})
            return Request(result)
        return call


class FakeDiscovery:
    def __init__(self):
        self.calls = []
        self.builds = []
        self.results = {
            ("drive", ("files",), "get"): {"id": "file_1", "parents": ["old_folder"]},
            ("drive", ("files",), "create"): {"id": "created_1"},
            ("drive", ("permissions",), "create"): {"id": "permission_1"},
            ("docs", ("documents",), "get"): {"documentId": "doc_1", "revisionId": "rev_2"},
            ("docs", ("documents",), "create"): {"documentId": "doc_1"},
            ("sheets", ("spreadsheets",), "get"): {"spreadsheetId": "sheet_1"},
            ("sheets", ("spreadsheets",), "create"): {"spreadsheetId": "sheet_1"},
            ("sheets", ("spreadsheets", "values"), "get"): {"range": "A1:B2", "values": [["a"]]},
            ("slides", ("presentations",), "get"): {"presentationId": "slide_1", "revisionId": "rev_3"},
            ("slides", ("presentations",), "create"): {"presentationId": "slide_1"},
            ("calendar", ("events",), "get"): {"id": "event_1"},
            ("calendar", ("events",), "insert"): {"id": "event_1"},
        }

    def build(self, api, version, *, credentials, cache_discovery):
        self.builds.append((api, version, credentials, cache_discovery))
        return Resource(self, api, ())

    def calls_for(self, api, method):
        return [kwargs for called_api, _, called_method, kwargs in self.calls
                if (called_api, called_method) == (api, method)]


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDiscovery()
        self.credential = object()
        self.auth = patch.object(services, "credentials_for", return_value=self.credential)
        self.build = patch.object(services, "build", side_effect=self.fake.build)
        self.auth.start()
        self.build.start()
        self.addCleanup(self.auth.stop)
        self.addCleanup(self.build.stop)

    def test_wrong_targets_fail_before_auth_or_network(self):
        with self.assertRaises(GoogleError):
            services.drive_trash("work", "")
        with self.assertRaises(GoogleError):
            services.docs_update("work", "doc_1", "", [{"insertText": {"text": "hello"}}])
        with self.assertRaises(GoogleError):
            services.sheets_values_update("work", "sheet_1", "", [["data"]])
        with self.assertRaises(GoogleError):
            services.calendar_events_list("work", "primary", "2026-11-01T00:00:00Z",
                                           "2026-12-03T00:00:00Z")
        with self.assertRaises(GoogleError):
            services.calendar_event_get("work", "", "event_1")
        self.assertFalse(self.fake.calls)
        self.assertFalse(self.fake.builds)

    def test_docs_revision_is_required_and_readback_retains_revision(self):
        requests = [{"insertText": {"endOfSegmentLocation": {}, "text": "hello"}}]
        result = services.docs_update("personal", "doc_1", "rev_1", requests)
        update = self.fake.calls_for("docs", "batchUpdate")[0]
        self.assertEqual(update, {"documentId": "doc_1", "body": {
            "requests": requests, "writeControl": {"requiredRevisionId": "rev_1"}}})
        self.assertEqual(result["revisionId"], "rev_2")
        self.assertEqual(self.fake.calls_for("docs", "get")[0]["documentId"], "doc_1")
        self.assertTrue(self.fake.calls_for("docs", "get")[0]["includeTabsContent"])

    def test_slides_revision_guard_and_reread(self):
        requests = [{"createSlide": {"objectId": "new_slide"}}]
        result = services.slides_update("work", "slide_1", "rev_2", requests)
        self.assertEqual(self.fake.calls_for("slides", "batchUpdate")[0], {
            "presentationId": "slide_1", "body": {"requests": requests,
            "writeControl": {"requiredRevisionId": "rev_2"}}})
        self.assertEqual(result["revisionId"], "rev_3")
        with self.assertRaises(GoogleError):
            services.slides_update("work", "slide_1", "", requests)

    def test_calendar_notifications_are_explicit_for_every_mutation(self):
        event = {"summary": "team", "attendees": [{"email": "team@example.com"}],
                 "start": {"dateTime": "2026-11-02T10:00:00Z"},
                 "end": {"dateTime": "2026-11-02T11:00:00Z"}}
        with self.assertRaises(TypeError):
            services.calendar_event_create("work", "primary", event)
        with self.assertRaises(TypeError):
            services.calendar_event_patch("work", "primary", "event_1", {"summary": "new"})
        with self.assertRaises(TypeError):
            services.calendar_event_delete("work", "primary", "event_1")
        with self.assertRaises(GoogleError):
            services.calendar_event_create("work", "primary", event, send_updates="maybe")
        self.assertFalse(self.fake.calls)
        services.calendar_event_create("work", "primary", event, send_updates="all")
        services.calendar_event_patch("work", "primary", "event_1", {"summary": "new"},
                                      send_updates="externalOnly")
        services.calendar_event_delete("work", "primary", "event_1", send_updates="none")
        for method, policy in (("insert", "all"), ("patch", "externalOnly"), ("delete", "none")):
            call = self.fake.calls_for("calendar", method)[0]
            self.assertEqual(call["calendarId"], "primary")
            self.assertEqual(call["sendUpdates"], policy)
        self.assertEqual(self.fake.calls_for("calendar", "get")[-1]["eventId"], "event_1")

    def test_exact_drive_targets_and_safe_delete_boundary(self):
        services.drive_move("work", "file_1", "new_folder")
        update = self.fake.calls_for("drive", "update")[0]
        self.assertEqual((update["fileId"], update["addParents"], update["removeParents"]),
                         ("file_1", "new_folder", "old_folder"))
        with self.assertRaises(TypeError):
            services.drive_share("work", "file_1", "other@example.com", "reader")
        services.drive_share("work", "file_1", "other@example.com", "reader",
                             send_notification_email=False)
        self.assertFalse(self.fake.calls_for("drive", "create")[-1]["sendNotificationEmail"])
        services.drive_trash("work", "file_1")
        self.assertEqual(self.fake.calls_for("drive", "update")[-1]["body"], {"trashed": True})
        self.assertFalse(self.fake.calls_for("drive", "delete"))
        services.drive_delete_permanently("work", "file_1")
        self.assertEqual(self.fake.calls_for("drive", "delete")[0]["fileId"], "file_1")
        listing = services.drive_list_folder("work", "new_folder", 7)
        self.assertIsInstance(listing, dict)
        self.assertEqual(self.fake.calls_for("drive", "list")[0]["q"],
                         "'new_folder' in parents and trashed = false")

    def test_download_refuses_overwrite_and_routes_export_or_media(self):
        class Downloader:
            def __init__(self, stream, request, *, chunksize):
                self.stream = stream
                self.request = request

            def next_chunk(self):
                self.stream.write(b"synthetic data")
                return None, True

        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            regular = private / "regular.bin"
            regular.write_bytes(b"existing")
            with patch.object(services, "MediaIoBaseDownload", Downloader):
                with self.assertRaisesRegex(GoogleError, "already exists"):
                    services.drive_download("work", "file_1", regular)
                self.assertEqual(regular.read_bytes(), b"existing")
                self.assertFalse(self.fake.calls)
                regular.unlink()
                self.fake.results[("drive", ("files",), "get")] = {
                    "id": "file_1", "mimeType": "application/octet-stream"}
                saved = services.drive_download("work", "file_1", regular)
                self.assertEqual(saved, {"id": "file_1", "path": str(regular), "bytes": 14})
                self.assertEqual(regular.read_bytes(), b"synthetic data")
                self.assertEqual(self.fake.calls_for("drive", "get_media")[0]["fileId"], "file_1")
                self.fake.results[("drive", ("files",), "get")] = {
                    "id": "file_1", "mimeType": "application/vnd.google-apps.document"}
                exported = private / "export.pdf"
                with self.assertRaisesRegex(GoogleError, "requires an explicit export"):
                    services.drive_download("work", "file_1", exported)
                with self.assertRaisesRegex(GoogleError, "only valid for native"):
                    self.fake.results[("drive", ("files",), "get")]["mimeType"] = "text/plain"
                    services.drive_download("work", "file_1", exported,
                                            export_mime_type="application/pdf")
                self.fake.results[("drive", ("files",), "get")]["mimeType"] = (
                    "application/vnd.google-apps.document")
                services.drive_download("work", "file_1", exported,
                                        export_mime_type="application/pdf")
                self.assertEqual(self.fake.calls_for("drive", "export_media")[0],
                                 {"fileId": "file_1", "mimeType": "application/pdf"})
                self.assertEqual(exported.read_bytes(), b"synthetic data")
                link = private / "link.pdf"
                link.symlink_to(exported)
                with self.assertRaisesRegex(GoogleError, "already exists"):
                    services.drive_download("work", "file_1", link,
                                            export_mime_type="application/pdf")

    def test_service_selection_and_structural_sheet_requests(self):
        services.drive_get("personal", "file_1")
        services.docs_create("personal", "Note")
        services.sheets_create("personal", "Budget")
        services.sheets_values_update("personal", "sheet_1", "A1:B2", [["a", 3]])
        requests = [{"addSheet": {"properties": {"title": "Costs"}}}]
        services.sheets_batch_update("personal", "sheet_1", requests)
        services.slides_create("personal", "Slides")
        services.calendar_freebusy("personal", ["primary"],
                                    "2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z")
        services.calendar_events_list("personal", "primary", "2026-11-01T00:00:00Z",
                                       "2026-11-02T00:00:00Z", maximum=12)
        self.assertEqual({api for api, _, _, _ in self.fake.builds},
                         {"drive", "docs", "sheets", "slides", "calendar"})
        self.assertTrue(all(credentials is self.credential and cache is False
                            for _, _, credentials, cache in self.fake.builds))
        self.assertEqual(self.fake.calls_for("sheets", "batchUpdate")[0]["body"],
                         {"requests": requests})
        self.assertEqual(self.fake.calls_for("calendar", "list")[0]["maxResults"], 12)
        self.assertEqual(self.fake.calls_for("calendar", "query")[0]["body"]["items"],
                         [{"id": "primary"}])

    def test_api_error_never_exposes_response_body(self):
        self.fake.results[("docs", ("documents",), "get")] = RuntimeError("private document content")
        with self.assertRaises(GoogleError) as raised:
            services.docs_get("work", "doc_1")
        self.assertNotIn("private document content", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
