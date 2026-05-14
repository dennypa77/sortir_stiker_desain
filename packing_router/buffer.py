"""Buffer management: assign slot, find slot by SKU, add wadah, status, overflow."""
import sqlite3
from typing import Optional

from . import config
from .db import get_connection, log_event, now_iso, transaction
from .exceptions import BufferFullError, WadahConflictError
from .models import BufferLocation, BufferStatus, Wadah


def _row_to_buffer_location(row: sqlite3.Row) -> BufferLocation:
    return BufferLocation(
        buffer_slot_id=row["id"],
        wadah_id=row["wadah_id"],
        wadah_nomor=row["wadah_nomor"],
        slot_number=row["slot_number"],
        sku=row["sku"],
        plastik_count=row["plastik_count"] or 0,
        is_overflow_of=row["is_overflow_of"],
    )


def _select_buffer_slot_by_id(conn: sqlite3.Connection, slot_id: int) -> Optional[BufferLocation]:
    row = conn.execute(
        """
        SELECT bs.id, bs.wadah_id, w.nomor AS wadah_nomor, bs.slot_number,
               bs.sku, bs.plastik_count, bs.is_overflow_of
        FROM buffer_slot bs
        JOIN wadah w ON w.id = bs.wadah_id
        WHERE bs.id = ?
        """,
        (slot_id,),
    ).fetchone()
    return _row_to_buffer_location(row) if row else None


def find_buffer_slot_for_sku(sku: str, conn: Optional[sqlite3.Connection] = None) -> Optional[BufferLocation]:
    """Cari slot existing untuk SKU. Jika ada multiple (overflow), return primary
    (yang ``is_overflow_of IS NULL``)."""
    c = conn or get_connection()
    row = c.execute(
        """
        SELECT bs.id, bs.wadah_id, w.nomor AS wadah_nomor, bs.slot_number,
               bs.sku, bs.plastik_count, bs.is_overflow_of
        FROM buffer_slot bs
        JOIN wadah w ON w.id = bs.wadah_id
        WHERE bs.sku = ? AND w.is_active = 1
        ORDER BY (bs.is_overflow_of IS NULL) DESC, w.nomor ASC, bs.slot_number ASC
        LIMIT 1
        """,
        (sku,),
    ).fetchone()
    return _row_to_buffer_location(row) if row else None


def _find_empty_slot_sequential(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    """Strategy: isi sequential — wadah 1 slot 1, slot 2, ..., baru pindah ke
    wadah 2. Order: ``wadah_nomor ASC, slot_number ASC`` untuk slot yang
    ``sku IS NULL`` di wadah aktif."""
    return conn.execute(
        """
        SELECT bs.id, bs.wadah_id, w.nomor AS wadah_nomor, bs.slot_number,
               bs.sku, bs.plastik_count, bs.is_overflow_of
        FROM buffer_slot bs
        JOIN wadah w ON w.id = bs.wadah_id
        WHERE bs.sku IS NULL AND w.is_active = 1
        ORDER BY w.nomor ASC, bs.slot_number ASC
        LIMIT 1
        """
    ).fetchone()


def assign_buffer_slot(sku: str, conn: Optional[sqlite3.Connection] = None) -> BufferLocation:
    """Assign slot kosong untuk SKU baru. Caller harus sudah dalam transaction.

    Raise :class:`BufferFullError` jika tidak ada slot kosong di wadah aktif.
    """
    c = conn or get_connection()
    row = _find_empty_slot_sequential(c)
    if row is None:
        raise BufferFullError("Tidak ada slot kosong di wadah aktif")
    c.execute(
        "UPDATE buffer_slot SET sku = ?, first_plastik_at = NULL, "
        "last_plastik_at = NULL, plastik_count = 0 WHERE id = ?",
        (sku, row["id"]),
    )
    return _select_buffer_slot_by_id(c, row["id"])  # type: ignore[return-value]


def handle_buffer_overflow(sku: str, conn: Optional[sqlite3.Connection] = None) -> BufferLocation:
    """Saat slot SKU sticky penuh. Caller harus sudah dalam transaction.

    Cari primary slot SKU ini, lalu assign slot baru dengan ``is_overflow_of`` = primary id.
    Raise :class:`BufferFullError` jika tidak ada slot kosong tersisa.
    """
    if not config.ALLOW_BUFFER_OVERFLOW:
        raise BufferFullError(f"Slot SKU {sku} penuh dan ALLOW_BUFFER_OVERFLOW=False")
    c = conn or get_connection()
    primary = find_buffer_slot_for_sku(sku, conn=c)
    if primary is None:
        return assign_buffer_slot(sku, conn=c)
    empty_row = _find_empty_slot_sequential(c)
    if empty_row is None:
        raise BufferFullError("Buffer penuh — tidak ada slot kosong untuk overflow")
    c.execute(
        "UPDATE buffer_slot SET sku = ?, is_overflow_of = ?, "
        "first_plastik_at = NULL, last_plastik_at = NULL, plastik_count = 0 "
        "WHERE id = ?",
        (sku, primary.buffer_slot_id, empty_row["id"]),
    )
    return _select_buffer_slot_by_id(c, empty_row["id"])  # type: ignore[return-value]


def increment_buffer_slot(
    slot_id: int,
    conn: Optional[sqlite3.Connection] = None,
    bundle_count: int = 1,
) -> BufferLocation:
    """Increment ``plastik_count`` di slot, berdasarkan jumlah BUNDLE.

    Caller harus dalam transaction. 1 plastik kecil = 1 bundle = 10pcs;
    1 plastik bundle besar = 5 bundle = 50pcs. ``bundle_count`` lewatkan
    sesuai pack_units dari scan_handler (1 atau 5).

    Set ``first_plastik_at`` jika belum ada, update ``last_plastik_at`` selalu.
    """
    if bundle_count < 1:
        bundle_count = 1
    c = conn or get_connection()
    ts = now_iso()
    c.execute(
        "UPDATE buffer_slot "
        "SET plastik_count = plastik_count + ?, "
        "    first_plastik_at = COALESCE(first_plastik_at, ?), "
        "    last_plastik_at = ? "
        "WHERE id = ?",
        (bundle_count, ts, ts, slot_id),
    )
    return _select_buffer_slot_by_id(c, slot_id)  # type: ignore[return-value]


def decrement_buffer_slot(
    slot_id: int,
    conn: Optional[sqlite3.Connection] = None,
    bundle_count: int = 1,
) -> BufferLocation:
    """Decrement ``plastik_count`` sebanyak ``bundle_count``. Saat 0 → reset slot."""
    if bundle_count < 1:
        bundle_count = 1
    c = conn or get_connection()
    c.execute(
        "UPDATE buffer_slot SET plastik_count = MAX(0, plastik_count - ?) WHERE id = ?",
        (bundle_count, slot_id),
    )
    row = c.execute(
        "SELECT plastik_count FROM buffer_slot WHERE id = ?", (slot_id,)
    ).fetchone()
    if row and row["plastik_count"] == 0:
        c.execute(
            "UPDATE buffer_slot SET sku = NULL, first_plastik_at = NULL, "
            "last_plastik_at = NULL, is_overflow_of = NULL WHERE id = ?",
            (slot_id,),
        )
    return _select_buffer_slot_by_id(c, slot_id)  # type: ignore[return-value]


def add_wadah(capacity: int = None, actor: str = "admin") -> Wadah:
    """Dynamic add wadah baru. Auto-increment ``nomor``, auto-create slot kosong."""
    cap = capacity if capacity is not None else config.SLOTS_PER_WADAH
    conn = get_connection()
    with transaction(conn) as c:
        row = c.execute("SELECT COALESCE(MAX(nomor), 0) AS m FROM wadah").fetchone()
        next_nomor = row["m"] + 1
        c.execute(
            "INSERT INTO wadah (nomor, capacity, is_active) VALUES (?, ?, 1)",
            (next_nomor, cap),
        )
        wadah_row = c.execute(
            "SELECT id, nomor, capacity, is_active FROM wadah WHERE nomor = ?",
            (next_nomor,),
        ).fetchone()
        for s in range(1, cap + 1):
            c.execute(
                "INSERT INTO buffer_slot (wadah_id, slot_number, plastik_count) "
                "VALUES (?, ?, 0)",
                (wadah_row["id"], s),
            )
        log_event(
            "add_wadah",
            actor,
            "wadah",
            wadah_row["id"],
            {"nomor": next_nomor, "capacity": cap},
            conn=c,
        )
    return Wadah(
        id=wadah_row["id"],
        nomor=wadah_row["nomor"],
        capacity=wadah_row["capacity"],
        is_active=bool(wadah_row["is_active"]),
    )


def remove_last_wadah(actor: str = "admin") -> dict:
    """Hapus wadah dengan nomor terbesar. Tolak kalau ada plastik di slot-nya.

    Saat sukses:
    - Cancel pending/in_progress harvester_task yang reference buffer_slot wadah ini.
    - DELETE buffer_slot rows untuk wadah ini.
    - DELETE wadah row.

    Raise :class:`WadahConflictError` kalau wadah masih berisi plastik
    (``plastik_count > 0`` di slot mana pun). Harvest atau Reset Buffer dulu.
    """
    conn = get_connection()
    with transaction(conn) as c:
        last = c.execute(
            "SELECT id, nomor FROM wadah WHERE is_active = 1 "
            "ORDER BY nomor DESC LIMIT 1"
        ).fetchone()
        if last is None:
            raise WadahConflictError("Tidak ada wadah tersisa untuk dihapus")
        has_plastik = c.execute(
            "SELECT COUNT(*) AS c FROM buffer_slot "
            "WHERE wadah_id = ? AND plastik_count > 0",
            (last["id"],),
        ).fetchone()
        if has_plastik["c"] > 0:
            raise WadahConflictError(
                f"Wadah {last['nomor']} masih berisi plastik. "
                f"Harvest atau Reset Buffer dulu sebelum hapus wadah."
            )
        c.execute(
            "UPDATE harvester_task SET status = 'cancelled', completed_at = ? "
            "WHERE buffer_slot_id IN (SELECT id FROM buffer_slot WHERE wadah_id = ?) "
            "AND status IN ('pending', 'in_progress')",
            (now_iso(), last["id"]),
        )
        c.execute("DELETE FROM buffer_slot WHERE wadah_id = ?", (last["id"],))
        c.execute("DELETE FROM wadah WHERE id = ?", (last["id"],))
        log_event(
            "remove_wadah",
            actor,
            "wadah",
            last["id"],
            {"nomor": last["nomor"]},
            conn=c,
        )
    return {"removed_nomor": last["nomor"]}


def get_buffer_status() -> BufferStatus:
    conn = get_connection()
    breakdown_rows = conn.execute(
        """
        SELECT w.id, w.nomor, w.capacity, w.is_active,
               SUM(CASE WHEN bs.sku IS NOT NULL THEN 1 ELSE 0 END) AS terpakai,
               SUM(CASE WHEN bs.sku IS NULL THEN 1 ELSE 0 END) AS kosong,
               SUM(COALESCE(bs.plastik_count, 0)) AS total_plastik
        FROM wadah w
        LEFT JOIN buffer_slot bs ON bs.wadah_id = w.id
        GROUP BY w.id, w.nomor, w.capacity, w.is_active
        ORDER BY w.nomor ASC
        """
    ).fetchall()
    breakdown = []
    total_wadah_aktif = 0
    total_slot = 0
    slot_terpakai = 0
    slot_kosong = 0
    for r in breakdown_rows:
        is_active = bool(r["is_active"])
        if is_active:
            total_wadah_aktif += 1
            total_slot += r["capacity"]
            slot_terpakai += r["terpakai"] or 0
            slot_kosong += r["kosong"] or 0
        breakdown.append(
            {
                "wadah_id": r["id"],
                "nomor": r["nomor"],
                "capacity": r["capacity"],
                "is_active": is_active,
                "terpakai": r["terpakai"] or 0,
                "kosong": r["kosong"] or 0,
                "total_plastik": r["total_plastik"] or 0,
            }
        )
    return BufferStatus(
        total_wadah_aktif=total_wadah_aktif,
        total_slot=total_slot,
        slot_terpakai=slot_terpakai,
        slot_kosong=slot_kosong,
        breakdown=breakdown,
    )
