# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Internal tooling for **PT Heavy Object Group / Stickitup** — custom die-cut sticker fulfillment. Three teams (print, gudang, packing/QC) share data. **Sejak 2026-05-18 source of truth pindah dari Google Spreadsheet ke ERP heavyobjectgroup** (`db.heavyobjectgroup.com` PostgREST). Google Spreadsheet `code.gs` Apps Script onEdit dipensiunkan; equivalent live di DB trigger. Repo berisi:

1. **Root scripts** — CustomTkinter desktop apps for the original workflow (`app.py`, `run_qc.py`, `qc_stasiun.py`). Data layer (Tab 1/3/4/6 + QC station) via `erp_client.py` ke PostgREST. Sheet `DATA_SALES` masih dipakai oleh `packing_router/sheets_log.py` saat ini (akan dimigrasi terpisah).
2. **`packing_router/` package** — a separate Flask + HTMX web app for the new sort-to-resi + SKU-sticky buffer workflow. It runs **alongside** the root scripts; per its own design, it must **not** modify the root files.

UI text, comments, log messages, and exception messages are in **Indonesian** (Bahasa). Match that when editing.

Target OS: **Windows 10/11**, **Python 3.10+** (dites 3.13).

## Common commands

### Root desktop apps
```bash
pip install -r requirements.txt        # or run install.bat
python app.py                          # sortir & cetak GUI (4 tabs) — start.bat also runs updater
python run_qc.py                       # Stasiun QC GUI — start_qc.bat also runs updater
python test_qc_parser.py               # 20 unit tests for SKU parser (no pytest needed)
python updater.py                      # pull latest of FILES_TO_UPDATE from GitHub raw
```

### packing_router (Flask web app)
```bash
pip install -r packing_router/requirements.txt
python -m packing_router.web.app                          # http://127.0.0.1:5000 (auto-redirects to /operator/scan)
python -m pytest packing_router/tests                     # all suites
python -m pytest packing_router/tests/test_buffer.py -v   # single suite
python -m packing_router.cron.aging_check                 # exit 1 if any buffer slot > BUFFER_AGING_HOURS
```
`start_packing.bat` runs updater → pip install → launches the Flask app. All `packing_router` knobs override via env vars prefixed `PACKING_ROUTER_*` (see `packing_router/config.py`; e.g. `PACKING_ROUTER_WEB_HOST=0.0.0.0` for LAN multi-station).

### Regression check after touching packing_router
```bash
git diff --stat HEAD -- app.py qc_stasiun.py run_qc.py qc_seed.py duplicate_files.py updater.py code.gs test_qc_parser.py requirements.txt config.json README.md start.bat start_qc.bat install.bat
# Output must be empty — packing_router work must not touch these files.
```

## Architecture

### Primary integration: ERP heavyobjectgroup PostgREST (sejak 2026-05-18)

Data layer Tab 1/3/4/6 di `app.py` + `qc_stasiun.py SheetAdapter` + `run_qc.py` pakai **`erp_client.py`** untuk talk ke `db.heavyobjectgroup.com` via PostgREST. Bridge JWT HS256 di-sign lokal dengan `VPS_DB_JWT_SECRET` (compatible dengan `erp-frontend/src/lib/server/vpsDbJwt.ts` di repo heavyobjectgroup).

**Mapping sheet → ERP tabel** (cutover total):

| Sheet (legacy) | ERP equivalent | Status |
|---|---|---|
| `DATABASE_STIKER` | `items` (filter `sub_category_id` stiker) + `inventory` + `stiker_design_attributes` | **CUTOVER** — desktop baca via `erp.get_stock_dict()` / `erp.fetch_database_stiker()` |
| `LOG_KELUAR` | `goods_issued` (trigger DB auto-decrement inventory) | **CUTOVER** — desktop tulis via `erp.issue_goods_batch()` |
| `PERMINTAAN_RESTOCK` | `stiker_restock_requests` (lihat heavyobjectgroup migration 20260518_001) | **CUTOVER** — desktop Tab 6 via `erp.fetch_restock_requests` / `submit/start/finish/delete_restock_request` |
| `LIST_PESANAN` | `stiker_orders` + `stiker_order_batches` + `stiker_order_allocations` | **CUTOVER** — QC station via `erp.fetch_list_pesanan()` / `update_resi_qc_status()`. Upload BigSeller pindah ke web `/operation/produksi/stiker-desain/list-pesanan` |
| `STOK_OPNAME` | `stock_opname` table | **CUTOVER** — pakai web `/stiker-desain/opname` |
| `DATA_SALES` | (belum dimigrasi — `packing_router/sheets_log.py` masih append ke sheet) | LEGACY |

**Apps Script `code.gs`** (`onEdit` trigger) **dipensiunkan** pasca cutover — rename function ke `_onEdit_disabled_<date>` di Apps Script editor. Menu `Kelola Gudang` masih boleh dipakai untuk upload data sales lama (kalau perlu rollback), tapi behavior LOG_MASUK auto-write sudah berpindah ke DB trigger `trg_stiker_restock_on_approve` di sisi ERP.

### erp_client.py (PostgREST client)

Stdlib-only (no requests/httpx). Konsisten dengan `updater.py` & `app.py` HTTP bridge yang juga stdlib.

- `ERPClient.from_config(config_data)` — factory dari `config.json` dict
- JWT auto-refresh 60s sebelum exp (TTL 1 jam, role default `service_role`)
- Threading lock untuk mint JWT thread-safe
- In-memory cache 5 menit untuk `fetch_database_stiker` (8K SKU ~3 detik per fetch)
- Methods: `get_stock_dict` / `get_item_id_by_sku` / `issue_goods` / `issue_goods_batch` / `fetch_list_pesanan` / `update_qc_status` / `find_resi` / `update_resi_qc_status` / `fetch_restock_requests` / `submit_restock_request` / `start_restock_production` / `finish_restock_production` / `delete_restock_request` / `resolve_location_id` / `ping`

### config.json schema baru (post-cutover)

```json
{
  "erp_base_url": "https://db.heavyobjectgroup.com",
  "erp_jwt_secret": "<VPS_DB_JWT_SECRET, sama dengan PGRST_JWT_SECRET di VPS>",
  "erp_location_id": "<UUID Gudang Stiker Siap Jual>",
  "erp_jwt_role": "service_role",
  "excel_path": "...", "master_path": "...", "hot_path": "...",
  "kekurangan_path": "...", "print_operator_name": "...",
  "gsheet_url": "<optional, legacy untuk packing_router>",
  "json_path": "<optional, legacy service-account JSON>"
}
```

`config.json` tetap gitignored. Setelah cutover, `gsheet_url` & `json_path` opsional (cuma dipakai `packing_router/sheets_log.py` untuk append `DATA_SALES`).

### migrate_sheet_to_erp.py

One-shot bulk migration script. Idempotent (re-runnable). Penggunaan:

```bash
python migrate_sheet_to_erp.py --dry-run    # preview, no write
python migrate_sheet_to_erp.py              # real migration
python migrate_sheet_to_erp.py --skip-items # hanya restock requests
```

Migrasi `DATABASE_STIKER` → `items` + `stiker_design_attributes` + `inventory.current_stock` @ default location ('Gudang Stiker Siap Jual'), dan `PERMINTAAN_RESTOCK` aktif (status WIP) → `stiker_restock_requests` dengan tag `Migrated from sheet PERMINTAAN_RESTOCK` di catatan. Skip SKU non-stiker (`-VN-` sheet pattern, `GK-` gantungan kunci prefix).

### Root desktop apps
- **`app.py`** — CustomTkinter `BotApp` dengan 6 tabs:
  - Tab 1 Koneksi Gudang — input `erp_base_url`, `erp_jwt_secret`, `erp_location_id`. Test koneksi via `ERPClient.ping()` + preview `fetch_database_stiker` count. (Tab ini dulu pakai gspread URL+JSON, sudah cutover.)
  - Tab 2 Pengaturan File — path Excel pesanan, Master PDF folder, Hot Folder output.
  - Tab 3 Eksekusi & Log — print pipeline (sort BigSeller orders, baca stok via `erp.get_stock_dict()`, push pengeluaran via `erp.issue_goods_batch()` — trigger DB auto-decrement inventory). Batched PDF ke Hot Folder per varian (Batch 10 vs Batch 50, max 20 files/sub-batch).
  - Tab 4 Scanner Resi Gudang — audio-feedback resi lookup, stok dari ERP.
  - Tab 5 Cetak Kekurangan — reprint stiker dari Excel KEKURANGAN (tidak nyentuh stok).
  - Tab 6 Permintaan Restock — list `stiker_restock_requests` dari ERP, start_production/finish_production/delete actions. Approve dilakukan di web ERP `/permintaan-restock` (perlu input `jumlah_aktual_gudang` + lokasi).
  - Juga menjalankan **local HTTP bridge** untuk web ERP — lihat section "ERP web bridge" di bawah (arah berbeda: web ERP push xlsx ke desktop).
- **`qc_stasiun.py`** — DB layer SQLite (`hasil/qc_data.db`, WAL, auto-migration old `operator_id NOT NULL` schema; backup di sebelah file). `SheetAdapter` rewrite pakai `ERPClient` — backend `stiker_orders` (bukan sheet `LIST_PESANAN` lagi). Constructor `SheetAdapter(config_data)` (sebelumnya `(spreadsheet)`). `QcStasiunWindow.__init__(parent, config_data)` (sebelumnya `(parent, spreadsheet)`). Operator name di-tag ke `qc_notes` field (desktop tidak punya user UUID).
- **`run_qc.py`** — Standalone launcher. `verify_erp_config()` replaces `connect_spreadsheet()` — ping ERP saat startup.
- **`erp_client.py`** — PostgREST HTTP client, lihat section "Primary integration" di atas.
- **`migrate_sheet_to_erp.py`** — one-shot script Sheet → ERP, lihat section "migrate_sheet_to_erp.py" di atas.
- **`duplicate_files.py`** — Standalone dedup script (stable backup is `duplicate_files - stable.py`, kept on purpose).
- **`updater.py`** — On every `start*.bat` run, fetches files listed in `update_manifest.txt` (or `FILES_TO_UPDATE` fallback) from `https://raw.githubusercontent.com/dennypa77/sortir_stiker_desain/main/`, overwrites the local copy if it differs. **`packing_router/` is NOT auto-updated**; updating it requires editing `updater.py`. Manifest sudah include `erp_client.py` + `migrate_sheet_to_erp.py`.

### ERP web bridge (app.py → port 8765)

`app.py` menjalankan local HTTP server di thread daemon (stdlib `ThreadingHTTPServer`, **no Flask**) yang menerima POST dari web ERP `staging.heavyobjectgroup.com/operation/produksi/stiker-desain/operator-print`. Operator klik tombol "Input ke Aplikasi" di web → data Batch Aktif terkirim ke aplikasi tanpa perlu pilih xlsx manual.

- **`GET /health`** — discovery; web call dengan timeout 2.5s sebelum POST.
- **`POST /import`** body `{ batch_code, batch_id?, source?, items: [{ sku, jumlah_lembar }] }` — handler tulis xlsx (`hasil/from_erp/<batch>_<timestamp>.xlsx`, kolom A=SKU B=Jumlah Lembar sesuai format pipeline existing), update `config_data["excel_path"]` + `entry_excel`, switch ke Tab 3, focus window (`deiconify + lift + topmost toggle`), dan log baris hijau. Operator tinggal klik MULAI PROSES.
- Handler HTTP runs di thread terpisah; semua UI mutation di-dispatch ke main thread via `self.after(0, ...)` + `threading.Event` (Tkinter tidak thread-safe).
- **CORS allowlist** di `config_data["erp_bridge_origins"]` (default: domain `heavyobjectgroup.com` + localhost dev). Port override via `config_data["erp_bridge_port"]` (default `8765`). Tanpa token auth — bergantung pada CORS allowlist + browser same-origin.
- Browser HTTPS bisa POST ke `http://127.0.0.1:*` tanpa mixed-content (localhost secure context, Chrome 94+).

### packing_router (Flask web app)
Owns its own DB: **`hasil/packing_router.db`** (SQLite, WAL, busy-retry with exponential backoff). Never touches `qc_data.db`. Auto-creates 8 tables + a default `DEFAULT_WADAH_COUNT=5` wadah on first launch.

Domain concepts:
- **Slot Aktif** — physical rack of N numbered slots (default 10), 1:1 with the active resi being assembled. Tracked in DB; slot count is admin-configurable at runtime.
- **Buffer** — `N wadah × SLOTS_PER_WADAH (default 10)` SKU-sticky shelves. One SKU stays in one slot; identical SKUs stack. When a slot exceeds `OVERFLOW_TRIGGER_COUNT` and `ALLOW_BUFFER_OVERFLOW=True`, a secondary slot is opened with `is_overflow_of` pointing at the primary; `find_buffer_slot_for_sku` always returns the primary.
- **Wave transition** — when ≥ `WAVE_NEXT_THRESHOLD_PCT` (default 90%) of the active wave is packed, the next wave auto-activates and re-uses freed slots.

Module map (each file roughly maps to one domain concern):

| Module | Responsibility |
|---|---|
| `config.py` | All tunables; every constant readable via env var `PACKING_ROUTER_<KEY>` |
| `db.py` | Connection singleton, schema bootstrap, transaction helpers |
| `models.py` / `exceptions.py` | Dataclasses + typed errors (`BufferFullError`, `HarvesterMismatchError`, …) |
| `utils.py` | `parse_sku`, `parse_barcode`, `derive_sku_full` — **port** of `app.py:BotApp.extract_numeric_id_and_pcs`; intentionally NOT imported because the original is bound to a Tkinter instance |
| `scan_handler.py` | Operator scan-plastik routing (the three colored actions) |
| `resi_setup.py` | Mode-1 setup, sheet import, wave transition |
| `buffer.py` / `slot_aktif.py` | Slot/wadah CRUD, find/assign, overflow |
| `harvester.py` | Two-phase pickup + dropoff validation |
| `maintenance.py` | Undo (window = `UNDO_WINDOW_SECONDS`, default 30s), cancel resi, pack resi |
| `reports.py` / `sheets_log.py` | Slot grid status, harvester queue, aging report; `DATA_SALES` append |
| `cron/aging_check.py` | Standalone CLI for Windows Task Scheduler |
| `web/app.py` | Flask `create_app()` factory; HTMX partial endpoints |
| `tests/conftest.py` | `tmp_db`, `buffer_seeded`, `small_buffer`, `tiny_slot_aktif` fixtures — every test gets a fresh DB via `monkeypatch` of `pr_config.DB_PATH` |

Routes are grouped per role: `/operator/scan` (operator), `/harvester/queue` (harvester), `/dashboard` and `/slot-aktif` (packer monitor), `/admin` (supervisor — sync sheet, add wadah, view aging). HTMX-driven partials live under `templates/partials/` and are returned by `*/refresh` and action endpoints.

### Concurrency model
- packing_router uses `BEGIN IMMEDIATE TRANSACTION` + retry on `SQLITE_BUSY` (`SQLITE_BUSY_RETRY_COUNT`, `SQLITE_BUSY_RETRY_BASE_MS`). Don't add long-held write transactions.
- The QC app talks to PostgREST via `ERPClient` di UI thread (panggilan blocking). Per-PC operasi: ~10-30 read/menit, ~5-20 write/menit. PostgREST rate-limit 60 req/s per IP (nginx). Tidak ada per-user quota lagi (sebelumnya gspread quota: 60 read/min + 100 write/min per service account).
- `ERPClient` cache in-memory 5 menit untuk `fetch_database_stiker` — kurangi roundtrip saat Tab 3 + Tab 4 + load_wip_map dipanggil bersamaan.

## Conventions worth respecting

- **Don't modify root scripts when working in `packing_router/`.** That separation is load-bearing — `packing_router/README.md` documents it and a regression `git diff` should be empty. The one exception is `updater.py`, which must be edited explicitly if you want to ship `packing_router` via auto-update.
- **Port, don't import, between root and `packing_router/`.** `parse_sku` was duplicated on purpose; if logic drifts in `app.py:BotApp.extract_numeric_id_and_pcs`, mirror it in `packing_router/utils.py` and update its tests.
- **Use the env-var override pattern** (`PACKING_ROUTER_<KEY>`) for any new config in `packing_router/config.py`; tests rely on `monkeypatch.setattr(pr_config, ...)` rather than env vars.
- **Writes ke ERP via `ERPClient` saja** untuk data layer baru. Jangan re-introduce gspread call di Tab 1/3/4/6 atau QC station — pattern udah cutover ke PostgREST. `packing_router/sheets_log.py` masih append `DATA_SALES` (legacy, pending migration berikutnya).
- **`gspread` library tetap di `requirements.txt`** karena `migrate_sheet_to_erp.py` baca sheet sebagai source, dan `packing_router/sheets_log.py` masih append `DATA_SALES`. Jangan remove sampai semua migrasi selesai.
- **Windows-specific calls** (`winsound.Beep`, `os.startfile`, `.bat` launchers, `pygame` audio) live only in the root apps. Keep `packing_router` cross-platform — it has to run on dev macOS/Linux for tests.
- **Indonesian copy.** UI labels, exception messages, log lines, and most docstrings are in Bahasa Indonesia. New strings should match.
- **Data files are gitignored** (`hasil/`, `*.db`, `*.json`, `data.xlsx`, `Kurang-*.xlsx`, `DATA-V10.xlsx`, `KEKURANGAN-V10.xlsx`). Don't commit a real `config.json` — it holds the service-account path.
