"""No-secret healthcheck for the manager and expected client bot count."""
import argparse
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from dotenv import load_dotenv
from runtime_health import clear_polling_conflicts, polling_conflict_count


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
DEFAULT_DB = Path(os.getenv("CUSTOMERSERVICEAI_DB_PATH", ROOT / "customerserviceai.db"))
TEST_SHOP_ID = os.getenv("TEST_SHOP_ID", "").strip()
MAIN_PATH = str(ROOT / "main.py")
CLIENT_PATH = str(ROOT / "client_bot.py")


def classify_process_argv(argv: list[str]) -> str | None:
    """Classify only direct Python launches of this project's entry points."""
    if not argv or not Path(argv[0]).name.startswith("python"):
        return None
    if CLIENT_PATH in argv[1:]:
        return "client"
    if MAIN_PATH in argv[1:]:
        return "manager"
    return None


def process_counts() -> tuple[int, int]:
    manager = 0
    clients = 0
    for cmdline_path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            argv = [
                arg.decode("utf-8", "replace")
                for arg in cmdline_path.read_bytes().split(b"\0")
                if arg
            ]
        except (OSError, PermissionError):
            continue
        process_type = classify_process_argv(argv)
        if process_type == "client":
            clients += 1
        elif process_type == "manager":
            manager += 1
    return manager, clients


def expected_clients(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    now = datetime.now(UTC).replace(tzinfo=None).isoformat()
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)) as conn:
        row = conn.execute(
            """SELECT COUNT(*)
               FROM shops sh
               LEFT JOIN subscriptions s ON s.shop_id = sh.shop_id
               WHERE sh.client_bot_token != ''
                 AND ((s.paid_until IS NOT NULL AND s.paid_until > ?)
                   OR (s.trial_end IS NOT NULL AND s.trial_end > ?)
                   OR (? != '' AND sh.shop_id = ?))""",
            (now, now, TEST_SHOP_ID, TEST_SHOP_ID),
        ).fetchone()
    return int(row[0]) if row else 0


def evaluate_health(manager: int, clients: int, expected: int, process_only: bool = False) -> bool:
    return manager == 1 and (process_only or clients == expected)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--process-only", action="store_true")
    parser.add_argument("--clear-conflicts", action="store_true")
    args = parser.parse_args()

    if args.clear_conflicts:
        print(json.dumps({"cleared_polling_conflicts": clear_polling_conflicts()}))
        return 0

    manager, clients = process_counts()
    expected = clients if args.process_only else expected_clients(args.db)
    conflicts = polling_conflict_count()
    healthy = not conflicts and evaluate_health(manager, clients, expected, args.process_only)
    print(json.dumps({
        "healthy": healthy,
        "manager": manager,
        "clients": clients,
        "expected_clients": expected,
        "polling_conflicts": conflicts,
    }, sort_keys=True))
    if conflicts:
        return 2
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
