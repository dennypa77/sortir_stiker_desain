"""Flask app Stasiun QC — frontend HTML pengganti UI desktop.

Routes meniru alur QcStasiunWindow (qc_stasiun.py):
  GET  /                            -> layar idle (scan resi)
  POST /resi/scan                   -> cari resi, mulai/resume sesi QC
  POST /session/<sid>/pack/scan     -> scan/ketik 1 pack, verifikasi SKU
  POST /session/<sid>/visual-confirm/<pid> -> konfirmasi item non-stiker
  POST /session/<sid>/approve       -> LOLOS, seal, tulis ke sheet
  POST /session/<sid>/reject        -> REJECT + alasan
  POST /session/<sid>/cancel        -> kembali ke idle (sesi tetap in_progress)
  POST /refresh                     -> reload cache sheet + stats

Suara & TTS dipindah ke browser: Web Audio API (beep) + speechSynthesis (TTS),
dipicu lewat header HX-Trigger (lihat base.html).
"""
from __future__ import annotations

import os
import json
import socket

from flask import Flask, render_template, request

from qc_stasiun import (
    QC_VERSION,
    REJECT_REASONS,
    STATUS_QC_APPROVED,
    STATUS_QC_REJECTED,
    SheetAdapter,
    calculate_packs_needed,
    close_session,
    create_session,
    find_active_session,
    find_completed_session,
    get_db,
    get_session_progress,
    increment_scan,
    init_db,
    is_session_complete,
    log_event,
    parse_sku,
    set_visual_confirm,
    stats_today,
)


def _sheet_rows_to_line_items(sheet_rows):
    """Replika QcStasiunWindow._sheet_rows_to_line_items (UI-agnostic).
    Aggregate by design_sku; non-stiker (design_sku None) di-key pakai SKU asli.
    """
    agg = {}
    for row in sheet_rows:
        sku_raw = row["bigseller_sku"]
        jumlah = row["jumlah"]
        design_sku, pcs_per_paket = parse_sku(sku_raw)
        if design_sku is None:
            if sku_raw in agg:
                continue
            agg[sku_raw] = {
                "design_sku": "", "bigseller_sku": sku_raw,
                "target_packs": 0, "is_non_stiker": True,
            }
        else:
            target = calculate_packs_needed(pcs_per_paket, jumlah)
            if design_sku in agg:
                agg[design_sku]["target_packs"] += target
            else:
                agg[design_sku] = {
                    "design_sku": design_sku, "bigseller_sku": sku_raw,
                    "target_packs": target, "is_non_stiker": False,
                }
    return list(agg.values())

CONFIG_FILE = "config.json"
WEB_HOST = "127.0.0.1"
WEB_PORT = 5057


# ============================================================
# KONEKSI SHEET (mandiri — tidak import run_qc.py supaya tidak
# menyeret dependency tkinter/pygame ke proses web)
# ============================================================
def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def connect_spreadsheet(cfg):
    """Authenticate ke Google Sheets pakai config existing (sama dgn run_qc.py)."""
    from google.oauth2.service_account import Credentials
    import gspread

    url = (cfg.get("gsheet_url") or "").strip()
    jpath = (cfg.get("json_path") or "").strip()
    if not url:
        raise RuntimeError(
            "config.json belum punya 'gsheet_url'. Set via app.py tab "
            "'Koneksi Gudang' dulu."
        )
    if not jpath or not os.path.exists(jpath):
        raise RuntimeError(f"File JSON credential tidak ditemukan: {jpath}")

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(jpath, scopes=scopes)
    client = gspread.authorize(creds)
    ss = (
        client.open_by_url(url)
        if "spreadsheets/d/" in url
        else client.open_by_key(url)
    )
    ss.worksheet("LIST_PESANAN")  # verifikasi sheet ada
    return ss


def operator_name():
    return (
        os.environ.get("COMPUTERNAME")
        or os.environ.get("USERNAME")
        or socket.gethostname()
        or "QC"
    )


# ============================================================
# VIEW HELPERS
# ============================================================
def _hx(trigger):
    """Bungkus dict event jadi header HX-Trigger (JSON)."""
    return {"HX-Trigger": json.dumps(trigger)} if trigger else {}


def _item_view(p):
    """View-model 1 baris checklist — mirror logika _refresh_progress_row()."""
    if p["is_non_stiker"]:
        if p["is_visual_confirmed"]:
            return {
                "state": "ok", "icon": "✔", "title": "✔ SUDAH DIKONFIRMASI",
                "detail": "Item non-stiker — visual confirm OK", "shortfall": "",
            }
        return {
            "state": "wait", "icon": "□", "title": "MENUNGGU KONFIRMASI",
            "detail": "Item non-stiker — klik Visual Confirm", "shortfall": "",
        }

    scanned, target = p["scanned_packs"], p["target_packs"]
    if scanned == 0:
        return {
            "state": "todo", "icon": "◯", "title": "BELUM DI-SCAN",
            "detail": f"Butuh {target} pack — scan 1x untuk verifikasi",
            "shortfall": f"⚠ KURANG {target} PACK" if target > 1 else "",
        }
    if target <= 1:
        detail = f"Butuh 1 pack • scan: {scanned}x"
        shortfall = ""
    elif scanned < target:
        detail = f"Butuh {target} pack • baru scan {scanned}x"
        shortfall = f"⚠ KURANG {target - scanned} PACK"
    elif scanned == target:
        detail = f"Butuh {target} pack • scan lengkap ({scanned}/{target})"
        shortfall = ""
    else:
        detail = f"Butuh {target} pack • scan {scanned}x (kelebihan {scanned - target}x — OK)"
        shortfall = ""
    return {
        "state": "ok", "icon": "✔", "title": "✔ SKU SESUAI",
        "detail": detail, "shortfall": shortfall,
    }


def _items_for(session_id):
    """Gabung progress row + view-model untuk template."""
    return [{**p, "view": _item_view(p)} for p in get_session_progress(session_id)]


def _resi_of(session_id):
    """Ambil resi_code dari id sesi (untuk hidden field approve di OOB swap)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT resi_code FROM qc_sessions WHERE id=?", (session_id,)
        ).fetchone()
    return row["resi_code"] if row else ""


def _wants_partial():
    return request.headers.get("HX-Request") == "true"


# ============================================================
# APP FACTORY
# ============================================================
def create_app():
    app = Flask(__name__)
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    init_db()

    # State proses: 1 adapter sheet (cache in-memory) dipakai semua request.
    app.config["ADAPTER"] = None
    app.config["CONN_ERROR"] = None
    try:
        ss = connect_spreadsheet(load_config())
        app.config["ADAPTER"] = SheetAdapter(ss)
    except Exception as e:  # noqa: BLE001
        app.config["CONN_ERROR"] = str(e)

    def adapter():
        return app.config["ADAPTER"]

    @app.context_processor
    def _globals():
        return {"qc_version": QC_VERSION}

    # ---- IDLE ----
    def _render_idle(status=None, status_kind="info", trigger=None):
        ad = adapter()
        try:
            total = ad.get_total_resi_count() if ad else 0
            pending = ad.get_pending_resi_count() if ad else 0
        except Exception:  # noqa: BLE001
            total = pending = 0
        ctx = {
            "stats": stats_today(),
            "total": total,
            "pending": pending,
            "status": status,
            "status_kind": status_kind,
            "conn_error": app.config["CONN_ERROR"],
        }
        tmpl = "partials/_idle.html" if _wants_partial() else "idle.html"
        return render_template(tmpl, **ctx), 200, _hx(trigger)

    def _render_session(resi_code, resi_data, sid, last_scan=None,
                        last_kind="info", trigger=None):
        ctx = {
            "resi_code": resi_code,
            "marketplace": (resi_data or {}).get("marketplace") or "Unknown",
            "batch_id": (resi_data or {}).get("batch_id") or "-",
            "session_id": sid,
            "items": _items_for(sid),
            "complete": is_session_complete(sid),
            "reject_reasons": REJECT_REASONS,
            "last_scan": last_scan,
            "last_kind": last_kind,
        }
        tmpl = "partials/_session.html" if _wants_partial() else "qc_session.html"
        return render_template(tmpl, **ctx), 200, _hx(trigger)

    @app.route("/")
    def index():
        return _render_idle()

    # ---- SCAN RESI ----
    @app.route("/resi/scan", methods=["POST"])
    def resi_scan():
        ad = adapter()
        resi = (request.form.get("resi") or "").strip()
        if not resi:
            return _render_idle()
        if not ad:
            return _render_idle(
                status="Koneksi sheet belum siap. Cek config & restart.",
                status_kind="error", trigger={"playMismatch": ""},
            )
        try:
            data = ad.find_resi(resi)
        except Exception as e:  # noqa: BLE001
            return _render_idle(
                status=f"Error fetch sheet: {e}", status_kind="error",
                trigger={"playMismatch": ""},
            )

        if not data:
            return _render_idle(
                status=(
                    f"Resi '{resi}' tidak ditemukan di LIST_PESANAN. "
                    "Pastikan tim gudang sudah upload export BigSeller."
                ),
                status_kind="error",
                trigger={"playMismatch": "", "qcSpeak": {"text": "Resi tidak ditemukan"}},
            )

        done_session = find_completed_session(resi)
        if done_session and done_session["status"] == STATUS_QC_APPROVED:
            return _render_idle(
                status=(
                    f"Resi {resi} SUDAH PERNAH approved "
                    f"({done_session.get('completed_at', '-')}). Tidak perlu di-QC ulang."
                ),
                status_kind="warn",
                trigger={"qcSpeak": {"text": "Resi sudah pernah disetujui"}},
            )

        active = find_active_session(resi)
        if active:
            return _render_session(
                resi, data, active["id"],
                last_scan="Resume sesi sebelumnya.", trigger={"playMatch": ""},
            )

        line_items = _sheet_rows_to_line_items(data["rows"])
        if not line_items:
            return _render_idle(
                status=f"Resi {resi} tidak punya line item valid.",
                status_kind="error", trigger={"playMismatch": ""},
            )

        sid = create_session(
            resi, None, data.get("batch_id"), data.get("marketplace"), line_items
        )
        log_event(sid, None, "session_start",
                  {"resi": resi, "batch": data.get("batch_id")})
        return _render_session(resi, data, sid, trigger={"playMatch": ""})

    # ---- SCAN PACK ----
    @app.route("/session/<int:sid>/pack/scan", methods=["POST"])
    def pack_scan(sid):
        scanned_value = (request.form.get("barcode") or "").strip()
        source = (request.form.get("source") or "scan").strip()
        if not scanned_value:
            return _scan_response(sid, "Input kosong.", "info", None)

        numeric_id, _ = parse_sku(scanned_value)
        target_id = numeric_id or scanned_value
        progress = get_session_progress(sid)
        match = next(
            (p for p in progress
             if not p["is_non_stiker"] and p["design_sku"] == target_id),
            None,
        )

        if match:
            was_first = match["scanned_packs"] == 0
            increment_scan(match["id"])
            scanned = match["scanned_packs"] + 1
            target = match["target_packs"]
            log_event(sid, None,
                      "scan_match" if source == "scan" else f"{source}_match",
                      {"scanned": scanned_value, "design_sku": target_id,
                       "scan_count": scanned, "target": target,
                       "first_scan": was_first})
            if was_first and target > 1:
                msg = f"✔ SKU {target_id} SESUAI • pastikan {target} pack masuk polymailer"
            elif was_first:
                msg = f"✔ SKU {target_id} SESUAI"
            else:
                msg = f"✔ SKU {target_id} sudah verified (scan ke-{scanned})"
            complete = is_session_complete(sid)
            trig = {"playMatch": ""}
            if complete:
                trig = {"playComplete": "", "qcSpeak": {"text": "Resi selesai, silakan seal"}}
            return _scan_response(sid, msg, "ok", trig)

        log_event(sid, None, "scan_mismatch",
                  {"scanned": scanned_value, "design_sku": target_id,
                   "reason": "not_in_resi", "source": source})
        return _scan_response(
            sid, f"✗ SKU {target_id} TIDAK ADA di resi ini", "error",
            {"playMismatch": "", "qcSpeak": {"text": "SKU tidak sesuai"}},
        )

    def _scan_response(sid, msg, kind, trigger):
        return (
            render_template(
                "partials/_scan_response.html",
                items=_items_for(sid),
                complete=is_session_complete(sid),
                session_id=sid,
                resi_code=_resi_of(sid),
                last_scan=msg,
                last_kind=kind,
            ),
            200,
            _hx(trigger),
        )

    # ---- VISUAL CONFIRM (non-stiker) ----
    @app.route("/session/<int:sid>/visual-confirm/<int:pid>", methods=["POST"])
    def visual_confirm(sid, pid):
        set_visual_confirm(pid, True)
        p = next((x for x in get_session_progress(sid) if x["id"] == pid), None)
        log_event(sid, None, "visual_confirm",
                  {"progress_id": pid, "sku": p["bigseller_sku"] if p else ""})
        complete = is_session_complete(sid)
        trig = {"playMatch": ""}
        if complete:
            trig = {"playComplete": "", "qcSpeak": {"text": "Resi selesai, silakan seal"}}
        return _scan_response(sid, "✔ Item non-stiker dikonfirmasi", "ok", trig)

    # ---- APPROVE ----
    @app.route("/session/<int:sid>/approve", methods=["POST"])
    def approve(sid):
        ad = adapter()
        resi = (request.form.get("resi") or "").strip()
        data = ad.find_resi(resi) if ad else None
        op = operator_name()
        marketplace = (data or {}).get("marketplace") or ""
        batch_id = (data or {}).get("batch_id") or ""

        if not is_session_complete(sid):
            return _render_session(resi, data, sid,
                                   last_scan="Belum semua item terverifikasi.",
                                   last_kind="error", trigger={"playMismatch": ""})
        try:
            if ad:
                ad.update_resi_qc_status(resi, STATUS_QC_APPROVED, op, "")
            close_session(sid, STATUS_QC_APPROVED)
            log_event(sid, None, "approve", {"resi": resi})
            if ad:
                try:
                    ad.append_qc_result(resi, marketplace, batch_id, "LOLOS", op)
                except Exception as e:  # noqa: BLE001
                    print(f"[Hasil QC] gagal catat approve {resi}: {e}")
        except Exception as e:  # noqa: BLE001
            close_session(sid, STATUS_QC_APPROVED)
            log_event(sid, None, "approve_sheet_fail", {"error": str(e)})
            return _render_idle(
                status=(f"Approve tersimpan lokal tapi GAGAL update sheet: {e}. "
                        "Klik Refresh lalu scan resi ini lagi untuk re-sync."),
                status_kind="error", trigger={"playMismatch": ""},
            )
        return _render_idle(
            status=f"✔ Resi {resi} approved. Silakan seal & lanjut resi berikutnya.",
            status_kind="ok",
            trigger={"playComplete": "", "qcSpeak": {"text": "Approved"}},
        )

    # ---- REJECT ----
    @app.route("/session/<int:sid>/reject", methods=["POST"])
    def reject(sid):
        ad = adapter()
        resi = (request.form.get("resi") or "").strip()
        reason = (request.form.get("reason") or REJECT_REASONS[0]).strip()
        notes = (request.form.get("notes") or "").strip()
        data = ad.find_resi(resi) if ad else None
        op = operator_name()
        marketplace = (data or {}).get("marketplace") or ""
        batch_id = (data or {}).get("batch_id") or ""
        full_notes = f"{reason}: {notes}".strip(": ").strip()

        try:
            if ad:
                ad.update_resi_qc_status(resi, STATUS_QC_REJECTED, op, full_notes)
            close_session(sid, STATUS_QC_REJECTED, reject_reason=reason, reject_notes=notes)
            log_event(sid, None, "reject",
                      {"resi": resi, "reason": reason, "notes": notes})
            if ad:
                try:
                    ad.append_qc_result(resi, marketplace, batch_id, "REJECT", op,
                                        reject_reason=reason, notes=notes)
                except Exception as e:  # noqa: BLE001
                    print(f"[Hasil QC] gagal catat reject {resi}: {e}")
        except Exception as e:  # noqa: BLE001
            close_session(sid, STATUS_QC_REJECTED, reject_reason=reason, reject_notes=notes)
            return _render_idle(
                status=f"Reject tersimpan lokal tapi gagal update sheet: {e}",
                status_kind="error", trigger={"playMismatch": ""},
            )
        return _render_idle(
            status=f"✗ Resi {resi} di-reject ({reason}). Pisahkan polymailer untuk koreksi.",
            status_kind="error",
            trigger={"playMismatch": "", "qcSpeak": {"text": "Rejected"}},
        )

    # ---- CANCEL (kembali ke idle, sesi tetap in_progress) ----
    @app.route("/session/<int:sid>/cancel", methods=["POST"])
    def cancel(sid):
        resi = (request.form.get("resi") or "").strip()
        log_event(sid, None, "cancel_session", {"resi": resi})
        return _render_idle(
            status=f"Sesi {resi} disimpan sebagai in_progress. Scan lagi untuk lanjut.",
            status_kind="info",
        )

    # ---- REFRESH SHEET ----
    @app.route("/refresh", methods=["POST"])
    def refresh():
        ad = adapter()
        if not ad:
            return render_template("partials/_stats.html", stats=stats_today(),
                                   total=0, pending=0,
                                   refresh_msg="Koneksi sheet belum siap.")
        try:
            ad.refresh()
            total = ad.get_total_resi_count()
            pending = ad.get_pending_resi_count()
            msg = f"✔ Berhasil load {total} resi • {pending} pending QC"
        except Exception as e:  # noqa: BLE001
            total = pending = 0
            msg = f"Refresh gagal: {e}"
        return (
            render_template("partials/_stats.html", stats=stats_today(),
                            total=total, pending=pending, refresh_msg=msg),
            200,
            _hx({"playSetup": ""}),
        )

    return app


if __name__ == "__main__":
    create_app().run(host=WEB_HOST, port=WEB_PORT, debug=True)
