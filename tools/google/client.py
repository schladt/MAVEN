"""MAVEN-owned, explicit-profile Google OAuth and narrowly directed Gmail reads."""

from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import getaddresses
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import quote, urlsplit

from botocore.exceptions import BotoCoreError, ClientError, ProfileNotFound
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
import requests

from tools.aws.settings import load_settings, prefix, required
from tools.memory.environment import MemoryClientError, selected_aws_session
from tools.project import instance_home


LOCKS = instance_home() / "runtime/google-auth-locks"
SCOPES = (
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
)
ACCOUNTS = ("work", "personal")
MAX_BYTES = 4096
EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\Z")
MAX_MIME_BYTES = 25 * 1024 * 1024
MESSAGE_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
DRIVE_ABOUT = "https://www.googleapis.com/drive/v3/about"
GMAIL_MESSAGES = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
GMAIL_DRAFTS = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"


class GoogleError(Exception):
    """Only non-secret, non-response-body diagnostic text is carried here."""


def _account(account: str) -> str:
    if account not in ACCOUNTS:
        raise GoogleError("select exactly one account: work or personal")
    return account


def _name(account: str, kind: str) -> str:
    account = _account(account)
    if kind not in ("client", "refresh"):
        raise GoogleError("invalid credential kind")
    try:
        return prefix(load_settings(), "MAVEN_GOOGLE_PARAMETER_PREFIX") + f"google-{account}-{kind}"
    except MemoryClientError:
        raise GoogleError("Google credential namespace is not configured in private settings") from None


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise GoogleError("local AWS/auth runtime directory must be private to the current user")


def _ssm():
    try:
        values = load_settings()
        return selected_aws_session(
            values, region=required(values, "MAVEN_AWS_REGION")).client("ssm")
    except (MemoryClientError, BotoCoreError, ProfileNotFound, OSError):
        raise GoogleError("AWS login unavailable; configure and log in to the explicitly selected profile") from None


def _aws_error(exc: ClientError) -> GoogleError:
    code = exc.response.get("Error", {}).get("Code", "")
    if code == "ParameterAlreadyExists":
        return GoogleError("account credential already exists; use explicit reauthorization if appropriate")
    if code in ("AccessDeniedException", "AccessDenied"):
        return GoogleError("AWS denied access to the selected MAVEN credential")
    return GoogleError("AWS credential request failed (response details withheld)")


def _read_parameter(ssm, name: str):
    try:
        item = ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            return None
        raise _aws_error(exc) from None
    except BotoCoreError:
        raise GoogleError("AWS credential request failed (response details withheld)") from None
    if item.get("Type") != "SecureString":
        raise GoogleError("selected credential is not a SecureString")
    return item


def _record(item, description: str) -> dict:
    if item is None:
        raise GoogleError(f"{description} is not imported or authorized")
    try:
        value = json.loads(item["Value"])
    except (KeyError, TypeError, ValueError):
        raise GoogleError(f"{description} is invalid") from None
    if not isinstance(value, dict):
        raise GoogleError(f"{description} is invalid")
    return value


def _encode(record: dict) -> str:
    value = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
    if len(value.encode("utf-8")) > MAX_BYTES:
        raise GoogleError("credential exceeds the 4 KB Standard parameter limit")
    return value


def _put(ssm, name: str, record: dict, *, overwrite: bool) -> None:
    try:
        ssm.put_parameter(Name=name, Value=_encode(record), Type="SecureString",
                          KeyId="alias/aws/ssm", Tier="Standard", Overwrite=overwrite)
    except ClientError as exc:
        raise _aws_error(exc) from None
    except BotoCoreError:
        raise GoogleError("AWS credential request failed (response details withheld)") from None


def _expected_email(value: str) -> str:
    if not isinstance(value, str) or not EMAIL.fullmatch(value) or len(value) > 254:
        raise GoogleError("expected email must be a valid single account address")
    return value.casefold()


def _client(record: dict) -> tuple[dict, str]:
    try:
        expected = _expected_email(record["expected_email"])
        installed = record["installed"]
        if not isinstance(installed, dict):
            raise ValueError
        for key in ("client_id", "client_secret"):
            if not isinstance(installed[key], str) or not installed[key]:
                raise ValueError
        if installed["auth_uri"] not in (
            "https://accounts.google.com/o/oauth2/auth",
            "https://accounts.google.com/o/oauth2/v2/auth",
        ) or installed["token_uri"] != "https://oauth2.googleapis.com/token":
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise GoogleError("selected OAuth Desktop client record is invalid") from None
    return {"installed": installed}, expected


def _refresh(record: dict, expected: str) -> str:
    scopes = record.get("scopes")
    if (record.get("expected_email") != expected or
            not isinstance(record.get("refresh_token"), str) or
            not record["refresh_token"] or
            not isinstance(scopes, list) or
            not all(isinstance(scope, str) for scope in scopes) or
            set(scopes) != set(SCOPES)):
        raise GoogleError("selected refresh credential identity or scopes are invalid")
    return record["refresh_token"]


def _check_scopes(granted, *, initial: bool) -> None:
    # Google may omit scope on a refresh response when the scope set is unchanged;
    # the persisted scope set was verified from the original token response.
    if granted is None and not initial:
        return
    if isinstance(granted, str):
        granted = granted.split()
    if not isinstance(granted, (list, tuple, set, frozenset)) or not set(SCOPES).issubset(set(granted)):
        raise GoogleError("Google did not grant all required scopes for this account")


def _identity(credentials: Credentials, expected: str) -> str:
    try:
        response = AuthorizedSession(credentials).get(DRIVE_ABOUT,
                                                         params={"fields": "user(emailAddress)"},
                                                         timeout=30)
        if response.status_code != 200:
            raise GoogleError(f"Google Drive identity verification failed (HTTP {response.status_code})")
        email = response.json()["user"]["emailAddress"]
        if _expected_email(email) != expected:
            raise GoogleError("Google returned a different account; credential not stored or used")
        return email
    except (requests.RequestException, ValueError, KeyError, TypeError, GoogleAuthError):
        raise GoogleError("Google Drive identity verification failed") from None


@contextmanager
def _lock(account: str):
    try:
        _private_dir(LOCKS)
        path = LOCKS / f"{_account(account)}.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise GoogleError("could not open private account authorization lock") from None
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise GoogleError("account authorization lock must be private")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    except OSError:
        raise GoogleError("could not use private account authorization lock") from None
    finally:
        os.close(fd)


def _loopback(uri: str) -> bool:
    if not isinstance(uri, str):
        return False
    parsed = urlsplit(uri)
    return (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")
            and parsed.username is None and parsed.password is None)


def import_client(account: str, source: str | Path, expected_email: str) -> str:
    """Import an owner-provided private Desktop client file, never a Magic token."""
    _account(account)
    expected = _expected_email(expected_email)
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise GoogleError("OAuth client source must be an owner-owned regular file")
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise GoogleError("OAuth client file exceeds the 4 KB Standard parameter limit")
        payload = json.loads(raw)
        if set(payload) != {"installed"}:
            raise GoogleError("source must contain only a Desktop OAuth installed client")
        installed = payload["installed"]
        fields = ("client_id", "client_secret", "auth_uri", "token_uri", "redirect_uris", "project_id")
        record = {"expected_email": expected, "installed": {key: installed[key] for key in fields if key in installed}}
        _client(record)
        redirects = record["installed"].get("redirect_uris")
        if not isinstance(redirects, list) or not redirects or not all(
            _loopback(uri) for uri in redirects
        ):
            raise GoogleError("source must be a loopback Desktop OAuth client")
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        raise GoogleError("could not read a valid private Desktop OAuth client file") from None
    with _lock(account):
        ssm = _ssm()
        if _read_parameter(ssm, _name(account, "client")) is not None:
            raise GoogleError("client already imported; existing credential retained")
        _put(ssm, _name(account, "client"), record, overwrite=False)
    return expected


def _client_record(ssm, account: str) -> tuple[dict, str]:
    return _client(_record(_read_parameter(ssm, _name(account, "client")), "selected client"))


def authorize(account: str, *, reauthorize: bool = False) -> str:
    """Only this explicit operation opens a system browser for account consent."""
    _account(account)
    with _lock(account):
        ssm = _ssm()
        config, expected = _client_record(ssm, account)
        name = _name(account, "refresh")
        existing = _read_parameter(ssm, name)
        if reauthorize and existing is None:
            raise GoogleError("no prior grant; run auth without --reauthorize")
        if not reauthorize and existing is not None:
            raise GoogleError("grant already exists; use auth --reauthorize explicitly")
        try:
            flow = InstalledAppFlow.from_client_config(config, scopes=list(SCOPES),
                                                        autogenerate_code_verifier=True)
            # InstalledAppFlow's local callback handler logs the URL (including its
            # authorization code) at INFO if the caller enables that logger.
            callback_logger = logging.getLogger("google_auth_oauthlib.flow")
            was_disabled = callback_logger.disabled
            callback_logger.disabled = True
            try:
                credentials = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True,
                                                    authorization_prompt_message="",
                                                    success_message="MAVEN authorization returned. You may close this tab.",
                                                    prompt="consent", access_type="offline",
                                                    login_hint=expected, include_granted_scopes="false")
            finally:
                callback_logger.disabled = was_disabled
            _check_scopes(flow.oauth2session.token.get("scope"), initial=True)
            _identity(credentials, expected)
            token = credentials.refresh_token
            if not isinstance(token, str) or not token:
                raise GoogleError("Google did not issue a refresh credential; prior grant retained")
        except GoogleError:
            raise
        except Exception:
            raise GoogleError("Google browser authorization failed; prior grant retained") from None
        # This local lock serializes this workstation; SSM has no conditional
        # PutParameter, so the version preflight cannot atomically cover another host.
        current = _read_parameter(ssm, name)
        if (current is None) != (existing is None) or (
            current is not None and current["Version"] != existing["Version"]
        ):
            raise GoogleError("credential changed during authorization; reauthorize after reviewing the selected account")
        _put(ssm, name, {"expected_email": expected, "refresh_token": token,
                         "scopes": list(SCOPES)}, overwrite=reauthorize)
        return expected


def _credentials(account: str) -> tuple[Credentials, str]:
    _account(account)
    with _lock(account):
        ssm = _ssm()
        config, expected = _client_record(ssm, account)
        item = _read_parameter(ssm, _name(account, "refresh"))
        refresh = _refresh(_record(item, "selected refresh credential"), expected)
        installed = config["installed"]
        credentials = Credentials(token=None, refresh_token=refresh,
                                  token_uri=installed["token_uri"],
                                  client_id=installed["client_id"], client_secret=installed["client_secret"],
                                  scopes=list(SCOPES))
        try:
            credentials.refresh(Request())
            _check_scopes(credentials.granted_scopes, initial=False)
            _identity(credentials, expected)
        except GoogleError:
            raise
        except (GoogleAuthError, requests.RequestException, ValueError):
            raise GoogleError("Google refresh failed for the selected account") from None
        if credentials.refresh_token != refresh:
            if not isinstance(credentials.refresh_token, str) or not credentials.refresh_token:
                raise GoogleError("Google returned an invalid replacement credential; prior value retained")
            # Detect a changed version before rotating; SSM offers no cross-host CAS.
            current = _read_parameter(ssm, _name(account, "refresh"))
            if current is None or current["Version"] != item["Version"]:
                raise GoogleError("credential changed during refresh; reauthorize the selected account")
            _put(ssm, _name(account, "refresh"),
                 {"expected_email": expected, "refresh_token": credentials.refresh_token,
                  "scopes": list(SCOPES)}, overwrite=True)
        return credentials, expected


def credentials_for(account: str) -> Credentials:
    """Return refreshed, scope-checked, Drive-identity-verified credentials; no consent."""
    return _credentials(account)[0]


def connect(account: str) -> dict:
    _, expected = _credentials(account)
    return {"account": account, "email": expected, "status": "connected",
            "scopes": list(SCOPES)}


def _gmail_get(credentials: Credentials, url: str, *, params: dict | None = None) -> dict:
    try:
        response = AuthorizedSession(credentials).get(url, params=params, timeout=45)
        if response.status_code == 204 and url == GMAIL_MESSAGES:
            return {"messages": []}
        if response.status_code != 200:
            raise GoogleError("Gmail read request failed for the selected account")
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (requests.RequestException, ValueError, GoogleAuthError):
        raise GoogleError("Gmail read request failed for the selected account") from None


def _mail_text(value: str) -> str:
    if not isinstance(value, str):
        raise GoogleError("Gmail returned invalid message metadata")
    try:
        value = str(make_header(decode_header(value)))
    except (LookupError, UnicodeDecodeError, ValueError):
        pass
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", value).strip()


def _message_ids(credentials: Credentials, query: str | None, maximum: int,
                 mailbox: str = "inbox") -> list[str]:
    if not isinstance(maximum, int) or not 1 <= maximum <= 500:
        raise GoogleError("--max must be between 1 and 500")
    ids = []
    if mailbox not in ("inbox", "all"):
        raise GoogleError("mailbox must be inbox or all")
    if query is not None and (not isinstance(query, str) or not query.strip()):
        raise GoogleError("mailbox query must be nonempty")
    if mailbox == "all" and query is None:
        raise GoogleError("all-mailbox listing requires an explicit query")
    token = None
    while len(ids) < maximum:
        params = {"maxResults": min(500, maximum - len(ids)),
                  "fields": "messages(id),nextPageToken"}
        if mailbox == "inbox":
            params["labelIds"] = "INBOX"
        else:
            params["includeSpamTrash"] = True
        if query is not None:
            params["q"] = query
        if token:
            params["pageToken"] = token
        page = _gmail_get(credentials, GMAIL_MESSAGES, params=params)
        entries = page.get("messages", [])
        if not isinstance(entries, list):
            raise GoogleError("Gmail returned an invalid message listing")
        for message in entries:
            identifier = message.get("id") if isinstance(message, dict) else None
            if not isinstance(identifier, str) or not MESSAGE_ID.fullmatch(identifier):
                raise GoogleError("Gmail returned an invalid message identifier")
            ids.append(identifier)
            if len(ids) == maximum:
                break
        next_token = page.get("nextPageToken")
        if not next_token:
            break
        if not isinstance(next_token, str) or next_token == token:
            raise GoogleError("Gmail returned an invalid listing page")
        token = next_token
    return ids


def gmail_list(account: str, query: str | None = None, maximum: int = 25,
               *, mailbox: str = "inbox") -> dict:
    """Summarize a bounded inbox query, or an explicit query across the mailbox."""
    credentials, expected = _credentials(account)
    messages = []
    for identifier in _message_ids(credentials, query, maximum, mailbox):
        item = _gmail_get(credentials, f"{GMAIL_MESSAGES}/{quote(identifier, safe='')}",
                          params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"],
                                  "fields": "id,threadId,labelIds,snippet,sizeEstimate,payload/headers"})
        if item.get("id") != identifier:
            raise GoogleError("Gmail returned an invalid message identifier")
        payload = item.get("payload") or {}
        if not isinstance(payload, dict):
            raise GoogleError("Gmail returned invalid message metadata")
        headers = payload.get("headers", [])
        if not isinstance(headers, list):
            raise GoogleError("Gmail returned invalid message metadata")
        values = {h["name"].lower(): _mail_text(h["value"]) for h in headers
                  if isinstance(h, dict) and isinstance(h.get("name"), str)
                  and isinstance(h.get("value"), str)}
        messages.append({"id": identifier, "thread_id": item.get("threadId"),
                         "date": values.get("date", ""), "from": values.get("from", ""),
                         "subject": values.get("subject", ""),
                         "snippet": _mail_text(item.get("snippet", "")),
                         "unread": "UNREAD" in item.get("labelIds", []),
                         "size_estimate": item.get("sizeEstimate")})
    return {"account": account, "email": expected, "messages": messages}


def gmail_download(account: str, ids: list[str], out: str | Path, *,
                   query: str | None = None, maximum: int = 25,
                   mailbox: str = "inbox") -> dict:
    """Download exact IDs or a requested bounded query as private .eml files."""
    if query is not None:
        if ids or not isinstance(query, str) or not query.strip():
            raise GoogleError("use either exact IDs or a nonempty inbox query")
    elif not ids or any(not isinstance(identifier, str) or not MESSAGE_ID.fullmatch(identifier) for identifier in ids):
        raise GoogleError("provide valid exact Gmail message IDs")
    if len(set(ids)) != len(ids):
        raise GoogleError("duplicate Gmail message IDs are not allowed")
    destination = Path(out)
    try:
        info = destination.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise GoogleError("output must be an existing private owner-owned directory")
    except FileNotFoundError:
        raise GoogleError("output directory must exist and be private") from None
    except OSError:
        raise GoogleError("output directory is unavailable") from None
    credentials, expected = _credentials(account)
    if query is not None:
        ids = _message_ids(credentials, query, maximum, mailbox)
    saved = []
    for identifier in ids:
        target = destination / f"{identifier}.eml"
        if target.exists() or target.is_symlink():
            saved.append({"id": identifier, "path": str(target), "status": "exists"})
            continue
        message = _gmail_get(credentials, f"{GMAIL_MESSAGES}/{quote(identifier, safe='')}",
                             params={"format": "raw", "fields": "id,raw"})
        if message.get("id") != identifier or not isinstance(message.get("raw"), str):
            raise GoogleError("Gmail returned an invalid raw message")
        try:
            raw = base64.b64decode(message["raw"] + "=" * (-len(message["raw"]) % 4),
                                   altchars=b"-_", validate=True)
        except (ValueError, binascii.Error):
            raise GoogleError("Gmail returned invalid message encoding") from None
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=destination, prefix=".maven-eml-",
                                             delete=False) as stream:
                temporary = Path(stream.name)
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, target, follow_symlinks=False)
            saved.append({"id": identifier, "path": str(target),
                          "status": "downloaded", "bytes": len(raw)})
        except FileExistsError:
            saved.append({"id": identifier, "path": str(target), "status": "exists"})
        except OSError:
            raise GoogleError("could not store downloaded mail privately") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return {"account": account, "email": expected, "saved": saved}


def _gmail_write(credentials: Credentials, method: str, url: str, body: dict | None = None,
                 *, expected_status: int = 200) -> dict:
    try:
        session = AuthorizedSession(credentials)
        response = session.request(method, url, json=body, timeout=45)
        if response.status_code != expected_status:
            raise GoogleError("Gmail write request failed for the selected account (response withheld)")
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (requests.RequestException, ValueError, GoogleAuthError):
        raise GoogleError("Gmail write request failed for the selected account (response withheld)") from None


def _gmail_id(identifier: str, kind: str = "message") -> str:
    if not isinstance(identifier, str) or not MESSAGE_ID.fullmatch(identifier):
        raise GoogleError(f"provide an exact valid Gmail {kind} ID")
    return identifier


def _gmail_result(result: dict, *, expected_id: str | None = None) -> dict:
    identifier = _gmail_id(result.get("id"))
    if expected_id is not None and identifier != expected_id:
        raise GoogleError("Gmail returned a different message ID")
    thread = _gmail_id(result.get("threadId"), "thread")
    return {"id": identifier, "thread_id": thread}


def gmail_message(account: str, message_id: str) -> dict:
    """Read exact message metadata only, without changing the read state."""
    identifier = _gmail_id(message_id)
    credentials, _ = _credentials(account)
    item = _gmail_get(credentials, f"{GMAIL_MESSAGES}/{identifier}",
                      params={"format": "metadata",
                              "metadataHeaders": ["From", "To", "Cc", "Date", "Subject"],
                              "fields": "id,threadId,labelIds,snippet,sizeEstimate,payload/headers"})
    result = _gmail_result(item, expected_id=identifier)
    payload = item.get("payload") or {}
    if not isinstance(payload, dict):
        raise GoogleError("Gmail returned invalid message metadata")
    headers = payload.get("headers", [])
    labels = item.get("labelIds", [])
    if not isinstance(headers, list) or not isinstance(labels, list) or any(
        not isinstance(label, str) or not MESSAGE_ID.fullmatch(label) for label in labels
    ):
        raise GoogleError("Gmail returned invalid message metadata")
    values = {header["name"].lower(): _mail_text(header["value"])[:256]
              for header in headers if isinstance(header, dict)
              and isinstance(header.get("name"), str) and isinstance(header.get("value"), str)}
    return {**result, "headers": {name: values.get(name, "")
                                  for name in ("from", "to", "cc", "date", "subject")},
            "snippet": _mail_text(item.get("snippet", ""))[:256],
            "labels": labels, "unread": "UNREAD" in labels,
            "size_estimate": item.get("sizeEstimate")}


def gmail_modify(account: str, message_id: str, *, add_labels: list[str] = (),
                 remove_labels: list[str] = ()) -> dict:
    """Modify only labels on one exact message (UNREAD controls read state)."""
    identifier = _gmail_id(message_id)
    add, remove = list(add_labels), list(remove_labels)
    if not add and not remove:
        raise GoogleError("specify labels to add or remove")
    if any(not isinstance(label, str) or not MESSAGE_ID.fullmatch(label)
           for label in add + remove) or set(add) & set(remove):
        raise GoogleError("provide valid nonconflicting Gmail label IDs")
    if any(label in ("TRASH", "SPAM", "SENT", "DRAFT") for label in add + remove):
        raise GoogleError("use a dedicated workflow for system mailbox state")
    credentials, _ = _credentials(account)
    item = _gmail_write(credentials, "POST", f"{GMAIL_MESSAGES}/{identifier}/modify",
                        {"addLabelIds": add, "removeLabelIds": remove})
    result = _gmail_result(item, expected_id=identifier)
    labels = item.get("labelIds")
    if not isinstance(labels, list) or any(
        not isinstance(label, str) or not MESSAGE_ID.fullmatch(label) for label in labels
    ):
        raise GoogleError("Gmail returned invalid message labels")
    return {**result, "labels": labels, "unread": "UNREAD" in labels}


def gmail_trash(account: str, message_id: str) -> dict:
    """Move an exact message to Trash; does not permanently delete it."""
    identifier = _gmail_id(message_id)
    credentials, _ = _credentials(account)
    return _gmail_result(_gmail_write(credentials, "POST",
                                      f"{GMAIL_MESSAGES}/{identifier}/trash", {}),
                         expected_id=identifier)


def gmail_untrash(account: str, message_id: str) -> dict:
    """Restore an exact message from Trash."""
    identifier = _gmail_id(message_id)
    credentials, _ = _credentials(account)
    return _gmail_result(_gmail_write(credentials, "POST",
                                      f"{GMAIL_MESSAGES}/{identifier}/untrash", {}),
                         expected_id=identifier)


def _mime_source(source: str | Path) -> bytes:
    try:
        source = Path(source)
        if source.suffix.lower() != ".eml":
            raise GoogleError("MIME source must be an existing private .eml file")
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077):
                raise GoogleError("MIME source must be a private owner-owned regular file")
            if info.st_size > MAX_MIME_BYTES:
                raise GoogleError("MIME source exceeds the 25 MB limit")
            raw = stream.read(MAX_MIME_BYTES + 1)
        if not raw or len(raw) > MAX_MIME_BYTES:
            raise GoogleError("MIME source must be nonempty and at most 25 MB")
        return raw
    except (OSError, TypeError, ValueError):
        raise GoogleError("could not read a private owner-owned MIME source") from None


def _mime_recipients(raw: bytes, expected: str, recipients: list[str]) -> None:
    """Require exactly the selected identity and caller-approved envelope addresses."""
    if not isinstance(recipients, list) or not recipients:
        raise GoogleError("provide explicit MIME recipients")
    approved = [_expected_email(address) for address in recipients]
    if len(set(approved)) != len(approved):
        raise GoogleError("duplicate MIME recipients are not allowed")
    if not raw or len(raw) > MAX_MIME_BYTES:
        raise GoogleError("MIME message must be nonempty and at most 25 MB")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if message.defects or any(part.defects for part in message.walk()):
        raise GoogleError("MIME source contains invalid message headers")
    if any(name.lower().startswith("resent-") for name in message.keys()):
        raise GoogleError("resent MIME headers are not supported")
    if len(message.get_all("From", [])) != 1 or message.get_all("Sender", []):
        raise GoogleError("MIME From must identify the selected account only")
    sender = getaddresses(message.get_all("From", []))
    if len(sender) != 1 or _expected_email(sender[0][1]) != expected:
        raise GoogleError("MIME From does not match the selected account")
    addresses = []
    for name in ("To", "Cc", "Bcc"):
        fields = message.get_all(name, [])
        if fields:
            addresses.extend(getaddresses(fields))
    if not addresses or any(not address or not EMAIL.fullmatch(address) for _, address in addresses):
        raise GoogleError("MIME recipients must be valid explicit addresses")
    actual = [address.casefold() for _, address in addresses]
    if len(actual) != len(set(actual)) or set(actual) != set(approved):
        raise GoogleError("MIME recipients differ from explicitly approved recipients")


def _raw_body(raw: bytes) -> dict:
    return {"message": {"raw": base64.urlsafe_b64encode(raw).decode("ascii")}}


def gmail_draft_create(account: str, source: str | Path, recipients: list[str]) -> dict:
    """Create a Gmail draft from a private MIME file with approved recipients."""
    raw = _mime_source(source)
    credentials, expected = _credentials(account)
    _mime_recipients(raw, expected, recipients)
    result = _gmail_write(credentials, "POST", GMAIL_DRAFTS, _raw_body(raw),
                          expected_status=200)
    return {"id": _gmail_id(result.get("id"), "draft"),
            "message_id": _gmail_id((result.get("message") or {}).get("id"))}


def gmail_draft_update(account: str, draft_id: str, source: str | Path,
                       recipients: list[str]) -> dict:
    """Replace exactly one Gmail draft with an approved private MIME file."""
    identifier = _gmail_id(draft_id, "draft")
    raw = _mime_source(source)
    credentials, expected = _credentials(account)
    _mime_recipients(raw, expected, recipients)
    result = _gmail_write(credentials, "PUT", f"{GMAIL_DRAFTS}/{identifier}",
                          _raw_body(raw), expected_status=200)
    if result.get("id") != identifier:
        raise GoogleError("Gmail returned a different draft ID")
    return {"id": identifier, "message_id": _gmail_id((result.get("message") or {}).get("id"))}


def _sent_message(credentials: Credentials, result: dict) -> dict:
    sent = _gmail_result(result)
    verified = _gmail_get(credentials, f"{GMAIL_MESSAGES}/{sent['id']}",
                          params={"format": "minimal", "fields": "id,threadId,labelIds"})
    labels = verified.get("labelIds")
    if (_gmail_result(verified, expected_id=sent["id"]) != sent
            or not isinstance(labels, list) or "SENT" not in labels):
        raise GoogleError("Gmail sent result could not be verified; do not retry sending")
    return sent


def _send_once(account: str, identity: str, action) -> dict:
    """A durable private reservation prevents an ambiguous API failure from resending."""
    digest = hashlib.sha256(identity.encode("ascii")).hexdigest()
    with _lock(account):
        reservation = LOCKS / f"{account}-gmail-send-{digest}.reserved"
        try:
            fd = os.open(reservation, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            raise GoogleError("send was already attempted; inspect Sent before any new send") from None
        except OSError:
            raise GoogleError("could not reserve private Gmail send state") from None
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(b"attempted\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            raise GoogleError("could not reserve private Gmail send state") from None
        return action()


def gmail_send(account: str, source: str | Path, recipients: list[str]) -> dict:
    """Send a private owner-provided MIME file at most once per account and file content."""
    raw = _mime_source(source)
    credentials, expected = _credentials(account)
    _mime_recipients(raw, expected, recipients)
    return _send_once(account, "file:" + hashlib.sha256(raw).hexdigest(),
                      lambda: _sent_message(
                          credentials, _gmail_write(credentials, "POST",
                                                     f"{GMAIL_MESSAGES}/send",
                                                     {"raw": base64.urlsafe_b64encode(raw).decode("ascii")})))


def gmail_draft_send(account: str, draft_id: str, recipients: list[str]) -> dict:
    """Send the validated MIME snapshot of an existing draft at most once."""
    identifier = _gmail_id(draft_id, "draft")
    credentials, expected = _credentials(account)
    draft = _gmail_get(credentials, f"{GMAIL_DRAFTS}/{identifier}",
                       params={"format": "raw", "fields": "id,message(id,raw)"})
    if draft.get("id") != identifier or not isinstance(draft.get("message"), dict):
        raise GoogleError("Gmail returned an invalid draft")
    encoded = draft["message"].get("raw")
    if not isinstance(encoded, str):
        raise GoogleError("Gmail returned an invalid draft MIME")
    try:
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_",
                               validate=True)
    except (ValueError, binascii.Error):
        raise GoogleError("Gmail returned invalid draft MIME encoding") from None
    _mime_recipients(raw, expected, recipients)
    return _send_once(account, "draft:" + identifier,
                      lambda: _sent_message(credentials, _gmail_write(
                          credentials, "POST", f"{GMAIL_DRAFTS}/send",
                          {"id": identifier, **_raw_body(raw)})))
