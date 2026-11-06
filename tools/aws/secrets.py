#!/usr/bin/env python3
"""Store small SSM secrets and encrypted private files for MAVEN."""

from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import hmac
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import warnings

from botocore.exceptions import BotoCoreError, ClientError, ProfileNotFound

from tools.aws.settings import load_settings, prefix, required
from tools.memory.environment import MemoryClientError, selected_aws_session

MAX_BYTES = 4096
NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")
KEY_ID = re.compile(r"[0-9a-f]{16}\Z")


class StoreError(Exception):
    """A safe error that contains no secret value or AWS response body."""


def _prefix(key: str, *, parameter: bool = True) -> str:
    try:
        return prefix(load_settings(), key, parameter=parameter)
    except MemoryClientError:
        raise StoreError("secret store namespace is not configured in private settings") from None


def parameter_name(name: str) -> str:
    if not NAME.fullmatch(name):
        raise StoreError("name must be one short lowercase segment (letters, digits, _, ., -)")
    return _prefix("MAVEN_SECRETS_PARAMETER_PREFIX") + name


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


def aws_session():
    try:
        values = load_settings()
        return selected_aws_session(values, region=required(values, "MAVEN_AWS_REGION"))
    except MemoryClientError:
        raise StoreError("AWS identity is unavailable; configure and log in to the explicitly selected profile") from None


def ssm_client():
    return aws_session().client("ssm")


def stored_value(client, name: str) -> str:
    parameter = client.get_parameter(Name=name, WithDecryption=True)["Parameter"]
    if parameter["Type"] != "SecureString":
        raise StoreError("refusing to use a parameter that is not SecureString")
    return parameter["Value"]


def file_config() -> tuple[str, str]:
    try:
        values = load_settings()
        bucket = required(values, "MAVEN_SECRETS_BUCKET")
        key_id = required(values, "MAVEN_SECRETS_KEY_ID")
    except MemoryClientError:
        raise StoreError("file store is not configured in private settings") from None
    if not isinstance(bucket, str) or not re.fullmatch(r"[a-z0-9.-]{3,63}", bucket):
        raise StoreError("file store bucket configuration is invalid")
    if not isinstance(key_id, str) or not KEY_ID.fullmatch(key_id):
        raise StoreError("file store key configuration is invalid")
    return bucket, key_id


def file_keyring(ssm, key_id: str):
    from aws_cryptographic_material_providers.mpl import AwsCryptographicMaterialProviders
    from aws_cryptographic_material_providers.mpl.config import MaterialProvidersConfig
    from aws_cryptographic_material_providers.mpl.models import AesWrappingAlg, CreateRawAesKeyringInput

    try:
        wrapping_key = base64.b64decode(
            stored_value(ssm, _prefix("MAVEN_SECRETS_FILE_KEY_PREFIX") + key_id), validate=True,
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


def upload_file(args, session, bucket: str, key_id: str) -> None:
    import aws_encryption_sdk
    from aws_encryption_sdk import CommitmentPolicy

    keyring = file_keyring(session.client("ssm"), key_id)
    s3 = session.client("s3")
    object_key = _prefix("MAVEN_SECRETS_OBJECT_PREFIX", parameter=False) + args.name
    context = {"maven-object": args.name, "maven-key-id": key_id}
    client = aws_encryption_sdk.EncryptionSDKClient(
        commitment_policy=CommitmentPolicy.REQUIRE_ENCRYPT_REQUIRE_DECRYPT,
    )
    with open(args.source, "rb") as source:
        source_size = os.fstat(source.fileno()).st_size
        if source_size > 5 * 1024 ** 4:
            raise StoreError("source exceeds the S3 object size limit")
        # Leave room for encryption overhead without exceeding S3's 10,000-part limit.
        part_size = max(8 * 1024 ** 2, math.ceil((source_size * 1.01 + 1024 ** 2) / 9500))
        with client.stream(
            mode="e", source=source, source_length=source_size,
            keyring=keyring, encryption_context=context,
        ) as encryptor:
            upload = s3.create_multipart_upload(
                Bucket=bucket, Key=object_key, Metadata={"maven-key-id": key_id},
                ServerSideEncryption="AES256",
            )
            upload_id = upload["UploadId"]
            parts = []
            try:
                while True:
                    chunk = encryptor.read(part_size)
                    if not chunk:
                        break
                    if len(chunk) < part_size:
                        part = bytearray(chunk)
                        while len(part) < part_size:
                            extra = encryptor.read(part_size - len(part))
                            if not extra:
                                break
                            part.extend(extra)
                        chunk = bytes(part)
                    if len(parts) >= 10000:
                        raise StoreError("source exceeds the S3 multipart limit")
                    number = len(parts) + 1
                    result = s3.upload_part(
                        Bucket=bucket, Key=object_key, UploadId=upload_id,
                        PartNumber=number, Body=chunk,
                    )
                    parts.append({"PartNumber": number, "ETag": result["ETag"]})
                s3.complete_multipart_upload(
                    Bucket=bucket, Key=object_key, UploadId=upload_id,
                    MultipartUpload={"Parts": parts}, IfNoneMatch="*",
                )
            except BaseException:
                try:
                    s3.abort_multipart_upload(Bucket=bucket, Key=object_key, UploadId=upload_id)
                except (BotoCoreError, ClientError):
                    print("Warning: incomplete S3 multipart upload may need administrator cleanup.", file=sys.stderr)
                raise
    print("Encrypted file stored.")


def restore_file(args, session, bucket: str) -> None:
    import aws_encryption_sdk
    from aws_encryption_sdk import CommitmentPolicy

    destination = Path(args.destination)
    if destination.exists() or destination.is_symlink():
        raise StoreError("destination exists; choose a new path")
    s3 = session.client("s3")
    response = s3.get_object(Bucket=bucket, Key=_prefix("MAVEN_SECRETS_OBJECT_PREFIX", parameter=False) + args.name)
    body = response["Body"]
    temp_path = None
    try:
        key_id = response.get("Metadata", {}).get("maven-key-id", "")
        if not KEY_ID.fullmatch(key_id):
            raise StoreError("file has no valid wrapping key identifier")
        keyring = file_keyring(session.client("ssm"), key_id)
        client = aws_encryption_sdk.EncryptionSDKClient(
            commitment_policy=CommitmentPolicy.REQUIRE_ENCRYPT_REQUIRE_DECRYPT,
        )
        context = {"maven-object": args.name, "maven-key-id": key_id}
        descriptor, temp_path = tempfile.mkstemp(prefix=".maven-restore-", dir=destination.parent)
        with os.fdopen(descriptor, "wb") as output:
            with client.stream(
                mode="d", source=body, source_length=response["ContentLength"],
                keyring=keyring, encryption_context=context,
            ) as decryptor:
                while chunk := decryptor.read(1024 ** 2):
                    output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temp_path, destination)
        except FileExistsError:
            raise StoreError("destination exists; choose a new path") from None
        print("File restored.")
    finally:
        body.close()
        if temp_path is not None:
            os.unlink(temp_path)


def file_action(args) -> None:
    from aws_encryption_sdk.exceptions import AWSEncryptionSDKClientError

    if not NAME.fullmatch(args.name):
        raise StoreError("name must be one short lowercase segment (letters, digits, _, ., -)")
    bucket, key_id = file_config()
    session = aws_session()
    try:
        if args.file_action == "put":
            upload_file(args, session, bucket, key_id)
        else:
            restore_file(args, session, bucket)
    except AWSEncryptionSDKClientError:
        raise StoreError("file encryption or authentication failed") from None


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
    files = commands.add_parser("file", help="encrypt and store or restore a private file")
    file_commands = files.add_subparsers(dest="file_action", required=True)
    put_file = file_commands.add_parser("put")
    put_file.add_argument("source", help="existing local plaintext file")
    put_file.add_argument("name", help="short, non-sensitive object name")
    get_file = file_commands.add_parser("get")
    get_file.add_argument("name", help="short, non-sensitive object name")
    get_file.add_argument("destination", help="new local output path; never overwritten")
    args = parser.parse_args(argv)
    try:
        if args.action == "file":
            file_action(args)
            return 0
        name = parameter_name(args.name)
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
        client = ssm_client()
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
            message = "name not found"
        elif code in ("PreconditionFailed", "ConditionalRequestConflict"):
            message = "object already exists or changed during upload; choose a new name"
        elif code in ("NoSuchKey", "404"):
            message = "object not found"
        elif code in ("AccessDeniedException", "AccessDenied"):
            message = "AWS denied this operation for the MAVEN profile"
        else:
            message = "AWS request failed (details withheld to protect secret data)"
        print(message, file=sys.stderr)
        return 2
    except (BotoCoreError, ProfileNotFound):
        print("AWS credentials or connection unavailable; log in to the explicitly configured profile", file=sys.stderr)
        return 2
    except StoreError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError:
        print("Local file access failed; check the path and permissions.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130

