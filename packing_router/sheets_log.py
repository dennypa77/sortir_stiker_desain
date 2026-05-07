"""Append baris log ke sheet DATA_SALES saat resi di-pack.

Fungsi tambahan ditulis di modul ini, BUKAN tambah ke app.py existing.
"""
import json
import os
from datetime import datetime
from typing import Iterable, Optional

from . import config
from .db import get_connection
from .utils import parse_sku


def _load_credentials_path():
    cfg_path = config.CONFIG_JSON_PATH
    if not os.path.exists(cfg_path):
        raise FileNotFoundError(f"config.json tidak ditemukan di {cfg_path}")
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    json_path = cfg.get("json_path") or cfg.get("json")
    sheet_url = cfg.get("gsheet_url") or cfg.get("sheet_url")
    if not json_path or not sheet_url:
        raise RuntimeError("config.json kurang field 'json_path' atau 'gsheet_url'")
    return json_path, sheet_url


def _get_data_sales_worksheet():
    import gspread  # type: ignore

    json_path, sheet_url = _load_credentials_path()
    gc = gspread.service_account(filename=json_path)
    sh = gc.open_by_url(sheet_url) if sheet_url.startswith("http") else gc.open_by_key(sheet_url)
    return sh.worksheet(config.DATA_SALES_SHEET_NAME)


def append_pack_log(resi_id: int, ws=None) -> int:
    """Append baris log ke sheet ``DATA_SALES``: (tanggal, ID master, total pcs).

    ``ws`` opsional — kalau dipass, skip auth gspread (dipakai testing).
    Return jumlah row yang di-append.
    """
    conn = get_connection()
    items = conn.execute(
        "SELECT sku, varian, quantity_ordered FROM resi_item WHERE resi_id = ?",
        (resi_id,),
    ).fetchall()
    rows = []
    today = datetime.now().strftime("%Y-%m-%d")
    for it in items:
        numeric_id, _ = parse_sku(it["sku"])
        if numeric_id is None:
            continue
        total_pcs = int(it["quantity_ordered"]) * int(it["varian"] or 0)
        rows.append([today, numeric_id, total_pcs])
    if not rows:
        return 0
    if ws is None:
        ws = _get_data_sales_worksheet()
    ws.append_rows(rows, value_input_option="USER_ENTERED")
    return len(rows)
