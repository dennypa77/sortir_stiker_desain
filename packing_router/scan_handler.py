"""Event 1: scan plastik di stasiun sortir.

Logic:
1. Lookup plastik by barcode (auto-create kalau belum ada — derive sku & varian dari barcode).
2. SELECT resi aktif yang butuh SKU ini → place_in_slot_aktif.
3. Else cek buffer existing → place_in_buffer_existing.
4. Else assign slot baru → place_in_buffer_new.

Semua di dalam BEGIN IMMEDIATE TRANSACTION untuk consistency.
"""
import sqlite3
from typing import Optional

from . import config
from .buffer import (
    assign_buffer_slot,
    find_buffer_slot_for_sku,
    handle_buffer_overflow,
    increment_buffer_slot,
)
from .db import get_connection, log_event, now_iso, transaction
from .exceptions import BarcodeFormatError
from .models import ScanResult
from .utils import parse_barcode


def _ensure_plastik(
    conn: sqlite3.Connection, barcode: str, pack_units: int = 1
) -> sqlite3.Row:
    """Selalu create plastik row baru per scan.

    HOG pakai barcode = numeric ID design (mis. ``445``) — banyak plastik fisik
    bisa punya barcode sama (bukan unique per plastik). Tiap scan = 1 plastik
    fisik baru → tiap scan jadi row baru di DB. Tidak ada lookup-and-reuse.

    Untuk dedup accidental double-scan operator, pakai tombol Undo dalam window
    30 detik.

    ``pack_units`` = jumlah pack 10pcs di plastik fisik ini. Default 1 (= 10pcs).
    Untuk plastik bundle 50pcs, operator pilih toggle 50 → pack_units=5.
    """
    numeric_id = parse_barcode(barcode)
    cur = conn.execute(
        "INSERT INTO plastik (barcode, sku, varian, scanned_at, pack_units) "
        "VALUES (?, ?, NULL, ?, ?)",
        (barcode, numeric_id, now_iso(), pack_units),
    )
    return conn.execute(
        "SELECT id, barcode, sku, varian, location_type, location_ref, pack_units "
        "FROM plastik WHERE id = ?",
        (cur.lastrowid,),
    ).fetchone()


def _find_active_resi_needing_sku(
    conn: sqlite3.Connection, numeric_id: str
) -> Optional[sqlite3.Row]:
    """Cari resi 'active' yang butuh stiker dengan ``numeric_id`` (= ID desain).

    Match berdasarkan ID saja — varian tidak relevant karena 1 plastik = 1 pack
    10pcs. Resi varian besar (20/50/100pcs) butuh multi-pack, tetap match dari
    plastik yang sama.

    Prioritas: slot aktif terkecil (FIFO setup), lalu item terkecil.
    """
    return conn.execute(
        """
        SELECT r.id AS resi_id, r.nomor_resi, r.slot_aktif_number,
               ri.id AS item_id, ri.sku, ri.varian,
               ri.quantity_ordered, ri.quantity_fulfilled, ri.prefilled_qty
        FROM resi r
        JOIN resi_item ri ON ri.resi_id = r.id
        WHERE r.status = 'active'
          AND r.slot_aktif_number IS NOT NULL
          AND ri.sku = ?
          AND (ri.quantity_ordered - COALESCE(ri.prefilled_qty, 0) - COALESCE(ri.quantity_fulfilled, 0)) > 0
        ORDER BY r.slot_aktif_number ASC, ri.id ASC
        LIMIT 1
        """,
        (numeric_id,),
    ).fetchone()


def _maybe_complete_resi(conn: sqlite3.Connection, resi_id: int) -> bool:
    """Cek apakah semua resi_item sudah fulfilled (prefilled + fulfilled >= ordered).
    Kalau ya, transition active→complete. Return True kalau baru transitioned."""
    row = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM resi_item
             WHERE resi_id = ?
               AND (quantity_ordered - COALESCE(prefilled_qty, 0) - COALESCE(quantity_fulfilled, 0)) > 0
            ) AS missing,
            (SELECT status FROM resi WHERE id = ?) AS current_status
        """,
        (resi_id, resi_id),
    ).fetchone()
    if row["missing"] == 0 and row["current_status"] == "active":
        conn.execute(
            "UPDATE resi SET status = 'complete', completed_at = ? WHERE id = ?",
            (now_iso(), resi_id),
        )
        return True
    return False


def handle_scan_plastik(
    barcode: str,
    operator_id: str,
    conn: Optional[sqlite3.Connection] = None,
    pack_units: int = 1,
) -> ScanResult:
    """Event 1 entry point.

    Note: caller bisa pass ``conn`` untuk reuse transaction (mis. dipanggil dari
    ``cancel_resi`` saat re-route plastik balik ke buffer).

    ``pack_units`` = jumlah pack 10pcs dalam 1 plastik fisik. Default 1 (10pcs).
    Operator switch ke 5 untuk plastik bundle 50pcs.
    """
    use_outer_conn = conn is not None
    c = conn or get_connection()
    barcode = barcode.strip()
    if not barcode:
        raise BarcodeFormatError("Barcode kosong")
    if pack_units < 1:
        pack_units = 1

    if use_outer_conn:
        result = _scan_inner(c, barcode, operator_id, pack_units)
    else:
        with transaction(c) as tc:
            result = _scan_inner(tc, barcode, operator_id, pack_units)
    return result


def _scan_inner(
    c: sqlite3.Connection, barcode: str, operator_id: str, pack_units: int = 1
) -> ScanResult:
    plastik = _ensure_plastik(c, barcode, pack_units=pack_units)
    sku = plastik["sku"]  # numeric_id
    varian = plastik["varian"]  # bisa NULL untuk plastik HOG (varian agnostic)
    plastik_id = plastik["id"]

    resi_match = _find_active_resi_needing_sku(c, sku)
    if resi_match is not None:
        c.execute(
            "UPDATE resi_item SET quantity_fulfilled = quantity_fulfilled + ? WHERE id = ?",
            (pack_units, resi_match["item_id"]),
        )
        c.execute(
            "UPDATE plastik SET location_type = 'slot_aktif', location_ref = ?, "
            "placed_at = ? WHERE id = ?",
            (resi_match["resi_id"], now_iso(), plastik_id),
        )
        completed = _maybe_complete_resi(c, resi_match["resi_id"])
        target_main = f"SLOT {resi_match['slot_aktif_number']}"
        target_suffix = f"(RESI {resi_match['nomor_resi']})"
        target_label = f"LETAKKAN KE {target_main} {target_suffix}"
        result = ScanResult(
            action="place_in_slot_aktif",
            target_label=target_label,
            target_prefix="LETAKKAN KE",
            target_main=target_main,
            target_suffix=target_suffix,
            barcode=barcode,
            sku=sku,
            varian=varian,
            target_slot_aktif_number=resi_match["slot_aktif_number"],
            target_resi_id=resi_match["resi_id"],
            target_resi_nomor=resi_match["nomor_resi"],
            extra={"resi_completed": completed, "pack_units": pack_units},
        )
        log_event(
            "scan",
            operator_id,
            "plastik",
            plastik_id,
            {
                "barcode": barcode,
                "sku": sku,
                "varian": varian,
                "action": result.action,
                "target_resi_id": resi_match["resi_id"],
                "target_resi_item_id": resi_match["item_id"],
                "target_slot_aktif_number": resi_match["slot_aktif_number"],
                "resi_completed": completed,
                "pack_units": pack_units,
            },
            conn=c,
        )
        return result

    existing = find_buffer_slot_for_sku(sku, conn=c)
    if existing is not None:
        if existing.plastik_count >= config.OVERFLOW_TRIGGER_COUNT:
            target = handle_buffer_overflow(sku, conn=c)
            target = increment_buffer_slot(
                target.buffer_slot_id, conn=c, bundle_count=pack_units
            )
            action = "place_in_buffer_new"
            extra = {"overflow_of": existing.buffer_slot_id}
            target_main = target.label()
            target_suffix = (
                f"(overflow dari WADAH {existing.wadah_nomor} SLOT {existing.slot_number})"
            )
            target_label = f"LETAKKAN KE {target_main} {target_suffix}"
        else:
            target = increment_buffer_slot(
                existing.buffer_slot_id, conn=c, bundle_count=pack_units
            )
            action = "place_in_buffer_existing"
            extra = {}
            target_main = target.label()
            target_suffix = f"(sudah berisi {target.plastik_count - pack_units} bundle)"
            target_label = f"LETAKKAN KE {target_main} {target_suffix}"
    else:
        target = assign_buffer_slot(sku, conn=c)
        target = increment_buffer_slot(
            target.buffer_slot_id, conn=c, bundle_count=pack_units
        )
        action = "place_in_buffer_new"
        extra = {}
        target_main = target.label()
        target_suffix = "(slot baru)"
        target_label = f"LETAKKAN KE {target_main} {target_suffix}"

    c.execute(
        "UPDATE plastik SET location_type = 'buffer', location_ref = ?, "
        "placed_at = ? WHERE id = ?",
        (target.buffer_slot_id, now_iso(), plastik_id),
    )

    extra_with_pack = {**extra, "pack_units": pack_units}
    result = ScanResult(
        action=action,
        target_label=target_label,
        target_prefix="LETAKKAN KE",
        target_main=target_main,
        target_suffix=target_suffix,
        barcode=barcode,
        sku=sku,
        varian=varian,
        target_buffer_slot_id=target.buffer_slot_id,
        existing_plastik_count=target.plastik_count,
        extra=extra_with_pack,
    )
    log_event(
        "scan",
        operator_id,
        "plastik",
        plastik_id,
        {
            "barcode": barcode,
            "sku": sku,
            "varian": varian,
            "action": action,
            "target_buffer_slot_id": target.buffer_slot_id,
            "wadah_nomor": target.wadah_nomor,
            "slot_number": target.slot_number,
            "plastik_count_after": target.plastik_count,
            **extra_with_pack,
        },
        conn=c,
    )
    return result
