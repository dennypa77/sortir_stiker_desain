"""Manajemen slot aktif (rak fisik resi).

Default ``DEFAULT_SLOT_AKTIF_COUNT`` slot saat first init. Bisa tambah slot
runtime via ``add_slot_aktif()`` (mirip pola ``add_wadah`` di buffer).
"""
from typing import List, Optional

from . import config
from .db import get_connection, log_event, transaction
from .exceptions import SlotAktifConflictError


def init_default_slot_aktif() -> None:
    """Bikin default slot aktif kalau tabel masih kosong."""
    conn = get_connection()
    cur = conn.execute("SELECT COUNT(*) AS c FROM slot_aktif")
    if cur.fetchone()["c"] > 0:
        return
    with transaction(conn) as c:
        for n in range(1, config.DEFAULT_SLOT_AKTIF_COUNT + 1):
            c.execute("INSERT INTO slot_aktif (nomor, is_active) VALUES (?, 1)", (n,))
        log_event(
            "add_slot_aktif",
            "system",
            "slot_aktif",
            None,
            {"reason": "init_default", "count": config.DEFAULT_SLOT_AKTIF_COUNT},
            conn=c,
        )


def add_slot_aktif(actor: str = "admin", count: int = 1) -> List[int]:
    """Tambah ``count`` slot baru. Auto-increment nomor (max + 1).

    Return list of nomor slot baru yang ter-add.
    """
    if count < 1:
        return []
    added: List[int] = []
    conn = get_connection()
    with transaction(conn) as c:
        row = c.execute("SELECT COALESCE(MAX(nomor), 0) AS m FROM slot_aktif").fetchone()
        start_n = (row["m"] or 0) + 1
        for i in range(count):
            nomor = start_n + i
            c.execute("INSERT INTO slot_aktif (nomor, is_active) VALUES (?, 1)", (nomor,))
            added.append(nomor)
        log_event(
            "add_slot_aktif",
            actor,
            "slot_aktif",
            None,
            {"added": added},
            conn=c,
        )
    return added


def get_slot_aktif_numbers(active_only: bool = True) -> List[int]:
    """Return list nomor slot urut ascending."""
    conn = get_connection()
    if active_only:
        rows = conn.execute(
            "SELECT nomor FROM slot_aktif WHERE is_active = 1 ORDER BY nomor ASC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT nomor FROM slot_aktif ORDER BY nomor ASC"
        ).fetchall()
    return [r["nomor"] for r in rows]


def get_slot_aktif_count(active_only: bool = True) -> int:
    nums = get_slot_aktif_numbers(active_only=active_only)
    return len(nums)


def remove_last_slot_aktif(actor: str = "admin", count: int = 1) -> List[int]:
    """Hapus ``count`` slot aktif paling akhir (max nomor).

    Tolak (raise :class:`SlotAktifConflictError`) kalau slot yang akan dihapus
    sedang dipakai resi ``active`` atau ``complete``. Pack atau cancel resi-nya
    dulu sebelum hapus slot.

    Return list nomor slot yang berhasil dihapus.
    """
    if count < 1:
        return []
    removed: List[int] = []
    conn = get_connection()
    with transaction(conn) as c:
        for _ in range(count):
            last = c.execute(
                "SELECT id, nomor FROM slot_aktif WHERE is_active = 1 "
                "ORDER BY nomor DESC LIMIT 1"
            ).fetchone()
            if last is None:
                raise SlotAktifConflictError("Tidak ada slot aktif tersisa untuk dihapus")
            in_use = c.execute(
                "SELECT id, nomor_resi FROM resi WHERE slot_aktif_number = ? "
                "AND status IN ('active', 'complete')",
                (last["nomor"],),
            ).fetchone()
            if in_use is not None:
                raise SlotAktifConflictError(
                    f"Slot {last['nomor']} sedang dipakai resi {in_use['nomor_resi']}. "
                    f"Pack atau cancel resi-nya dulu sebelum hapus slot."
                )
            c.execute("DELETE FROM slot_aktif WHERE id = ?", (last["id"],))
            removed.append(last["nomor"])
        log_event(
            "remove_slot_aktif",
            actor,
            "slot_aktif",
            None,
            {"removed": removed},
            conn=c,
        )
    return removed


def slot_aktif_exists(nomor: int) -> bool:
    conn = get_connection()
    row = conn.execute(
        "SELECT 1 FROM slot_aktif WHERE nomor = ? AND is_active = 1",
        (nomor,),
    ).fetchone()
    return row is not None
