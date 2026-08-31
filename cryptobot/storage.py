"""SQLite: audit-журнал, персистенція paper-позицій та app_state."""

from __future__ import annotations

import json
import sqlite3
import threading
import time

from cryptobot import config, runtime
from cryptobot.util import number


db_lock = threading.Lock()
db_connection: sqlite3.Connection | None = None


def init_storage():
    global db_connection
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    db_connection = sqlite3.connect(
        config.DATA_DIR / "cryptobot.db", check_same_thread=False
    )
    db_connection.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at INTEGER NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )
    db_connection.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_positions (
            id TEXT PRIMARY KEY,
            payload TEXT NOT NULL
        )
        """
    )
    db_connection.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_closed (
            id TEXT PRIMARY KEY,
            closed_at INTEGER NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )
    db_connection.execute(
        """
        CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    db_connection.execute(
        """
        CREATE TABLE IF NOT EXISTS live_positions (
            id TEXT PRIMARY KEY,
            payload TEXT NOT NULL
        )
        """
    )
    db_connection.execute(
        """
        CREATE TABLE IF NOT EXISTS live_closed (
            id TEXT PRIMARY KEY,
            closed_at INTEGER NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )
    # Backfill closures produced by older versions from the durable audit journal.
    for created_at, payload in db_connection.execute(
        "SELECT created_at, payload FROM audit_events "
        "WHERE kind = 'paper_close' ORDER BY id DESC LIMIT 500"
    ).fetchall():
        try:
            closed = json.loads(payload)
            if closed.get("id"):
                db_connection.execute(
                    "INSERT OR IGNORE INTO paper_closed(id, closed_at, payload) VALUES (?, ?, ?)",
                    (closed["id"], int(closed.get("closedAt", created_at)), payload),
                )
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    db_connection.commit()
    rows = db_connection.execute("SELECT id, payload FROM paper_positions").fetchall()
    closed_rows = db_connection.execute(
        "SELECT payload FROM paper_closed ORDER BY closed_at DESC LIMIT 500"
    ).fetchall()
    live_rows = db_connection.execute("SELECT id, payload FROM live_positions").fetchall()
    live_closed_rows = db_connection.execute(
        "SELECT payload FROM live_closed ORDER BY closed_at DESC LIMIT 500"
    ).fetchall()
    state_rows = dict(db_connection.execute("SELECT key, value FROM app_state").fetchall())
    with runtime.paper_lock:
        for position_id, payload in rows:
            try:
                runtime.paper_positions[position_id] = json.loads(payload)
            except json.JSONDecodeError:
                pass
        for (payload,) in closed_rows:
            try:
                runtime.paper_closed.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
    with runtime.live_lock:
        for position_id, payload in live_rows:
            try:
                runtime.live_positions[position_id] = json.loads(payload)
            except json.JSONDecodeError:
                pass
        for (payload,) in live_closed_rows:
            try:
                runtime.live_closed.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
    runtime.automation_state["paused"] = state_rows.get("paused", "false") == "true"
    runtime.automation_state["killSwitch"] = state_rows.get("killSwitch", "false") == "true"
    runtime.automation_state["readinessNotified"] = (
        state_rows.get("readinessNotified", "false") == "true"
    )
    runtime.automation_state["lastReportAt"] = int(
        number(state_rows.get("lastReportAt"), runtime.automation_state["lastReportAt"])
    )


def audit(kind, payload):
    if db_connection is None:
        return
    with db_lock:
        db_connection.execute(
            "INSERT INTO audit_events(created_at, kind, payload) VALUES (?, ?, ?)",
            (int(time.time() * 1000), kind, json.dumps(payload, ensure_ascii=False)),
        )
        db_connection.commit()


def persist_app_state(key, value):
    if db_connection is None:
        return
    if isinstance(value, bool):
        encoded = "true" if value else "false"
    else:
        encoded = str(value)
    with db_lock:
        db_connection.execute(
            "INSERT OR REPLACE INTO app_state(key, value) VALUES (?, ?)",
            (key, encoded),
        )
        db_connection.commit()


def set_control_state(*, paused=None, kill_switch=None):
    if paused is not None:
        runtime.automation_state["paused"] = bool(paused)
        persist_app_state("paused", runtime.automation_state["paused"])
    if kill_switch is not None:
        runtime.automation_state["killSwitch"] = bool(kill_switch)
        persist_app_state("killSwitch", runtime.automation_state["killSwitch"])


def persist_paper(position):
    if db_connection is None:
        return
    with db_lock:
        db_connection.execute(
            "INSERT OR REPLACE INTO paper_positions(id, payload) VALUES (?, ?)",
            (position["id"], json.dumps(position, ensure_ascii=False)),
        )
        db_connection.commit()


def persist_closed_paper(closed):
    if db_connection is None:
        return
    with db_lock:
        db_connection.execute(
            "INSERT OR REPLACE INTO paper_closed(id, closed_at, payload) VALUES (?, ?, ?)",
            (
                closed["id"],
                int(closed["closedAt"]),
                json.dumps(closed, ensure_ascii=False),
            ),
        )
        db_connection.execute("DELETE FROM paper_positions WHERE id = ?", (closed["id"],))
        db_connection.commit()


def persist_live(position):
    if db_connection is None:
        return
    with db_lock:
        db_connection.execute(
            "INSERT OR REPLACE INTO live_positions(id, payload) VALUES (?, ?)",
            (position["id"], json.dumps(position, ensure_ascii=False)),
        )
        db_connection.commit()


def persist_closed_live(closed):
    if db_connection is None:
        return
    with db_lock:
        db_connection.execute(
            "INSERT OR REPLACE INTO live_closed(id, closed_at, payload) VALUES (?, ?, ?)",
            (
                closed["id"],
                int(closed.get("closedAt") or int(time.time() * 1000)),
                json.dumps(closed, ensure_ascii=False),
            ),
        )
        db_connection.execute("DELETE FROM live_positions WHERE id = ?", (closed["id"],))
        db_connection.commit()
