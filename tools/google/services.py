"""Explicit-profile, exact-target Google Workspace operations.

No service is built and no account content is accessed until a function is called.
Callers supply exact resource IDs, bounded ranges/windows, and API request bodies.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import os
from pathlib import Path
import re
import stat
import tempfile

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from tools.google.client import EMAIL, GoogleError, credentials_for


_DRIVE_FIELDS = "id,name,mimeType,parents,trashed,webViewLink,modifiedTime,size,description"
_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
_SEND_UPDATES = ("all", "externalOnly", "none")


def _id(value: str, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value) or len(value) > 512:
        raise GoogleError(f"{label} must be an explicit resource ID")
    return value


def _calendar_id(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 512
            or any(ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in value)):
        raise GoogleError("calendar ID must be an explicit calendar ID")
    return value


def _text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise GoogleError(f"{label} must be nonempty text (at most 1024 characters)")
    return value


def _body(value: dict, label: str) -> dict:
    if not isinstance(value, dict) or not value:
        raise GoogleError(f"{label} must be a nonempty object")
    return value


def _requests(value: list[dict]) -> list[dict]:
    if (not isinstance(value, list) or not value or len(value) > 100
            or any(not isinstance(item, dict) or len(item) != 1 for item in value)):
        raise GoogleError("requests must be 1–100 precise API request objects")
    return value


def _range(value: str) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > 512
            or any(ord(c) < 32 for c in value)):
        raise GoogleError("A1 range must be explicit")
    return value


def _window(time_min: str, time_max: str) -> None:
    try:
        start = datetime.fromisoformat(time_min.replace("Z", "+00:00"))
        end = datetime.fromisoformat(time_max.replace("Z", "+00:00"))
        if (start.utcoffset() is None or end.utcoffset() is None
                or not start < end <= start + timedelta(days=31)):
            raise ValueError
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise GoogleError("time window requires timezone-aware ISO timestamps, increasing by at most 31 days") from None


def _limit(maximum: int) -> int:
    if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= 100:
        raise GoogleError("maximum must be between 1 and 100")
    return maximum


def _service(account: str, api: str, version: str):
    # credentials_for verifies the selected profile, granted scopes and Drive identity.
    credentials = credentials_for(account)
    try:
        return build(api, version, credentials=credentials, cache_discovery=False)
    except Exception:
        raise GoogleError(f"{api} service initialization failed (details withheld)") from None


def _execute(request, api: str):
    try:
        return request.execute()
    except Exception:
        # HttpError's string/response body can include private document or mail content.
        raise GoogleError(f"{api} request failed (response details withheld)") from None


def drive_get(account: str, file_id: str) -> dict:
    _id(file_id, "file ID")
    service = _service(account, "drive", "v3")
    return _execute(service.files().get(fileId=file_id, fields=_DRIVE_FIELDS,
                                        supportsAllDrives=True), "Drive")


def drive_download(account: str, file_id: str, destination: str | Path, *,
                   export_mime_type: str | None = None) -> dict:
    """Store an exact file privately; native Workspace files need an export MIME type."""
    _id(file_id, "file ID")
    if export_mime_type is not None:
        _text(export_mime_type, "export MIME type")
    if not isinstance(destination, (str, Path)) or not str(destination):
        raise GoogleError("destination must be an explicit file path")
    try:
        target = Path(destination)
        directory = target.parent
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise GoogleError("destination parent must be an existing private owner-owned directory")
        if target.is_symlink() or target.exists():
            raise GoogleError("destination already exists; refusing overwrite")
    except (OSError, ValueError, TypeError):
        raise GoogleError("destination must be a usable private path") from None
    service = _service(account, "drive", "v3")
    metadata = _execute(service.files().get(fileId=file_id, fields="id,mimeType",
                                             supportsAllDrives=True), "Drive")
    if metadata.get("id") != file_id:
        raise GoogleError("Drive returned an invalid file identifier")
    mime_type = metadata.get("mimeType")
    if not isinstance(mime_type, str) or not mime_type:
        raise GoogleError("Drive returned invalid file metadata")
    native = mime_type.startswith("application/vnd.google-apps.")
    if native and export_mime_type is None:
        raise GoogleError("native Workspace file requires an explicit export MIME type")
    if not native and export_mime_type is not None:
        raise GoogleError("export MIME type is only valid for native Workspace files")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=directory, prefix=".maven-drive-",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            try:
                request = (service.files().export_media(fileId=file_id, mimeType=export_mime_type)
                           if native else service.files().get_media(fileId=file_id,
                                                                    supportsAllDrives=True))
                downloader = MediaIoBaseDownload(stream, request, chunksize=4 * 1024 * 1024)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
            except Exception:
                raise GoogleError("Drive download failed (response details withheld)") from None
            count = stream.tell()
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic no-replace install: a concurrent creator or symlink wins; never overwrite it.
        os.link(temporary, target, follow_symlinks=False)
        return {"id": file_id, "path": str(target), "bytes": count}
    except FileExistsError:
        raise GoogleError("destination already exists; refusing overwrite") from None
    except OSError:
        raise GoogleError("could not store Drive file privately") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def drive_list_folder(account: str, folder_id: str, maximum: int = 100,
                      page_token: str | None = None) -> dict:
    _id(folder_id, "folder ID")
    _limit(maximum)
    if page_token is not None:
        _text(page_token, "page token")
    service = _service(account, "drive", "v3")
    return _execute(service.files().list(q=f"'{folder_id}' in parents and trashed = false",
                                         fields=f"nextPageToken,files({_DRIVE_FIELDS})",
                                         pageSize=maximum, pageToken=page_token,
                                         supportsAllDrives=True, includeItemsFromAllDrives=True), "Drive")


def drive_create(account: str, name: str, parent_id: str, *, mime_type: str | None = None,
                 upload_path: str | Path | None = None) -> dict:
    _text(name, "file name")
    _id(parent_id, "parent folder ID")
    if mime_type is not None:
        _text(mime_type, "MIME type")
    if upload_path is None and mime_type is None:
        raise GoogleError("specify MIME type for a new file or an upload path")
    service = _service(account, "drive", "v3")
    media = _media(upload_path, mime_type) if upload_path is not None else None
    result = _execute(service.files().create(body={"name": name, "parents": [parent_id],
                                                    **({"mimeType": mime_type} if mime_type else {})},
                                             media_body=media, fields="id", supportsAllDrives=True), "Drive")
    return drive_get(account, _id(result.get("id"), "created file ID"))


def _media(path: str | Path, mime_type: str | None):
    if not isinstance(path, (str, Path)) or not str(path):
        raise GoogleError("upload path must be explicit")
    try:
        return MediaFileUpload(str(path), mimetype=mime_type, chunksize=4 * 1024 * 1024,
                               resumable=True)
    except (OSError, ValueError, TypeError):
        raise GoogleError("upload file unavailable (details withheld)") from None


def drive_update(account: str, file_id: str, *, name: str | None = None,
                 description: str | None = None, upload_path: str | Path | None = None,
                 mime_type: str | None = None) -> dict:
    _id(file_id, "file ID")
    if name is not None:
        _text(name, "file name")
    if description is not None and (not isinstance(description, str) or len(description) > 4096):
        raise GoogleError("description must be at most 4096 characters")
    if mime_type is not None:
        _text(mime_type, "MIME type")
        if upload_path is None:
            raise GoogleError("MIME type requires an upload path")
    if name is None and description is None and upload_path is None:
        raise GoogleError("specify a file change or upload path")
    service = _service(account, "drive", "v3")
    media = _media(upload_path, mime_type) if upload_path is not None else None
    body = {key: value for key, value in (("name", name), ("description", description)) if value is not None}
    _execute(service.files().update(fileId=file_id, body=body, media_body=media,
                                    fields="id", supportsAllDrives=True), "Drive")
    return drive_get(account, file_id)


def drive_move(account: str, file_id: str, destination_folder_id: str) -> dict:
    _id(file_id, "file ID")
    _id(destination_folder_id, "destination folder ID")
    service = _service(account, "drive", "v3")
    current = _execute(service.files().get(fileId=file_id, fields="id,parents",
                                           supportsAllDrives=True), "Drive")
    parents = current.get("parents", [])
    if destination_folder_id in parents:
        return drive_get(account, file_id)
    options = {"fileId": file_id, "addParents": destination_folder_id,
               "fields": "id", "supportsAllDrives": True}
    if parents:
        options["removeParents"] = ",".join(parents)
    _execute(service.files().update(**options), "Drive")
    return drive_get(account, file_id)


def drive_share(account: str, file_id: str, email: str, role: str, *,
                send_notification_email: bool) -> dict:
    _id(file_id, "file ID")
    if not isinstance(email, str) or len(email) > 254 or not EMAIL.fullmatch(email):
        raise GoogleError("recipient email must be explicit")
    if role not in ("reader", "commenter", "writer"):
        raise GoogleError("role must be reader, commenter or writer")
    if not isinstance(send_notification_email, bool):
        raise GoogleError("send_notification_email must be explicit boolean")
    service = _service(account, "drive", "v3")
    result = _execute(service.permissions().create(fileId=file_id,
                     body={"type": "user", "role": role, "emailAddress": email},
                     sendNotificationEmail=send_notification_email,
                     fields="id,role,type,emailAddress", supportsAllDrives=True), "Drive")
    return {"file": drive_get(account, file_id), "permission": result}


def drive_trash(account: str, file_id: str) -> dict:
    _id(file_id, "file ID")
    service = _service(account, "drive", "v3")
    _execute(service.files().update(fileId=file_id, body={"trashed": True},
                                    fields="id", supportsAllDrives=True), "Drive")
    return drive_get(account, file_id)


def drive_delete_permanently(account: str, file_id: str) -> dict:
    """Irreversible deletion; never called by drive_trash."""
    _id(file_id, "file ID")
    service = _service(account, "drive", "v3")
    _execute(service.files().delete(fileId=file_id, supportsAllDrives=True), "Drive")
    return {"id": file_id, "deleted": True}


def docs_get(account: str, document_id: str) -> dict:
    _id(document_id, "document ID")
    service = _service(account, "docs", "v1")
    return _execute(service.documents().get(documentId=document_id,
                                            includeTabsContent=True), "Docs")


def docs_create(account: str, title: str) -> dict:
    _text(title, "document title")
    service = _service(account, "docs", "v1")
    result = _execute(service.documents().create(body={"title": title}), "Docs")
    return docs_get(account, _id(result.get("documentId"), "created document ID"))


def docs_update(account: str, document_id: str, revision_id: str,
                requests: list[dict]) -> dict:
    _id(document_id, "document ID")
    _text(revision_id, "required revision ID")
    _requests(requests)
    service = _service(account, "docs", "v1")
    _execute(service.documents().batchUpdate(documentId=document_id,
                body={"requests": requests, "writeControl": {"requiredRevisionId": revision_id}}), "Docs")
    return docs_get(account, document_id)


def sheets_get(account: str, spreadsheet_id: str) -> dict:
    _id(spreadsheet_id, "spreadsheet ID")
    service = _service(account, "sheets", "v4")
    return _execute(service.spreadsheets().get(spreadsheetId=spreadsheet_id,
                              fields="spreadsheetId,spreadsheetUrl,properties,sheets(properties)"), "Sheets")


def sheets_create(account: str, title: str) -> dict:
    _text(title, "spreadsheet title")
    service = _service(account, "sheets", "v4")
    result = _execute(service.spreadsheets().create(body={"properties": {"title": title}},
                       fields="spreadsheetId"), "Sheets")
    return sheets_get(account, _id(result.get("spreadsheetId"), "created spreadsheet ID"))


def sheets_values_get(account: str, spreadsheet_id: str, a1_range: str) -> dict:
    _id(spreadsheet_id, "spreadsheet ID")
    _range(a1_range)
    service = _service(account, "sheets", "v4")
    return _execute(service.spreadsheets().values().get(spreadsheetId=spreadsheet_id,
                                                         range=a1_range), "Sheets")


def sheets_values_update(account: str, spreadsheet_id: str, a1_range: str,
                         values: list[list], *, input_option: str = "RAW") -> dict:
    _id(spreadsheet_id, "spreadsheet ID")
    _range(a1_range)
    if (not isinstance(values, list) or not values or len(values) > 1000
            or any(not isinstance(row, list) or len(row) > 1000 for row in values)):
        raise GoogleError("values must be a nonempty, bounded matrix")
    if input_option not in ("RAW", "USER_ENTERED"):
        raise GoogleError("input_option must be RAW or USER_ENTERED")
    service = _service(account, "sheets", "v4")
    _execute(service.spreadsheets().values().update(spreadsheetId=spreadsheet_id,
                    range=a1_range, valueInputOption=input_option,
                    body={"range": a1_range, "majorDimension": "ROWS", "values": values}), "Sheets")
    return sheets_values_get(account, spreadsheet_id, a1_range)


def sheets_batch_update(account: str, spreadsheet_id: str, requests: list[dict]) -> dict:
    _id(spreadsheet_id, "spreadsheet ID")
    _requests(requests)
    service = _service(account, "sheets", "v4")
    _execute(service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id,
                                                 body={"requests": requests}), "Sheets")
    return sheets_get(account, spreadsheet_id)


def slides_get(account: str, presentation_id: str) -> dict:
    _id(presentation_id, "presentation ID")
    service = _service(account, "slides", "v1")
    return _execute(service.presentations().get(presentationId=presentation_id), "Slides")


def slides_create(account: str, title: str) -> dict:
    _text(title, "presentation title")
    service = _service(account, "slides", "v1")
    result = _execute(service.presentations().create(body={"title": title}), "Slides")
    return slides_get(account, _id(result.get("presentationId"), "created presentation ID"))


def slides_update(account: str, presentation_id: str, revision_id: str,
                  requests: list[dict]) -> dict:
    _id(presentation_id, "presentation ID")
    _text(revision_id, "required revision ID")
    _requests(requests)
    service = _service(account, "slides", "v1")
    _execute(service.presentations().batchUpdate(presentationId=presentation_id,
               body={"requests": requests, "writeControl": {"requiredRevisionId": revision_id}}), "Slides")
    return slides_get(account, presentation_id)


def _notifications(send_updates: str) -> str:
    if send_updates not in _SEND_UPDATES:
        raise GoogleError("send_updates must explicitly be all, externalOnly or none")
    return send_updates


def calendar_freebusy(account: str, calendar_ids: list[str],
                      time_min: str, time_max: str) -> dict:
    if not isinstance(calendar_ids, list) or not 1 <= len(calendar_ids) <= 10:
        raise GoogleError("select 1–10 explicit calendar IDs")
    for calendar_id in calendar_ids:
        _calendar_id(calendar_id)
    _window(time_min, time_max)
    service = _service(account, "calendar", "v3")
    return _execute(service.freebusy().query(body={"timeMin": time_min, "timeMax": time_max,
                        "items": [{"id": cid} for cid in calendar_ids]}), "Calendar")


def calendar_event_get(account: str, calendar_id: str, event_id: str) -> dict:
    _calendar_id(calendar_id)
    _id(event_id, "event ID")
    service = _service(account, "calendar", "v3")
    return _execute(service.events().get(calendarId=calendar_id, eventId=event_id), "Calendar")


def calendar_events_list(account: str, calendar_id: str, time_min: str,
                         time_max: str, maximum: int = 100,
                         page_token: str | None = None) -> dict:
    _calendar_id(calendar_id)
    _window(time_min, time_max)
    _limit(maximum)
    if page_token is not None:
        _text(page_token, "page token")
    service = _service(account, "calendar", "v3")
    return _execute(service.events().list(calendarId=calendar_id,
                    timeMin=time_min, timeMax=time_max, maxResults=maximum,
                    pageToken=page_token, singleEvents=True, orderBy="startTime"), "Calendar")


def calendar_event_create(account: str, calendar_id: str, event: dict,
                          *, send_updates: str) -> dict:
    _calendar_id(calendar_id)
    _body(event, "event")
    _notifications(send_updates)
    service = _service(account, "calendar", "v3")
    result = _execute(service.events().insert(calendarId=calendar_id, body=event,
                                               sendUpdates=send_updates), "Calendar")
    return calendar_event_get(account, calendar_id, _id(result.get("id"), "created event ID"))


def calendar_event_patch(account: str, calendar_id: str, event_id: str,
                         changes: dict, *, send_updates: str) -> dict:
    _calendar_id(calendar_id)
    _id(event_id, "event ID")
    _body(changes, "event changes")
    _notifications(send_updates)
    service = _service(account, "calendar", "v3")
    _execute(service.events().patch(calendarId=calendar_id, eventId=event_id,
                                     body=changes, sendUpdates=send_updates), "Calendar")
    return calendar_event_get(account, calendar_id, event_id)


def calendar_event_delete(account: str, calendar_id: str, event_id: str,
                          *, send_updates: str) -> dict:
    _calendar_id(calendar_id)
    _id(event_id, "event ID")
    _notifications(send_updates)
    service = _service(account, "calendar", "v3")
    _execute(service.events().delete(calendarId=calendar_id, eventId=event_id,
                                      sendUpdates=send_updates), "Calendar")
    return {"calendarId": calendar_id, "id": event_id, "deleted": True}
