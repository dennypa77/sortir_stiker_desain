"""Edge cases yang melibatkan overflow buffer slot.

BUG #3 (potential): harvester pickup picks FIFO plastik. Saat plastik SKU sama
ada di primary + overflow slot, harvester task hanya di primary slot. Kalau
FIFO ambil plastik yang lokasinya di overflow slot, harvester task lookup
match buffer_slot wrong → mismatch error padahal seharusnya bisa.
"""
import pytest

from packing_router import config as pr_config
from packing_router.buffer import find_buffer_slot_for_sku
from packing_router.db import get_connection
from packing_router.exceptions import HarvesterMismatchError
from packing_router.harvester import harvester_pickup_scan, harvester_dropoff_scan
from packing_router.resi_setup import handle_setup_resi_aktif
from packing_router.scan_handler import handle_scan_plastik


def _seed_resi(nomor="RESI-O", sku="1446", varian=10, ordered=1, slot_number=1):
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
    handle_setup_resi_aktif(rid, slot_number=slot_number)
    return rid


def test_pickup_works_when_plastik_in_overflow_slot(buffer_seeded, monkeypatch):
    """Sequence:
    1. Scan 1446 → primary slot
    2. Scan 1446 (count → trigger overflow) → overflow slot
    3. Setup resi yang butuh 1 bundle 1446
       (auto-create task untuk primary slot, karena find_buffer_slot_for_sku
        return primary)
    4. Harvester scan barcode 1446

    Edge: FIFO pick plastik tertua. Plastik tertua ada di primary, jadi ini
    OK. Tapi kalau primary sudah kosong (semua plastik diharvest), dan masih
    ada plastik di overflow, behavior?
    """
    monkeypatch.setattr(pr_config, "OVERFLOW_TRIGGER_COUNT", 2)
    monkeypatch.setattr(pr_config, "ALLOW_BUFFER_OVERFLOW", True)
    handle_scan_plastik("1446", operator_id="op1")
    handle_scan_plastik("1446", operator_id="op1")
    # Sekarang primary punya 2 bundle (= OVERFLOW_TRIGGER_COUNT). Scan ke-3
    # akan trigger overflow.
    handle_scan_plastik("1446", operator_id="op1")

    conn = get_connection()
    slots = conn.execute(
        "SELECT id, plastik_count, is_overflow_of FROM buffer_slot WHERE sku = ?",
        ("1446",),
    ).fetchall()
    primary = next(s for s in slots if s["is_overflow_of"] is None)
    overflow = next((s for s in slots if s["is_overflow_of"] is not None), None)
    assert primary["plastik_count"] == 2
    assert overflow is not None
    assert overflow["plastik_count"] == 1

    # Setup resi butuh 1 bundle
    rid = _seed_resi(sku="1446", ordered=1, slot_number=3)
    # Harvester pickup — ambil 1 plastik (FIFO → primary plastik tertua)
    pickup = harvester_pickup_scan("1446", harvester_id="hv1")
    assert pickup.target_slot_aktif_number == 3


def test_pickup_after_primary_drained_picks_overflow(buffer_seeded, monkeypatch):
    """BUG #3 candidate: primary slot dikosongkan via cancel/decrement, plastik
    masih ada di overflow slot. Saat resi baru setup pakai SKU sama, task akan
    dibuat untuk overflow (karena find_buffer_slot_for_sku return overflow saat
    primary kosong). Pickup harus sukses."""
    monkeypatch.setattr(pr_config, "OVERFLOW_TRIGGER_COUNT", 2)
    monkeypatch.setattr(pr_config, "ALLOW_BUFFER_OVERFLOW", True)
    # 3 scan → primary 2, overflow 1
    handle_scan_plastik("1446", operator_id="op1")
    handle_scan_plastik("1446", operator_id="op1")
    handle_scan_plastik("1446", operator_id="op1")

    # Drain primary via reset (simulasi semua plastik primary dipakai)
    conn = get_connection()
    # Cari plastik di primary slot dan delete location
    primary_id = conn.execute(
        "SELECT id FROM buffer_slot WHERE sku = '1446' AND is_overflow_of IS NULL"
    ).fetchone()["id"]
    conn.execute(
        "UPDATE plastik SET location_type = 'returned' WHERE location_ref = ?",
        (primary_id,),
    )
    conn.execute(
        "UPDATE buffer_slot SET plastik_count = 0, sku = NULL WHERE id = ?",
        (primary_id,),
    )

    # Sekarang find_buffer_slot harus return overflow slot (yang sekarang efektif primary)
    slot = find_buffer_slot_for_sku("1446")
    assert slot is not None
    # Setup resi → task dibuat untuk slot ini
    rid = _seed_resi(sku="1446", ordered=1, slot_number=4)
    # Pickup harus sukses
    pickup = harvester_pickup_scan("1446", harvester_id="hv1")
    assert pickup.target_slot_aktif_number == 4


def test_dropoff_succeeds_for_setup_then_pickup_flow(buffer_seeded):
    """Smoke test: setup → pickup → dropoff happy path 10pcs."""
    handle_scan_plastik("1446", operator_id="op1")
    rid = _seed_resi(sku="1446", ordered=1, slot_number=5)
    harvester_pickup_scan("1446", harvester_id="hv1")
    res = harvester_dropoff_scan(
        "1446", target_slot_aktif_number=5, harvester_id="hv1"
    )
    assert res.resi_completed is True
