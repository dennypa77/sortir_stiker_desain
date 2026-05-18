"""Test pack_units (10pcs vs 50pcs bundle) consistency across scan/harvester/quick_harvest/undo flow.

Konvensi: 1 bundle = 10pcs.
- Scan plastik 10pcs → pack_units=1 (default).
- Scan plastik bundle 50pcs → pack_units=5.

Akar bug: harvester flow tidak konsisten — increment_buffer/quantity_fulfilled
default 1, abaikan plastik.pack_units, sehingga state buffer ↔ resi_item desync
saat ada bundle 50pcs masuk via harvester double-scan / quick_harvest.
"""
import pytest

from packing_router.buffer import find_buffer_slot_for_sku
from packing_router.db import get_connection
from packing_router.harvester import (
    harvester_dropoff_scan,
    harvester_pickup_scan,
    quick_harvest_to_resi,
)
from packing_router.maintenance import cancel_resi
from packing_router.resi_setup import handle_setup_resi_aktif
from packing_router.scan_handler import handle_scan_plastik


def _seed_resi_active(sku: str, ordered: int, slot_number: int, varian: int = 10, nomor: str = "RESI-X"):
    """Helper: bikin 1 resi pending dengan 1 item, lalu setup ke slot."""
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO wave (bigseller_batch_id, wave_number, status) VALUES ('B', 1, 'active')"
    )
    cur = conn.execute(
        "INSERT INTO resi (wave_id, nomor_resi, status) VALUES (?, ?, 'pending')",
        (cur.lastrowid, nomor),
    )
    resi_id = cur.lastrowid
    conn.execute(
        "INSERT INTO resi_item (resi_id, sku, varian, quantity_ordered) VALUES (?, ?, ?, ?)",
        (resi_id, sku, varian, ordered),
    )
    handle_setup_resi_aktif(resi_id, slot_number=slot_number)
    return resi_id


class TestBufferIncrementBundle:
    def test_single_scan_50pcs_increments_buffer_by_5(self, buffer_seeded):
        """1 plastik 50pcs → buffer_slot.plastik_count = 5 bundle."""
        handle_scan_plastik("1446", operator_id="op1", pack_units=5)
        slot = find_buffer_slot_for_sku("1446")
        assert slot is not None
        assert slot.plastik_count == 5

    def test_mixed_10_and_50_accumulates_correctly(self, buffer_seeded):
        """3× scan 10pcs + 1× scan 50pcs di SKU sama → 3 + 5 = 8 bundle."""
        handle_scan_plastik("1446", operator_id="op1", pack_units=1)
        handle_scan_plastik("1446", operator_id="op1", pack_units=1)
        handle_scan_plastik("1446", operator_id="op1", pack_units=1)
        handle_scan_plastik("1446", operator_id="op1", pack_units=5)
        slot = find_buffer_slot_for_sku("1446")
        assert slot.plastik_count == 8


class TestScanMatchAccountsForPackUnits:
    def test_scan_50pcs_to_active_resi_fulfills_5_bundle(self, buffer_seeded):
        """Resi butuh 5 bundle (varian 50pcs × 1). Scan 1 plastik 50pcs harus
        langsung complete (5 bundle fulfilled sekaligus)."""
        resi_id = _seed_resi_active("1446", ordered=5, slot_number=3, varian=50)
        result = handle_scan_plastik("1446", operator_id="op1", pack_units=5)
        assert result.action == "place_in_slot_aktif"
        conn = get_connection()
        row = conn.execute(
            "SELECT status, quantity_fulfilled FROM resi r "
            "JOIN resi_item ri ON ri.resi_id = r.id WHERE r.id = ?",
            (resi_id,),
        ).fetchone()
        assert row["quantity_fulfilled"] == 5
        assert row["status"] == "complete"


class TestHarvesterRespectsPackUnits:
    """Bug regression: harvester flow harus respect pack_units."""

    def test_pickup_then_dropoff_50pcs_decrements_full_bundle(self, buffer_seeded):
        """Buffer punya 1 plastik 50pcs (=5 bundle). Resi butuh 5 bundle.
        Setelah pickup+dropoff: buffer harus kosong (5 bundle hilang),
        resi quantity_fulfilled=5, status=complete."""
        # Scan ke buffer dulu (50pcs)
        handle_scan_plastik("1446", operator_id="op1", pack_units=5)
        # Setup resi butuh 5 bundle (varian 50 × 1 = 5)
        resi_id = _seed_resi_active("1446", ordered=5, slot_number=4, varian=50)

        # Buffer setelah setup: harus masih 5 bundle (belum harvest)
        slot = find_buffer_slot_for_sku("1446")
        assert slot.plastik_count == 5

        # Pickup → in_transit
        harvester_pickup_scan("1446", harvester_id="hv1")
        # Buffer harus turun jadi 0 (5 bundle hilang dari 1 plastik)
        slot_after_pickup = find_buffer_slot_for_sku("1446")
        assert slot_after_pickup is None, (
            "Buffer harus kosong setelah pickup plastik 50pcs (bug: decrement default 1)"
        )

        # Dropoff → fulfilled +5
        res = harvester_dropoff_scan(
            "1446", target_slot_aktif_number=4, harvester_id="hv1"
        )
        assert res.resi_completed is True, (
            "Resi harus complete setelah harvest plastik 50pcs full match "
            "(bug: dropoff increment fulfilled hanya +1)"
        )
        conn = get_connection()
        row = conn.execute(
            "SELECT quantity_fulfilled, status FROM resi r "
            "JOIN resi_item ri ON ri.resi_id = r.id WHERE r.id = ?",
            (resi_id,),
        ).fetchone()
        assert row["quantity_fulfilled"] == 5
        assert row["status"] == "complete"

    def test_quick_harvest_50pcs_moves_full_bundle(self, buffer_seeded):
        """Quick harvest 1 plastik 50pcs (=5 bundle) untuk resi yang butuh 5 bundle.
        Buffer harus kosong, resi complete."""
        handle_scan_plastik("1446", operator_id="op1", pack_units=5)
        resi_id = _seed_resi_active("1446", ordered=5, slot_number=6, varian=50)

        result = quick_harvest_to_resi(resi_id)
        assert result["resi_completed"] is True, (
            "Quick harvest plastik 50pcs harus fully fulfill resi 5-bundle "
            "(bug: increment fulfilled per-plastik =1)"
        )
        slot = find_buffer_slot_for_sku("1446")
        assert slot is None, "Buffer harus kosong setelah quick harvest"

    def test_quick_harvest_50pcs_partial_when_resi_smaller(self, buffer_seeded):
        """Buffer 1 plastik 50pcs (5 bundle), resi cuma butuh 3 bundle.
        Harus pindah 3 bundle (sisa 2 di buffer), resi complete."""
        handle_scan_plastik("1446", operator_id="op1", pack_units=5)
        resi_id = _seed_resi_active("1446", ordered=3, slot_number=6, varian=10)

        result = quick_harvest_to_resi(resi_id)
        slot = find_buffer_slot_for_sku("1446")
        # Tergantung desain, plastik 50pcs single-unit: kalau dipindah, mungkin
        # full 5 bundle pindah (over-fulfill) atau hanya 3 (split).
        # Pilihan paling waras: kalau plastik fisik tidak bisa di-split, pindah
        # 5 → over-fulfill, resi complete dengan over.
        # Yang penting: state buffer ↔ fulfilled konsisten.
        conn = get_connection()
        row = conn.execute(
            "SELECT quantity_fulfilled FROM resi_item WHERE resi_id = ?",
            (resi_id,),
        ).fetchone()
        buf_count = slot.plastik_count if slot else 0
        # Total bundle = fulfilled + buffer_remaining harus = 5
        assert row["quantity_fulfilled"] + buf_count == 5, (
            f"Total bundle hilang: fulfilled={row['quantity_fulfilled']}, "
            f"buf_remain={buf_count} (harus jumlah=5)"
        )
        assert result["resi_completed"] is True


class TestUndoPackUnits:
    """Undo scan 50pcs harus revert 5 bundle, bukan 1."""

    def test_undo_buffer_scan_50pcs_reverts_5_bundle(self, buffer_seeded):
        """Scan 50pcs ke buffer (slot baru), undo → buffer slot harus kembali kosong (0 bundle, sku NULL)."""
        handle_scan_plastik("1446", operator_id="op1", pack_units=5)
        from packing_router.maintenance import undo_last_scan
        undo_last_scan("op1")
        slot = find_buffer_slot_for_sku("1446")
        assert slot is None, "Setelah undo, buffer slot harus reset (sku NULL)"

    def test_undo_match_50pcs_reverts_5_fulfilled(self, buffer_seeded):
        """Scan 50pcs match resi, undo → quantity_fulfilled balik 0."""
        resi_id = _seed_resi_active("1446", ordered=5, slot_number=3, varian=50)
        handle_scan_plastik("1446", operator_id="op1", pack_units=5)
        from packing_router.maintenance import undo_last_scan
        undo_last_scan("op1")
        conn = get_connection()
        row = conn.execute(
            "SELECT status, quantity_fulfilled FROM resi r "
            "JOIN resi_item ri ON ri.resi_id = r.id WHERE r.id = ?",
            (resi_id,),
        ).fetchone()
        assert row["quantity_fulfilled"] == 0
        assert row["status"] == "active"
