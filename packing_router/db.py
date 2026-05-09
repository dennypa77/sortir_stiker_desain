"""SQLite connection + schema init.

Per-thread connection (sqlite3 default). WAL mode + retry on busy.
File DB terpisah dari hasil/qc_data.db (existing QC station DB) — modul ini
WAJIB pakai hasil/packing_router.db sendiri.
"""
import json
import os
import random
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from . import config


_local = threading.local()


SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS wave (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bigseller_batch_id TEXT,
    wave_number INTEGER,
    status TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    activated_at TEXT,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS resi (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wave_id INTEGER REFERENCES wave(id),
    nomor_resi TEXT UNIQUE NOT NULL,
    slot_aktif_number INTEGER,
    status TEXT,
    setup_at TEXT,
    completed_at TEXT,
    packed_at TEXT
);

CREATE TABLE IF NOT EXISTS resi_item (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resi_id INTEGER REFERENCES resi(id) ON DELETE CASCADE,
    sku TEXT NOT NULL,
    varian INTEGER,
    quantity_ordered INTEGER NOT NULL,
    quantity_fulfilled INTEGER DEFAULT 0,
    prefilled_qty INTEGER DEFAULT 0,
    UNIQUE (resi_id, sku, varian)
);

CREATE TABLE IF NOT EXISTS wadah (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nomor INTEGER UNIQUE NOT NULL,
    capacity INTEGER DEFAULT 10,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS buffer_slot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wadah_id INTEGER REFERENCES wadah(id),
    slot_number INTEGER,
    sku TEXT,
    plastik_count INTEGER DEFAULT 0,
    first_plastik_at TEXT,
    last_plastik_at TEXT,
    is_overflow_of INTEGER REFERENCES buffer_slot(id),
    UNIQUE (wadah_id, slot_number)
);

CREATE TABLE IF NOT EXISTS plastik (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    barcode TEXT NOT NULL,
    sku TEXT NOT NULL,
    varian INTEGER,
    location_type TEXT,
    location_ref INTEGER,
    scanned_at TEXT,
    placed_at TEXT
);

CREATE TABLE IF NOT EXISTS harvester_task (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    buffer_slot_id INTEGER REFERENCES buffer_slot(id),
    target_resi_id INTEGER REFERENCES resi(id),
    sku TEXT,
    status TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT,
    actor TEXT,
    entity_type TEXT,
    entity_id INTEGER,
    payload TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS slot_aktif (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nomor INTEGER UNIQUE NOT NULL,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_resi_status ON resi(status);
CREATE INDEX IF NOT EXISTS idx_resi_slot ON resi(slot_aktif_number);
CREATE INDEX IF NOT EXISTS idx_resi_item_sku ON resi_item(sku, varian);
CREATE INDEX IF NOT EXISTS idx_buffer_slot_sku ON buffer_slot(sku);
CREATE INDEX IF NOT EXISTS idx_plastik_barcode ON plastik(barcode);
CREATE INDEX IF NOT EXISTS idx_harvester_task_status ON harvester_task(status);
CREATE INDEX IF NOT EXISTS idx_event_log_created ON event_log(created_at);
CREATE INDEX IF NOT EXISTS idx_event_log_actor ON event_log(actor, created_at);
CREATE INDEX IF NOT EXISTS idx_slot_aktif_nomor ON slot_aktif(nomor);
"""


def _ensure_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def _new_connection(path: str) -> sqlite3.Connection:
    _ensure_dir(path)
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def get_connection(path: Optional[str] = None) -> sqlite3.Connection:
    """Return per-thread connection. Auto-init schema saat first connect."""
    db_path = path or config.DB_PATH
    cached = getattr(_local, "conn", None)
    cached_path = getattr(_local, "path", None)
    if cached is not None and cached_path == db_path:
        return cached
    if cached is not None:
        try:
            cached.close()
        except Exception:
            pass
    conn = _new_connection(db_path)
    _local.conn = conn
    _local.path = db_path
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_DDL)
    _migrate_add_prefilled_qty(conn)


def _migrate_add_prefilled_qty(conn: sqlite3.Connection) -> None:
    """Idempotent migration: tambah kolom prefilled_qty kalau DB lama tidak punya."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(resi_item)").fetchall()}
    if "prefilled_qty" not in cols:
        conn.execute("ALTER TABLE resi_item ADD COLUMN prefilled_qty INTEGER DEFAULT 0")


def reset_connection() -> None:
    """For tests — close current thread's connection so next get_connection() reconnects."""
    cached = getattr(_local, "conn", None)
    if cached is not None:
        try:
            cached.close()
        except Exception:
            pass
    _local.conn = None
    _local.path = None


@contextmanager
def transaction(conn: Optional[sqlite3.Connection] = None) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE + retry on SQLITE_BUSY (no SELECT FOR UPDATE in SQLite)."""
    c = conn or get_connection()
    last_exc: Optional[BaseException] = None
    for attempt in range(config.SQLITE_BUSY_RETRY_COUNT + 1):
        try:
            c.execute("BEGIN IMMEDIATE;")
            try:
                yield c
                c.execute("COMMIT;")
                return
            except BaseException:
                try:
                    c.execute("ROLLBACK;")
                except sqlite3.Error:
                    pass
                raise
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" in msg or "busy" in msg:
                last_exc = e
                if attempt < config.SQLITE_BUSY_RETRY_COUNT:
                    backoff_ms = config.SQLITE_BUSY_RETRY_BASE_MS * (2 ** attempt)
                    time.sleep((backoff_ms + random.randint(0, backoff_ms)) / 1000.0)
                    continue
            raise
    if last_exc is not None:
        raise last_exc


def log_event(
    event_type: str,
    actor: str,
    entity_type: str,
    entity_id: Optional[int],
    payload: Optional[dict] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """Insert event_log row. Caller bertanggung jawab untuk transaction context."""
    c = conn or get_connection()
    payload_str = json.dumps(payload, default=str) if payload is not None else None
    cur = c.execute(
        "INSERT INTO event_log (event_type, actor, entity_type, entity_id, payload) "
        "VALUES (?, ?, ?, ?, ?)",
        (event_type, actor, entity_type, entity_id, payload_str),
    )
    return cur.lastrowid


def init_default_wadah() -> None:
    """Dipanggil saat first run kalau wadah belum ada — bikin default N wadah × M slot."""
    conn = get_connection()
    cur = conn.execute("SELECT COUNT(*) AS c FROM wadah;")
    if cur.fetchone()["c"] > 0:
        return
    with transaction(conn) as c:
        for i in range(1, config.DEFAULT_WADAH_COUNT + 1):
            c.execute(
                "INSERT INTO wadah (nomor, capacity, is_active) VALUES (?, ?, 1)",
                (i, config.SLOTS_PER_WADAH),
            )
            wadah_id = c.execute(
                "SELECT id FROM wadah WHERE nomor = ?", (i,)
            ).fetchone()["id"]
            for s in range(1, config.SLOTS_PER_WADAH + 1):
                c.execute(
                    "INSERT INTO buffer_slot (wadah_id, slot_number, plastik_count) "
                    "VALUES (?, ?, 0)",
                    (wadah_id, s),
                )
        log_event(
            "add_wadah",
            "system",
            "wadah",
            None,
            {"reason": "init_default", "count": config.DEFAULT_WADAH_COUNT},
            conn=c,
        )


def now_iso() -> str:
    """ISO timestamp string konsisten dengan CURRENT_TIMESTAMP SQLite."""
    return time.strftime("%Y-%m-%d %H:%M:%S")
