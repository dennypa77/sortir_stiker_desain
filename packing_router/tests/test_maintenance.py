"""Test cancel_resi, undo_last_scan, pack_resi."""
import time

import pytest

from packing_router import config as pr_config
from packing_router.db import get_connection, now_iso
from packing_router.exceptions import UndoWindowExpiredError
from packing_router.maintenance import cancel_resi, pack_resi, undo_last_scan
from packing_router.scan_handler import handle_scan_plastik


def _seed_active_resi(conn, sku="1446", varian=10, packs=1, slot=1):
    cur = conn.execute(
        "INSERT INTO wave (bigseller_batch_id, wave_number, status) VALUES ('B', 1, 'active')"
    )
    wave_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO resi (wave_id, nomor_resi, slot_aktif_number, status, setup_at) "
        "VALUES (?, 'RESI-X', ?, 'active', ?)",
        (wave_id, slot, now_iso()),
    )
    resi_id = cur.lastrowid
    conn.execute(
        "INSERT INTO resi_item (resi_id, sku, varian, quantity_ordered) VALUES (?, ?, ?, ?)",
        (resi_id, sku, varian, packs),
    )
    return resi_id


class TestUndoLastScan:
    def test_undo_buffer_new_resets_slot(self, buffer_seeded):
        handle_scan_plastik("9999", operator_id="op1")
        result = undo_last_scan(operator_id="op1")
        assert result.action_undone == "place_in_buffer_new"
        from packing_router.buffer import find_buffer_slot_for_sku
        assert find_buffer_slot_for_sku("9999") is None

    def test_undo_slot_aktif_decrements_fulfilled(self, buffer_seeded):
        conn = get_connection()
        resi_id = _seed_active_resi(conn, sku="1446", varian=10, packs=2, slot=3)
        handle_scan_plastik("1446", operator_id="op1")
        undo_last_scan(operator_id="op1")
        item = conn.execute(
            "SELECT quantity_fulfilled FROM resi_item WHERE resi_id = ?", (resi_id,)
        ).fetchone()
        assert item["quantity_fulfilled"] == 0

    def test_undo_after_window_expired_raises(self, buffer_seeded, monkeypatch):
        monkeypatch.setattr(pr_config, "UNDO_WINDOW_SECONDS", 0)
        handle_scan_plastik("9999", operator_id="op1")
        time.sleep(1.1)
        with pytest.raises(UndoWindowExpiredError):
            undo_last_scan(operator_id="op1")

    def test_undo_twice_raises(self, buffer_seeded):
        handle_scan_plastik("9999", operator_id="op1")
        undo_last_scan(operator_id="op1")
        with pytest.raises(UndoWindowExpiredError):
            undo_last_scan(operator_id="op1")


class TestCancelResi:
    def test_cancel_routes_plastik_back_to_buffer(self, buffer_seeded):
        conn = get_connection()
        resi_id = _seed_active_resi(conn, sku="1446", varian=10, packs=1, slot=4)
        handle_scan_plastik("1446", operator_id="op1")
        result = cancel_resi(resi_id)
        assert any(r["action"].startswith("place_in_buffer") for r in result["rerouted"])
        row = conn.execute("SELECT status FROM resi WHERE id = ?", (resi_id,)).fetchone()
        assert row["status"] == "cancelled"


class TestPackResi:
    def test_pack_only_complete_resi(self, buffer_seeded):
        conn = get_connection()
        resi_id = _seed_active_resi(conn, sku="2222", varian=10, packs=1, slot=1)
        with pytest.raises(Exception):
            pack_resi(resi_id)

    def test_pack_releases_slot_and_marks_packed(self, buffer_seeded):
        conn = get_connection()
        resi_id = _seed_active_resi(conn, sku="1446", varian=10, packs=1, slot=2)
        handle_scan_plastik("1446", operator_id="op1")
        pack_resi(resi_id)
        row = conn.execute(
            "SELECT status, packed_at, slot_aktif_number FROM resi WHERE id = ?",
            (resi_id,),
        ).fetchone()
        assert row["status"] == "packed"
        assert row["packed_at"] is not None
        assert row["slot_aktif_number"] is None


class TestMarkResiDone:
    def test_mark_done_active_to_complete(self, buffer_seeded):
        conn = get_connection()
        resi_id = _seed_active_resi(conn, sku="445", varian=50, packs=5, slot=1)
        # Scan 1 plastik (qty fulfilled=1, masih kurang 4 pack)
        from packing_router.scan_handler import handle_scan_plastik
        handle_scan_plastik("445", operator_id="op1")
        # Force mark done meskipun qty kurang
        from packing_router.maintenance import mark_resi_done
        res = mark_resi_done(resi_id, actor="op1")
        row = conn.execute(
            "SELECT status, completed_at FROM resi WHERE id = ?", (resi_id,)
        ).fetchone()
        assert row["status"] == "complete"
        assert row["completed_at"] is not None

    def test_mark_done_already_complete_raises(self, buffer_seeded):
        conn = get_connection()
        resi_id = _seed_active_resi(conn, sku="333", varian=10, packs=1, slot=1)
        from packing_router.scan_handler import handle_scan_plastik
        from packing_router.maintenance import mark_resi_done
        from packing_router.exceptions import SlotAktifConflictError
        handle_scan_plastik("333", operator_id="op1")  # langsung complete (1 pack = 1 pack)
        with pytest.raises(SlotAktifConflictError):
            mark_resi_done(resi_id)


class TestResetEndpoints:
    def test_reset_slot_aktif_returns_resi_to_pending(self, buffer_seeded):
        from packing_router.resi_setup import setup_resi_by_nomor, sync_list_pesanan_to_db
        from packing_router.maintenance import reset_slot_aktif
        sync_list_pesanan_to_db(sheet_rows=[
            {"Batch_ID": "B", "Nomor_Resi": "R1", "SKU": "100-A-10pcs", "Jumlah": 1},
            {"Batch_ID": "B", "Nomor_Resi": "R2", "SKU": "200-B-10pcs", "Jumlah": 1},
        ])
        setup_resi_by_nomor("R1")
        setup_resi_by_nomor("R2")
        result = reset_slot_aktif()
        assert result["affected_resi_count"] == 2
        conn = get_connection()
        rows = conn.execute(
            "SELECT status, slot_aktif_number FROM resi WHERE nomor_resi IN ('R1', 'R2')"
        ).fetchall()
        for row in rows:
            assert row["status"] == "pending"
            assert row["slot_aktif_number"] is None

    def test_reset_buffer_clears_all_slots(self, buffer_seeded):
        from packing_router.scan_handler import handle_scan_plastik
        from packing_router.maintenance import reset_buffer
        from packing_router.buffer import find_buffer_slot_for_sku
        handle_scan_plastik("999", operator_id="op1")
        handle_scan_plastik("999", operator_id="op1")
        handle_scan_plastik("888", operator_id="op1")
        result = reset_buffer()
        assert result["plastik_returned"] == 3
        assert find_buffer_slot_for_sku("999") is None
        assert find_buffer_slot_for_sku("888") is None


class TestSlotColorLogic:
    def test_active_with_untouched_is_merah(self, buffer_seeded):
        from packing_router.reports import get_slot_aktif_match_status
        conn = get_connection()
        cur = conn.execute(
            "INSERT INTO wave (bigseller_batch_id, wave_number, status) VALUES ('B', 1, 'active')"
        )
        cur = conn.execute(
            "INSERT INTO resi (wave_id, nomor_resi, slot_aktif_number, status, setup_at) "
            "VALUES (?, 'RX', 5, 'active', ?)",
            (cur.lastrowid, now_iso()),
        )
        rid = cur.lastrowid
        conn.execute(
            "INSERT INTO resi_item (resi_id, sku, varian, quantity_ordered, quantity_fulfilled) "
            "VALUES (?, 'A', 10, 2, 0)",  # untouched
            (rid,),
        )
        slots = get_slot_aktif_match_status()
        slot5 = next(s for s in slots if s["slot_number"] == 5)
        assert slot5["status"] == "merah"

    def test_active_with_all_touched_is_kuning(self, buffer_seeded):
        from packing_router.reports import get_slot_aktif_match_status
        conn = get_connection()
        cur = conn.execute(
            "INSERT INTO wave (bigseller_batch_id, wave_number, status) VALUES ('B', 1, 'active')"
        )
        cur = conn.execute(
            "INSERT INTO resi (wave_id, nomor_resi, slot_aktif_number, status, setup_at) "
            "VALUES (?, 'RY', 7, 'active', ?)",
            (cur.lastrowid, now_iso()),
        )
        rid = cur.lastrowid
        # All SKUs touched (fulfilled >= 1) but qty < ordered
        conn.execute(
            "INSERT INTO resi_item (resi_id, sku, varian, quantity_ordered, quantity_fulfilled) "
            "VALUES (?, 'A', 10, 5, 2)",
            (rid,),
        )
        conn.execute(
            "INSERT INTO resi_item (resi_id, sku, varian, quantity_ordered, quantity_fulfilled) "
            "VALUES (?, 'B', 10, 3, 1)",
            (rid,),
        )
        slots = get_slot_aktif_match_status()
        slot7 = next(s for s in slots if s["slot_number"] == 7)
        assert slot7["status"] == "kuning"

    def test_complete_overdue_is_biru(self, buffer_seeded):
        from packing_router.reports import get_slot_aktif_match_status
        from packing_router import config as pr_config
        conn = get_connection()
        # Set timeout 0 supaya langsung overdue
        cur = conn.execute(
            "INSERT INTO wave (bigseller_batch_id, wave_number, status) VALUES ('B', 1, 'active')"
        )
        cur = conn.execute(
            "INSERT INTO resi (wave_id, nomor_resi, slot_aktif_number, status, completed_at) "
            "VALUES (?, 'RZ', 9, 'complete', '2020-01-01 00:00:00')",
            (cur.lastrowid,),
        )
        slots = get_slot_aktif_match_status()
        slot9 = next(s for s in slots if s["slot_number"] == 9)
        assert slot9["status"] == "biru"
