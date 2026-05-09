"""Test fitur 'Sudah dari Stok Gudang' (stabilo) — per-SKU button di slot card.

Workflow baru (post-revert preview):
1. Operator scan resi → setup ke slot aktif (1-step, no preview).
2. Untuk SKU yang sudah dari stok gudang (stabilo), operator klik tombol
   '📦 Gudang' di slot card → ``mark_resi_item_prefilled`` set
   ``prefilled_qty = quantity_ordered``.
3. Resi auto-complete kalau semua item sudah ter-fulfill (prefilled+fulfilled
   >= ordered).
"""
import pytest

from packing_router.db import get_connection
from packing_router.exceptions import ResiNotFoundError, SlotAktifConflictError
from packing_router.maintenance import (
    cancel_resi,
    mark_resi_item_prefilled,
)
from packing_router.reports import get_slot_aktif_match_status
from packing_router.resi_setup import (
    setup_resi_by_nomor,
    sync_list_pesanan_to_db,
)
from packing_router.scan_handler import handle_scan_plastik


def _sync_resi(nomor_resi: str, sku_rows):
    rows = []
    for sku, jumlah in sku_rows:
        rows.append({
            "Batch_ID": "B-PREFILL",
            "Nomor_Resi": nomor_resi,
            "SKU": sku,
            "Jumlah": jumlah,
        })
    sync_list_pesanan_to_db(sheet_rows=rows)


def _resi_id(nomor_resi: str) -> int:
    conn = get_connection()
    return conn.execute(
        "SELECT id FROM resi WHERE nomor_resi = ?", (nomor_resi,)
    ).fetchone()["id"]


def _resi_status(nomor_resi: str) -> str:
    conn = get_connection()
    return conn.execute(
        "SELECT status FROM resi WHERE nomor_resi = ?", (nomor_resi,)
    ).fetchone()["status"]


def _item_id(nomor_resi: str, sku: str) -> int:
    conn = get_connection()
    return conn.execute(
        """
        SELECT ri.id FROM resi_item ri
        JOIN resi r ON r.id = ri.resi_id
        WHERE r.nomor_resi = ? AND ri.sku = ?
        """,
        (nomor_resi, sku),
    ).fetchone()["id"]


class TestSetupSimple:
    def test_setup_no_prefill_normal(self, buffer_seeded):
        """Setup resi 1-step, semua SKU mulai dari nol."""
        _sync_resi("R-NORM", [("445-A-10pcs", 1)])
        result = setup_resi_by_nomor("R-NORM")
        assert result.slot_number == 1
        assert _resi_status("R-NORM") == "active"


class TestMarkPrefilled:
    def test_mark_single_sku_prefilled(self, buffer_seeded):
        """ResiA pesan SKUA dan SKUB. Klik gudang untuk SKUA → SKUA prefilled,
        sisa SKUB."""
        _sync_resi("R-AB", [("445-A-10pcs", 1), ("999-B-10pcs", 1)])
        setup_resi_by_nomor("R-AB")
        rid = _resi_id("R-AB")
        item_a = _item_id("R-AB", "445")

        result = mark_resi_item_prefilled(rid, item_a)
        assert result["is_prefilled"] is True
        assert result["prefilled_qty"] == 1
        assert result["sku"] == "445"
        assert result["resi_completed"] is False  # SKU 999 belum

        # State DB
        conn = get_connection()
        item_a_row = conn.execute(
            "SELECT prefilled_qty FROM resi_item WHERE id = ?", (item_a,)
        ).fetchone()
        assert item_a_row["prefilled_qty"] == 1

    def test_mark_all_skus_completes_resi(self, buffer_seeded):
        """Mark semua SKU prefilled → resi langsung complete."""
        _sync_resi("R-ALL", [("445-A-10pcs", 1), ("999-B-10pcs", 1)])
        setup_resi_by_nomor("R-ALL")
        rid = _resi_id("R-ALL")
        mark_resi_item_prefilled(rid, _item_id("R-ALL", "445"))
        result = mark_resi_item_prefilled(rid, _item_id("R-ALL", "999"))
        assert result["resi_completed"] is True
        assert _resi_status("R-ALL") == "complete"

    def test_mark_then_scan_remaining_completes_resi(self, buffer_seeded):
        """Skenario user: SKUA mark gudang, lalu SKUB di-scan via plastik
        weeding → resi complete."""
        _sync_resi("R-SCAN", [("445-A-10pcs", 1), ("999-B-10pcs", 1)])
        setup_resi_by_nomor("R-SCAN")
        rid = _resi_id("R-SCAN")
        mark_resi_item_prefilled(rid, _item_id("R-SCAN", "445"))
        # Scan plastik 999 dari weeding → harus match resi & complete
        scan = handle_scan_plastik("999", operator_id="op")
        assert scan.action == "place_in_slot_aktif"
        assert scan.target_resi_nomor == "R-SCAN"
        assert scan.extra.get("resi_completed") is True
        assert _resi_status("R-SCAN") == "complete"

    def test_toggle_unmark_reverts_status(self, buffer_seeded):
        """Mark prefilled → resi complete. Toggle off (klik tombol Lepas) →
        resi balik ke active, prefilled_qty=0."""
        _sync_resi("R-TOG", [("445-A-10pcs", 1)])
        setup_resi_by_nomor("R-TOG")
        rid = _resi_id("R-TOG")
        item_id = _item_id("R-TOG", "445")
        # Pertama → mark
        r1 = mark_resi_item_prefilled(rid, item_id)
        assert r1["is_prefilled"] is True
        assert _resi_status("R-TOG") == "complete"
        # Kedua → toggle off
        r2 = mark_resi_item_prefilled(rid, item_id)
        assert r2["is_prefilled"] is False
        assert r2["prefilled_qty"] == 0
        assert _resi_status("R-TOG") == "active"

    def test_mark_cancels_pending_harvester_task(self, buffer_seeded):
        """Buffer punya SKU 445. Resi butuh 445 → harvester task dibuat saat
        setup. Lalu mark 445 sebagai dari gudang → task pending di-cancel."""
        # Pre-seed buffer
        handle_scan_plastik("445", operator_id="op")
        _sync_resi("R-HV", [("445-A-10pcs", 1)])
        setup_resi_by_nomor("R-HV")
        rid = _resi_id("R-HV")
        conn = get_connection()
        # Pastikan ada task pending dulu
        n_before = conn.execute(
            "SELECT COUNT(*) AS c FROM harvester_task "
            "WHERE target_resi_id = ? AND status = 'pending'",
            (rid,),
        ).fetchone()["c"]
        assert n_before == 1
        # Mark prefilled
        mark_resi_item_prefilled(rid, _item_id("R-HV", "445"))
        # Task pending sekarang harus cancelled
        n_after = conn.execute(
            "SELECT COUNT(*) AS c FROM harvester_task "
            "WHERE target_resi_id = ? AND status = 'pending'",
            (rid,),
        ).fetchone()["c"]
        n_cancelled = conn.execute(
            "SELECT COUNT(*) AS c FROM harvester_task "
            "WHERE target_resi_id = ? AND status = 'cancelled'",
            (rid,),
        ).fetchone()["c"]
        assert n_after == 0
        assert n_cancelled == 1

    def test_mark_unknown_resi_raises(self, buffer_seeded):
        with pytest.raises(ResiNotFoundError):
            mark_resi_item_prefilled(99999, 1)

    def test_mark_unknown_item_raises(self, buffer_seeded):
        _sync_resi("R-ER", [("445-A-10pcs", 1)])
        setup_resi_by_nomor("R-ER")
        rid = _resi_id("R-ER")
        with pytest.raises(ResiNotFoundError):
            mark_resi_item_prefilled(rid, 99999)

    def test_mark_packed_resi_raises(self, buffer_seeded):
        from packing_router.maintenance import pack_resi
        _sync_resi("R-PK", [("445-A-10pcs", 1)])
        setup_resi_by_nomor("R-PK")
        rid = _resi_id("R-PK")
        # Mark all prefilled → complete → pack
        mark_resi_item_prefilled(rid, _item_id("R-PK", "445"))
        pack_resi(rid)
        # Coba mark lagi setelah packed → raise
        with pytest.raises(SlotAktifConflictError):
            mark_resi_item_prefilled(rid, _item_id("R-PK", "445"))


class TestSlotCardDisplay:
    def test_dashboard_shows_prefilled_section(self, buffer_seeded):
        """Slot card harus tampilkan: SKU yang prefilled di section 'Stok Gudang ✓'.
        Item di section harus include item_id supaya tombol toggle bisa POST."""
        _sync_resi("R-D", [("445-A-10pcs", 1), ("999-B-10pcs", 1)])
        setup_resi_by_nomor("R-D")
        rid = _resi_id("R-D")
        mark_resi_item_prefilled(rid, _item_id("R-D", "445"))

        slots = get_slot_aktif_match_status()
        target = next(s for s in slots if s["nomor_resi"] == "R-D")
        prefilled = target["prefilled"]
        assert len(prefilled) == 1
        assert prefilled[0]["sku"] == "445"
        assert prefilled[0]["item_id"] == _item_id("R-D", "445")
        # SKU 999 masih di missing
        missing_skus = {m["sku"] for m in target["missing"]}
        assert "999" in missing_skus
        # Missing items juga punya item_id (untuk tombol Gudang)
        m999 = next(m for m in target["missing"] if m["sku"] == "999")
        assert m999["item_id"] == _item_id("R-D", "999")


class TestPrefillCancelReset:
    def test_cancel_resi_resets_prefilled(self, buffer_seeded):
        _sync_resi("R-CR", [("445-A-10pcs", 1)])
        setup_resi_by_nomor("R-CR")
        rid = _resi_id("R-CR")
        mark_resi_item_prefilled(rid, _item_id("R-CR", "445"))
        cancel_resi(rid)
        conn = get_connection()
        row = conn.execute(
            "SELECT prefilled_qty FROM resi_item WHERE resi_id = ?", (rid,)
        ).fetchone()
        assert row["prefilled_qty"] == 0
