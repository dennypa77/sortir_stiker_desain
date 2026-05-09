"""Setup resi ke Slot Aktif + sync data dari LIST_PESANAN sheet.

Flow yang baru (sesuai workflow HOG):
1. Admin klik "Sync Sheet" di /admin → modul fetch SEMUA row dari sheet
   ``LIST_PESANAN`` → upsert ke DB sebagai resi `pending` (TIDAK auto-setup
   ke slot aktif).
2. Operator scan kertas resi yang fisik datang → ``setup_resi_by_nomor`` →
   sistem cari di pool DB → assign ke slot aktif kosong terkecil → tampilkan
   instruksi "ambil dari buffer" untuk SKU yang sudah nyangkut.
"""
import json
import os
import sqlite3
from typing import List, Optional

from . import config
from .buffer import find_buffer_slot_for_sku
from .db import get_connection, log_event, now_iso, transaction
from .exceptions import (
    ResiNotFoundError,
    SlotAktifConflictError,
    WaveTransitionError,
)
from .models import ImportResult, SetupResult
from .slot_aktif import get_slot_aktif_numbers, slot_aktif_exists
from .utils import packs_needed, parse_sku


# --- Setup resi (manual, scan-by-scan) ---

def _next_empty_slot(conn: sqlite3.Connection) -> Optional[int]:
    """Return nomor slot aktif aktif terkecil yang kosong (lihat tabel ``slot_aktif``).
    None kalau semua slot terpakai."""
    used = conn.execute(
        """
        SELECT slot_aktif_number FROM resi
        WHERE status IN ('active', 'complete')
          AND slot_aktif_number IS NOT NULL
        """
    ).fetchall()
    used_set = {r["slot_aktif_number"] for r in used}
    available = conn.execute(
        "SELECT nomor FROM slot_aktif WHERE is_active = 1 ORDER BY nomor ASC"
    ).fetchall()
    for r in available:
        if r["nomor"] not in used_set:
            return r["nomor"]
    return None


def _create_harvester_tasks_for_resi(
    conn: sqlite3.Connection,
    resi_id: int,
    actor: str,
) -> List[int]:
    """Untuk tiap resi_item yang masih kurang (sisa = ordered - prefilled - fulfilled),
    cek buffer_slot dengan SKU sama. Buat ``harvester_task`` (status='pending')
    sebanyak min(buffer plastik_count, sisa quantity)."""
    items = conn.execute(
        """
        SELECT id, sku, varian, quantity_ordered, quantity_fulfilled, prefilled_qty
        FROM resi_item
        WHERE resi_id = ?
          AND (quantity_ordered - COALESCE(prefilled_qty, 0) - COALESCE(quantity_fulfilled, 0)) > 0
        """,
        (resi_id,),
    ).fetchall()
    created: List[int] = []
    for item in items:
        sisa = (
            item["quantity_ordered"]
            - (item["prefilled_qty"] or 0)
            - (item["quantity_fulfilled"] or 0)
        )
        if sisa <= 0:
            continue
        slot = find_buffer_slot_for_sku(item["sku"], conn=conn)
        if slot is None or slot.plastik_count <= 0:
            continue
        n_tasks = min(slot.plastik_count, sisa)
        for _ in range(n_tasks):
            cur = conn.execute(
                "INSERT INTO harvester_task (buffer_slot_id, target_resi_id, sku, status) "
                "VALUES (?, ?, ?, 'pending')",
                (slot.buffer_slot_id, resi_id, item["sku"]),
            )
            task_id = cur.lastrowid
            created.append(task_id)
            log_event(
                "setup_resi",
                actor,
                "harvester_task",
                task_id,
                {
                    "resi_id": resi_id,
                    "sku": item["sku"],
                    "buffer_slot_id": slot.buffer_slot_id,
                },
                conn=conn,
            )
    return created




def _build_buffer_pickup_instructions(
    conn: sqlite3.Connection, resi_id: int
) -> List[dict]:
    """Setelah resi di-setup, susun list instruksi pickup dari buffer
    (untuk display ke operator/harvester). Hanya item yang sisa-nya > 0
    setelah dikurangi prefilled_qty + quantity_fulfilled."""
    instructions: List[dict] = []
    items = conn.execute(
        """
        SELECT id, sku, varian, quantity_ordered, quantity_fulfilled, prefilled_qty
        FROM resi_item WHERE resi_id = ?
          AND (quantity_ordered - COALESCE(prefilled_qty, 0) - COALESCE(quantity_fulfilled, 0)) > 0
        ORDER BY id ASC
        """,
        (resi_id,),
    ).fetchall()
    for it in items:
        sisa = (
            it["quantity_ordered"]
            - (it["prefilled_qty"] or 0)
            - (it["quantity_fulfilled"] or 0)
        )
        slot = find_buffer_slot_for_sku(it["sku"], conn=conn)
        if slot is None or slot.plastik_count <= 0:
            continue
        ambil = min(slot.plastik_count, sisa)
        instructions.append(
            {
                "sku": it["sku"],
                "varian": it["varian"],
                "ambil_pack": ambil,
                "butuh_pack": sisa,
                "buffer_slot_id": slot.buffer_slot_id,
                "wadah_nomor": slot.wadah_nomor,
                "slot_number": slot.slot_number,
                "buffer_label": slot.label(),
            }
        )
    return instructions


def handle_setup_resi_aktif(
    resi_id: int,
    slot_number: int,
    actor: str = "admin",
    conn: Optional[sqlite3.Connection] = None,
) -> SetupResult:
    """Setup resi ke Slot Aktif (slot_number 1..N). 1-step, tidak pakai
    preview/checkbox prefilled. Untuk SKU yang sudah dari stok gudang,
    operator klik tombol "📦 Gudang" per-SKU di slot card setelah setup."""
    if not slot_aktif_exists(slot_number):
        raise SlotAktifConflictError(
            f"slot_number {slot_number} tidak ada di tabel slot_aktif (atau is_active=0)"
        )

    use_outer = conn is not None
    c = conn or get_connection()

    def _do(tc: sqlite3.Connection) -> SetupResult:
        resi_row = tc.execute(
            "SELECT id, nomor_resi, status, slot_aktif_number FROM resi WHERE id = ?",
            (resi_id,),
        ).fetchone()
        if resi_row is None:
            raise ResiNotFoundError(f"Resi id={resi_id} tidak ditemukan")
        if resi_row["status"] in ("active", "complete"):
            raise SlotAktifConflictError(
                f"Resi {resi_row['nomor_resi']} sudah aktif di slot {resi_row['slot_aktif_number']}"
            )
        if resi_row["status"] in ("packed", "cancelled"):
            raise SlotAktifConflictError(
                f"Resi {resi_row['nomor_resi']} sudah {resi_row['status']}, tidak bisa di-setup ulang"
            )
        conflict = tc.execute(
            "SELECT id FROM resi WHERE slot_aktif_number = ? "
            "AND status IN ('active', 'complete') AND id != ?",
            (slot_number, resi_id),
        ).fetchone()
        if conflict is not None:
            raise SlotAktifConflictError(
                f"Slot {slot_number} sudah dipakai resi id={conflict['id']}"
            )
        tc.execute(
            "UPDATE resi SET slot_aktif_number = ?, status = 'active', setup_at = ? "
            "WHERE id = ?",
            (slot_number, now_iso(), resi_id),
        )
        tc.execute(
            "UPDATE wave SET status = 'active', activated_at = COALESCE(activated_at, ?) "
            "WHERE id = (SELECT wave_id FROM resi WHERE id = ?) AND status = 'pending'",
            (now_iso(), resi_id),
        )
        task_ids = _create_harvester_tasks_for_resi(tc, resi_id, actor)
        pickups = _build_buffer_pickup_instructions(tc, resi_id)
        log_event(
            "setup_resi",
            actor,
            "resi",
            resi_id,
            {
                "nomor_resi": resi_row["nomor_resi"],
                "slot_number": slot_number,
                "buffer_pickups": len(pickups),
                "harvester_tasks": len(task_ids),
            },
            conn=tc,
        )
        return SetupResult(
            resi_id=resi_id,
            nomor_resi=resi_row["nomor_resi"],
            slot_number=slot_number,
            harvester_tasks_created=task_ids,
            buffer_pickups=pickups,
        )

    if use_outer:
        return _do(c)
    with transaction(c) as tc:
        return _do(tc)


def setup_resi_by_nomor(
    nomor_resi: str,
    actor: str = "operator",
) -> SetupResult:
    """Operator scan kertas resi (nomor marketplace) → setup ke slot kosong terkecil.

    Resi harus sudah ada di DB (sync-ed dari sheet). Kalau belum ada, raise
    ``ResiNotFoundError`` — minta admin Sync Sheet di /admin dulu.

    Untuk SKU yang sudah dari stok gudang (stabilo): operator klik tombol
    "📦 Gudang" per-SKU di slot card setelah resi di-setup (lihat
    ``mark_resi_item_prefilled`` di maintenance.py).
    """
    nomor_resi = nomor_resi.strip()
    if not nomor_resi:
        raise ResiNotFoundError("Nomor resi kosong")
    conn = get_connection()
    with transaction(conn) as c:
        resi_row = c.execute(
            "SELECT id, status FROM resi WHERE nomor_resi = ?",
            (nomor_resi,),
        ).fetchone()
        if resi_row is None:
            raise ResiNotFoundError(
                f"Resi '{nomor_resi}' belum ada di pool. Klik 'Sync Sheet' di /admin "
                f"atau pastikan tim gudang sudah upload BigSeller ke LIST_PESANAN."
            )
        if resi_row["status"] in ("active", "complete"):
            row = c.execute(
                "SELECT slot_aktif_number FROM resi WHERE id = ?", (resi_row["id"],)
            ).fetchone()
            raise SlotAktifConflictError(
                f"Resi '{nomor_resi}' sudah di-setup di Slot {row['slot_aktif_number']}"
            )
        if resi_row["status"] == "packed":
            raise SlotAktifConflictError(f"Resi '{nomor_resi}' sudah di-pack")
        if resi_row["status"] == "cancelled":
            raise SlotAktifConflictError(f"Resi '{nomor_resi}' sudah di-cancel")
        slot = _next_empty_slot(c)
        if slot is None:
            raise SlotAktifConflictError(
                "Slot Aktif penuh (50/50 terpakai). Tunggu ada yang di-pack dulu."
            )
        return handle_setup_resi_aktif(resi_row["id"], slot, actor=actor, conn=c)


# --- Sync data dari sheet LIST_PESANAN ---

def sync_list_pesanan_to_db(
    actor: str = "admin",
    sheet_rows: Optional[List[dict]] = None,
) -> dict:
    """Fetch SEMUA row dari sheet ``LIST_PESANAN`` → upsert ke DB sebagai resi
    `pending` (kalau belum ada). Resi yang sudah di DB di-skip (tidak overwrite).

    Konversi unit pesanan → packs:
    - SKU diparse jadi (numeric_id, varian).
    - quantity_ordered = jumlah * (varian / 10) [packs].
    - resi_item.sku = numeric_id (untuk match ke barcode plastik numeric).

    Return dict ringkasan: ``{batches: N, resis_inserted: M, items_inserted: K, skipped: S}``.

    ``sheet_rows`` opsional — kalau dipass, skip fetch gspread (testing).
    """
    rows = sheet_rows if sheet_rows is not None else _fetch_list_pesanan_rows()
    grouped: dict = {}
    for r in rows:
        batch = str(r.get("Batch_ID", "")).strip()
        nomor = str(r.get("Nomor_Resi", "")).strip()
        sku_raw = str(r.get("SKU", "")).strip()
        try:
            jumlah = int(r.get("Jumlah") or 0)
        except (TypeError, ValueError):
            jumlah = 0
        if not batch or not nomor or not sku_raw or jumlah <= 0:
            continue
        numeric_id, varian = parse_sku(sku_raw)
        if numeric_id is None:
            continue
        key = (batch, nomor)
        grouped.setdefault(key, [])
        grouped[key].append((numeric_id, varian, jumlah))

    if not grouped:
        return {"batches": 0, "resis_inserted": 0, "items_inserted": 0, "skipped": 0}

    conn = get_connection()
    resis_inserted = 0
    items_inserted = 0
    skipped = 0
    batches_seen = set()
    with transaction(conn) as c:
        for (batch, nomor), items in grouped.items():
            batches_seen.add(batch)
            existing = c.execute(
                "SELECT id FROM resi WHERE nomor_resi = ?", (nomor,)
            ).fetchone()
            if existing is not None:
                skipped += 1
                continue
            wave = c.execute(
                "SELECT id FROM wave WHERE bigseller_batch_id = ?", (batch,)
            ).fetchone()
            if wave is None:
                cur = c.execute(
                    "INSERT INTO wave (bigseller_batch_id, wave_number, status) "
                    "VALUES (?, 1, 'pending')",
                    (batch,),
                )
                wave_id = cur.lastrowid
            else:
                wave_id = wave["id"]
            cur = c.execute(
                "INSERT INTO resi (wave_id, nomor_resi, status) VALUES (?, ?, 'pending')",
                (wave_id, nomor),
            )
            resi_id = cur.lastrowid
            resis_inserted += 1
            agg: dict = {}
            for numeric_id, varian, jumlah in items:
                agg.setdefault((numeric_id, varian), 0)
                agg[(numeric_id, varian)] += packs_needed(jumlah, varian)
            for (numeric_id, varian), packs in agg.items():
                if packs <= 0:
                    continue
                c.execute(
                    "INSERT INTO resi_item (resi_id, sku, varian, quantity_ordered) "
                    "VALUES (?, ?, ?, ?)",
                    (resi_id, numeric_id, varian, packs),
                )
                items_inserted += 1
        log_event(
            "sync_sheet",
            actor,
            "wave",
            None,
            {
                "batches": list(batches_seen),
                "resis_inserted": resis_inserted,
                "items_inserted": items_inserted,
                "skipped_existing": skipped,
            },
            conn=c,
        )
    return {
        "batches": len(batches_seen),
        "resis_inserted": resis_inserted,
        "items_inserted": items_inserted,
        "skipped": skipped,
    }


def _fetch_list_pesanan_rows() -> List[dict]:
    """Fetch sheet LIST_PESANAN via gspread. Auth dari config.json repo root."""
    import gspread  # type: ignore

    cfg_path = config.CONFIG_JSON_PATH
    if not os.path.exists(cfg_path):
        raise WaveTransitionError(f"config.json tidak ditemukan di {cfg_path}")
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    json_path = cfg.get("json_path") or cfg.get("json")
    sheet_url = cfg.get("gsheet_url") or cfg.get("sheet_url")
    if not json_path or not sheet_url:
        raise WaveTransitionError(
            "config.json kurang field 'json_path' atau 'gsheet_url'"
        )
    gc = gspread.service_account(filename=json_path)
    sh = gc.open_by_url(sheet_url) if sheet_url.startswith("http") else gc.open_by_key(sheet_url)
    ws = sh.worksheet(config.LIST_PESANAN_SHEET_NAME)
    return ws.get_all_records()


# --- Legacy: import_from_list_pesanan_sheet (KEPT untuk backward compat tests) ---

def import_from_list_pesanan_sheet(
    batch_id: str,
    actor: str = "admin",
    sheet_rows: Optional[List[dict]] = None,
) -> ImportResult:
    """[DEPRECATED] Bulk import + auto-setup wave pertama.

    Dipertahankan untuk backward compat. Workflow HOG yang baru pakai
    ``sync_list_pesanan_to_db`` + ``setup_resi_by_nomor``.
    """
    rows = sheet_rows if sheet_rows is not None else _fetch_list_pesanan_rows()
    rows = [r for r in rows if str(r.get("Batch_ID", "")).strip() == batch_id]
    if not rows:
        raise WaveTransitionError(
            f"Batch_ID '{batch_id}' tidak punya row di sheet {config.LIST_PESANAN_SHEET_NAME}"
        )

    summary = sync_list_pesanan_to_db(actor=actor, sheet_rows=rows)
    if summary["resis_inserted"] == 0 and summary["skipped"] == 0:
        raise WaveTransitionError("Tidak ada resi valid setelah filter")

    conn = get_connection()
    with transaction(conn) as c:
        wave_row = c.execute(
            "SELECT id FROM wave WHERE bigseller_batch_id = ? ORDER BY id ASC LIMIT 1",
            (batch_id,),
        ).fetchone()
        if wave_row is None:
            raise WaveTransitionError(f"Wave untuk batch {batch_id} tidak ditemukan setelah sync")
        first_wave_id = wave_row["id"]

        c.execute(
            "UPDATE wave SET status = 'active', activated_at = ? WHERE id = ? AND status = 'pending'",
            (now_iso(), first_wave_id),
        )
        resis = c.execute(
            "SELECT id FROM resi WHERE wave_id = ? AND status = 'pending' "
            "ORDER BY id ASC LIMIT ?",
            (first_wave_id, len(get_slot_aktif_numbers())),
        ).fetchall()
        setups: List[SetupResult] = []
        for idx, r in enumerate(resis, start=1):
            setups.append(handle_setup_resi_aktif(r["id"], idx, actor=actor, conn=c))

    return ImportResult(
        batch_id=batch_id,
        waves_created=1,
        resis_imported=summary["resis_inserted"] + summary["skipped"],
        items_imported=summary["items_inserted"],
        first_wave_id=first_wave_id,
        setup_results=setups,
    )


def try_activate_next_wave(actor: str = "system") -> Optional[List[SetupResult]]:
    """[DEPRECATED] Auto-activate next wave saat threshold packed terpenuhi.

    Tidak dipanggil otomatis lagi di workflow baru. Operator setup resi manual
    via scan. Function ini dipertahankan untuk backward compat tests.
    """
    conn = get_connection()
    with transaction(conn) as c:
        active = c.execute(
            "SELECT id, bigseller_batch_id, wave_number FROM wave WHERE status = 'active' "
            "ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if active is None:
            return None
        stats = c.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status = 'packed' THEN 1 ELSE 0 END) AS packed "
            "FROM resi WHERE wave_id = ?",
            (active["id"],),
        ).fetchone()
        total = stats["total"] or 0
        packed = stats["packed"] or 0
        if total == 0 or (packed * 100 // total) < config.WAVE_NEXT_THRESHOLD_PCT:
            return None
        c.execute(
            "UPDATE wave SET status = 'closed', closed_at = ? WHERE id = ?",
            (now_iso(), active["id"]),
        )
        log_event("wave_closed", actor, "wave", active["id"], None, conn=c)
        next_wave = c.execute(
            "SELECT id FROM wave "
            "WHERE bigseller_batch_id = ? AND status = 'pending' "
            "  AND wave_number > ? "
            "ORDER BY wave_number ASC LIMIT 1",
            (active["bigseller_batch_id"], active["wave_number"]),
        ).fetchone()
        if next_wave is None:
            return []
        c.execute(
            "UPDATE wave SET status = 'active', activated_at = ? WHERE id = ?",
            (now_iso(), next_wave["id"]),
        )
        resis = c.execute(
            "SELECT id FROM resi WHERE wave_id = ? AND status = 'pending' "
            "ORDER BY id ASC LIMIT ?",
            (next_wave["id"], len(get_slot_aktif_numbers())),
        ).fetchall()
        setups: List[SetupResult] = []
        for idx, r in enumerate(resis, start=1):
            setups.append(handle_setup_resi_aktif(r["id"], idx, actor=actor, conn=c))
        return setups
