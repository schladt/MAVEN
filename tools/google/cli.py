#!/usr/bin/env python3
"""Explicit MAVEN Google account consent and targeted Gmail workflows."""

from __future__ import annotations

import argparse
import json
import sys



def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="MAVEN Google account access (explicit account required)")
    parser.add_argument("--account", choices=("work", "personal"), required=True)
    commands = parser.add_subparsers(dest="action", required=True)
    import_command = commands.add_parser("import-client", help="import a private Desktop OAuth client file")
    import_command.add_argument("source", help="trusted private OAuth Desktop JSON file")
    import_command.add_argument("--expected-email", required=True)
    auth_command = commands.add_parser("auth", help="open browser for explicit account consent")
    auth_command.add_argument("--reauthorize", action="store_true", help="replace an existing grant after verification")
    connect_command = commands.add_parser("connect", help="verify identity without launching consent")
    connect_command.add_argument("--json", action="store_true")
    gmail_command = commands.add_parser("gmail", help="task-directed mailbox reads and writes")
    gmail_commands = gmail_command.add_subparsers(dest="gmail_action", required=True)
    list_command = gmail_commands.add_parser("list", help="list message summaries")
    list_command.add_argument("--query", help="Gmail search expression")
    list_command.add_argument("--mailbox", choices=("inbox", "all"), default="inbox")
    list_command.add_argument("--max", type=int, default=25, dest="maximum")
    list_command.add_argument("--json", action="store_true")
    download_command = gmail_commands.add_parser("download", help="save exact or queried messages as private .eml")
    download_command.add_argument("ids", nargs="*", help="exact Gmail message IDs, or use --query")
    download_command.add_argument("--query", help="Gmail search expression")
    download_command.add_argument("--mailbox", choices=("inbox", "all"), default="inbox")
    download_command.add_argument("--max", type=int, dest="maximum", help="maximum newest matches with --query (default 25)")
    download_command.add_argument("--out", required=True, help="existing private output directory")
    download_command.add_argument("--json", action="store_true")
    message_command = gmail_commands.add_parser("message", help="get exact message metadata")
    message_command.add_argument("id")
    modify_command = gmail_commands.add_parser("modify", help="change labels on an exact message")
    modify_command.add_argument("id")
    modify_command.add_argument("--add-label", action="append", default=[])
    modify_command.add_argument("--remove-label", action="append", default=[])
    for action in ("trash", "untrash"):
        exact = gmail_commands.add_parser(action, help=f"{action} an exact message")
        exact.add_argument("id")
    create = gmail_commands.add_parser("draft-create", help="create a private-source MIME draft")
    create.add_argument("source", help="existing private .eml file")
    create.add_argument("--recipient", action="append", required=True, help="exact To/Cc/Bcc recipient; repeat")
    update = gmail_commands.add_parser("draft-update", help="replace an exact draft with private-source MIME")
    update.add_argument("id")
    update.add_argument("source", help="existing private .eml file")
    update.add_argument("--recipient", action="append", required=True, help="exact To/Cc/Bcc recipient; repeat")
    draft_send = gmail_commands.add_parser("draft-send", help="send an exact previously reviewed draft")
    draft_send.add_argument("id")
    draft_send.add_argument("--recipient", action="append", required=True, help="exact To/Cc/Bcc recipient; repeat")
    send = gmail_commands.add_parser("send", help="send an exact private-source MIME message once")
    send.add_argument("source", help="existing private .eml file")
    send.add_argument("--recipient", action="append", required=True, help="exact To/Cc/Bcc recipient; repeat")
    args = parser.parse_args(argv)
    try:
        from tools.google import client as maven_google
    except ModuleNotFoundError as error:
        if error.name and (error.name == "requests" or error.name.startswith(("google", "google_auth_oauthlib"))):
            print("Google support is optional; install MAVEN with the google extra or install.py --google.", file=sys.stderr)
            return 2
        raise
    if args.action == "gmail" and args.gmail_action == "download":
        if (args.ids and (args.query is not None or args.maximum is not None)) or (
            not args.ids and args.query is None
        ):
            download_command.error("pass exact IDs or --query/--max, not both or neither")
    try:
        if args.action == "import-client":
            email = maven_google.import_client(args.account, args.source, args.expected_email)
            print(f"{args.account}: imported Desktop client for {email}; no Google consent started")
        elif args.action == "auth":
            email = maven_google.authorize(args.account, reauthorize=args.reauthorize)
            print(f"{args.account}: authorized and verified {email}")
        elif args.action == "connect":
            result = maven_google.connect(args.account)
            if args.json:
                print(json.dumps(result))
            else:
                print(f"{result['account']}: {result['email']} ({result['status']})")
        elif args.gmail_action == "list":
            result = maven_google.gmail_list(args.account, args.query, args.maximum, mailbox=args.mailbox)
            if args.json:
                print(json.dumps(result))
            else:
                print(f"{result['account']}: {result['email']} ({len(result['messages'])} messages)")
                for message in result["messages"]:
                    print(f"{message['id']}  {message['date']}  {message['from']}  {'* ' if message['unread'] else ''}{message['subject']}")
        elif args.gmail_action == "download":
            result = maven_google.gmail_download(args.account, args.ids, args.out,
                                                  query=args.query, mailbox=args.mailbox,
                                                  maximum=args.maximum if args.maximum is not None else 25)
            if args.json:
                print(json.dumps(result))
            else:
                print(f"{result['account']}: {result['email']} ({len(result['saved'])} messages saved)")
                for saved in result["saved"]:
                    print(saved["path"])
        else:
            action = args.gmail_action
            if action == "message":
                result = maven_google.gmail_message(args.account, args.id)
            elif action == "modify":
                result = maven_google.gmail_modify(args.account, args.id,
                                                   add_labels=args.add_label,
                                                   remove_labels=args.remove_label)
            elif action == "trash":
                result = maven_google.gmail_trash(args.account, args.id)
            elif action == "untrash":
                result = maven_google.gmail_untrash(args.account, args.id)
            elif action == "draft-create":
                result = maven_google.gmail_draft_create(args.account, args.source, args.recipient)
            elif action == "draft-update":
                result = maven_google.gmail_draft_update(args.account, args.id, args.source, args.recipient)
            elif action == "draft-send":
                result = maven_google.gmail_draft_send(args.account, args.id, args.recipient)
            else:
                result = maven_google.gmail_send(args.account, args.source, args.recipient)
            print(json.dumps(result))
        return 0
    except maven_google.GoogleError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
