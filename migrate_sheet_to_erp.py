"""
migrate_sheet_to_erp.py — one-shot migration script

Migrasi data aktif dari Google Spreadsheet (sortir_stiker_desain backend lama)
ke ERP heavyobjectgroup (PostgREST):

  DATABASE_STIKER     -> items (filter sub_category stiker) + stiker_design_attributes
                       + inventory.current_stock @ 'Gudang Stiker Siap Jual'
  PERMINTAAN_RESTOCK  -> stiker_restock_requests (status WIP saja)

DATA_SALES & LIST_PESANAN TIDAK dimigrasi — DATA_SALES bisa di-derive dari
stiker_orders history, LIST_PESANAN sudah dipopulate via /list-pesanan upload
saat tim gudang upload BigSeller.

Idempotent: re-runnable. Pakai upsert pattern (ON CONFLICT do update / merge).

Penggunaan:
    python migrate_sheet_to_erp.py --dry-run    # preview saja, no write
    python migrate_sheet_to_erp.py              # run nyata
    python migrate_sheet_to_erp.py --skip-items # skip master, hanya migrasi restock
    python migrate_sheet_to_erp.py --skip-restock

Prereq:
    1. config.json sudah ada: gsheet_url, json_path (untuk source) +
       erp_base_url, erp_jwt_secret, erp_location_id (untuk target).
    2. Migration SQL 20260518_001_stiker_restock_requests.sql sudah di-apply
       ke DB target (lokasi 'Gudang Stiker Siap Jual' harus ada).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime

from google.oauth2.service_account import Credentials
import gspread

from erp_client import ERPClient, ERPClientError, STIKER_SUB_CATEGORY_ID


CONFIG_FILE = "config.json"
DEFAULT_LOCATION_NAME = "Gudang Stiker Siap Jual"
STIKER_SHEET_NAME = "DATABASE_STIKER"
RESTOCK_SHEET_NAME = "PERMINTAAN_RESTOCK"

# Pcs per lembar default untuk items baru
DEFAULT_PCS_PER_LEMBAR = 100

# Kolom DATABASE_STIKER: A=ID Master (sku), B=Nama Desain, ..., H=Stok aktif (idx 7)
DB_STIKER_COL_SKU = 0
DB_STIKER_COL_NAME = 1
DB_STIKER_COL_STOK = 7

# Kolom PERMINTAAN_RESTOCK (v10.3 layout, lihat code.gs):
# A=Tanggal_Request B=SKU C=Jumlah_Request D=Jml_Bundle E=Requester F=Status
# G=Tanggal_Mulai_Print H=Print_Operator I=Jumlah_Aktual_Gudang J=Approve
# K=Tanggal_Approve L=Catatan
PR_COL_TANGGAL = 0
PR_COL_SKU = 1
PR_COL_JUMLAH = 2
PR_COL_REQUESTER = 4
PR_COL_STATUS = 5
PR_COL_TGL_MULAI = 6
PR_COL_OPERATOR = 7
PR_COL_AKTUAL_GUDANG = 8
PR_COL_TGL_APPROVE = 10
PR_COL_CATATAN = 11

WIP_STATUSES = {"pending", "in_progress", "menunggu_approval"}


def is_non_stiker_sku(sku: str) -> bool:
    """Skip stiker sheet ('-VN-') & gantungan kunci ('GK-') — bukan domain sortir_stiker."""
    if not sku:
        return False
    s = str(sku).strip().upper()
    return s.startswith("GK-") or "-VN-" in s


def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        raise SystemExit(f"config.json tidak ditemukan di {os.getcwd()}")
    with open(CONFIG_FILE) as f:
        return json.load(f)


def connect_sheet(cfg: dict):
    url = cfg.get("gsheet_url", "").strip()
    jpath = cfg.get("json_path", "").strip()
    if not url or not os.path.exists(jpath):
        raise SystemExit("gsheet_url atau json_path belum di-set di config.json.")
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(jpath, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_url(url) if "spreadsheets/d/" in url else client.open_by_key(url)


def upsert_inventory(erp: ERPClient, item_id: str, location_id: str, current_stock: int) -> None:
    """Set inventory.current_stock untuk (item_id, location_id). Idempotent.
    Pakai PostgREST upsert via Prefer: resolution=merge-duplicates.
    """
    body = [{
        "item_id": item_id,
        "location_id": location_id,
        "current_stock": int(current_stock),
        "updated_at": datetime.now().isoformat(),
    }]
    erp._request(
        "POST", "inventory", body=body,
        prefer="resolution=merge-duplicates,return=representation",
    )


def fetch_existing_items_by_sku(erp: ERPClient) -> dict[str, str]:
    """Return {sku: item_id} untuk semua items stiker existing."""
    rows = erp._request(
        "GET", "items",
        params={
            "sub_category_id": f"eq.{STIKER_SUB_CATEGORY_ID}",
            "is_deleted": "eq.false",
            "parent_item_id": "is.null",
            "select": "id,sku",
        },
    ) or []
    return {(r.get("sku") or "").strip(): r["id"] for r in rows if r.get("sku") and r.get("id")}


def insert_new_items(erp: ERPClient, new_sku_rows: list[dict]) -> dict[str, str]:
    """Bulk INSERT items baru. Return {sku: id}."""
    if not new_sku_rows:
        return {}
    body = [
        {
            "sku": r["sku"],
            "name": r["name"],
            "sub_category_id": STIKER_SUB_CATEGORY_ID,
            "parent_item_id": None,
            "is_deleted": False,
        }
        for r in new_sku_rows
    ]
    res = erp._request("POST", "items", body=body, prefer="return=representation")
    return {(r.get("sku") or "").strip(): r["id"] for r in (res or []) if r.get("id")}


def upsert_design_attrs(erp: ERPClient, item_ids: list[str], pcs_per_lembar: int = DEFAULT_PCS_PER_LEMBAR) -> None:
    """Ensure stiker_design_attributes row exists for each item_id (default values)."""
    if not item_ids:
        return
    body = [
        {
            "item_id": iid,
            "safety_stock": 0,
            "reorder_point": 0,
            "target_stok": 0,
            "kategori_stok": "STOK",
            "status_produksi": "AKTIF",
            "pcs_per_lembar": pcs_per_lembar,
        }
        for iid in item_ids
    ]
    erp._request(
        "POST", "stiker_design_attributes", body=body,
        prefer="resolution=merge-duplicates,return=representation",
    )


def migrate_database_stiker(erp: ERPClient, spreadsheet, location_id: str, dry_run: bool) -> dict:
    """Migrate DATABASE_STIKER sheet → items + stiker_design_attributes + inventory."""
    print(f"\n[1/2] DATABASE_STIKER → items + inventory @ location_id={location_id[:8]}...")
    try:
        ws = spreadsheet.worksheet(STIKER_SHEET_NAME)
    except Exception as e:
        print(f"  SKIP: sheet {STIKER_SHEET_NAME} tidak ada ({e})")
        return {"items_existing": 0, "items_new": 0, "inventory_set": 0, "skipped": 0}

    rows = ws.get_all_values()
    if len(rows) < 2:
        print(f"  SKIP: sheet {STIKER_SHEET_NAME} kosong.")
        return {"items_existing": 0, "items_new": 0, "inventory_set": 0, "skipped": 0}

    sheet_rows: list[dict] = []
    skipped_count = 0
    for r in rows[1:]:
        if len(r) < 8:
            skipped_count += 1
            continue
        sku = str(r[DB_STIKER_COL_SKU]).strip()
        name = str(r[DB_STIKER_COL_NAME]).strip() or f"Stiker-{sku}"
        try:
            stok = int(float(r[DB_STIKER_COL_STOK])) if r[DB_STIKER_COL_STOK] else 0
        except (ValueError, TypeError):
            stok = 0
        if not sku or is_non_stiker_sku(sku):
            skipped_count += 1
            continue
        sheet_rows.append({"sku": sku, "name": name, "stok": stok})

    print(f"  Sheet rows valid: {len(sheet_rows)}, skipped: {skipped_count}")

    existing = fetch_existing_items_by_sku(erp)
    print(f"  Existing items di ERP: {len(existing)}")

    to_insert = [r for r in sheet_rows if r["sku"] not in existing]
    print(f"  Items baru yang perlu di-INSERT: {len(to_insert)}")

    if dry_run:
        print("  [DRY-RUN] Skip insert items + attrs + inventory.")
        return {
            "items_existing": len(existing),
            "items_new": len(to_insert),
            "inventory_set": 0,
            "skipped": skipped_count,
        }

    # Insert new items in batches of 100 (PostgREST request body limit)
    new_id_map = {}
    BATCH = 100
    for i in range(0, len(to_insert), BATCH):
        batch = to_insert[i:i + BATCH]
        ids = insert_new_items(erp, batch)
        new_id_map.update(ids)
        print(f"  + INSERT batch {i // BATCH + 1}: {len(ids)}/{len(batch)}")

    # Merge map: existing + new
    sku_to_id = {**existing, **new_id_map}

    # Insert default attrs for new items
    new_item_ids = list(new_id_map.values())
    if new_item_ids:
        for i in range(0, len(new_item_ids), BATCH):
            upsert_design_attrs(erp, new_item_ids[i:i + BATCH])
        print(f"  + Default stiker_design_attributes di-insert untuk {len(new_item_ids)} item baru")

    # Set inventory.current_stock per item
    inventory_set = 0
    for r in sheet_rows:
        item_id = sku_to_id.get(r["sku"])
        if not item_id:
            continue
        try:
            upsert_inventory(erp, item_id, location_id, r["stok"])
            inventory_set += 1
            if inventory_set % 500 == 0:
                print(f"    inventory updated: {inventory_set}/{len(sheet_rows)}")
        except ERPClientError as e:
            print(f"    ERROR set inventory sku={r['sku']}: {e}")

    print(f"  ✔ Inventory di-set untuk {inventory_set} item.")
    return {
        "items_existing": len(existing),
        "items_new": len(new_id_map),
        "inventory_set": inventory_set,
        "skipped": skipped_count,
    }


def migrate_permintaan_restock(erp: ERPClient, spreadsheet, dry_run: bool) -> dict:
    """Migrate PERMINTAAN_RESTOCK aktif (status WIP) → stiker_restock_requests.

    Hanya row dgn status pending/in_progress/menunggu_approval yang di-migrate.
    Row approved/rejected/dibatalkan SKIP (sudah final, tidak perlu).
    """
    print(f"\n[2/2] PERMINTAAN_RESTOCK aktif → stiker_restock_requests...")
    try:
        ws = spreadsheet.worksheet(RESTOCK_SHEET_NAME)
    except Exception as e:
        print(f"  SKIP: sheet {RESTOCK_SHEET_NAME} tidak ada ({e})")
        return {"inserted": 0, "skipped_status": 0, "skipped_unknown_sku": 0}

    rows = ws.get_all_values()
    if len(rows) < 2:
        print(f"  SKIP: sheet {RESTOCK_SHEET_NAME} kosong.")
        return {"inserted": 0, "skipped_status": 0, "skipped_unknown_sku": 0}

    existing_items = fetch_existing_items_by_sku(erp)
    print(f"  Items lookup di ERP: {len(existing_items)}")

    to_insert: list[dict] = []
    skipped_status = 0
    skipped_unknown_sku = 0
    for r in rows[1:]:
        if len(r) < 6:
            continue
        status = str(r[PR_COL_STATUS]).strip().lower()
        if status not in WIP_STATUSES:
            skipped_status += 1
            continue
        sku = str(r[PR_COL_SKU]).strip()
        if not sku:
            continue
        item_id = existing_items.get(sku)
        if not item_id:
            skipped_unknown_sku += 1
            print(f"    [WARN] SKU {sku} (status={status}) tidak ada di items ERP — SKIP.")
            continue
        try:
            jumlah_pcs = int(float(r[PR_COL_JUMLAH])) if r[PR_COL_JUMLAH] else 0
        except (ValueError, TypeError):
            jumlah_pcs = 0
        if jumlah_pcs <= 0:
            continue

        requester = str(r[PR_COL_REQUESTER]).strip() if len(r) > PR_COL_REQUESTER else ""
        operator = str(r[PR_COL_OPERATOR]).strip() if len(r) > PR_COL_OPERATOR else ""
        catatan_orig = str(r[PR_COL_CATATAN]).strip() if len(r) > PR_COL_CATATAN else ""
        meta_parts = []
        if requester:
            meta_parts.append(f"Requester: {requester}")
        if operator:
            meta_parts.append(f"Print: {operator}")
        if catatan_orig:
            meta_parts.append(catatan_orig)
        # Tag bahwa ini dari migrasi
        meta_parts.append("Migrated from sheet PERMINTAAN_RESTOCK")
        catatan = " | ".join(meta_parts)

        # Jumlah aktual gudang (optional, kalau sudah diisi)
        try:
            aktual = int(float(r[PR_COL_AKTUAL_GUDANG])) if len(r) > PR_COL_AKTUAL_GUDANG and r[PR_COL_AKTUAL_GUDANG] else None
        except (ValueError, TypeError):
            aktual = None

        # Tanggal mulai print (kalau ada)
        tgl_mulai = str(r[PR_COL_TGL_MULAI]).strip() if len(r) > PR_COL_TGL_MULAI else ""
        tgl_mulai_iso = parse_sheet_datetime(tgl_mulai) if tgl_mulai else None

        to_insert.append({
            "item_id": item_id,
            "sku_raw": sku,
            "jumlah_pcs_request": jumlah_pcs,
            "jumlah_aktual_gudang": aktual,
            "status": status,
            "tanggal_mulai_print": tgl_mulai_iso,
            "catatan": catatan,
        })

    print(f"  Total WIP rows to insert: {len(to_insert)}")
    print(f"  Skipped (status non-WIP): {skipped_status}, skipped (SKU unknown): {skipped_unknown_sku}")

    if dry_run:
        print("  [DRY-RUN] Skip insert.")
        return {
            "inserted": 0,
            "skipped_status": skipped_status,
            "skipped_unknown_sku": skipped_unknown_sku,
        }

    inserted = 0
    BATCH = 100
    for i in range(0, len(to_insert), BATCH):
        batch = to_insert[i:i + BATCH]
        try:
            erp._request("POST", "stiker_restock_requests", body=batch,
                         prefer="return=representation")
            inserted += len(batch)
            print(f"  + INSERT batch {i // BATCH + 1}: {len(batch)}")
        except ERPClientError as e:
            print(f"  ERROR batch {i // BATCH + 1}: {e}")

    print(f"  ✔ Restock requests di-insert: {inserted}")
    return {
        "inserted": inserted,
        "skipped_status": skipped_status,
        "skipped_unknown_sku": skipped_unknown_sku,
    }


def parse_sheet_datetime(s: str) -> str | None:
    """Parse '2026-05-18 09:15:00' atau '2026-05-18' → ISO timestamptz. Return None kalau gagal."""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).isoformat()
        except ValueError:
            continue
    return None


def main():
    parser = argparse.ArgumentParser(description="One-shot migration Sheet → ERP")
    parser.add_argument("--dry-run", action="store_true", help="Preview saja, no write")
    parser.add_argument("--skip-items", action="store_true", help="Skip migrate DATABASE_STIKER")
    parser.add_argument("--skip-restock", action="store_true", help="Skip migrate PERMINTAAN_RESTOCK")
    args = parser.parse_args()

    print("=" * 70)
    print("Migration Sheet → ERP (sortir_stiker_desain)")
    if args.dry_run:
        print("MODE: DRY-RUN (no writes)")
    print("=" * 70)

    cfg = load_config()
    spreadsheet = connect_sheet(cfg)
    print(f"Sheet source connected: {spreadsheet.title}")

    try:
        erp = ERPClient.from_config(cfg)
    except ERPClientError as e:
        sys.exit(f"ERPClient init gagal: {e}")
    erp.ping()
    print(f"ERP target reachable: {erp.base_url}")

    # Resolve default location
    location_id = cfg.get("erp_location_id", "").strip()
    if not location_id:
        location_id = erp.resolve_location_id(DEFAULT_LOCATION_NAME)
        if not location_id:
            sys.exit(
                f"Lokasi '{DEFAULT_LOCATION_NAME}' tidak ditemukan di ERP. "
                "Apply migration SQL 20260518_001_stiker_restock_requests.sql dulu."
            )
        print(f"Default location resolved: '{DEFAULT_LOCATION_NAME}' = {location_id}")

    results = {}
    if not args.skip_items:
        results["items"] = migrate_database_stiker(erp, spreadsheet, location_id, args.dry_run)
    else:
        print("\n[1/2] SKIP DATABASE_STIKER per --skip-items")

    if not args.skip_restock:
        results["restock"] = migrate_permintaan_restock(erp, spreadsheet, args.dry_run)
    else:
        print("\n[2/2] SKIP PERMINTAAN_RESTOCK per --skip-restock")

    print("\n" + "=" * 70)
    print("SUMMARY:")
    print(json.dumps(results, indent=2))
    print("=" * 70)


if __name__ == "__main__":
    main()
