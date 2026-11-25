#!/usr/bin/env python3
"""Safely validate and install a rotated Telegram manager-bot token."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.error
import urllib.request


EXPECTED_BOT_ID = 8679975695
EXPECTED_USERNAME = "ownercustomerservicebot"
TOKEN_RE = re.compile(r"^[0-9]{6,15}:[A-Za-z0-9_-]{20,}$")


def validate_token(token: str) -> tuple[int, str]:
    if not TOKEN_RE.fullmatch(token):
        raise ValueError("Token format is invalid")

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/getMe",
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ValueError(f"Telegram validation failed: {type(exc).__name__}") from None

    result = payload.get("result") or {}
    if not payload.get("ok"):
        raise ValueError("Telegram rejected the token")
    return int(result.get("id") or 0), str(result.get("username") or "").lower()


def replace_env_token(env_path: Path, token: str) -> None:
    content = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    replacement = f"MANAGER_BOT_TOKEN={token}"
    pattern = re.compile(r"(?m)^MANAGER_BOT_TOKEN=.*$")
    if pattern.search(content):
        updated = pattern.sub(replacement, content, count=1)
    else:
        separator = "" if not content or content.endswith("\n") else "\n"
        updated = f"{content}{separator}{replacement}\n"

    env_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=".env.rotate-",
        dir=env_path.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temporary:
            temporary.write(updated)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, env_path)
        os.chmod(env_path, 0o600)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install a new token for @ownercustomerservicebot without echoing it."
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=Path(__file__).resolve().parent / ".env",
    )
    args = parser.parse_args()

    token = getpass.getpass("New @ownercustomerservicebot token: ").strip()
    bot_id, username = validate_token(token)
    if bot_id != EXPECTED_BOT_ID or username != EXPECTED_USERNAME:
        raise SystemExit("Refusing token: it belongs to a different Telegram bot")

    replace_env_token(args.env, token)
    print("MANAGER_BOT_TOKEN updated safely; token value was not printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
