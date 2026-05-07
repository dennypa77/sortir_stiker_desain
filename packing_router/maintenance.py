"""cancel_resi, undo_last_scan, pack_resi, mark_resi_done, reset_slot_aktif, reset_buffer."""
import json
import sqlite3
from typing import Optional

from . import config
from .buffer import decrement_buffer_slot, increment_buffer_slot
from .db import get_connection, log_event, now_iso, transaction
from .exceptions import ResiNotFoundError, SlotAktifConflictError, UndoWindowExpiredError
from .models import UndoResult
from .scan_handler import handle_scan_plastik


def pack_resi(
    resi_id: int,
    actor: str = "packer",
) -> dict:
    """Transition resi 'complete' → 'packed'. Release ``slot_aktif_number``.
    Hook untuk Google Sheets log diserahkan ke caller (web layer)."""
    conn = get_connection()
    with transaction(conn) as c:
        row = c.execute(
            "SELECT id, status, slot_aktif_number, nomor_resi FROM resi WHERE id = ?",
            (resi_id,),
        ).fetchone()
        if row is None:
            raise ResiNotFoundError(f"Resi id={resi_id} tidak ditemukan")
        if row["status"] != "complete":
            raise ResiNotFoundError(
                f"Resi id={resi_id} status='{row['status']}', butuh 'complete' untuk pack"
            )
        c.execute(
            "UPDATE resi SET status = 'packed', packed_at = ?, slot_aktif_number = NULL "
            "WHERE id = ?",
            (now_iso(), resi_id),
        )
        c.execute(
            "UPDATE plastik SET location_type = 'packed' "
            "WHERE location_type = 'slot_aktif' AND location_ref = ?",
            (resi_id,),
        )
        log_event("pack", actor, "resi", resi_id, {"nomor_resi": row["nomor_resi"]}, conn=c)
    return {"resi_id": resi_id, "nomor_resi": row["nomor_resi"]}


def cancel_resi(resi_id: int, actor: str = "admin") -> dict:
    """Cancel resi mid-flow. Plastik di Slot Aktif resi ini di-route ulang via
    ``handle_scan_plastik`` (mungkin masuk slot aktif lain yang butuh, atau balik
    ke buffer)."""
    conn = get_connection()
    with transaction(conn) as c:
        row = c.execute(
            "SELECT id, nomor_resi, status, slot_aktif_number FROM resi WHERE id = ?",
            (resi_id,),
        ).fetchone()
        if row is None:
            raise ResiNotFoundError(f"Resi id={resi_id} tidak ditemukan")
        if row["status"] in ("packed", "cancelled"):
            return {"resi_id": resi_id, "already": row["status"]}

        plastiks = c.execute(
            "SELECT id, barcode, sku, varian FROM plastik "
            "WHERE location_type = 'slot_aktif' AND location_ref = ?",
            (resi_id,),
        ).fetchall()

        c.execute(
            "UPDATE resi SET status = 'cancelled', slot_aktif_number = NULL WHERE id = ?",
            (resi_id,),
        )
        c.execute(
            "UPDATE resi_item SET quantity_fulfilled = 0 WHERE resi_id = ?",
            (resi_id,),
        )
        c.execute(
            "UPDATE harvester_task SET status = 'cancelled' "
            "WHERE target_resi_id = ? AND status IN ('pending', 'in_progress')",
            (resi_id,),
        )
        # In-transit plastik untuk task yang baru di-cancel → balik ke slot buffer asalnya.
        cancelled_tasks = c.execute(
            "SELECT id, buffer_slot_id FROM harvester_task "
            "WHERE target_resi_id = ? AND status = 'cancelled'",
            (resi_id,),
        ).fetchall()
        for ct in cancelled_tasks:
            in_transit = c.execute(
                "SELECT id FROM plastik WHERE location_type = 'in_transit' AND location_ref = ?",
                (ct["id"],),
            ).fetchall()
            for p in in_transit:
                c.execute(
                    "UPDATE plastik SET location_type = 'buffer', location_ref = ? WHERE id = ?",
                    (ct["buffer_slot_id"], p["id"]),
                )
                increment_buffer_slot(ct["buffer_slot_id"], conn=c)

        rerouted = []
        for p in plastiks:
            c.execute(
                "UPDATE plastik SET location_type = NULL, location_ref = NULL WHERE id = ?",
                (p["id"],),
            )
            re = handle_scan_plastik(p["barcode"], operator_id=actor, conn=c)
            rerouted.append({"barcode": p["barcode"], "action": re.action})

        log_event(
            "cancel",
            actor,
            "resi",
            resi_id,
            {"nomor_resi": row["nomor_resi"], "rerouted": rerouted},
            conn=c,
        )
    return {"resi_id": resi_id, "nomor_resi": row["nomor_resi"], "rerouted": rerouted}


def undo_last_scan(operator_id: str, within_seconds: Optional[int] = None) -> UndoResult:
    """Rollback scan terakhir operator dalam window detik."""
    window = within_seconds if within_seconds is not None else config.UNDO_WINDOW_SECONDS
    conn = get_connection()
    with transaction(conn) as c:
        row = c.execute(
            """
            SELECT id, entity_id, payload, created_at,
                   CAST((julianday('now') - julianday(created_at)) * 86400 AS REAL) AS age_sec
            FROM event_log
            WHERE event_type = 'scan' AND actor = ?
            ORDER BY id DESC LIMIT 1
            """,
            (operator_id,),
        ).fetchone()
        if row is None:
            raise UndoWindowExpiredError(f"Tidak ada scan terakhir untuk operator '{operator_id}'")
        if row["age_sec"] is not None and row["age_sec"] > window:
            raise UndoWindowExpiredError(
                f"Scan terakhir {row['age_sec']:.1f} detik yang lalu, melebihi window {window}s"
            )
        already_undone = c.execute(
            "SELECT id FROM event_log WHERE event_type = 'undo' "
            "AND payload LIKE ? LIMIT 1",
            (f'%"undone_event_id": {row["id"]}%',),
        ).fetchone()
        if already_undone is not None:
            raise UndoWindowExpiredError(f"Scan event id={row['id']} sudah pernah di-undo")

        payload = json.loads(row["payload"]) if row["payload"] else {}
        action = payload.get("action")
        plastik_id = row["entity_id"]
        detail = ""

        if action == "place_in_slot_aktif":
            item_id = payload.get("target_resi_item_id")
            resi_id = payload.get("target_resi_id")
            if item_id is not None:
                c.execute(
                    "UPDATE resi_item SET quantity_fulfilled = MAX(0, quantity_fulfilled - 1) "
                    "WHERE id = ?",
                    (item_id,),
                )
            if resi_id is not None and payload.get("resi_completed"):
                c.execute(
                    "UPDATE resi SET status = 'active', completed_at = NULL WHERE id = ?",
                    (resi_id,),
                )
            c.execute(
                "UPDATE plastik SET location_type = NULL, location_ref = NULL, "
                "placed_at = NULL WHERE id = ?",
                (plastik_id,),
            )
            detail = f"Decrement resi_item id={item_id}, plastik id={plastik_id} cleared"

        elif action == "place_in_buffer_existing":
            slot_id = payload.get("target_buffer_slot_id")
            if slot_id is not None:
                decrement_buffer_slot(slot_id, conn=c)
            c.execute(
                "UPDATE plastik SET location_type = NULL, location_ref = NULL, "
                "placed_at = NULL WHERE id = ?",
                (plastik_id,),
            )
            detail = f"Decrement buffer_slot id={slot_id}, plastik id={plastik_id} cleared"

        elif action == "place_in_buffer_new":
            slot_id = payload.get("target_buffer_slot_id")
            if slot_id is not None:
                decrement_buffer_slot(slot_id, conn=c)
            c.execute(
                "UPDATE plastik SET location_type = NULL, location_ref = NULL, "
                "placed_at = NULL WHERE id = ?",
                (plastik_id,),
            )
            detail = f"Reset buffer_slot id={slot_id}, plastik id={plastik_id} cleared"

        else:
            raise UndoWindowExpiredError(f"Action '{action}' tidak bisa di-undo")

        log_event(
            "undo",
            operator_id,
            "plastik",
            plastik_id,
            {"undone_event_id": row["id"], "action": action, "detail": detail},
            conn=c,
        )
        return UndoResult(event_id=row["id"], action_undone=action, detail=detail)


def mark_resi_done(resi_id: int, actor: str = "operator") -> dict:
    """Force-mark resi → 'complete' walaupun ``quantity_fulfilled < quantity_ordered``.

    Use case: 1 plastik fisik bisa berisi banyak pcs (mis. 50pcs sudah bundled).
    Sistem hitung 1 scan = 1 pack 10pcs, tapi realita-nya plastik itu mungkin
    sudah cukup untuk fulfill seluruh resi. Operator klik tombol Done untuk
    konfirmasi manual bahwa resi siap pack.

    Slot resi otomatis berubah dari kuning (semua SKU tersentuh, qty kurang) →
    hijau (siap pack).
    """
    conn = get_connection()
    with transaction(conn) as c:
        resi = c.execute(
            "SELECT id, nomor_resi, status FROM resi WHERE id = ?",
            (resi_id,),
        ).fetchone()
        if resi is None:
            raise ResiNotFoundError(f"Resi id={resi_id} tidak ditemukan")
        if resi["status"] != "active":
            raise SlotAktifConflictError(
                f"Resi status='{resi['status']}', butuh 'active' untuk mark Done"
            )
        c.execute(
            "UPDATE resi SET status = 'complete', completed_at = ? WHERE id = ?",
            (now_iso(), resi_id),
        )
        log_event(
            "mark_done",
            actor,
            "resi",
            resi_id,
            {"nomor_resi": resi["nomor_resi"], "forced": True},
            conn=c,
        )
    return {"resi_id": resi_id, "nomor_resi": resi["nomor_resi"]}


def reset_slot_aktif(actor: str = "admin") -> dict:
    """Reset semua slot aktif. Resi 'active'/'complete' → 'pending', slot dilepas.

    - Resi: ``status='pending'``, ``slot_aktif_number=NULL``, ``setup_at=NULL``,
      ``completed_at=NULL``.
    - resi_item: ``quantity_fulfilled=0``.
    - Plastik di slot_aktif/in_transit: ``location_type='returned'``.
    - Harvester task pending/in_progress: cancelled.
    """
    conn = get_connection()
    with transaction(conn) as c:
        affected = [
            r["id"]
            for r in c.execute(
                "SELECT id FROM resi WHERE status IN ('active', 'complete') "
                "AND slot_aktif_number IS NOT NULL"
            ).fetchall()
        ]
        c.execute(
            "UPDATE resi SET slot_aktif_number = NULL, status = 'pending', "
            "setup_at = NULL, completed_at = NULL "
            "WHERE status IN ('active', 'complete')"
        )
        if affected:
            placeholders = ",".join("?" * len(affected))
            c.execute(
                f"UPDATE resi_item SET quantity_fulfilled = 0 "
                f"WHERE resi_id IN ({placeholders})",
                affected,
            )
        c.execute(
            "UPDATE plastik SET location_type = 'returned', location_ref = NULL "
            "WHERE location_type IN ('slot_aktif', 'in_transit')"
        )
        c.execute(
            "UPDATE harvester_task SET status = 'cancelled', completed_at = ? "
            "WHERE status IN ('pending', 'in_progress')",
            (now_iso(),),
        )
        log_event(
            "reset_slot_aktif",
            actor,
            "system",
            None,
            {"affected_resi_count": len(affected)},
            conn=c,
        )
    return {"affected_resi_count": len(affected)}


def reset_buffer(actor: str = "admin") -> dict:
    """Reset semua buffer slot ke kosong.

    - buffer_slot: ``sku=NULL``, ``plastik_count=0``, timestamp NULL,
      ``is_overflow_of=NULL``.
    - Plastik di buffer: ``location_type='returned'``.
    - Harvester task pending/in_progress: cancelled (buffer source-nya kosong).
    """
    conn = get_connection()
    with transaction(conn) as c:
        plastik_count = c.execute(
            "SELECT COUNT(*) AS c FROM plastik WHERE location_type = 'buffer'"
        ).fetchone()["c"]
        c.execute(
            "UPDATE plastik SET location_type = 'returned', location_ref = NULL "
            "WHERE location_type = 'buffer'"
        )
        c.execute(
            "UPDATE buffer_slot SET sku = NULL, plastik_count = 0, "
            "first_plastik_at = NULL, last_plastik_at = NULL, "
            "is_overflow_of = NULL"
        )
        c.execute(
            "UPDATE harvester_task SET status = 'cancelled', completed_at = ? "
            "WHERE status IN ('pending', 'in_progress')",
            (now_iso(),),
        )
        log_event(
            "reset_buffer",
            actor,
            "system",
            None,
            {"plastik_returned": plastik_count},
            conn=c,
        )
    return {"plastik_returned": plastik_count}
