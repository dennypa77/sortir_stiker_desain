"""Test race condition: 2 thread scan barcode plastik dengan ID sama bersamaan
tidak bikin duplicate buffer slot."""
import threading

from packing_router.buffer import find_buffer_slot_for_sku
from packing_router.db import get_connection
from packing_router.scan_handler import handle_scan_plastik


def test_two_threads_scan_same_sku_no_duplicate_assignment(buffer_seeded):
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def worker(idx):
        try:
            barrier.wait()
            # Barcode harus unik per scan (UNIQUE constraint di plastik.barcode),
            # tapi numeric_id-nya sama supaya ke buffer slot yang sama.
            # Pakai full format dengan seq berbeda.
            res = handle_scan_plastik(f"5555-10PCS-{idx:04d}", operator_id=f"op{idx}")
            results.append(res)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=worker, args=(1,))
    t2 = threading.Thread(target=worker, args=(2,))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"Expected no errors, got: {errors}"
    assert len(results) == 2

    primary = find_buffer_slot_for_sku("5555")
    assert primary is not None

    conn = get_connection()
    matching_slots = conn.execute(
        "SELECT id, plastik_count, is_overflow_of FROM buffer_slot WHERE sku = ?",
        ("5555",),
    ).fetchall()
    primaries = [s for s in matching_slots if s["is_overflow_of"] is None]
    assert len(primaries) == 1, (
        f"Expected exactly 1 primary slot for SKU, got {len(primaries)}"
    )
    total_plastik = sum(s["plastik_count"] for s in matching_slots)
    assert total_plastik == 2
