"""Test buffer management: assign, find, increment/decrement, overflow, add_wadah.

Schema baru: buffer_slot.sku = numeric_id (e.g. "1446"), bukan SKU full.
"""
import pytest

from packing_router import config as pr_config
from packing_router.buffer import (
    add_wadah,
    assign_buffer_slot,
    decrement_buffer_slot,
    find_buffer_slot_for_sku,
    get_buffer_status,
    handle_buffer_overflow,
    increment_buffer_slot,
)
from packing_router.db import get_connection, transaction
from packing_router.exceptions import BufferFullError


def test_find_buffer_slot_for_sku_returns_none_initial(buffer_seeded):
    assert find_buffer_slot_for_sku("9999") is None


def test_assign_then_find(buffer_seeded):
    conn = get_connection()
    with transaction(conn) as c:
        loc = assign_buffer_slot("1446", conn=c)
    assert loc.sku == "1446"
    found = find_buffer_slot_for_sku("1446")
    assert found is not None
    assert found.buffer_slot_id == loc.buffer_slot_id


def test_sequential_strategy_fills_wadah_1_first(buffer_seeded):
    """Buffer assignment harus sequential: wadah 1 sampai penuh dulu, baru wadah 2."""
    conn = get_connection()
    with transaction(conn) as c:
        first = assign_buffer_slot("100", conn=c)
    assert first.wadah_nomor == 1
    assert first.slot_number == 1
    with transaction(conn) as c:
        second = assign_buffer_slot("200", conn=c)
    # Sequential: tetap wadah 1, slot 2 (bukan pindah ke wadah 2)
    assert second.wadah_nomor == 1
    assert second.slot_number == 2


def test_sequential_strategy_moves_to_next_wadah_when_full(small_buffer):
    """Setelah wadah 1 (3 slot) penuh, slot baru ke wadah berikutnya."""
    # small_buffer = 1 wadah × 3 slot. Setelah 3 SKU, wadah 1 penuh.
    # Untuk test sequential cross-wadah, butuh 2 wadah. Tambah 1 wadah.
    add_wadah(capacity=3)
    conn = get_connection()
    with transaction(conn) as c:
        for sku in ["A", "B", "C"]:
            assign_buffer_slot(sku, conn=c)
    # Wadah 1 sekarang penuh. Slot baru harus ke wadah 2 slot 1.
    with transaction(conn) as c:
        next_slot = assign_buffer_slot("D", conn=c)
    assert next_slot.wadah_nomor == 2
    assert next_slot.slot_number == 1


def test_buffer_full_raises(small_buffer):
    conn = get_connection()
    with transaction(conn) as c:
        for i in range(3):
            assign_buffer_slot(f"SKU{i}", conn=c)
    with pytest.raises(BufferFullError):
        with transaction(conn) as c:
            assign_buffer_slot("OVERFLOW", conn=c)


def test_increment_sets_first_and_last(buffer_seeded):
    conn = get_connection()
    with transaction(conn) as c:
        loc = assign_buffer_slot("1446", conn=c)
        loc = increment_buffer_slot(loc.buffer_slot_id, conn=c)
        loc = increment_buffer_slot(loc.buffer_slot_id, conn=c)
    assert loc.plastik_count == 2
    found = find_buffer_slot_for_sku("1446")
    assert found.plastik_count == 2


def test_decrement_to_zero_resets_slot(buffer_seeded):
    conn = get_connection()
    with transaction(conn) as c:
        loc = assign_buffer_slot("1446", conn=c)
        increment_buffer_slot(loc.buffer_slot_id, conn=c)
        decrement_buffer_slot(loc.buffer_slot_id, conn=c)
    assert find_buffer_slot_for_sku("1446") is None


def test_handle_buffer_overflow_assigns_secondary(buffer_seeded, monkeypatch):
    monkeypatch.setattr(pr_config, "ALLOW_BUFFER_OVERFLOW", True)
    conn = get_connection()
    with transaction(conn) as c:
        primary = assign_buffer_slot("1446", conn=c)
    with transaction(conn) as c:
        secondary = handle_buffer_overflow("1446", conn=c)
    assert secondary.buffer_slot_id != primary.buffer_slot_id
    assert secondary.is_overflow_of == primary.buffer_slot_id


def test_find_returns_primary_when_overflow_exists(buffer_seeded):
    conn = get_connection()
    with transaction(conn) as c:
        primary = assign_buffer_slot("1446", conn=c)
        handle_buffer_overflow("1446", conn=c)
    found = find_buffer_slot_for_sku("1446")
    assert found.buffer_slot_id == primary.buffer_slot_id
    assert found.is_overflow_of is None


def test_add_wadah_dynamic(buffer_seeded):
    before = get_buffer_status()
    new_w = add_wadah(capacity=5)
    after = get_buffer_status()
    assert new_w.nomor == before.total_wadah_aktif + 1
    assert after.total_wadah_aktif == before.total_wadah_aktif + 1
    assert after.total_slot == before.total_slot + 5


def test_get_buffer_status_breakdown(buffer_seeded):
    status = get_buffer_status()
    assert status.total_wadah_aktif == 2
    assert status.total_slot == 20
    assert status.slot_terpakai == 0
    assert status.slot_kosong == 20
    assert len(status.breakdown) == 2



def test_remove_last_wadah_ok(buffer_seeded):
    """Hapus wadah terakhir saat semua slot kosong."""
    from packing_router.buffer import remove_last_wadah, get_buffer_status
    before = get_buffer_status()
    res = remove_last_wadah()
    assert res["removed_nomor"] == before.total_wadah_aktif
    after = get_buffer_status()
    assert after.total_wadah_aktif == before.total_wadah_aktif - 1


def test_remove_wadah_with_plastik_raises(buffer_seeded):
    """Wadah masih ada plastik → tolak hapus."""
    from packing_router.buffer import remove_last_wadah
    from packing_router.scan_handler import handle_scan_plastik
    from packing_router.exceptions import WadahConflictError
    handle_scan_plastik("999", operator_id="op1")
    # Wadah 1 sekarang ada plastik. Coba hapus wadah 2 → boleh.
    res = remove_last_wadah()
    assert res["removed_nomor"] == 2
    # Sekarang sisa wadah 1 (yang ada plastik). Hapus lagi → tolak.
    with pytest.raises(WadahConflictError):
        remove_last_wadah()


def test_remove_wadah_when_empty_raises(buffer_seeded):
    from packing_router.buffer import remove_last_wadah, get_buffer_status
    from packing_router.exceptions import WadahConflictError
    n = get_buffer_status().total_wadah_aktif
    for _ in range(n):
        remove_last_wadah()
    with pytest.raises(WadahConflictError):
        remove_last_wadah()
