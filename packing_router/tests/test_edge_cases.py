"""Edge case stress tests untuk find bugs di skenario marginal.

Skenario:
- Buffer overflow chain (3 slot SKU sama)
- Concurrent scan + harvester pickup race
- Resi over-fulfill (scan lebih dari yang dipesan)
- Reset di tengah harvester in_progress
- Sequence of operations: scan→setup→cancel→re-setup→pack
- Multi-resi sharing 1 SKU
- Plastik dengan barcode duplicate dari operator beda
"""
import threading

import pytest

from packing_router import config as pr_config
from packing_router.buffer import find_buffer_slot_for_sku
from packing_router.db import get_connection, reset_connection
from packing_router.exceptions import (
    BufferFullError,
    HarvesterMismatchError,
    ResiNotFoundError,
    SlotAktifConflictError,
)
from packing_router.harvester import (
    harvester_dropoff_scan,
    harvester_pickup_scan,
    quick_harvest_to_resi,
)
from packing_router.maintenance import (
    cancel_resi,
    mark_resi_done,
    pack_resi,
    reset_buffer,
    reset_slot_aktif,
    undo_last_scan,
)
from packing_router.resi_setup import handle_setup_resi_aktif, setup_resi_by_nomor
from packing_router.scan_handler import handle_scan_plastik


def _seed_resi(nomor="RESI-E", sku="1446", varian=10, ordered=1, slot_number=None):
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO wave (bigseller_batch_id, wave_number, status) VALUES ('B', 1, 'active')"
    )
    cur = conn.execute(
        "INSERT INTO resi (wave_id, nomor_resi, status) VALUES (?, ?, 'pending')",
        (cur.lastrowid, nomor),
    )
    rid = cur.lastrowid
    conn.execute(
        "INSERT INTO resi_item (resi_id, sku, varian, quantity_ordered) VALUES (?, ?, ?, ?)",
        (rid, sku, varian, ordered),
    )
    if slot_number is not None:
        handle_setup_resi_aktif(rid, slot_number=slot_number)
    return rid


class TestBufferOverflowChain:
    def test_overflow_creates_secondary_when_primary_at_trigger(self, buffer_seeded, monkeypatch):
        """Saat plastik_count >= OVERFLOW_TRIGGER_COUNT, scan baru harus assign slot baru."""
        monkeypatch.setattr(pr_config, "OVERFLOW_TRIGGER_COUNT", 3)
        monkeypatch.setattr(pr_config, "ALLOW_BUFFER_OVERFLOW", True)
        # 3 scan → plastik_count = 3 → trigger
        handle_scan_plastik("1446", operator_id="op1")
        handle_scan_plastik("1446", operator_id="op1")
        handle_scan_plastik("1446", operator_id="op1")
        # 4th scan → overflow slot baru
        handle_scan_plastik("1446", operator_id="op1")
        conn = get_connection()
        slots = conn.execute(
            "SELECT id, plastik_count, is_overflow_of FROM buffer_slot WHERE sku = ?",
            ("1446",),
        ).fetchall()
        assert len(slots) == 2
        assert any(s["is_overflow_of"] is not None for s in slots)

    def test_overflow_disabled_then_raises(self, small_buffer, monkeypatch):
        """Saat ALLOW_BUFFER_OVERFLOW=False, scan ke slot SKU penuh + buffer penuh harus raise."""
        monkeypatch.setattr(pr_config, "OVERFLOW_TRIGGER_COUNT", 2)
        monkeypatch.setattr(pr_config, "ALLOW_BUFFER_OVERFLOW", False)
        # Fill buffer (3 slot) dengan 3 SKU berbeda dulu
        handle_scan_plastik("100", operator_id="op1")
        handle_scan_plastik("200", operator_id="op1")
        handle_scan_plastik("300", operator_id="op1")
        # SKU 100 sudah ada di slot, scan lagi (existing → 2 plastik → trigger overflow)
        handle_scan_plastik("100", operator_id="op1")
        # Scan ke-3 untuk SKU 100 → buffer penuh, overflow not allowed → raise
        with pytest.raises(BufferFullError):
            handle_scan_plastik("100", operator_id="op1")


class TestOverFulfill:
    def test_extra_scan_after_complete_routes_to_buffer(self, buffer_seeded):
        """Resi sudah complete. Scan plastik SKU yang sama lagi → masuk buffer
        (bukan over-fulfill resi yang sudah complete)."""
        rid = _seed_resi(sku="1446", ordered=1, slot_number=1)
        # Scan pertama → resi complete
        handle_scan_plastik("1446", operator_id="op1")
        conn = get_connection()
        row = conn.execute("SELECT status FROM resi WHERE id = ?", (rid,)).fetchone()
        assert row["status"] == "complete"
        # Scan kedua → harus masuk buffer (resi tidak lagi 'active')
        result = handle_scan_plastik("1446", operator_id="op1")
        assert result.action in ("place_in_buffer_new", "place_in_buffer_existing")


class TestResetWhileInProgress:
    def test_reset_slot_aktif_cancels_in_progress_harvester_task(self, buffer_seeded):
        handle_scan_plastik("1446", operator_id="op1")
        rid = _seed_resi(sku="1446", ordered=1, slot_number=3)
        harvester_pickup_scan("1446", harvester_id="hv1")
        # Sekarang ada task in_progress + plastik in_transit
        reset_slot_aktif(actor="admin")
        conn = get_connection()
        statuses = {
            r["status"]
            for r in conn.execute("SELECT status FROM harvester_task").fetchall()
        }
        assert "in_progress" not in statuses
        assert "cancelled" in statuses
        # plastik in_transit → returned (location_type='returned')
        loc = conn.execute(
            "SELECT location_type FROM plastik WHERE barcode = '1446'"
        ).fetchone()
        assert loc["location_type"] == "returned"

    def test_reset_buffer_cancels_pending_harvester_task(self, buffer_seeded):
        handle_scan_plastik("1446", operator_id="op1")
        rid = _seed_resi(sku="1446", ordered=1, slot_number=2)
        reset_buffer(actor="admin")
        conn = get_connection()
        statuses = {
            r["status"]
            for r in conn.execute("SELECT status FROM harvester_task").fetchall()
        }
        assert "pending" not in statuses


class TestFullLifecycle:
    def test_scan_setup_cancel_resetup_pack(self, buffer_seeded):
        """Full lifecycle: scan→setup→complete→cancel→re-create→setup→pack."""
        # 1. Scan plastik (no resi yet) → buffer
        handle_scan_plastik("1446", operator_id="op1")
        # 2. Create resi & setup
        rid = _seed_resi(sku="1446", ordered=1, nomor="RESI-LC", slot_number=1)
        # Resi complete via quick_harvest
        quick_harvest_to_resi(rid)
        # 3. Cancel resi
        cancel_resi(rid, actor="admin")
        conn = get_connection()
        row = conn.execute("SELECT status FROM resi WHERE id = ?", (rid,)).fetchone()
        assert row["status"] == "cancelled"
        # 4. Plastik (re-routed) sekarang di buffer lagi
        slot = find_buffer_slot_for_sku("1446")
        assert slot is not None
        # 5. New resi, setup, quick harvest, pack
        rid2 = _seed_resi(sku="1446", ordered=1, nomor="RESI-LC2", slot_number=2)
        quick_harvest_to_resi(rid2)
        row = conn.execute("SELECT status FROM resi WHERE id = ?", (rid2,)).fetchone()
        assert row["status"] == "complete"
        pack_resi(rid2)
        row = conn.execute("SELECT status, slot_aktif_number FROM resi WHERE id = ?", (rid2,)).fetchone()
        assert row["status"] == "packed"
        assert row["slot_aktif_number"] is None


class TestMultiResiSharingSku:
    def test_multi_resi_same_sku_get_distinct_slots(self, buffer_seeded):
        """2 resi pending pakai SKU 1446. Setup dua-duanya. Scan plastik 1446
        harus route ke slot dengan resi yang setup duluan (FIFO)."""
        r1 = _seed_resi(sku="1446", ordered=1, nomor="R1", slot_number=1)
        r2 = _seed_resi(sku="1446", ordered=1, nomor="R2", slot_number=2)
        result = handle_scan_plastik("1446", operator_id="op1")
        assert result.target_resi_id == r1, "FIFO: slot terkecil dapat duluan"
        # Scan kedua → R2
        result = handle_scan_plastik("1446", operator_id="op1")
        assert result.target_resi_id == r2

    def test_one_buffer_plastik_shared_between_resi_via_quick_harvest(self, buffer_seeded):
        """2 plastik 1446 di buffer (1 plastik=1 bundle), 2 resi butuh 1 bundle each.
        Quick harvest resi 1 → ambil 1 plastik. Quick harvest resi 2 → ambil 1 sisa."""
        handle_scan_plastik("1446", operator_id="op1")
        handle_scan_plastik("1446", operator_id="op1")
        r1 = _seed_resi(sku="1446", ordered=1, nomor="QH1", slot_number=1)
        r2 = _seed_resi(sku="1446", ordered=1, nomor="QH2", slot_number=2)
        quick_harvest_to_resi(r1)
        slot = find_buffer_slot_for_sku("1446")
        assert slot.plastik_count == 1
        quick_harvest_to_resi(r2)
        slot = find_buffer_slot_for_sku("1446")
        assert slot is None


class TestDuplicateBarcodeMultipleOperators:
    def test_two_operators_scan_same_barcode_create_separate_plastik_rows(self, buffer_seeded):
        """1 barcode 1446 di-scan 2× oleh operator beda. Masing-masing harus
        bikin row plastik baru. Buffer count → 2 bundle."""
        handle_scan_plastik("1446", operator_id="opA")
        handle_scan_plastik("1446", operator_id="opB")
        slot = find_buffer_slot_for_sku("1446")
        assert slot.plastik_count == 2
        conn = get_connection()
        cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM plastik WHERE barcode = '1446'"
        ).fetchone()["c"]
        assert cnt == 2


class TestPackTransitions:
    def test_pack_resi_only_works_for_complete(self, buffer_seeded):
        rid = _seed_resi(sku="1446", ordered=1, slot_number=3)
        with pytest.raises(ResiNotFoundError):
            pack_resi(rid)  # status='active', butuh 'complete'

    def test_pack_idempotency_raises_on_second_call(self, buffer_seeded):
        rid = _seed_resi(sku="1446", ordered=1, slot_number=3)
        handle_scan_plastik("1446", operator_id="op1")
        pack_resi(rid)
        # Pack lagi → error (sudah packed)
        with pytest.raises(ResiNotFoundError):
            pack_resi(rid)


class TestCancelEdgeCases:
    def test_cancel_packed_resi_no_op(self, buffer_seeded):
        rid = _seed_resi(sku="1446", ordered=1, slot_number=3)
        handle_scan_plastik("1446", operator_id="op1")
        pack_resi(rid)
        res = cancel_resi(rid, actor="admin")
        assert res.get("already") == "packed"

    def test_cancel_nonexistent_resi_raises(self, buffer_seeded):
        with pytest.raises(ResiNotFoundError):
            cancel_resi(99999)


class TestUndoEdgeCases:
    def test_undo_with_negative_window_always_expired(self, buffer_seeded):
        """Negative window = immediately expired regardless of age.
        (Pakai negative, bukan 0, supaya tidak flaky karena sub-second age.)"""
        handle_scan_plastik("1446", operator_id="op1")
        from packing_router.exceptions import UndoWindowExpiredError
        with pytest.raises(UndoWindowExpiredError):
            undo_last_scan("op1", within_seconds=-1)


class TestSetupResiByNomor:
    def test_setup_no_match_in_pool_raises(self, buffer_seeded):
        with pytest.raises(ResiNotFoundError):
            setup_resi_by_nomor("RESI-NOT-FOUND")

    def test_setup_already_packed_raises(self, buffer_seeded):
        rid = _seed_resi(sku="1446", ordered=1, slot_number=1, nomor="RESI-PKD")
        handle_scan_plastik("1446", operator_id="op1")
        pack_resi(rid)
        with pytest.raises(SlotAktifConflictError):
            setup_resi_by_nomor("RESI-PKD")

    def test_setup_cancelled_raises(self, buffer_seeded):
        rid = _seed_resi(sku="1446", ordered=1, slot_number=1, nomor="RESI-CCL")
        cancel_resi(rid)
        with pytest.raises(SlotAktifConflictError):
            setup_resi_by_nomor("RESI-CCL")

    def test_setup_when_all_slots_full_raises(self, tiny_slot_aktif):
        """2 slot aktif total, isi 2 resi → pool full."""
        _seed_resi(sku="A", ordered=1, slot_number=1, nomor="R1")
        _seed_resi(sku="B", ordered=1, slot_number=2, nomor="R2")
        conn = get_connection()
        cur = conn.execute(
            "INSERT INTO wave (bigseller_batch_id, wave_number, status) VALUES ('B', 1, 'active')"
        )
        cur = conn.execute(
            "INSERT INTO resi (wave_id, nomor_resi, status) VALUES (?, 'R3', 'pending')",
            (cur.lastrowid,),
        )
        rid = cur.lastrowid
        conn.execute(
            "INSERT INTO resi_item (resi_id, sku, varian, quantity_ordered) VALUES (?, 'C', 10, 1)",
            (rid,),
        )
        with pytest.raises(SlotAktifConflictError):
            setup_resi_by_nomor("R3")


class TestHarvesterEdgeCases:
    def test_pickup_when_buffer_decremented_externally(self, buffer_seeded):
        """Harvester task ada, tapi plastik sudah hilang dari buffer.
        Pickup harus raise mismatch."""
        handle_scan_plastik("1446", operator_id="op1")
        rid = _seed_resi(sku="1446", ordered=1, slot_number=3)
        # Manual decrement plastik (simulasi: operator manual ngerampungin)
        conn = get_connection()
        conn.execute("UPDATE plastik SET location_type = 'returned' WHERE barcode = '1446'")
        with pytest.raises(HarvesterMismatchError):
            harvester_pickup_scan("1446", harvester_id="hv1")

    def test_dropoff_without_pickup_raises(self, buffer_seeded):
        with pytest.raises(HarvesterMismatchError):
            harvester_dropoff_scan(
                "9999", target_slot_aktif_number=1, harvester_id="hv1"
            )


class TestMarkDoneEdgeCases:
    def test_mark_done_packed_raises(self, buffer_seeded):
        rid = _seed_resi(sku="1446", ordered=1, slot_number=1)
        handle_scan_plastik("1446", operator_id="op1")
        pack_resi(rid)
        with pytest.raises(SlotAktifConflictError):
            mark_resi_done(rid)

    def test_mark_done_pending_raises(self, buffer_seeded):
        conn = get_connection()
        cur = conn.execute(
            "INSERT INTO wave (bigseller_batch_id, wave_number, status) VALUES ('B', 1, 'active')"
        )
        cur = conn.execute(
            "INSERT INTO resi (wave_id, nomor_resi, status) VALUES (?, 'P1', 'pending')",
            (cur.lastrowid,),
        )
        with pytest.raises(SlotAktifConflictError):
            mark_resi_done(cur.lastrowid)
