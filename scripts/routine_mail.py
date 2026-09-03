"""routine_mail.py: SMTP delivery of the rendered digest; recipient and credentials come only from private config.

Split out of routine_digest.py; routine_digest.py re-exports every name so callers and tests are unchanged.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PathsError, fmt, vault_root  # noqa: E402
from routine_digest_core import (  # noqa: E402
    GMAIL_CLIP_BYTES,
)


MAIL_CONFIG = "_meta/mail.toml"

def load_mail_config(ov: Path) -> dict[str, Any]:
    """SMTP settings from private vault config.

    Deliberately not in the repo and not in the prompt: the recipient is the
    single most important field here, and it must not be somewhere a model can
    reach or a routine prompt can restate. The address lives in config, the
    script reads it, and nothing between the two can redirect a message.

        # $OV/_meta/mail.toml
        [smtp]
        host = "smtp.gmail.com"
        port = 587
        username = "you@example.com"     # also the recipient; this is send-self
        keychain_service = "atelier-smtp"
    """
    path = ov / MAIL_CONFIG
    if not path.is_file():
        raise SystemExit(f"mail config missing: {fmt(path)}")
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise SystemExit(f"mail config unreadable: {exc!r}") from exc
    smtp = document.get("smtp")
    if not isinstance(smtp, dict):
        raise SystemExit(f"{fmt(path)} needs an [smtp] table")
    missing = [k for k in ("host", "username") if not smtp.get(k)]
    if missing:
        raise SystemExit(f"{fmt(path)} [smtp] missing: {', '.join(missing)}")
    if not smtp.get("keychain_service") and not smtp.get("password_file"):
        raise SystemExit(
            f"{fmt(path)} [smtp] needs keychain_service, password_file, or both"
        )
    return smtp

KEYCHAIN_TIMEOUT_SECONDS = 10

def _refuse_vault_secret(path: Path) -> None:
    """A password file under $OV is on a synced Drive mount: refuse it."""
    try:
        root = vault_root().resolve()
    except PathsError:
        return
    try:
        inside = path.resolve().is_relative_to(root)
    except OSError:
        return
    if inside:
        raise SystemExit(
            f"{fmt(path)} is under $OV; a password file must live outside the "
            "synced vault (for example ~/.config/atelier/)"
        )

def smtp_password(smtp: dict[str, Any]) -> str:
    """The app password, from the keychain if reachable, else a 0600 file.

    Never an argument, never an environment variable, never in the repo, and
    never under $OV -- $OV is a synced Drive mount, so a secret written there
    leaves the machine and stays in Drive's revision history.

    The keychain is preferred and tried first, but it cannot be relied on alone.
    Inside the routine sandbox the read blocks on an interaction prompt that no
    one will ever answer, which is worse than failing: an unattended job hangs
    until its timeout. So the read is hard-bounded, and a file fallback exists
    for exactly that case. The file is protected by permissions rather than by
    the keychain, which is a real reduction; it is the price of a delivery path
    that works unattended.
    """
    service = str(smtp.get("keychain_service") or "")
    account = str(smtp["username"])
    tried: list[str] = []

    if service:
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-w", "-s", service, "-a", account],
                capture_output=True,
                text=True,
                timeout=KEYCHAIN_TIMEOUT_SECONDS,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            tried.append(f"keychain {service!r}: {result.stderr.strip() or 'not found'}")
        except subprocess.TimeoutExpired:
            tried.append(
                f"keychain {service!r}: timed out after {KEYCHAIN_TIMEOUT_SECONDS}s "
                "(blocked on an interaction prompt this context cannot answer)"
            )

    raw_path = smtp.get("password_file")
    if raw_path:
        path = Path(str(raw_path)).expanduser()
        _refuse_vault_secret(path)
        if path.is_file():
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                raise SystemExit(
                    f"{fmt(path)} is mode {mode:o}; a password file must be 0600 "
                    "(chmod 600 it)"
                )
            password = path.read_text(encoding="utf-8").strip().replace(" ", "")
            if password:
                return password
            tried.append(f"{fmt(path)}: empty")
        else:
            tried.append(f"{fmt(path)}: missing")

    raise SystemExit(
        "no SMTP password available; tried " + "; ".join(tried or ["nothing configured"])
    )

def build_message(html_text: str, subject: str, sender: str, recipient: str):
    """One multipart/alternative message carrying the artifact verbatim."""
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    # A short plain-text alternative rather than a stripped rendering: a second
    # wording of the same document is the "parallel summary" the protocol warns
    # about, and every client that matters renders the HTML part.
    message.set_content(
        "This digest is an HTML message. If you are reading this, your client "
        "did not render it; the canonical copy is the artifact in your vault."
    )
    message.add_alternative(html_text, subtype="html")
    return message

def mail(
    ov: Path,
    html_text: str,
    subject: str,
    *,
    dry_run: bool = False,
) -> int:
    """Send the artifact to the configured account, and nowhere else."""
    import smtplib

    smtp = load_mail_config(ov)
    host = str(smtp["host"])
    port = int(smtp.get("port", 587))
    account = str(smtp["username"])
    size = len(html_text.encode("utf-8"))
    if size > GMAIL_CLIP_BYTES:
        print(
            f"warning: {size / 1024:.0f} KB message; Gmail clips past "
            f"{GMAIL_CLIP_BYTES // 1000} KB",
            file=sys.stderr,
        )
    if dry_run:
        print(f"would send {size / 1024:.0f} KB to {account} via {host}:{port}")
        print(f"subject: {subject}")
        return 0

    message = build_message(html_text, subject, account, account)
    password = smtp_password(smtp)
    try:
        with smtplib.SMTP(host, port, timeout=60) as server:
            server.starttls()
            server.login(account, password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        print(f"send failed: {exc!r}", file=sys.stderr)
        return 1
    print(f"sent {size / 1024:.0f} KB to {account}")
    return 0
