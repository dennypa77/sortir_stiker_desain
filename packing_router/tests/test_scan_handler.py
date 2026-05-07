"""Test handle_scan_plastik: 3 jalur action + transition resi → complete.

Schema baru:
- Barcode plastik = numeric ID (e.g. "445", "1446")
- resi_item.sku = numeric_id (= ID stiker desain), bukan "1446-10PCS"
- 1 plastik = 1 pack 10pcs. Resi varian 50pcs butuh 5 plastik.
"""
from packing_router.db import get_connection, now_iso, transaction
from packing_router.scan_handler import handle_scan_plastik


def _seed_resi(conn, nomor, numeric_id, varian, packs_needed, slot=1, resi_status="active"):
    with transaction(conn) as c:
        wave = c.execute(
            "INSERT INTO wave (bigseller_batch_id, wave_number, status) VALUES (?, ?, 'active')",
            ("TEST-BATCH", 1),
        )
        wave_id = wave.lastrowid
        cur = c.execute(
            "INSERT INTO resi (wave_id, nomor_resi, slot_aktif_number, status, setup_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (wave_id, nomor, slot, resi_status, now_iso()),
        )
        resi_id = cur.lastrowid
        c.execute(
            "INSERT INTO resi_item (resi_id, sku, varian, quantity_ordered) VALUES (?, ?, ?, ?)",
            (resi_id, numeric_id, varian, packs_needed),
        )
    return resi_id


class TestScanRoutingToSlotAktif:
    def test_scan_matches_active_resi_routes_to_slot_aktif(self, buffer_seeded):
        conn = get_connection()
        _seed_resi(conn, "RESI-A", "1446", varian=10, packs_needed=2, slot=5)
        result = handle_scan_plastik("1446", operator_id="op1")
        assert result.action == "place_in_slot_aktif"
        assert result.target_slot_aktif_number == 5
        assert result.target_resi_nomor == "RESI-A"
        item = conn.execute(
            "SELECT quantity_fulfilled FROM resi_item WHERE sku = ?", ("1446",)
        ).fetchone()
        assert item["quantity_fulfilled"] == 1

    def test_scan_matches_resi_with_larger_varian(self, buffer_seeded):
        # Resi minta varian 50pcs (= 5 pack). Scan 1 plastik harus increment 1.
        conn = get_connection()
        _seed_resi(conn, "RESI-B", "445", varian=50, packs_needed=5, slot=1)
        result = handle_scan_plastik("445", operator_id="op1")
        assert result.action == "place_in_slot_aktif"
        item = conn.execute(
            "SELECT quantity_fulfilled, quantity_ordered FROM resi_item WHERE sku = ?",
            ("445",),
        ).fetchone()
        assert item["quantity_fulfilled"] == 1
        assert item["quantity_ordered"] == 5


class TestScanRoutingToBuffer:
    def test_scan_no_match_creates_new_buffer_slot(self, buffer_seeded):
        result = handle_scan_plastik("9999", operator_id="op1")
        assert result.action == "place_in_buffer_new"
        assert result.target_buffer_slot_id is not None
        assert result.existing_plastik_count == 1

    def test_second_scan_same_sku_sticky(self, buffer_seeded):
        first = handle_scan_plastik("9999", operator_id="op1")
        second = handle_scan_plastik("9999", operator_id="op1")
        assert second.action == "place_in_buffer_existing"
        assert second.target_buffer_slot_id == first.target_buffer_slot_id
        assert second.existing_plastik_count == 2


class TestResiCompletion:
    def test_resi_transitions_to_complete_when_all_packs_fulfilled(self, buffer_seeded):
        conn = get_connection()
        # Resi butuh 2 pack (varian 20pcs)
        _seed_resi(conn, "RESI-C", "8888", varian=20, packs_needed=2, slot=10)
        handle_scan_plastik("8888", operator_id="op1")
        row1 = conn.execute(
            "SELECT status FROM resi WHERE nomor_resi = ?", ("RESI-C",)
        ).fetchone()
        assert row1["status"] == "active"  # baru 1 pack, butuh 2

        handle_scan_plastik("8888", operator_id="op1")
        row2 = conn.execute(
            "SELECT status, completed_at FROM resi WHERE nomor_resi = ?", ("RESI-C",)
        ).fetchone()
        assert row2["status"] == "complete"
        assert row2["completed_at"] is not None
