#!/usr/bin/env python3
"""Daily backup of the babytracker SQLite DB.

Uses the sqlite3 backup API (consistent even while the app is writing),
writes a dated copy into backups/, and prunes to keep the newest N copies.
Exit code 0 on success; prints one line per backup.
"""

import os
import sqlite3
import sys
from datetime import datetime

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("DB_PATH", os.path.join(APP_DIR, "data", "baby.db"))
BACKUP_DIR = os.path.join(APP_DIR, "backups")
KEEP = 30


def main() -> int:
    if not os.path.exists(DB_PATH):
        print(f"backup FAILED: db not found at {DB_PATH}")
        return 1
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    dest = os.path.join(BACKUP_DIR, f"babytracker-{stamp}.db")

    src = sqlite3.connect(DB_PATH, timeout=15)
    out = sqlite3.connect(dest)
    with out:
        src.backup(out)
    out.close()
    src.close()

    # Prune: keep the newest KEEP files.
    backups = sorted(
        f for f in os.listdir(BACKUP_DIR) if f.startswith("babytracker-") and f.endswith(".db")
    )
    for old in backups[:-KEEP]:
        os.remove(os.path.join(BACKUP_DIR, old))

    # Sanity check: the copy must be a readable DB.
    check = sqlite3.connect(dest)
    n = check.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    check.close()
    # Silent on success (cron delivers stdout verbatim; empty = no message).
    if os.environ.get("BACKUP_VERBOSE"):
        print(f"backup OK: {dest} ({os.path.getsize(dest)} bytes, {n} events)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
