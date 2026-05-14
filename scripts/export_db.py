"""Export the full trades.db to a single JSON file.

Usage: py scripts/export_db.py [--db data/trades.db] [--out data/trades_export.json]
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone


def export(db_path: str, out_path: str) -> None:
    if not os.path.exists(db_path):
        print(f"DB not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    table_names = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]

    tables: dict[str, list[dict]] = {}
    for name in table_names:
        rows = conn.execute(f"SELECT * FROM {name}").fetchall()
        tables[name] = [dict(r) for r in rows]

    conn.close()

    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_db": os.path.abspath(db_path),
        "tables": tables,
    }

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    total_rows = sum(len(v) for v in tables.values())
    print(f"Wrote {out_path} — {len(tables)} table(s), {total_rows} row(s).")
    for name, rows in tables.items():
        print(f"  {name}: {len(rows)} row(s)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/trades.db")
    ap.add_argument("--out", default="data/trades_export.json")
    args = ap.parse_args()
    export(args.db, args.out)
