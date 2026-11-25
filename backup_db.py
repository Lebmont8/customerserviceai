"""Create a consistent SQLite backup without exposing row contents."""
import argparse
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = Path(os.getenv("CUSTOMERSERVICEAI_DB_PATH", ROOT / "customerserviceai.db"))


def backup(source: Path, output: Path) -> Path:
    source = source.resolve()
    output = output.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"source database not found: {source}")
    if source == output:
        raise ValueError("backup target must differ from source")
    output.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10)) as src:
        with closing(sqlite3.connect(output, timeout=10)) as dst:
            src.backup(dst)
            result = dst.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError("backup integrity_check failed")
    os.chmod(output, 0o600)
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or ROOT / "backups" / f"customerserviceai-{datetime.utcnow():%Y%m%dT%H%M%SZ}.db"
    print(backup(args.source, output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
