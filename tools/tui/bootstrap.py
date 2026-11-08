"""Stdlib-only, consented bootstrap for the optional terminal interface."""
from __future__ import annotations

from importlib import metadata, util
import os
from pathlib import Path
import subprocess
import signal
import sys
import tempfile
import venv

UI_REQUIREMENTS = ("textual>=2.1,<3", "packaging>=24,<27")


def ui_available():
    try:
        return (util.find_spec("textual") is not None
                and util.find_spec("packaging") is not None
                and int(metadata.version("textual").split(".")[0]) == 2
                and 24 <= int(metadata.version("packaging").split(".")[0]) < 27)
    except (metadata.PackageNotFoundError, ValueError):
        return False


def run_terminal(command, *, env=None, cwd=None):
    """Wait for the foreground TUI to pause itself instead of killing it on a signal."""
    child = subprocess.Popen(command, env=env, cwd=cwd)

    def request_pause(number, _frame):
        if child.poll() is None:
            child.send_signal(number)

    previous = {number: signal.signal(number, request_pause)
                for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        return child.wait()
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def ensure_ui(argv: list[str]) -> int | None:
    """Return None when ready, otherwise run this same installer in a private UI runtime."""
    if ui_available():
        return None
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Interactive installation requires a terminal. Use --local-only for explicit headless installation.", file=sys.stderr)
        return 2
    print("MAVEN needs Textual for its terminal interface. Download UI dependencies into a private temporary runtime?\n"
          "System Python will not be modified. TLS verification and your certificate settings remain enabled.")
    try:
        if input("Download and continue? [y/N]: ").strip().lower() not in ("y", "yes"):
            print("No installation performed. Existing noninteractive commands remain available.")
            return 130
        environment = dict(os.environ)
        for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
            environment.pop(key, None)
        if not environment.get("PIP_CERT"):
            certificate = next((environment[key] for key in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "AWS_CA_BUNDLE") if environment.get(key)), None)
            if certificate:
                environment["PIP_CERT"] = certificate
        with tempfile.TemporaryDirectory(prefix="maven-ui-bootstrap-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            print("Preparing the private terminal-interface runtime…")
            venv.EnvBuilder(with_pip=True).create(root / "venv")
            python = root / "venv/bin/python"
            subprocess.run([str(python), "-m", "pip", "install", *UI_REQUIREMENTS],
                           env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            return run_terminal([str(python), *argv], env=environment)
    except (EOFError, KeyboardInterrupt):
        print("Bootstrap paused; no readiness was declared.", file=sys.stderr)
        return 130
    except (OSError, subprocess.SubprocessError):
        print("UI bootstrap failed. Check network access, Python venv support and your trusted CA bundle. "
              "Use --ca-bundle with an approved PEM bundle when needed; never disable TLS verification.", file=sys.stderr)
        return 2
