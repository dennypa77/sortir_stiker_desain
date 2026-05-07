"""Test sync sheet, setup resi by nomor, legacy bulk import."""
import pytest

from packing_router import config as pr_config
from packing_router.db import get_connection
from packing_router.exceptions import ResiNotFoundError, SlotAktifConflictError
from packing_router.resi_setup import (
    handle_setup_resi_aktif,
    import_from_list_pesanan_sheet,
    setup_resi_by_nomor,
    sync_list_pesanan_to_db,
    try_activate_next_wave,
)
from packing_router.scan_handler import handle_scan_plastik


def _make_pesanan_rows(batch_id, n_resis, sku_template="445-DESIGN-10pcs"):
    rows = []
    for i in range(1, n_resis + 1):
        rows.append({
            "Batch_ID": batch_id,
            "Nomor_Resi": f"SPXID{i:010d}",
            "SKU": sku_template,
            "Jumlah": 1,
        })
    return rows


# --- New flow: sync_list_pesanan_to_db + setup_resi_by_nomor ---

class TestSyncSheet:
    def test_sync_inserts_pending_resis(self, buffer_seeded):
        rows = _make_pesanan_rows("BATCH-1", 5, "1446-RETRO-10pcs")
        result = sync_list_pesanan_to_db(sheet_rows=rows)
        assert result["resis_inserted"] == 5
        assert result["items_inserted"] == 5
        conn = get_connection()
        cnt = conn.execute("SELECT COUNT(*) AS c FROM resi WHERE status = 'pending'").fetchone()
        assert cnt["c"] == 5

    def test_sync_skips_existing(self, buffer_seeded):
        rows = _make_pesanan_rows("BATCH-2", 3)
        sync_list_pesanan_to_db(sheet_rows=rows)
        result2 = sync_list_pesanan_to_db(sheet_rows=rows)
        assert result2["resis_inserted"] == 0
        assert result2["skipped"] == 3

    def test_sync_packs_calculation_for_varian_50(self, buffer_seeded):
        # Varian 50pcs × jumlah 1 → 5 pack
        rows = [{
            "Batch_ID": "B-PACK",
            "Nomor_Resi": "RESI-PACK",
            "SKU": "445-NAME-50pcs",
            "Jumlah": 1,
        }]
        sync_list_pesanan_to_db(sheet_rows=rows)
        conn = get_connection()
        item = conn.execute(
            "SELECT sku, varian, quantity_ordered FROM resi_item "
            "WHERE resi_id = (SELECT id FROM resi WHERE nomor_resi = 'RESI-PACK')"
        ).fetchone()
        assert item["sku"] == "445"  # numeric only
        assert item["varian"] == 50
        assert item["quantity_ordered"] == 5  # 5 pack

    def test_sync_aggregates_multi_row_same_resi(self, buffer_seeded):
        # 1 resi punya 2 SKU berbeda
        rows = [
            {"Batch_ID": "B-MULTI", "Nomor_Resi": "RESI-MULTI", "SKU": "100-A-10pcs", "Jumlah": 1},
            {"Batch_ID": "B-MULTI", "Nomor_Resi": "RESI-MULTI", "SKU": "200-B-20pcs", "Jumlah": 1},
        ]
        sync_list_pesanan_to_db(sheet_rows=rows)
        conn = get_connection()
        items = conn.execute(
            "SELECT sku, varian, quantity_ordered FROM resi_item "
            "WHERE resi_id = (SELECT id FROM resi WHERE nomor_resi = 'RESI-MULTI') "
            "ORDER BY sku"
        ).fetchall()
        assert len(items) == 2
        skus = {it["sku"] for it in items}
        assert skus == {"100", "200"}


class TestSetupResiByNomor:
    def test_setup_assigns_to_next_empty_slot(self, buffer_seeded):
        rows = _make_pesanan_rows("B", 3)
        sync_list_pesanan_to_db(sheet_rows=rows)
        result = setup_resi_by_nomor("SPXID0000000001")
        assert result.slot_number == 1  # slot kosong terkecil
        # Setup resi kedua → slot 2
        result2 = setup_resi_by_nomor("SPXID0000000002")
        assert result2.slot_number == 2

    def test_setup_unknown_resi_raises(self, buffer_seeded):
        with pytest.raises(ResiNotFoundError):
            setup_resi_by_nomor("UNKNOWN-RESI")

    def test_setup_already_active_raises(self, buffer_seeded):
        rows = _make_pesanan_rows("B", 1)
        sync_list_pesanan_to_db(sheet_rows=rows)
        setup_resi_by_nomor("SPXID0000000001")
        with pytest.raises(SlotAktifConflictError):
            setup_resi_by_nomor("SPXID0000000001")

    def test_setup_returns_buffer_pickups_when_buffer_has_sku(self, buffer_seeded):
        # Pre-seed: scan plastik dulu ke buffer
        handle_scan_plastik("1446", operator_id="op1")
        handle_scan_plastik("1446", operator_id="op1")
        # Lalu sync resi yang butuh SKU 1446
        rows = [{"Batch_ID": "B", "Nomor_Resi": "RESI-X", "SKU": "1446-A-10pcs", "Jumlah": 1}]
        sync_list_pesanan_to_db(sheet_rows=rows)
        result = setup_resi_by_nomor("RESI-X")
        assert len(result.buffer_pickups) == 1
        assert result.buffer_pickups[0]["sku"] == "1446"
        assert result.buffer_pickups[0]["ambil_pack"] == 1  # butuh 1 pack
        assert len(result.harvester_tasks_created) == 1

    def test_setup_when_slot_full_raises(self, tiny_slot_aktif):
        rows = _make_pesanan_rows("B", 3)
        sync_list_pesanan_to_db(sheet_rows=rows)
        setup_resi_by_nomor("SPXID0000000001")
        setup_resi_by_nomor("SPXID0000000002")
        with pytest.raises(SlotAktifConflictError):
            setup_resi_by_nomor("SPXID0000000003")


# --- Backward-compat: handle_setup_resi_aktif tetap berfungsi ---

class TestHandleSetupResiAktif:
    def test_creates_harvester_task_when_buffer_has_sku(self, buffer_seeded):
        handle_scan_plastik("1446", operator_id="op1")
        handle_scan_plastik("1446", operator_id="op1")
        conn = get_connection()
        cur = conn.execute(
            "INSERT INTO wave (bigseller_batch_id, wave_number, status) VALUES ('X', 1, 'active')"
        )
        wave_id = cur.lastrowid
        cur2 = conn.execute(
            "INSERT INTO resi (wave_id, nomor_resi, status) VALUES (?, ?, 'pending')",
            (wave_id, "RESI-XYZ"),
        )
        resi_id = cur2.lastrowid
        conn.execute(
            "INSERT INTO resi_item (resi_id, sku, varian, quantity_ordered) VALUES (?, ?, ?, ?)",
            (resi_id, "1446", 10, 2),
        )
        result = handle_setup_resi_aktif(resi_id, slot_number=3)
        assert len(result.harvester_tasks_created) == 2
        assert len(result.buffer_pickups) == 1


# --- Legacy bulk import (deprecated tapi masih jalan) ---

class TestLegacyImport:
    def test_bulk_import_still_works(self, buffer_seeded):
        # buffer_seeded fixture pakai 30 slot aktif default
        rows = _make_pesanan_rows("BATCH-LEGACY", 60)
        result = import_from_list_pesanan_sheet("BATCH-LEGACY", sheet_rows=rows)
        # 60 resi import, hanya 30 di-setup ke slot aktif (sisanya tetap pending)
        assert len(result.setup_results) == 30
