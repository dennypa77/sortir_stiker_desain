"""Test cancel_resi reroute plastik preserving pack_units."""
from packing_router.buffer import find_buffer_slot_for_sku
from packing_router.db import get_connection
from packing_router.maintenance import cancel_resi
from packing_router.resi_setup import handle_setup_resi_aktif
from packing_router.scan_handler import handle_scan_plastik


def _seed_resi_active(sku: str, ordered: int, slot_number: int, varian: int = 10, nomor: str = "RESI-C1"):
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


def test_cancel_resi_reroutes_50pcs_plastik_preserving_bundle(buffer_seeded):
    """Resi aktif berisi 1 plastik 50pcs (5 bundle). Setelah cancel, plastik
    ter-route ke buffer dengan plastik_count=5 bundle (bukan 1)."""
    resi_id = _seed_resi_active("1446", ordered=5, slot_number=2, varian=50)
    # Scan 50pcs masuk slot aktif
    handle_scan_plastik("1446", operator_id="op1", pack_units=5)
    conn = get_connection()
    row = conn.execute(
        "SELECT quantity_fulfilled FROM resi_item WHERE resi_id = ?", (resi_id,)
    ).fetchone()
    assert row["quantity_fulfilled"] == 5

    # Cancel resi → plastik harus reroute ke buffer
    cancel_resi(resi_id, actor="admin")

    slot = find_buffer_slot_for_sku("1446")
    assert slot is not None, "Plastik harus reroute ke buffer"
    assert slot.plastik_count == 5, (
        f"Buffer harus 5 bundle (asli plastik 50pcs), bukan {slot.plastik_count} "
        f"— bug: handle_scan_plastik dipanggil tanpa pack_units, default 1"
    )
