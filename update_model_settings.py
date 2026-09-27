#!/usr/bin/env python3
"""Update per-chat model overrides in bot_settings to match the new default model.

The bot's default model is controlled by the CLAUDE_MODEL environment variable,
but individual chats can override it via the /model command, which stores a row
in bot_settings with key='model'. When the deployment-wide default model changes,
those per-chat overrides can be left pointing at the old model. This script finds
every such override and updates it to the new target model.

Usage:
    python update_model_settings.py

Configuration:
    DATABASE_URL   - PostgreSQL connection string (required)
    TARGET_MODEL   - Model id to set per-chat overrides to (default: claude-opus-5-5)

The script is idempotent: running it multiple times is safe, since it always sets
the affected rows to the same target value.
"""

import os
import sys

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
TARGET_MODEL = os.environ.get("TARGET_MODEL", "claude-opus-5-5").strip()
SETTING_KEY = "model"


def _connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def _fetch_model_rows(cur):
    cur.execute(
        "SELECT chat_id, value FROM bot_settings WHERE key = %s ORDER BY chat_id",
        (SETTING_KEY,),
    )
    return cur.fetchall()


def main() -> int:
    print(f"Target model: {TARGET_MODEL!r}", flush=True)

    with _connect() as conn:
        with conn.cursor() as cur:
            before_rows = _fetch_model_rows(cur)

            print(f"Found {len(before_rows)} row(s) with key='{SETTING_KEY}' before update:", flush=True)
            for row in before_rows:
                print(f"  chat_id={row['chat_id']} value={row['value']!r}", flush=True)

            if not before_rows:
                print("No per-chat model overrides found. Nothing to update.", flush=True)
                return 0

            cur.execute(
                "UPDATE bot_settings SET value = %s WHERE key = %s",
                (TARGET_MODEL, SETTING_KEY),
            )
            updated_count = cur.rowcount
        conn.commit()

    print(f"Updated {updated_count} row(s) with key='{SETTING_KEY}' to value={TARGET_MODEL!r}.", flush=True)

    with _connect() as conn:
        with conn.cursor() as cur:
            after_rows = _fetch_model_rows(cur)

    print(f"Final state: {len(after_rows)} row(s) with key='{SETTING_KEY}' after update:", flush=True)
    for row in after_rows:
        print(f"  chat_id={row['chat_id']} value={row['value']!r}", flush=True)

    mismatched = [row for row in after_rows if row["value"] != TARGET_MODEL]
    if mismatched:
        print(
            f"WARNING: {len(mismatched)} row(s) do not match the target model after update: {mismatched}",
            flush=True,
        )
        return 1

    print("Verification succeeded: all per-chat model overrides match the target model.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
