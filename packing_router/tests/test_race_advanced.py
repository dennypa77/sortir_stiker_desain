"""Advanced race condition tests untuk verifikasi BEGIN IMMEDIATE serialization.

Skenario:
1. 2 operator scan SKU sama bareng-bareng → resi butuh 1 bundle. Hanya 1
   yang harus match resi, satu lagi ke buffer.
2. 2 harvester pickup plastik beda barcode sama → tidak crash, distinct tasks.
3. 3 operator scan + 1 harvester pickup concurrent.
"""
import threading
import time

import pytest

from packing_router.buffer import find_buffer_slot_for_sku
from packing_router.db import get_connection
from packing_router.harvester import harvester_pickup_scan
from packing_router.resi_setup import handle_setup_resi_aktif
from packing_router.scan_handler import handle_scan_plastik


def _seed_resi(nomor, sku, ordered, slot_number):
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
        (rid, sku, 10, ordered),
    )
    handle_setup_resi_aktif(rid, slot_number=slot_number)
    return rid


def test_concurrent_scans_to_resi_needing_1_no_over_fulfill(buffer_seeded):
    """Resi butuh 1 bundle. 2 thread scan 1446 bareng. Cuma 1 yang match,
    1 lagi ke buffer (resi tidak over-fulfilled)."""
    rid = _seed_resi("RESI-RACE", "1446", ordered=1, slot_number=1)
    barrier = threading.Barrier(2)
    actions = []
    errors = []

    def worker(idx):
        try:
            barrier.wait()
            res = handle_scan_plastik(f"1446-10PCS-{idx:04d}", operator_id=f"op{idx}")
            actions.append(res.action)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"Errors: {errors}"
    assert actions.count("place_in_slot_aktif") == 1, (
        f"Hanya 1 yang harus match. Got: {actions}"
    )
    assert actions.count("place_in_buffer_new") + actions.count("place_in_buffer_existing") == 1
    # Resi fulfilled = 1 (tidak over)
    conn = get_connection()
    row = conn.execute(
        "SELECT quantity_fulfilled, status FROM resi_item ri JOIN resi r ON r.id=ri.resi_id "
        "WHERE r.id = ?",
        (rid,),
    ).fetchone()
    assert row["quantity_fulfilled"] == 1
    assert row["status"] == "complete"


def test_high_concurrency_5_scans_distinct_resi_no_crash(buffer_seeded):
    """5 thread scan SKU berbeda bareng. Semua harus sukses ke buffer, no DB
    deadlock/crash."""
    barrier = threading.Barrier(5)
    errors = []

    def worker(idx):
        try:
            barrier.wait()
            handle_scan_plastik(f"{2000+idx}", operator_id=f"op{idx}")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"Errors: {errors}"
    conn = get_connection()
    used = conn.execute(
        "SELECT COUNT(*) AS c FROM buffer_slot WHERE sku IS NOT NULL"
    ).fetchone()["c"]
    assert used == 5
