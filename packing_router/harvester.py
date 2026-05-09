"""Harvester flow: double-scan (pickup→dropoff) + quick harvest (one-click)."""
import sqlite3
from typing import Optional

from .buffer import decrement_buffer_slot, find_buffer_slot_for_sku
from .db import get_connection, log_event, now_iso, transaction
from .exceptions import HarvesterMismatchError, ResiNotFoundError, SlotAktifConflictError
from .models import HarvesterDropoffResult, HarvesterPickupResult
from .scan_handler import _ensure_plastik, _maybe_complete_resi


def _resolve_buffer_label(conn: sqlite3.Connection, slot_id: int) -> str:
    row = conn.execute(
        "SELECT w.nomor AS wadah_nomor, bs.slot_number "
        "FROM buffer_slot bs JOIN wadah w ON w.id = bs.wadah_id WHERE bs.id = ?",
        (slot_id,),
    ).fetchone()
    return f"WADAH {row['wadah_nomor']} SLOT {row['slot_number']}" if row else "?"


def harvester_pickup_scan(
    barcode: str,
    harvester_id: str,
    conn: Optional[sqlite3.Connection] = None,
) -> HarvesterPickupResult:
    """Scan plastik di buffer (sebelum ambil).

    Validasi:
    - Plastik dengan barcode ini exist & ada di buffer.
    - Ada ``harvester_task`` ``status='pending'`` dengan SKU & buffer_slot_id sama.

    Side effects:
    - Decrement ``plastik_count`` slot buffer.
    - Update ``plastik.location_type='in_transit'``.
    - Mark task ``status='in_progress'``, set ``started_at``.
    """
    use_outer = conn is not None
    c = conn or get_connection()

    def _do(tc: sqlite3.Connection) -> HarvesterPickupResult:
        plastik = tc.execute(
            "SELECT id, sku, varian, location_type, location_ref FROM plastik "
            "WHERE barcode = ? AND location_type = 'buffer' "
            "ORDER BY placed_at ASC, id ASC LIMIT 1",
            (barcode,),
        ).fetchone()
        if plastik is None:
            raise HarvesterMismatchError(
                f"Plastik barcode '{barcode}' tidak ditemukan di buffer"
            )
        task = tc.execute(
            """
            SELECT ht.id AS task_id, ht.buffer_slot_id, ht.target_resi_id, ht.sku,
                   r.slot_aktif_number, r.nomor_resi
            FROM harvester_task ht
            JOIN resi r ON r.id = ht.target_resi_id
            WHERE ht.status = 'pending'
              AND ht.sku = ?
              AND ht.buffer_slot_id = ?
            ORDER BY ht.created_at ASC, ht.id ASC
            LIMIT 1
            """,
            (plastik["sku"], plastik["location_ref"]),
        ).fetchone()
        if task is None:
            raise HarvesterMismatchError(
                f"Tidak ada harvester_task pending untuk SKU {plastik['sku']} "
                f"di buffer_slot {plastik['location_ref']}"
            )
        tc.execute(
            "UPDATE harvester_task SET status = 'in_progress', started_at = ? WHERE id = ?",
            (now_iso(), task["task_id"]),
        )
        decrement_buffer_slot(plastik["location_ref"], conn=tc)
        tc.execute(
            "UPDATE plastik SET location_type = 'in_transit', location_ref = ? WHERE id = ?",
            (task["task_id"], plastik["id"]),
        )
        result = HarvesterPickupResult(
            task_id=task["task_id"],
            barcode=barcode,
            sku=plastik["sku"],
            buffer_slot_id=task["buffer_slot_id"],
            buffer_label=_resolve_buffer_label(tc, task["buffer_slot_id"]),
            target_slot_aktif_number=task["slot_aktif_number"],
            target_resi_nomor=task["nomor_resi"],
        )
        log_event(
            "harvest_pickup",
            harvester_id,
            "harvester_task",
            task["task_id"],
            {
                "barcode": barcode,
                "sku": plastik["sku"],
                "buffer_slot_id": task["buffer_slot_id"],
                "target_resi_id": task["target_resi_id"],
            },
            conn=tc,
        )
        return result

    if use_outer:
        return _do(c)
    with transaction(c) as tc:
        return _do(tc)


def harvester_dropoff_scan(
    barcode: str,
    target_slot_aktif_number: int,
    harvester_id: str,
    conn: Optional[sqlite3.Connection] = None,
) -> HarvesterDropoffResult:
    """Scan plastik di Slot Aktif (saat dropoff).

    Validasi:
    - Plastik exist, location_type='in_transit'.
    - Ada task ``in_progress`` dengan plastik.location_ref == task.id.
    - ``target_slot_aktif_number`` sesuai dengan ``resi.slot_aktif_number`` di task.

    Side effects:
    - Update plastik ``location_type='slot_aktif'``, ``location_ref=resi_id``, ``placed_at``.
    - Increment ``resi_item.quantity_fulfilled`` (ambil item pertama yang masih kurang).
    - Mark task ``status='done'``, ``completed_at``.
    - Cek transisi resi ``active → complete``.
    """
    use_outer = conn is not None
    c = conn or get_connection()

    def _do(tc: sqlite3.Connection) -> HarvesterDropoffResult:
        plastik = tc.execute(
            "SELECT id, sku, varian, location_type, location_ref FROM plastik "
            "WHERE barcode = ? AND location_type = 'in_transit' "
            "ORDER BY id ASC LIMIT 1",
            (barcode,),
        ).fetchone()
        if plastik is None:
            raise HarvesterMismatchError(
                f"Plastik barcode '{barcode}' tidak ada di status in_transit"
            )
        task = tc.execute(
            """
            SELECT ht.id AS task_id, ht.target_resi_id, ht.sku, ht.buffer_slot_id,
                   r.slot_aktif_number, r.nomor_resi
            FROM harvester_task ht
            JOIN resi r ON r.id = ht.target_resi_id
            WHERE ht.id = ? AND ht.status = 'in_progress' AND ht.sku = ?
            """,
            (plastik["location_ref"], plastik["sku"]),
        ).fetchone()
        if task is None:
            raise HarvesterMismatchError(
                f"Task untuk plastik {barcode} (location_ref={plastik['location_ref']}) "
                f"tidak ditemukan / sudah selesai"
            )
        if task["slot_aktif_number"] != target_slot_aktif_number:
            raise HarvesterMismatchError(
                f"Slot tujuan salah: task expect slot {task['slot_aktif_number']}, "
                f"scan ke slot {target_slot_aktif_number}"
            )
        item = tc.execute(
            """
            SELECT id, quantity_fulfilled, quantity_ordered, prefilled_qty FROM resi_item
            WHERE resi_id = ? AND sku = ?
              AND (quantity_ordered - COALESCE(prefilled_qty, 0) - COALESCE(quantity_fulfilled, 0)) > 0
            ORDER BY id ASC LIMIT 1
            """,
            (task["target_resi_id"], plastik["sku"]),
        ).fetchone()
        if item is None:
            raise HarvesterMismatchError(
                f"Resi {task['nomor_resi']} sudah tidak butuh SKU {plastik['sku']}"
            )
        tc.execute(
            "UPDATE resi_item SET quantity_fulfilled = quantity_fulfilled + 1 WHERE id = ?",
            (item["id"],),
        )
        tc.execute(
            "UPDATE plastik SET location_type = 'slot_aktif', location_ref = ?, "
            "placed_at = ? WHERE id = ?",
            (task["target_resi_id"], now_iso(), plastik["id"]),
        )
        tc.execute(
            "UPDATE harvester_task SET status = 'done', completed_at = ? WHERE id = ?",
            (now_iso(), task["task_id"]),
        )
        completed = _maybe_complete_resi(tc, task["target_resi_id"])
        result = HarvesterDropoffResult(
            task_id=task["task_id"],
            barcode=barcode,
            target_slot_aktif_number=target_slot_aktif_number,
            target_resi_nomor=task["nomor_resi"],
            resi_completed=completed,
        )
        log_event(
            "harvest_dropoff",
            harvester_id,
            "harvester_task",
            task["task_id"],
            {
                "barcode": barcode,
                "sku": plastik["sku"],
                "target_resi_id": task["target_resi_id"],
                "target_slot_aktif_number": target_slot_aktif_number,
                "resi_completed": completed,
            },
            conn=tc,
        )
        return result

    if use_outer:
        return _do(c)
    with transaction(c) as tc:
        return _do(tc)


def quick_harvest_to_resi(
    resi_id: int,
    actor: str = "admin",
) -> dict:
    """One-click pindah semua plastik dari buffer ke slot aktif resi ini.

    Untuk setiap ``resi_item`` yang masih kurang:
    - Cari ``buffer_slot`` dengan SKU sama (primary, FIFO).
    - Pick plastik FIFO sebanyak ``min(plastik_count, sisa)``.
    - Transfer: ``location_type='buffer' → 'slot_aktif'``,
      ``location_ref=resi_id``, ``placed_at=now``.
    - Decrement buffer count, increment ``quantity_fulfilled``.
    - Cancel pending ``harvester_task`` untuk combo ini (sudah ditangani langsung).

    Tidak ada validasi scan fisik — operator tetap harus pindah plastik secara
    fisik dari buffer ke slot aktif sesuai instruksi yang ditampilkan.
    """
    conn = get_connection()
    moved: list = []
    completed = False
    with transaction(conn) as c:
        resi = c.execute(
            "SELECT id, nomor_resi, slot_aktif_number, status FROM resi WHERE id = ?",
            (resi_id,),
        ).fetchone()
        if resi is None:
            raise ResiNotFoundError(f"Resi id={resi_id} tidak ditemukan")
        if resi["status"] not in ("active", "complete"):
            raise SlotAktifConflictError(
                f"Resi status='{resi['status']}', butuh active/complete"
            )
        items = c.execute(
            "SELECT id, sku, varian, quantity_ordered, quantity_fulfilled, prefilled_qty "
            "FROM resi_item WHERE resi_id = ? "
            "AND (quantity_ordered - COALESCE(prefilled_qty, 0) - COALESCE(quantity_fulfilled, 0)) > 0 "
            "ORDER BY id ASC",
            (resi_id,),
        ).fetchall()
        for item in items:
            sisa = (
                item["quantity_ordered"]
                - (item["prefilled_qty"] or 0)
                - (item["quantity_fulfilled"] or 0)
            )
            slot = find_buffer_slot_for_sku(item["sku"], conn=c)
            if slot is None or slot.plastik_count <= 0:
                continue
            n_move = min(slot.plastik_count, sisa)
            plastiks = c.execute(
                "SELECT id, barcode FROM plastik "
                "WHERE location_type = 'buffer' AND location_ref = ? "
                "ORDER BY placed_at ASC, id ASC LIMIT ?",
                (slot.buffer_slot_id, n_move),
            ).fetchall()
            for p in plastiks:
                c.execute(
                    "UPDATE plastik SET location_type = 'slot_aktif', "
                    "location_ref = ?, placed_at = ? WHERE id = ?",
                    (resi_id, now_iso(), p["id"]),
                )
                decrement_buffer_slot(slot.buffer_slot_id, conn=c)
                c.execute(
                    "UPDATE resi_item SET quantity_fulfilled = quantity_fulfilled + 1 "
                    "WHERE id = ?",
                    (item["id"],),
                )
                moved.append(
                    {
                        "barcode": p["barcode"],
                        "sku": item["sku"],
                        "wadah_nomor": slot.wadah_nomor,
                        "slot_number": slot.slot_number,
                        "to_slot_aktif": resi["slot_aktif_number"],
                    }
                )
            c.execute(
                "UPDATE harvester_task SET status = 'cancelled', completed_at = ? "
                "WHERE target_resi_id = ? AND sku = ? AND status = 'pending'",
                (now_iso(), resi_id, item["sku"]),
            )
        if moved:
            completed = _maybe_complete_resi(c, resi_id)
        log_event(
            "quick_harvest",
            actor,
            "resi",
            resi_id,
            {
                "nomor_resi": resi["nomor_resi"],
                "moved_count": len(moved),
                "resi_completed": completed,
            },
            conn=c,
        )
    pending: list = []
    for item in conn.execute(
        "SELECT sku, varian, quantity_ordered, quantity_fulfilled, prefilled_qty "
        "FROM resi_item WHERE resi_id = ? "
        "AND (quantity_ordered - COALESCE(prefilled_qty, 0) - COALESCE(quantity_fulfilled, 0)) > 0 "
        "ORDER BY id ASC",
        (resi_id,),
    ).fetchall():
        pending.append(
            {
                "sku": item["sku"],
                "varian": item["varian"],
                "kurang": (
                    item["quantity_ordered"]
                    - (item["prefilled_qty"] or 0)
                    - (item["quantity_fulfilled"] or 0)
                ),
            }
        )
    return {"moved": moved, "pending": pending, "resi_completed": completed}
