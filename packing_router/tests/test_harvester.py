"""Test harvester double-scan + quick harvest flow."""
import pytest

from packing_router.db import get_connection
from packing_router.exceptions import HarvesterMismatchError, ResiNotFoundError
from packing_router.harvester import (
    harvester_dropoff_scan,
    harvester_pickup_scan,
    quick_harvest_to_resi,
)
from packing_router.resi_setup import handle_setup_resi_aktif
from packing_router.scan_handler import handle_scan_plastik


def _setup_buffer_then_resi(buffer_seeded, sku_id="1446", varian=10, packs=1):
    """Seed: 1 plastik di buffer + 1 resi pending → setup ke slot 7."""
    handle_scan_plastik(sku_id, operator_id="op1")
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO wave (bigseller_batch_id, wave_number, status) VALUES ('B', 1, 'active')"
    )
    cur = conn.execute(
        "INSERT INTO resi (wave_id, nomor_resi, status) VALUES (?, ?, 'pending')",
        (cur.lastrowid, "RESI-1"),
    )
    resi_id = cur.lastrowid
    conn.execute(
        "INSERT INTO resi_item (resi_id, sku, varian, quantity_ordered) VALUES (?, ?, ?, ?)",
        (resi_id, sku_id, varian, packs),
    )
    handle_setup_resi_aktif(resi_id, slot_number=7)
    return resi_id


class TestPickupDropoff:
    def test_full_flow_pickup_then_dropoff(self, buffer_seeded):
        _setup_buffer_then_resi(buffer_seeded)
        pickup = harvester_pickup_scan("1446", harvester_id="hv1")
        assert pickup.target_slot_aktif_number == 7
        dropoff = harvester_dropoff_scan(
            "1446", target_slot_aktif_number=7, harvester_id="hv1"
        )
        assert dropoff.resi_completed is True

    def test_dropoff_wrong_slot_raises(self, buffer_seeded):
        _setup_buffer_then_resi(buffer_seeded)
        harvester_pickup_scan("1446", harvester_id="hv1")
        with pytest.raises(HarvesterMismatchError):
            harvester_dropoff_scan(
                "1446", target_slot_aktif_number=99, harvester_id="hv1"
            )

    def test_pickup_unknown_barcode_raises(self, buffer_seeded):
        with pytest.raises(HarvesterMismatchError):
            harvester_pickup_scan("9999", harvester_id="hv1")

    def test_pickup_without_task_raises(self, buffer_seeded):
        # Plastik di buffer tapi belum ada task (resi belum di-setup)
        handle_scan_plastik("9999", operator_id="op1")
        with pytest.raises(HarvesterMismatchError):
            harvester_pickup_scan("9999", harvester_id="hv1")


class TestQuickHarvest:
    def test_quick_harvest_moves_buffer_to_slot_and_completes(self, buffer_seeded):
        # Pre-seed: 2 plastik 1446 di buffer
        handle_scan_plastik("1446", operator_id="op1")
        handle_scan_plastik("1446", operator_id="op1")
        # Seed resi yang butuh 2 pack 1446
        conn = get_connection()
        cur = conn.execute(
            "INSERT INTO wave (bigseller_batch_id, wave_number, status) VALUES ('B', 1, 'active')"
        )
        cur = conn.execute(
            "INSERT INTO resi (wave_id, nomor_resi, status) VALUES (?, ?, 'pending')",
            (cur.lastrowid, "RESI-Q1"),
        )
        resi_id = cur.lastrowid
        conn.execute(
            "INSERT INTO resi_item (resi_id, sku, varian, quantity_ordered) VALUES (?, ?, ?, 2)",
            (resi_id, "1446", 20),
        )
        # Setup resi ke slot 5 (ini auto-create harvester_task pending)
        handle_setup_resi_aktif(resi_id, slot_number=5)

        # Quick harvest — semua 2 plastik harus pindah, resi complete
        result = quick_harvest_to_resi(resi_id)
        assert len(result["moved"]) == 2
        assert result["resi_completed"] is True
        # Buffer slot kosong
        from packing_router.buffer import find_buffer_slot_for_sku
        assert find_buffer_slot_for_sku("1446") is None
        # Pending harvester_task untuk resi ini → cancelled
        rows = conn.execute(
            "SELECT status FROM harvester_task WHERE target_resi_id = ?", (resi_id,)
        ).fetchall()
        statuses = {r["status"] for r in rows}
        assert "cancelled" in statuses

    def test_quick_harvest_partial_move_when_buffer_kurang(self, buffer_seeded):
        # 1 plastik di buffer, resi butuh 3
        handle_scan_plastik("777", operator_id="op1")
        conn = get_connection()
        cur = conn.execute(
            "INSERT INTO wave (bigseller_batch_id, wave_number, status) VALUES ('B', 1, 'active')"
        )
        cur = conn.execute(
            "INSERT INTO resi (wave_id, nomor_resi, status) VALUES (?, ?, 'pending')",
            (cur.lastrowid, "RESI-Q2"),
        )
        resi_id = cur.lastrowid
        conn.execute(
            "INSERT INTO resi_item (resi_id, sku, varian, quantity_ordered) VALUES (?, ?, ?, 3)",
            (resi_id, "777", 30),
        )
        handle_setup_resi_aktif(resi_id, slot_number=2)

        result = quick_harvest_to_resi(resi_id)
        assert len(result["moved"]) == 1
        assert result["resi_completed"] is False
        assert any(p["sku"] == "777" and p["kurang"] == 2 for p in result["pending"])

    def test_quick_harvest_no_buffer_match_returns_empty(self, buffer_seeded):
        # Resi ada, buffer kosong
        conn = get_connection()
        cur = conn.execute(
            "INSERT INTO wave (bigseller_batch_id, wave_number, status) VALUES ('B', 1, 'active')"
        )
        cur = conn.execute(
            "INSERT INTO resi (wave_id, nomor_resi, slot_aktif_number, status) "
            "VALUES (?, ?, 1, 'active')",
            (cur.lastrowid, "RESI-Q3"),
        )
        resi_id = cur.lastrowid
        conn.execute(
            "INSERT INTO resi_item (resi_id, sku, varian, quantity_ordered) VALUES (?, ?, ?, 1)",
            (resi_id, "8888", 10),
        )
        result = quick_harvest_to_resi(resi_id)
        assert result["moved"] == []
        assert result["resi_completed"] is False

    def test_quick_harvest_unknown_resi_raises(self, buffer_seeded):
        with pytest.raises(ResiNotFoundError):
            quick_harvest_to_resi(99999)
