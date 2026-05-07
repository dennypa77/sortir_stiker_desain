"""Test dynamic slot aktif: init default, add, remove, query."""
import pytest

from packing_router.exceptions import SlotAktifConflictError
from packing_router.slot_aktif import (
    add_slot_aktif,
    get_slot_aktif_count,
    get_slot_aktif_numbers,
    init_default_slot_aktif,
    remove_last_slot_aktif,
    slot_aktif_exists,
)


def test_init_default_creates_30_slots(buffer_seeded):
    # buffer_seeded fixture sudah panggil init dengan DEFAULT_SLOT_AKTIF_COUNT=30
    nums = get_slot_aktif_numbers()
    assert nums == list(range(1, 31))
    assert get_slot_aktif_count() == 30


def test_init_default_idempotent(buffer_seeded):
    # Panggil init lagi tidak akan double up
    init_default_slot_aktif()
    init_default_slot_aktif()
    assert get_slot_aktif_count() == 30


def test_add_single_slot(buffer_seeded):
    added = add_slot_aktif()
    assert added == [31]
    assert get_slot_aktif_count() == 31
    assert slot_aktif_exists(31)


def test_add_batch_slots(buffer_seeded):
    added = add_slot_aktif(count=5)
    assert added == [31, 32, 33, 34, 35]
    assert get_slot_aktif_count() == 35


def test_slot_aktif_exists_for_unknown(buffer_seeded):
    assert not slot_aktif_exists(999)


def test_tiny_fixture_only_2_slots(tiny_slot_aktif):
    nums = get_slot_aktif_numbers()
    assert nums == [1, 2]
    assert get_slot_aktif_count() == 2


def test_remove_last_slot_aktif_ok(buffer_seeded):
    initial = get_slot_aktif_count()
    removed = remove_last_slot_aktif()
    assert removed == [initial]
    assert get_slot_aktif_count() == initial - 1


def test_remove_multiple_slot_aktif(buffer_seeded):
    initial = get_slot_aktif_count()
    removed = remove_last_slot_aktif(count=3)
    assert removed == [initial, initial - 1, initial - 2]
    assert get_slot_aktif_count() == initial - 3


def test_remove_slot_in_use_raises(buffer_seeded):
    """Slot terakhir sedang dipakai resi 'active' → tolak."""
    from packing_router.db import get_connection, now_iso
    conn = get_connection()
    last_nomor = max(get_slot_aktif_numbers())
    cur = conn.execute(
        "INSERT INTO wave (bigseller_batch_id, wave_number, status) VALUES ('B', 1, 'active')"
    )
    conn.execute(
        "INSERT INTO resi (wave_id, nomor_resi, slot_aktif_number, status, setup_at) "
        "VALUES (?, 'R-IN-USE', ?, 'active', ?)",
        (cur.lastrowid, last_nomor, now_iso()),
    )
    with pytest.raises(SlotAktifConflictError):
        remove_last_slot_aktif()


def test_remove_slot_aktif_when_empty_raises(buffer_seeded):
    """Hapus semua slot, lalu coba hapus lagi → error."""
    n = get_slot_aktif_count()
    remove_last_slot_aktif(count=n)
    with pytest.raises(SlotAktifConflictError):
        remove_last_slot_aktif()
