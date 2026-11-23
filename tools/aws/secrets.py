#!/usr/bin/env python3
"""Store small SSM secrets and encrypted private files for MAVEN."""

from __future__ import annotations

import argparse
import base64
from contextlib import nullcontext
import binascii
import getpass
import hmac
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from types import MappingProxyType
import warnings

from botocore.exceptions import BotoCoreError, ClientError, ProfileNotFound

from tools.aws.settings import load_settings, prefix, required
from tools.memory.environment import MemoryClientError, selected_aws_session
from tools.recovery import DEFAULT_NEXT_ACTION

MAX_BYTES = 4096
NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")
KEY_ID = re.compile(r"[0-9a-f]{16}\Z")


class StoreError(Exception):
    """A safe error that contains no secret value or AWS response body."""


class StorageRejected(StoreError):
    """Known rejection before publication; never an uncertain provider outcome."""


def parameter_name(name: str, *, values=None) -> str:
    if not NAME.fullmatch(name):
        raise StoreError("name must be one short lowercase segment (letters, digits, _, ., -)")
    try:
        return prefix(load_settings() if values is None else values, "MAVEN_SECRETS_PARAMETER_PREFIX") + name
    except MemoryClientError:
        raise StoreError("secret store namespace is not configured in private settings") from None


def secret_input(from_stdin: bool) -> str:
    if from_stdin:
        if sys.stdin.isatty():
            raise StoreError("--stdin requires a pipe; use the hidden prompt for one line")
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise StoreError("value exceeds the 4 KB Standard parameter limit")
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise StoreError("value must be UTF-8 text") from None
    else:
        try:
            with open("/dev/tty", "rb"):
                pass
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                value = getpass.getpass("Secret: ")
        except (OSError, EOFError, getpass.GetPassWarning):
            raise StoreError("a private terminal is required; use --stdin for a piped value") from None
    if not value:
        raise StoreError("value must not be empty")
    if len(value.encode("utf-8")) > MAX_BYTES:
        raise StoreError("value exceeds the 4 KB Standard parameter limit")
    return value


def aws_session(values=None):
    try:
        values = load_settings() if values is None else values
        return selected_aws_session(values, region=required(values, "MAVEN_AWS_REGION"))
    except MemoryClientError:
        raise StoreError("AWS identity is unavailable; configure and log in to the explicitly selected profile") from None


def stored_value(client, name: str) -> str:
    parameter = client.get_parameter(Name=name, WithDecryption=True)["Parameter"]
    if parameter["Type"] != "SecureString":
        raise StoreError("refusing to use a parameter that is not SecureString")
    return parameter["Value"]


def file_config(values=None) -> tuple[str, str]:
    try:
        values = load_settings() if values is None else values
        bucket = required(values, "MAVEN_SECRETS_BUCKET")
        key_id = required(values, "MAVEN_SECRETS_KEY_ID")
    except MemoryClientError:
        raise StoreError("file store is not configured in private settings") from None
    if not isinstance(bucket, str) or not re.fullmatch(r"[a-z0-9.-]{3,63}", bucket):
        raise StoreError("file store bucket configuration is invalid")
    if not isinstance(key_id, str) or not KEY_ID.fullmatch(key_id):
        raise StoreError("file store key configuration is invalid")
    return bucket, key_id


def file_keyring(ssm, key_id: str, *, values):
    from aws_cryptographic_material_providers.mpl import AwsCryptographicMaterialProviders
    from aws_cryptographic_material_providers.mpl.config import MaterialProvidersConfig
    from aws_cryptographic_material_providers.mpl.models import AesWrappingAlg, CreateRawAesKeyringInput

    try:
        wrapping_key = base64.b64decode(
            stored_value(ssm, prefix(values, "MAVEN_SECRETS_FILE_KEY_PREFIX") + key_id), validate=True,
        )
    except (ValueError, binascii.Error):
        raise StoreError("file wrapping key is invalid") from None
    if len(wrapping_key) != 32:
        raise StoreError("file wrapping key is invalid")
    return AwsCryptographicMaterialProviders(
        config=MaterialProvidersConfig(),
    ).create_raw_aes_keyring(input=CreateRawAesKeyringInput(
        key_namespace="maven-ssm",
        key_name=key_id,
        wrapping_key=wrapping_key,
        wrapping_alg=AesWrappingAlg.ALG_AES256_GCM_IV12_TAG16,
    ))


def open_input(path, *, private=False):
    """Reject special files before opening; recheck the nonblocking descriptor for races."""
    path = Path(path).expanduser().absolute()
    if path != path.resolve():
        raise StorageRejected("input paths must not contain symlinks")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise StorageRejected("input must be a regular file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                or private and (opened.st_uid != os.getuid() or opened.st_mode & 0o077)):
            raise StorageRejected("input must be an unchanged owner-private regular file" if private
                                  else "input must be an unchanged regular file")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def upload_file(args, selected) -> None:
    from tools.aws.secret_store import DEFAULT_STREAM_LIMIT, MAX_FILE_BYTES
    from_stdin = getattr(args, "stdin", False)
    if from_stdin and (args.source is not None or sys.stdin.isatty()):
        raise StoreError("--stdin requires a binary pipe and no source path")
    if not from_stdin and args.source is None:
        raise StoreError("provide an existing source file or --stdin")
    maximum = args.max_bytes if args.max_bytes is not None else (
        DEFAULT_STREAM_LIMIT if from_stdin else MAX_FILE_BYTES)
    context = nullcontext(sys.stdin.buffer) if from_stdin else open_input(args.source)
    with context as stream:
        size = None if from_stdin else os.fstat(stream.fileno()).st_size
        receipt = selected.put_file(args.name, stream, source_size=size, max_bytes=maximum)
    print(json.dumps(receipt) if args.json else "Encrypted file stored.")


def restore_file(args, selected) -> None:
    selected.restore_file(args.name, args.destination)
    print("File restored.")


def file_action(args) -> None:
    from aws_encryption_sdk.exceptions import AWSEncryptionSDKClientError
    from cryptography.exceptions import InvalidSignature

    if not NAME.fullmatch(args.name):
        raise StoreError("name must be one short lowercase segment (letters, digits, _, ., -)")
    from tools.aws.secret_store import SecretStore
    values = MappingProxyType(dict(load_settings()))
    selected = SecretStore(values, session=aws_session(values))
    try:
        if args.file_action == "put":
            upload_file(args, selected)
        else:
            restore_file(args, selected)
    except (AWSEncryptionSDKClientError, InvalidSignature):
        raise StoreError("file encryption or authentication failed; verify the selected store and encryption key "
                         "before retrying") from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="MAVEN SSM secrets and encrypted S3 files")
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("put", "replace", "check", "reveal", "use-stdin"):
        command = commands.add_parser(action)
        command.add_argument("name", help="short, non-sensitive name")
        if action in ("put", "replace", "check"):
            command.add_argument("--stdin", action="store_true", help="read an exact UTF-8 value from a pipe")
        elif action == "reveal":
            command.add_argument("--tty", action="store_true", required=True, help="show value only on your terminal")
        elif action == "use-stdin":
            command.add_argument("command", nargs=argparse.REMAINDER, help="-- trusted program [args]")
    listing = commands.add_parser("list", help="list names/metadata only; never decrypt values")
    listing.add_argument("--domain", help="exact dotted domain prefix")
    listing.add_argument("--cursor", help="continuation from the preceding page")
    listing.add_argument("--limit", type=int, default=20, help="page size per provider (1-50)")
    files = commands.add_parser("file", help="encrypt and store or restore a private file")
    file_commands = files.add_subparsers(dest="file_action", required=True)
    put_file = file_commands.add_parser("put")
    put_file.add_argument("source", nargs="?", help="existing local plaintext file; omit with --stdin")
    put_file.add_argument("name", help="short, non-sensitive object name")
    put_file.add_argument("--stdin", action="store_true", help="encrypt a bounded binary pipe without plaintext spooling")
    put_file.add_argument("--max-bytes", type=int, help="maximum plaintext bytes (pipe default: 64 GiB)")
    put_file.add_argument("--json", action="store_true", help="return a non-secret storage receipt")
    get_file = file_commands.add_parser("get")
    get_file.add_argument("name", help="short, non-sensitive object name")
    get_file.add_argument("destination", help="new local output path; never overwritten")
    args = parser.parse_args(argv)
    try:
        if args.action == "list":
            from tools.aws.secret_store import SecretStore
            print(json.dumps(SecretStore(load_settings()).list(args.domain, args.cursor, args.limit)))
            return 0
        if args.action == "file":
            file_action(args)
            return 0
        values = MappingProxyType(dict(load_settings()))
        name = parameter_name(args.name, values=values)
        if args.action == "use-stdin":
            argv = args.command[1:] if args.command[:1] == ["--"] else args.command
            if not argv:
                raise StoreError("provide a trusted program after --")
            with open("/dev/tty", "rb"):
                pass
        elif args.action == "reveal":
            with open("/dev/tty", "w"):
                pass

        # Read a new value before making any AWS request; never put it in argv or the environment.
        value = secret_input(args.stdin) if args.action in ("put", "replace", "check") else None
        client = aws_session(values).client("ssm")
        if args.action in ("put", "replace"):
            if args.action == "replace":
                stored_value(client, name)  # Refuse to turn a missing name into a new parameter.
            client.put_parameter(
                Name=name, Value=value, Type="SecureString", KeyId="alias/aws/ssm",
                Tier="Standard", Overwrite=args.action == "replace",
            )
            print("Stored.")
        elif args.action == "check":
            match = hmac.compare_digest(stored_value(client, name).encode("utf-8"), value.encode("utf-8"))
            print("Match." if match else "Mismatch.")
            return 0 if match else 1
        elif args.action == "reveal":
            value = stored_value(client, name)
            with open("/dev/tty", "w", encoding="utf-8") as terminal:
                terminal.write(value + "\n")
        else:
            value = stored_value(client, name)
            try:
                result = subprocess.run(argv, input=value.encode("utf-8"), check=False)
            except OSError:
                raise StoreError("could not start the trusted program") from None
            return result.returncode
        return 0
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ParameterAlreadyExists":
            message = "name already exists; use replace for an intentional update"
        elif code == "ParameterNotFound":
            message = "name not found; use maven-secrets list to check names in the selected store"
        elif code in ("PreconditionFailed", "ConditionalRequestConflict"):
            message = "object already exists or changed during upload; choose a new name"
        elif code in ("NoSuchKey", "404"):
            message = "object not found; use maven-secrets list to check names in the selected store"
        elif code in ("AccessDeniedException", "AccessDenied"):
            message = "AWS denied this operation; check the selected profile and its permissions for this store"
        else:
            message = "AWS request failed (details withheld to protect secret data). " + DEFAULT_NEXT_ACTION
    except (BotoCoreError, ProfileNotFound):
        message = "AWS credentials or connection unavailable; log in to the explicitly configured profile"
    except StoreError as exc:
        message = str(exc) + "\nNext action: " + DEFAULT_NEXT_ACTION
    except MemoryClientError as exc:
        message = "Secret store unavailable: " + exc.code + "\nNext action: " + DEFAULT_NEXT_ACTION
    except OSError:
        message = "Local file access failed; check the path and permissions."
    except KeyboardInterrupt:
        print("Interrupted. " + DEFAULT_NEXT_ACTION, file=sys.stderr)
        return 130
    except Exception:
        message = "Secret operation could not finish (details withheld to protect secret data). " + DEFAULT_NEXT_ACTION
    print(message, file=sys.stderr)
    return 2

