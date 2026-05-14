"""Flask app: 4 view (operator/scan, harvester/queue, slot-aktif, admin)."""
from __future__ import annotations

from flask import Flask, jsonify, redirect, render_template, request, url_for

from .. import config
from ..buffer import add_wadah, get_buffer_status, remove_last_wadah
from ..db import get_connection, init_default_wadah
from ..exceptions import (
    BarcodeFormatError,
    BufferFullError,
    HarvesterMismatchError,
    PackingRouterError,
    ResiNotFoundError,
    SlotAktifConflictError,
    UndoWindowExpiredError,
    WadahConflictError,
    WaveTransitionError,
)
from ..harvester import (
    harvester_dropoff_scan,
    harvester_pickup_scan,
    quick_harvest_to_resi,
)
from ..maintenance import (
    cancel_resi,
    mark_resi_done,
    mark_resi_item_prefilled,
    pack_resi,
    reset_buffer,
    reset_slot_aktif,
    undo_last_scan,
)
from ..reports import (
    get_buffer_aging_report,
    get_buffer_match_status,
    get_harvester_queue,
    get_slot_aktif_match_status,
    get_slot_aktif_status,
)
from ..slot_aktif import (
    add_slot_aktif,
    get_slot_aktif_count,
    init_default_slot_aktif,
    remove_last_slot_aktif,
)
from ..resi_setup import (
    import_from_list_pesanan_sheet,
    setup_resi_by_nomor,
    sync_list_pesanan_to_db,
    try_activate_next_wave,
)
from ..scan_handler import handle_scan_plastik


def _trigger(*events: str) -> dict:
    """HX-Trigger header value untuk fire event di body. Filter empty string."""
    real = [e for e in events if e]
    return {"HX-Trigger": ", ".join(real)} if real else {}


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    get_connection()
    init_default_wadah()
    init_default_slot_aktif()

    @app.errorhandler(PackingRouterError)
    def _handle_domain_error(err: PackingRouterError):
        if request.headers.get("HX-Request"):
            return render_template("partials/_alert.html", message=str(err)), 200
        return jsonify({"error": err.__class__.__name__, "message": str(err)}), 400

    @app.route("/")
    def root():
        return redirect(url_for("dashboard_view"))

    # --- Unified dashboard: slot aktif (atas) + buffer (bawah) + side panel ---
    @app.route("/dashboard", methods=["GET"])
    def dashboard_view():
        operator_id = request.args.get("op", "").strip() or _default_operator_id()
        return render_template(
            "dashboard.html",
            operator_id=operator_id,
            slots=get_slot_aktif_match_status(),
            buffer=get_buffer_match_status(),
            tasks=get_harvester_queue(),
            slot_count=get_slot_aktif_count(),
            kuning_min=config.SLOT_KUNING_TIMEOUT_MIN,
            undo_window=config.UNDO_WINDOW_SECONDS,
        )

    @app.route("/dashboard/refresh", methods=["GET"])
    def dashboard_refresh():
        return render_template(
            "partials/_dashboard_grids.html",
            slots=get_slot_aktif_match_status(),
            buffer=get_buffer_match_status(),
            kuning_min=config.SLOT_KUNING_TIMEOUT_MIN,
        )

    # --- Operator scan ---
    @app.route("/operator/scan", methods=["GET"])
    def operator_scan_view():
        operator_id = request.args.get("op", "").strip() or _default_operator_id()
        history = _get_recent_scans(operator_id, limit=5)
        return render_template(
            "operator_scan.html",
            operator_id=operator_id,
            history=history,
            undo_window=config.UNDO_WINDOW_SECONDS,
        )

    @app.route("/operator/scan", methods=["POST"])
    def operator_scan_submit():
        operator_id = (request.form.get("operator_id") or "").strip() or _default_operator_id()
        barcode = (request.form.get("barcode") or "").strip()
        pack_size_raw = (request.form.get("pack_size") or "10").strip()
        pack_units = 5 if pack_size_raw == "50" else 1
        if not barcode:
            return (
                render_template(
                    "partials/_scan_result.html",
                    error="Barcode kosong",
                    history=_get_recent_scans(operator_id, limit=5),
                    operator_id=operator_id,
                ),
                200,
                _trigger("playError"),
            )
        try:
            result = handle_scan_plastik(
                barcode, operator_id=operator_id, pack_units=pack_units
            )
        except (BarcodeFormatError, BufferFullError) as e:
            return (
                render_template(
                    "partials/_scan_result.html",
                    error=str(e),
                    history=_get_recent_scans(operator_id, limit=5),
                    operator_id=operator_id,
                ),
                200,
                _trigger("playError"),
            )
        match_event = "playMatch" if result.action == "place_in_slot_aktif" else ""
        return (
            render_template(
                "partials/_scan_result.html",
                result=result,
                history=_get_recent_scans(operator_id, limit=5),
                operator_id=operator_id,
            ),
            200,
            _trigger("playScan", match_event),
        )

    @app.route("/operator/setup-resi", methods=["POST"])
    def operator_setup_resi():
        operator_id = (request.form.get("operator_id") or "").strip() or _default_operator_id()
        nomor_resi = (request.form.get("nomor_resi") or "").strip()
        if not nomor_resi:
            return (
                render_template(
                    "partials/_setup_resi_result.html",
                    error="Nomor resi kosong",
                    operator_id=operator_id,
                ),
                200,
                _trigger("playError"),
            )
        try:
            result = setup_resi_by_nomor(nomor_resi, actor=operator_id)
        except (ResiNotFoundError, SlotAktifConflictError) as e:
            return (
                render_template(
                    "partials/_setup_resi_result.html",
                    error=str(e),
                    operator_id=operator_id,
                ),
                200,
                _trigger("playError"),
            )
        match_event = "playMatch" if result.buffer_pickups else ""
        return (
            render_template(
                "partials/_setup_resi_result.html",
                setup_result=result,
                operator_id=operator_id,
            ),
            200,
            _trigger("playSetup", match_event),
        )

    @app.route("/operator/undo", methods=["POST"])
    def operator_undo():
        operator_id = (request.form.get("operator_id") or "").strip() or _default_operator_id()
        try:
            res = undo_last_scan(operator_id)
            msg = f"Undo OK — {res.action_undone}: {res.detail}"
        except UndoWindowExpiredError as e:
            msg = f"Undo gagal — {e}"
        return render_template(
            "partials/_scan_result.html",
            info=msg,
            history=_get_recent_scans(operator_id, limit=5),
            operator_id=operator_id,
        )

    # --- Harvester (queue & slot aktif sekarang gabung di /dashboard) ---
    @app.route("/harvester/queue", methods=["GET"])
    def harvester_queue_view():
        return redirect(url_for("dashboard_view"))

    @app.route("/slot-aktif", methods=["GET"])
    def slot_aktif_view():
        return redirect(url_for("dashboard_view"))

    @app.route("/harvester/queue/refresh", methods=["GET"])
    def harvester_queue_refresh():
        harvester_id = request.args.get("hv", "").strip() or _default_operator_id()
        return render_template(
            "partials/_harvester_tasks.html",
            harvester_id=harvester_id,
            tasks=get_harvester_queue(),
        )

    @app.route("/harvester/pickup", methods=["POST"])
    def harvester_pickup():
        harvester_id = (request.form.get("harvester_id") or "").strip() or _default_operator_id()
        barcode = (request.form.get("barcode") or "").strip()
        try:
            res = harvester_pickup_scan(barcode, harvester_id=harvester_id)
            return (
                render_template(
                    "partials/_harvester_pickup_ok.html",
                    result=res,
                    harvester_id=harvester_id,
                    tasks=get_harvester_queue(),
                ),
                200,
                _trigger("playPickup"),
            )
        except HarvesterMismatchError as e:
            return (
                render_template(
                    "partials/_harvester_alert.html",
                    message=str(e),
                    harvester_id=harvester_id,
                    tasks=get_harvester_queue(),
                ),
                200,
                _trigger("playError"),
            )

    @app.route("/harvester/dropoff", methods=["POST"])
    def harvester_dropoff():
        harvester_id = (request.form.get("harvester_id") or "").strip() or _default_operator_id()
        barcode = (request.form.get("barcode") or "").strip()
        try:
            slot_no = int(request.form.get("slot_aktif_number", "0"))
        except ValueError:
            slot_no = 0
        try:
            res = harvester_dropoff_scan(
                barcode, target_slot_aktif_number=slot_no, harvester_id=harvester_id
            )
            match_event = "playMatch" if res.resi_completed else ""
            return (
                render_template(
                    "partials/_harvester_dropoff_ok.html",
                    result=res,
                    harvester_id=harvester_id,
                    tasks=get_harvester_queue(),
                ),
                200,
                _trigger("playPickup", match_event),
            )
        except HarvesterMismatchError as e:
            return (
                render_template(
                    "partials/_harvester_alert.html",
                    message=str(e),
                    harvester_id=harvester_id,
                    tasks=get_harvester_queue(),
                ),
                200,
                _trigger("playError"),
            )

    @app.route("/slot-aktif/<int:resi_id>/details-panel", methods=["GET"])
    def slot_details_panel(resi_id: int):
        """Render panel detail slot (Kurang + Stok Gudang) untuk sidebar.
        Dipanggil saat operator klik kartu slot di dashboard."""
        slots = get_slot_aktif_match_status()
        slot = next((s for s in slots if s.get("resi_id") == resi_id), None)
        return render_template(
            "partials/_slot_details_panel.html",
            slot=slot,
            oob=False,
        )

    @app.route("/slot-aktif/<int:resi_id>/prefill/<int:item_id>", methods=["POST"])
    def mark_prefilled_action(resi_id: int, item_id: int):
        """Toggle SKU 'sudah dari stok gudang' (stabilo). Response berisi:
        - main: dashboard_grids partial (untuk #dashboard-grids)
        - OOB: details panel partial (untuk #kurang-panel di sidebar)"""
        actor = (request.form.get("actor") or _default_operator_id()).strip()
        try:
            res = mark_resi_item_prefilled(resi_id, item_id, actor=actor)
            label = "📦 ditandai dari gudang" if res["is_prefilled"] else "↺ tanda gudang dilepas"
            info = f"{label} — SKU {res['sku']}"
            if res["resi_completed"]:
                info += " — RESI LENGKAP, siap pack!"
            match_event = "playMatch" if res["resi_completed"] else "playSetup"
            slots = get_slot_aktif_match_status()
            target_slot = next((s for s in slots if s.get("resi_id") == resi_id), None)
            html = render_template(
                "partials/_dashboard_grids.html",
                slots=slots,
                buffer=get_buffer_match_status(),
                info=info,
                kuning_min=config.SLOT_KUNING_TIMEOUT_MIN,
            )
            html += render_template(
                "partials/_slot_details_panel.html",
                slot=target_slot,
                oob=True,
            )
            return html, 200, _trigger(match_event)
        except (ResiNotFoundError, SlotAktifConflictError) as e:
            return (
                render_template(
                    "partials/_dashboard_grids.html",
                    slots=get_slot_aktif_match_status(),
                    buffer=get_buffer_match_status(),
                    error=str(e),
                    kuning_min=config.SLOT_KUNING_TIMEOUT_MIN,
                ),
                200,
                _trigger("playError"),
            )

    @app.route("/slot-aktif/<int:resi_id>/done", methods=["POST"])
    def mark_done_action(resi_id: int):
        actor = (request.form.get("actor") or _default_operator_id()).strip()
        try:
            res = mark_resi_done(resi_id, actor=actor)
            return (
                render_template(
                    "partials/_dashboard_grids.html",
                    slots=get_slot_aktif_match_status(),
                    buffer=get_buffer_match_status(),
                    info=f"✓ Done {res['nomor_resi']} — siap pack",
                    kuning_min=config.SLOT_KUNING_TIMEOUT_MIN,
                ),
                200,
                _trigger("playMatch"),
            )
        except (ResiNotFoundError, SlotAktifConflictError) as e:
            return (
                render_template(
                    "partials/_dashboard_grids.html",
                    slots=get_slot_aktif_match_status(),
                    buffer=get_buffer_match_status(),
                    error=str(e),
                    kuning_min=config.SLOT_KUNING_TIMEOUT_MIN,
                ),
                200,
                _trigger("playError"),
            )

    @app.route("/slot-aktif/<int:resi_id>/quick-harvest", methods=["POST"])
    def quick_harvest_action(resi_id: int):
        actor = (request.form.get("actor") or _default_operator_id()).strip()
        try:
            result = quick_harvest_to_resi(resi_id, actor=actor)
        except (ResiNotFoundError, SlotAktifConflictError) as e:
            return (
                render_template(
                    "partials/_dashboard_grids.html",
                    slots=get_slot_aktif_match_status(),
                    buffer=get_buffer_match_status(),
                    error=str(e),
                    kuning_min=config.SLOT_KUNING_TIMEOUT_MIN,
                ),
                200,
                _trigger("playError"),
            )
        if not result["moved"]:
            info = "⚠️ Tidak ada plastik di buffer yang match dengan resi ini."
            return (
                render_template(
                    "partials/_dashboard_grids.html",
                    slots=get_slot_aktif_match_status(),
                    buffer=get_buffer_match_status(),
                    info=info,
                    kuning_min=config.SLOT_KUNING_TIMEOUT_MIN,
                ),
                200,
                _trigger("playError"),
            )
        moves_text = ", ".join(
            f"{m['barcode']} (W{m['wadah_nomor']}S{m['slot_number']}→Slot {m['to_slot_aktif']})"
            for m in result["moved"]
        )
        info = f"⚡ Pindah {len(result['moved'])} plastik: {moves_text}"
        if result["resi_completed"]:
            info += " — RESI LENGKAP, siap pack!"
        return (
            render_template(
                "partials/_dashboard_grids.html",
                slots=get_slot_aktif_match_status(),
                buffer=get_buffer_match_status(),
                info=info,
                kuning_min=config.SLOT_KUNING_TIMEOUT_MIN,
            ),
            200,
            _trigger("playPickup", "playMatch"),
        )

    # --- Slot aktif actions (pack/cancel) — view-nya gabung di /dashboard ---
    @app.route("/slot-aktif/<int:resi_id>/pack", methods=["POST"])
    def pack_resi_action(resi_id: int):
        actor = (request.form.get("actor") or "packer").strip()
        try:
            res = pack_resi(resi_id, actor=actor)
            return (
                render_template(
                    "partials/_dashboard_grids.html",
                    slots=get_slot_aktif_match_status(),
                    buffer=get_buffer_match_status(),
                    info=f"Packed {res['nomor_resi']}",
                    kuning_min=config.SLOT_KUNING_TIMEOUT_MIN,
                ),
                200,
                _trigger("playMatch"),
            )
        except ResiNotFoundError as e:
            return (
                render_template(
                    "partials/_dashboard_grids.html",
                    slots=get_slot_aktif_match_status(),
                    buffer=get_buffer_match_status(),
                    error=str(e),
                    kuning_min=config.SLOT_KUNING_TIMEOUT_MIN,
                ),
                200,
                _trigger("playError"),
            )

    @app.route("/slot-aktif/<int:resi_id>/cancel", methods=["POST"])
    def cancel_resi_action(resi_id: int):
        actor = (request.form.get("actor") or "admin").strip()
        try:
            cancel_resi(resi_id, actor=actor)
            return render_template(
                "partials/_dashboard_grids.html",
                slots=get_slot_aktif_match_status(),
                buffer=get_buffer_match_status(),
                info=f"Resi id={resi_id} cancelled, plastik di-route ulang",
                kuning_min=config.SLOT_KUNING_TIMEOUT_MIN,
            )
        except ResiNotFoundError as e:
            return render_template(
                "partials/_dashboard_grids.html",
                slots=get_slot_aktif_match_status(),
                buffer=get_buffer_match_status(),
                error=str(e),
                kuning_min=config.SLOT_KUNING_TIMEOUT_MIN,
            )

    # --- Admin ---
    @app.route("/admin", methods=["GET"])
    def admin_view():
        return render_template(
            "admin.html",
            buffer_status=get_buffer_status(),
            aging=get_buffer_aging_report(),
            throughput=_get_throughput_per_hour(),
            aging_threshold=config.BUFFER_AGING_HOURS,
            slot_count=get_slot_aktif_count(),
        )

    @app.route("/admin/slot-aktif", methods=["POST"])
    def admin_add_slot_aktif():
        actor = (request.form.get("actor") or "admin").strip()
        try:
            count = max(1, int(request.form.get("count") or "1"))
        except ValueError:
            count = 1
        added = add_slot_aktif(actor=actor, count=count)
        return render_template(
            "partials/_admin_slot_aktif.html",
            slot_count=get_slot_aktif_count(),
            info=f"{len(added)} slot aktif baru ditambahkan: {added}",
        )

    @app.route("/admin/wadah", methods=["POST"])
    def admin_add_wadah():
        cap_raw = request.form.get("capacity") or str(config.SLOTS_PER_WADAH)
        try:
            cap = int(cap_raw)
        except ValueError:
            cap = config.SLOTS_PER_WADAH
        wadah = add_wadah(capacity=cap)
        return render_template(
            "partials/_admin_buffer.html",
            buffer_status=get_buffer_status(),
            info=f"Wadah {wadah.nomor} ditambahkan ({wadah.capacity} slot)",
        )

    @app.route("/admin/wadah/remove", methods=["POST"])
    def admin_remove_wadah():
        actor = (request.form.get("actor") or "admin").strip()
        try:
            res = remove_last_wadah(actor=actor)
            return (
                render_template(
                    "partials/_admin_buffer.html",
                    buffer_status=get_buffer_status(),
                    info=f"✓ Wadah {res['removed_nomor']} dihapus",
                ),
                200,
                _trigger("playSetup"),
            )
        except WadahConflictError as e:
            return (
                render_template(
                    "partials/_admin_buffer.html",
                    buffer_status=get_buffer_status(),
                    error=str(e),
                ),
                200,
                _trigger("playError"),
            )

    @app.route("/admin/slot-aktif/remove", methods=["POST"])
    def admin_remove_slot_aktif():
        actor = (request.form.get("actor") or "admin").strip()
        try:
            count = max(1, int(request.form.get("count") or "1"))
        except ValueError:
            count = 1
        try:
            removed = remove_last_slot_aktif(actor=actor, count=count)
            return (
                render_template(
                    "partials/_admin_slot_aktif.html",
                    slot_count=get_slot_aktif_count(),
                    info=f"✓ {len(removed)} slot dihapus: {removed}",
                ),
                200,
                _trigger("playSetup"),
            )
        except SlotAktifConflictError as e:
            return (
                render_template(
                    "partials/_admin_slot_aktif.html",
                    slot_count=get_slot_aktif_count(),
                    error=str(e),
                ),
                200,
                _trigger("playError"),
            )

    @app.route("/admin/reset-slot-aktif", methods=["POST"])
    def admin_reset_slot_aktif():
        actor = (request.form.get("actor") or "admin").strip()
        try:
            res = reset_slot_aktif(actor=actor)
            msg = f"✓ Reset slot aktif: {res['affected_resi_count']} resi dikembalikan ke pool pending."
            return render_template(
                "partials/_admin_reset.html", info=msg
            ), 200, _trigger("playSetup")
        except Exception as e:  # noqa: BLE001
            return render_template(
                "partials/_admin_reset.html", error=f"Reset gagal: {e}"
            ), 200, _trigger("playError")

    @app.route("/admin/reset-buffer", methods=["POST"])
    def admin_reset_buffer():
        actor = (request.form.get("actor") or "admin").strip()
        try:
            res = reset_buffer(actor=actor)
            msg = f"✓ Reset buffer: {res['plastik_returned']} plastik di-mark returned."
            return render_template(
                "partials/_admin_reset.html", info=msg
            ), 200, _trigger("playSetup")
        except Exception as e:  # noqa: BLE001
            return render_template(
                "partials/_admin_reset.html", error=f"Reset gagal: {e}"
            ), 200, _trigger("playError")

    @app.route("/admin/sync-sheet", methods=["POST"])
    def admin_sync_sheet():
        actor = (request.form.get("actor") or "admin").strip()
        try:
            summary = sync_list_pesanan_to_db(actor=actor)
            msg = (
                f"Sync OK: {summary['batches']} batch, "
                f"{summary['resis_inserted']} resi baru, "
                f"{summary['items_inserted']} SKU items, "
                f"{summary['skipped']} resi sudah ada (skip)."
            )
            return render_template("partials/_admin_import.html", info=msg)
        except Exception as e:  # noqa: BLE001
            return render_template("partials/_admin_import.html", error=f"Sync gagal: {e}")

    @app.route("/admin/import", methods=["POST"])
    def admin_import_batch():
        batch_id = (request.form.get("batch_id") or "").strip()
        actor = (request.form.get("actor") or "admin").strip()
        if not batch_id:
            return render_template(
                "partials/_admin_import.html",
                error="Batch_ID kosong",
            )
        try:
            res = import_from_list_pesanan_sheet(batch_id, actor=actor)
            return render_template(
                "partials/_admin_import.html",
                info=(
                    f"Import OK: batch {res.batch_id} → {res.resis_imported} resi, "
                    f"{res.waves_created} wave, {len(res.setup_results)} resi di-setup ke Slot Aktif"
                ),
            )
        except WaveTransitionError as e:
            return render_template("partials/_admin_import.html", error=str(e))

    @app.route("/admin/wave-next", methods=["POST"])
    def admin_force_next_wave():
        actor = (request.form.get("actor") or "admin").strip()
        setups = try_activate_next_wave(actor=actor)
        msg = (
            f"Wave berikutnya activated, {len(setups)} resi di-setup"
            if setups
            else "Tidak ada wave berikutnya yang bisa di-activate (threshold belum tercapai atau tidak ada wave pending)"
        )
        return render_template("partials/_admin_import.html", info=msg)

    return app


def _default_operator_id() -> str:
    import os
    import socket

    return os.environ.get("COMPUTERNAME") or socket.gethostname()


def _get_recent_scans(operator_id: str, limit: int = 5):
    """Return list of dict {created_at, event_type, summary} — formatted readable."""
    import json as _json

    conn = get_connection()
    rows = conn.execute(
        """
        SELECT id, event_type, payload, created_at FROM event_log
        WHERE actor = ? AND event_type IN ('scan', 'undo')
        ORDER BY id DESC LIMIT ?
        """,
        (operator_id, limit),
    ).fetchall()

    out = []
    for r in rows:
        try:
            payload = _json.loads(r["payload"]) if r["payload"] else {}
        except (TypeError, ValueError):
            payload = {}
        if r["event_type"] == "scan":
            barcode = payload.get("barcode", "?")
            sku = payload.get("sku", "?")
            action = payload.get("action", "")
            pack_units = int(payload.get("pack_units") or 1)
            bundle_tag = " [50pcs]" if pack_units >= 5 else ""
            if action == "place_in_slot_aktif":
                target = payload.get("target_slot_aktif_number", "?")
                summary = f"Scan {barcode}{bundle_tag} → SLOT {target} (SKU {sku})"
                if payload.get("resi_completed"):
                    summary += " ✓ resi lengkap"
            elif action == "place_in_buffer_existing":
                w = payload.get("wadah_nomor", "?")
                s = payload.get("slot_number", "?")
                cnt = payload.get("plastik_count_after", "?")
                summary = f"Scan {barcode}{bundle_tag} → buffer W{w}S{s} ({cnt} pack)"
            elif action == "place_in_buffer_new":
                w = payload.get("wadah_nomor", "?")
                s = payload.get("slot_number", "?")
                summary = f"Scan {barcode}{bundle_tag} → buffer W{w}S{s} (slot baru)"
            else:
                summary = f"Scan {barcode}{bundle_tag}"
        elif r["event_type"] == "undo":
            act = payload.get("action", "scan")
            summary = f"Undo {act}"
        else:
            summary = r["event_type"]
        out.append(
            {
                "created_at": r["created_at"],
                "event_type": r["event_type"],
                "summary": summary,
            }
        )
    return out


def _get_throughput_per_hour():
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT strftime('%Y-%m-%d %H:00', created_at) AS hour, COUNT(*) AS n
        FROM event_log
        WHERE event_type = 'pack' AND created_at >= datetime('now', '-12 hours')
        GROUP BY hour ORDER BY hour DESC
        """
    ).fetchall()
    return [{"hour": r["hour"], "count": r["n"]} for r in rows]


if __name__ == "__main__":
    app = create_app()
    app.run(host=config.WEB_HOST, port=config.WEB_PORT, debug=config.WEB_DEBUG)
