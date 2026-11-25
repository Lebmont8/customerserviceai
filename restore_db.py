"""Verify and restore a backup to an explicit target path."""
import argparse
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from backup_db import backup


ROOT = Path(__file__).resolve().parent
LIVE_DB = Path(os.getenv("CUSTOMERSERVICEAI_DB_PATH", ROOT / "customerserviceai.db")).resolve()


def verify(path: Path):
    with closing(sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=10)) as conn:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError("source backup integrity_check failed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--allow-live-target", action="store_true")
    args = parser.parse_args()
    source = args.source.resolve()
    target = args.target.resolve()
    if target == LIVE_DB and not args.allow_live_target:
        raise SystemExit("refusing live restore without --allow-live-target")
    verify(source)
    backup(source, target)
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
