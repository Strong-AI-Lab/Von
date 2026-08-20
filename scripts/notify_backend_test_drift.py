#!/usr/bin/env python3
"""Email the nightly backend test drift result.

JVNAUTOSCI-2656. A report nobody reads is decorative, and GitHub's default for a
scheduled workflow is to mail whoever last edited the cron expression, which is
an accident of authorship rather than a decision. This sends the result to a
named recipient instead.

Sends only when there is something to act on: newly failing tests, or a run that
broke before it could report. A nightly that mails on success trains its reader
to ignore it.

Credentials come from the environment and are never logged. If they are absent
this exits quietly rather than failing the run, so the suite result is not lost
behind a notification problem:

    NIGHTLY_SMTP_USERNAME       account to authenticate as
    NIGHTLY_SMTP_APP_PASSWORD   app password, not the account password
    NIGHTLY_SMTP_HOST           default smtp.gmail.com
    NIGHTLY_SMTP_PORT           default 587, STARTTLS
    NIGHTLY_NOTIFY_TO           default zhanvonwitbrock@gmail.com
    NIGHTLY_NOTIFY_CC           default witbrock@gmail.com
"""

from __future__ import annotations

import os
import smtplib
import ssl
import sys
from email.message import EmailMessage

DEFAULT_RECIPIENT = "zhanvonwitbrock@gmail.com"
# Copied while the notification path is new and its reliability unproven.
DEFAULT_CC = "witbrock@gmail.com"
DEFAULT_HOST = "smtp.gmail.com"
DEFAULT_PORT = 587


def build_message(status: str, body: str) -> EmailMessage:
    sender = os.environ["NIGHTLY_SMTP_USERNAME"]
    recipient = os.environ.get("NIGHTLY_NOTIFY_TO", DEFAULT_RECIPIENT)
    cc = os.environ.get("NIGHTLY_NOTIFY_CC", DEFAULT_CC)

    repo = os.environ.get("GITHUB_REPOSITORY", "Von")
    run_id = os.environ.get("GITHUB_RUN_ID")
    run_url = (
        f"https://github.com/{repo}/actions/runs/{run_id}" if run_id else "(local run)"
    )

    subject = {
        "new_failures": f"[{repo}] Nightly backend tests: new failures",
        "broken": f"[{repo}] Nightly backend tests: the run itself failed",
    }.get(status, f"[{repo}] Nightly backend tests: {status}")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    if cc and cc != recipient:
        message["Cc"] = cc
    message.set_content(
        f"{body.strip()}\n\n"
        f"Run: {run_url}\n\n"
        "Newly failing tests should be fixed or reverted. Adding them to\n"
        "ci/known_test_failures.txt is not a remedy: that list may only shrink.\n"
    )
    return message


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: notify_backend_test_drift.py <status> [body-file]")
        return 2
    status = sys.argv[1]
    body = ""
    if len(sys.argv) > 2 and os.path.exists(sys.argv[2]):
        body = open(sys.argv[2], encoding="utf-8").read()

    username = os.environ.get("NIGHTLY_SMTP_USERNAME")
    password = os.environ.get("NIGHTLY_SMTP_APP_PASSWORD")
    if not (username and password):
        # Deliberately not an error. The suite result matters more than the
        # notification, and a missing secret should not turn a green run red.
        print(
            "NIGHTLY_SMTP_USERNAME or NIGHTLY_SMTP_APP_PASSWORD is unset; "
            "skipping the notification. Add both as repository secrets to "
            "enable it."
        )
        return 0

    host = os.environ.get("NIGHTLY_SMTP_HOST", DEFAULT_HOST)
    port = int(os.environ.get("NIGHTLY_SMTP_PORT", DEFAULT_PORT))
    message = build_message(status, body)

    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(username, password)
            smtp.send_message(message)
    except Exception as exc:
        # Report the class of failure, never the credentials.
        print(f"notification failed ({type(exc).__name__}): {exc}")
        return 1

    recipients = message["To"] + (f", cc {message['Cc']}" if message["Cc"] else "")
    print(f"notified {recipients}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
