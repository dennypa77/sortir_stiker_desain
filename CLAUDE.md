# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Internal tooling for **PT Heavy Object Group / Stickitup** — custom die-cut sticker fulfillment. Three teams (print, gudang, packing/QC) share one Google Spreadsheet as the source of truth. The repo contains two largely independent applications that target **Windows 10/11** and **Python 3.10+** (tested on 3.13):

1. **Root scripts** — CustomTkinter desktop apps for the original workflow (`app.py`, `run_qc.py`, `qc_stasiun.py`) plus a Google Apps Script (`code.gs`) pasted into the shared spreadsheet.
2. **`packing_router/` package** — a separate Flask + HTMX web app for the new sort-to-resi + SKU-sticky buffer workflow. It runs **alongside** the root scripts; per its own design, it must **not** modify the root files.

UI text, comments, log messages, and exception messages are in **Indonesian** (Bahasa). Match that when editing.

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

### Shared integration: one Google Spreadsheet
Both apps authenticate with a service-account JSON whose path lives in `config.json` (root, gitignored). All cross-team coordination happens through sheets:

| Sheet | Writer | Readers |
|---|---|---|
| `DATABASE_STIKER` | `app.py`, Apps Script (opname sync) | `app.py` (stock check) |
| `LOG_KELUAR` | `app.py` (Tab 3 with the LOG_KELUAR checkbox enabled) | audit |
| `DATA_SALES` | Apps Script upload, `packing_router` append-on-pack | trend analytics |
| `LIST_PESANAN` | Apps Script auto-populate from BigSeller upload | `run_qc.py`, `packing_router` (read-only) |
| `STOK_OPNAME` | manual + Apps Script `Sinkronisasi Opname` | — |

Apps Script (`code.gs`) installs a `Kelola Gudang` menu in the spreadsheet; that menu is the single way new orders enter the system. `packing_router` only **reads** `LIST_PESANAN` and **appends** to `DATA_SALES` — it never overwrites either.

### Root desktop apps
- **`app.py`** — CustomTkinter `BotApp` with 4 tabs: Koneksi Gudang (gspread auth + `config.json`), Pengaturan File (paths), Eksekusi & Log (the print pipeline that sorts BigSeller orders, deducts from `DATABASE_STIKER`, and writes batched PDFs to a hot folder split per varian: Batch 10 vs Batch 50, max 20 files per sub-batch), and Scanner Resi Gudang (audio-feedback resi lookup against in-memory cache). Juga menjalankan **local HTTP bridge** untuk web ERP — lihat section "ERP web bridge" di bawah.
- **`qc_stasiun.py`** — DB layer (SQLite at `hasil/qc_data.db`, WAL, auto-migration when the old `operator_id NOT NULL` schema is detected; backup written next to the file) plus `QcStasiunWindow` (Toplevel) and dialogs. Operator login is currently dormant — `qc_seed.py` CLI still exists for re-enabling later.
- **`run_qc.py`** — Standalone launcher that loads `config.json`, opens the spreadsheet, and shows `QcStasiunWindow`.
- **`duplicate_files.py`** — Standalone dedup script (stable backup is `duplicate_files - stable.py`, kept on purpose).
- **`updater.py`** — On every `start*.bat` run, fetches files listed in `update_manifest.txt` (or `FILES_TO_UPDATE` fallback) from `https://raw.githubusercontent.com/dennypa77/sortir_stiker_desain/main/`, overwrites the local copy if it differs. **`packing_router/` is NOT auto-updated**; updating it requires editing `updater.py`.

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
- The QC app talks to gspread directly from the UI thread; expect ~60 reads/min and ~100 writes/min as the per-user Sheets API ceiling.

## Conventions worth respecting

- **Don't modify root scripts when working in `packing_router/`.** That separation is load-bearing — `packing_router/README.md` documents it and a regression `git diff` should be empty. The one exception is `updater.py`, which must be edited explicitly if you want to ship `packing_router` via auto-update.
- **Port, don't import, between root and `packing_router/`.** `parse_sku` was duplicated on purpose; if logic drifts in `app.py:BotApp.extract_numeric_id_and_pcs`, mirror it in `packing_router/utils.py` and update its tests.
- **Use the env-var override pattern** (`PACKING_ROUTER_<KEY>`) for any new config in `packing_router/config.py`; tests rely on `monkeypatch.setattr(pr_config, ...)` rather than env vars.
- **Writes to gspread are append/upsert only** for shared sheets — never overwrite or delete `LIST_PESANAN` / `DATA_SALES` rows from Python; bulk maintenance is done by `code.gs` menu items.
- **Windows-specific calls** (`winsound.Beep`, `os.startfile`, `.bat` launchers, `pygame` audio) live only in the root apps. Keep `packing_router` cross-platform — it has to run on dev macOS/Linux for tests.
- **Indonesian copy.** UI labels, exception messages, log lines, and most docstrings are in Bahasa Indonesia. New strings should match.
- **Data files are gitignored** (`hasil/`, `*.db`, `*.json`, `data.xlsx`, `Kurang-*.xlsx`, `DATA-V10.xlsx`, `KEKURANGAN-V10.xlsx`). Don't commit a real `config.json` — it holds the service-account path.
