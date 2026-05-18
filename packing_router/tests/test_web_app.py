"""End-to-end smoke test untuk Flask web app via test_client.

Goal: exercise tiap route + happy-path + error-path tanpa butuh browser.
Akan expose template rendering errors, jinja undefined-var, route mismatch.
"""
import pytest

from packing_router import config as pr_config
from packing_router.db import get_connection, reset_connection
from packing_router.scan_handler import handle_scan_plastik
from packing_router.web.app import create_app


@pytest.fixture
def client(tmp_db):
    """Flask test client dengan DB sementara."""
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _seed_resi(nomor="RESI-A", sku="1446", varian=10, ordered=1):
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
    return rid


class TestRoutesRender:
    def test_root_redirects_to_dashboard(self, client):
        r = client.get("/")
        assert r.status_code in (301, 302)
        assert "/dashboard" in r.headers["Location"]

    def test_dashboard_renders(self, client):
        r = client.get("/dashboard")
        assert r.status_code == 200
        assert b"slot" in r.data.lower() or b"buffer" in r.data.lower()

    def test_dashboard_refresh_renders_partial(self, client):
        r = client.get("/dashboard/refresh")
        assert r.status_code == 200

    def test_operator_scan_get(self, client):
        r = client.get("/operator/scan")
        assert r.status_code == 200

    def test_harvester_queue_redirect(self, client):
        r = client.get("/harvester/queue")
        assert r.status_code in (301, 302)

    def test_slot_aktif_redirect(self, client):
        r = client.get("/slot-aktif")
        assert r.status_code in (301, 302)

    def test_admin_view(self, client):
        r = client.get("/admin")
        assert r.status_code == 200

    def test_harvester_queue_refresh(self, client):
        r = client.get("/harvester/queue/refresh")
        assert r.status_code == 200


class TestScanRoute:
    def test_empty_barcode_returns_error_partial(self, client):
        r = client.post("/operator/scan", data={"barcode": "", "operator_id": "op1"})
        assert r.status_code == 200
        assert b"kosong" in r.data.lower()

    def test_invalid_barcode_returns_error_partial(self, client):
        r = client.post(
            "/operator/scan",
            data={"barcode": "abc-xyz!!!", "operator_id": "op1"},
        )
        assert r.status_code == 200

    def test_scan_to_buffer_returns_target_label(self, client):
        r = client.post(
            "/operator/scan",
            data={"barcode": "1446", "operator_id": "op1", "pack_size": "10"},
        )
        assert r.status_code == 200
        assert b"WADAH" in r.data.upper() or b"slot" in r.data.lower()

    def test_scan_with_pack_size_50_uses_pack_units_5(self, client):
        r = client.post(
            "/operator/scan",
            data={"barcode": "1446", "operator_id": "op1", "pack_size": "50"},
        )
        assert r.status_code == 200
        # 1 plastik 50pcs = 5 bundle
        from packing_router.buffer import find_buffer_slot_for_sku
        slot = find_buffer_slot_for_sku("1446")
        assert slot.plastik_count == 5

    def test_scan_match_resi_returns_slot_target(self, client):
        rid = _seed_resi(nomor="RESI-X", sku="1446", varian=10, ordered=1)
        from packing_router.resi_setup import handle_setup_resi_aktif
        handle_setup_resi_aktif(rid, slot_number=3)
        r = client.post(
            "/operator/scan",
            data={"barcode": "1446", "operator_id": "op1"},
        )
        assert r.status_code == 200
        assert b"SLOT 3" in r.data.upper() or b"RESI-X" in r.data


class TestSetupResiRoute:
    def test_setup_empty_returns_error(self, client):
        r = client.post(
            "/operator/setup-resi",
            data={"nomor_resi": "", "operator_id": "op1"},
        )
        assert r.status_code == 200
        assert b"kosong" in r.data.lower()

    def test_setup_unknown_resi_returns_error(self, client):
        r = client.post(
            "/operator/setup-resi",
            data={"nomor_resi": "RESI-NOT-EXIST", "operator_id": "op1"},
        )
        assert r.status_code == 200

    def test_setup_existing_resi_assigns_slot(self, client):
        _seed_resi(nomor="RESI-SETUP", sku="1446", ordered=1)
        r = client.post(
            "/operator/setup-resi",
            data={"nomor_resi": "RESI-SETUP", "operator_id": "op1"},
        )
        assert r.status_code == 200


class TestUndoRoute:
    def test_undo_with_no_scan_returns_error_msg(self, client):
        r = client.post("/operator/undo", data={"operator_id": "op-empty"})
        assert r.status_code == 200
        assert b"gagal" in r.data.lower() or b"undo" in r.data.lower()


class TestSlotActions:
    def test_quick_harvest_route_handles_nonexistent_resi(self, client):
        r = client.post("/slot-aktif/99999/quick-harvest", data={"actor": "op1"})
        # Domain error handler returns 200 (HX) or 400 (JSON). Either acceptable.
        assert r.status_code in (200, 400)

    def test_quick_harvest_route_actual(self, client):
        rid = _seed_resi(nomor="RESI-QH", sku="1446", ordered=1)
        from packing_router.resi_setup import handle_setup_resi_aktif
        handle_setup_resi_aktif(rid, slot_number=1)
        handle_scan_plastik("1446", operator_id="seed", pack_units=1)
        r = client.post(f"/slot-aktif/{rid}/quick-harvest", data={"actor": "op1"})
        assert r.status_code == 200

    def test_done_action(self, client):
        rid = _seed_resi(nomor="RESI-D", sku="1446", ordered=2)
        from packing_router.resi_setup import handle_setup_resi_aktif
        handle_setup_resi_aktif(rid, slot_number=2)
        r = client.post(f"/slot-aktif/{rid}/done", data={"actor": "op1"})
        assert r.status_code == 200

    def test_pack_when_complete(self, client):
        rid = _seed_resi(nomor="RESI-P", sku="1446", ordered=1)
        from packing_router.resi_setup import handle_setup_resi_aktif
        handle_setup_resi_aktif(rid, slot_number=4)
        # Match scan → resi auto-complete
        handle_scan_plastik("1446", operator_id="op1")
        r = client.post(f"/slot-aktif/{rid}/pack", data={"actor": "packer"})
        assert r.status_code == 200

    def test_cancel_route(self, client):
        rid = _seed_resi(nomor="RESI-C", sku="1446", ordered=1)
        from packing_router.resi_setup import handle_setup_resi_aktif
        handle_setup_resi_aktif(rid, slot_number=5)
        r = client.post(f"/slot-aktif/{rid}/cancel", data={"actor": "admin"})
        assert r.status_code == 200

    def test_slot_details_panel_existing(self, client):
        rid = _seed_resi(nomor="RESI-PNL", sku="1446", ordered=1)
        from packing_router.resi_setup import handle_setup_resi_aktif
        handle_setup_resi_aktif(rid, slot_number=6)
        r = client.get(f"/slot-aktif/{rid}/details-panel")
        assert r.status_code == 200

    def test_slot_details_panel_nonexistent(self, client):
        r = client.get("/slot-aktif/99999/details-panel")
        assert r.status_code == 200

    def test_prefill_toggle_action(self, client):
        rid = _seed_resi(nomor="RESI-PRE", sku="1446", ordered=1)
        from packing_router.resi_setup import handle_setup_resi_aktif
        handle_setup_resi_aktif(rid, slot_number=7)
        conn = get_connection()
        item_id = conn.execute(
            "SELECT id FROM resi_item WHERE resi_id = ?", (rid,)
        ).fetchone()["id"]
        r = client.post(f"/slot-aktif/{rid}/prefill/{item_id}", data={"actor": "op1"})
        assert r.status_code == 200


class TestHarvesterRoutes:
    def test_pickup_unknown_barcode_returns_alert(self, client):
        r = client.post(
            "/harvester/pickup",
            data={"barcode": "9999", "harvester_id": "hv1"},
        )
        assert r.status_code == 200

    def test_dropoff_with_bad_slot_returns_alert(self, client):
        r = client.post(
            "/harvester/dropoff",
            data={"barcode": "9999", "slot_aktif_number": "abc", "harvester_id": "hv1"},
        )
        assert r.status_code == 200


class TestAdminRoutes:
    def test_add_slot_aktif(self, client):
        r = client.post("/admin/slot-aktif", data={"count": "3"})
        assert r.status_code == 200

    def test_add_slot_aktif_invalid_count_defaults_to_1(self, client):
        r = client.post("/admin/slot-aktif", data={"count": "abc"})
        assert r.status_code == 200

    def test_add_wadah(self, client):
        r = client.post("/admin/wadah", data={"capacity": "5"})
        assert r.status_code == 200

    def test_add_wadah_invalid_capacity(self, client):
        r = client.post("/admin/wadah", data={"capacity": "xyz"})
        assert r.status_code == 200

    def test_remove_wadah_when_empty(self, client):
        # buffer_seeded default: 2 wadah kosong → bisa hapus 1
        r = client.post("/admin/wadah/remove", data={"actor": "admin"})
        assert r.status_code == 200

    def test_remove_slot_aktif(self, client):
        r = client.post("/admin/slot-aktif/remove", data={"count": "1"})
        assert r.status_code == 200

    def test_reset_slot_aktif(self, client):
        r = client.post("/admin/reset-slot-aktif", data={"actor": "admin"})
        assert r.status_code == 200

    def test_reset_buffer(self, client):
        handle_scan_plastik("1446", operator_id="op1")
        r = client.post("/admin/reset-buffer", data={"actor": "admin"})
        assert r.status_code == 200

    def test_sync_sheet_without_config_returns_error_partial(self, client):
        r = client.post("/admin/sync-sheet", data={"actor": "admin"})
        # gspread import-time fail (no config.json) → renders error partial
        assert r.status_code == 200
        assert b"gagal" in r.data.lower() or b"error" in r.data.lower()

    def test_import_batch_empty_id(self, client):
        r = client.post("/admin/import", data={"batch_id": ""})
        assert r.status_code == 200
        assert b"kosong" in r.data.lower()

    def test_wave_next(self, client):
        r = client.post("/admin/wave-next", data={"actor": "admin"})
        assert r.status_code == 200
