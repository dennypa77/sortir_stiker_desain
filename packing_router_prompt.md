# Prompt — Packing Router Stiker

## Tujuan

Tambahkan **package Python baru `packing_router/`** ke dalam repo existing
`sortir_stiker_desain` (https://github.com/dennypa77/sortir_stiker_desain,
local path `D:\Project\Bot Stiker V2`). Modul ini mengimplementasikan sistem
**Sort-to-Resi + Buffer SKU-sticky** untuk stasiun packing produk Stiker
Desain di HOG (Heavy Object Group).

---

## ⚠️ ATURAN UTAMA — BACA DULU

1. **JANGAN ubah, refactor, hapus, atau rename file & fungsi yang SUDAH ADA**
   di repo. File-file existing yang **read-only** (TIDAK boleh disentuh):
   `app.py`, `qc_stasiun.py`, `run_qc.py`, `qc_seed.py`, `duplicate_files.py`,
   `duplicate_files - stable.py`, `updater.py`, `code.gs`, `test_qc_parser.py`,
   `requirements.txt`, `config.json`, `README.md`, `start.bat`, `start_qc.bat`,
   `install.bat`. Semua kode lama harus tetap berjalan persis seperti sekarang.
2. **Tambah file BARU saja** — semua kode baru masuk ke folder
   `packing_router/`. Tidak ada file di root yang dimodifikasi.
3. **Boleh BACA & IMPORT dari kode lama**, tapi karena helper SKU di repo ini
   adalah **method instance** (`BotApp.extract_numeric_id_and_pcs` di
   `app.py:533`) yang tightly-coupled ke Tkinter, JANGAN import dari `app.py`.
   Sebagai gantinya, **copy regex logic-nya** ke `packing_router/utils.py`
   sebagai pure function. (Ini bukan duplikasi yang dilarang — ini untuk
   menghindari coupling ke GUI class).
4. Repo `sortir-ganci` (https://github.com/dennypa77/sortir-ganci) **TIDAK ada
   hubungannya** dengan project ini — itu untuk QC gantungan kunci, domain
   berbeda. Jangan import, extend, atau menyentuh repo ganci sama sekali.
5. **Database/storage**: existing repo punya SQLite di `hasil/qc_data.db` untuk
   QC station. Modul `packing_router` **WAJIB pakai file DB terpisah
   `hasil/packing_router.db`** — TIDAK numpang di `qc_data.db`. Schema baru,
   connection terpisah, tidak ada cross-table FK ke entitas QC.

---

## Konteks bisnis

- HOG memproses ~1.500 resi/hari Stiker Desain dari marketplace (Shopee,
  Tokopedia, Lazada, TikTok Seller).
- 1 batch BigSeller = 300 resi. Sehari ~5 batch.
- 1 plastik output weeding = 1 SKU dalam varian 10/20/50/100 pcs.
- Rata-rata 2-3 SKU per resi.
- ~50% SKU di-fulfill dari stok gudang (sudah ditandai stabilo di kertas
  resi). 50% lagi dari produksi baru (print → cutting → weeding).
- Bottleneck: cutting & weeding lambat, packing menunggu — picker buang
  waktu mencari plastik di matras lesehan.

## Konsep solusi

Inversi logika: alih-alih packer mencari plastik, sistem mengarahkan setiap
plastik ke tujuannya.

- **Slot Aktif**: rak fisik dengan 50 slot, mapping 1:1 ke 50 resi dalam
  wave aktif. 1 batch BigSeller (300 resi) = 6 wave × 50 resi.
- **Buffer**: rak fisik dengan **N wadah configurable** (default 5, bisa
  ditambah anytime tanpa restart sistem), setiap wadah punya 10 slot
  bersekat. **Aturan SKU-sticky**: 1 SKU = 1 slot di buffer, plastik dengan
  SKU sama menumpuk di slot yang sama (FIFO saat diambil).
- **Harvester role**: bertugas memindahkan plastik dari Buffer ke Slot
  Aktif saat ada match (resi cocoknya muncul setelah plastik sudah
  di-buffer).

---

## Dua event utama

### Event 1: Scan plastik (di stasiun sortir)

Saat operator scan barcode plastik:

1. Sistem cari apakah SKU plastik dibutuhkan oleh resi mana pun di Slot
   Aktif (yang `quantity_fulfilled < quantity_ordered`).
2. **Jika ada match di Slot Aktif** → display instruksi:
   `"LETAKKAN KE SLOT N (RESI X)"` → operator letakkan → sistem update
   state plastik (location = slot_aktif, ref = resi_id) dan increment
   fulfilled count.
3. **Jika tidak ada match di Slot Aktif** → cek Buffer:
   - **SKU sudah punya slot di Buffer** → display:
     `"LETAKKAN KE WADAH X SLOT Y (sudah berisi N plastik)"` → tumpuk di
     slot existing, increment plastik_count.
   - **SKU belum punya slot di Buffer** → assign slot kosong (least-full
     wadah aktif) → display: `"LETAKKAN KE WADAH X SLOT Y (slot baru)"` →
     operator letakkan, sistem catat sku → slot mapping.
4. Log event `scan` ke `event_log`.

### Event 2: Setup resi baru di Slot Aktif

Saat resi baru di-setup ke Slot Aktif (otomatis saat wave aktif berganti,
atau manual saat admin import dari sheet `LIST_PESANAN`):

1. Untuk setiap SKU yang dibutuhkan resi tersebut → sistem cek Buffer: ada
   slot dengan SKU itu?
2. **Jika ada** → buat task `harvester_task`: `"Ambil dari Wadah X Slot Y →
   bawa ke Slot N (Resi ABCDE)"` → masuk ke harvester queue, muncul di
   dashboard harvester.
3. Harvester eksekusi task:
   - Scan plastik saat ambil dari buffer (validasi SKU benar, decrement
     `plastik_count` slot buffer, set plastik `location_type='in_transit'`).
   - Bawa ke Slot Aktif.
   - Scan plastik lagi saat letakkan di Slot Aktif (validasi slot benar,
     update state plastik).
4. Log event `harvest_pickup` dan `harvest_dropoff`.

### Status indicator slot aktif & transisi resi

- **Merah**: ada SKU yang `quantity_fulfilled < quantity_ordered` (resi
  belum lengkap). Resi `status='active'`.
- **Hijau**: semua SKU sudah `quantity_fulfilled == quantity_ordered`,
  resi siap di-pack. Resi `status='complete'`, `completed_at` ter-set saat
  transisi ini terjadi (di dalam `harvester_dropoff_scan` atau
  `handle_scan_plastik` ketika fulfilled count terakhir tercapai).
- **Kuning**: hijau dan sudah > `SLOT_KUNING_TIMEOUT_MIN` menit menunggu
  pack (escalate). Status resi tetap `complete` (warna kuning hanya UI
  indicator, derived dari `now() - completed_at`).
- Saat di-pack: `status='packed'`, `packed_at` ter-set, `slot_aktif_number`
  di-release.

Packer hanya pack slot hijau, FIFO berdasarkan timestamp `completed_at`.

### Wave transition trigger

`try_activate_next_wave()` dipanggil setelah setiap pack. Logic:
- Hitung `% packed` dari resi di wave aktif.
- Jika ≥ `WAVE_NEXT_THRESHOLD_PCT` (default 90%) → mark wave sekarang
  `closed`, mark wave berikutnya `active`, auto-setup 50 resi pertama wave
  baru ke Slot Aktif (panggil `handle_setup_resi_aktif` per resi).
- Slot fisik 1-50 di-reuse: slot yang resinya `packed` di wave lama →
  di-release, lalu di-assign ke resi wave baru.

---

## Database schema (SQLite)

DB file: `hasil/packing_router.db` (terpisah dari `qc_data.db`).
Auto-init saat first run. Pakai SQLite WAL mode untuk concurrency.

```sql
CREATE TABLE IF NOT EXISTS wave (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bigseller_batch_id TEXT,
    wave_number INTEGER,
    status TEXT,  -- pending, active, closed
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    activated_at TEXT,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS resi (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wave_id INTEGER REFERENCES wave(id),
    nomor_resi TEXT UNIQUE NOT NULL,
    slot_aktif_number INTEGER,  -- 1-50, NULL kalau belum di Slot Aktif
    status TEXT,  -- pending, active, complete, packed, cancelled
    setup_at TEXT,
    completed_at TEXT,
    packed_at TEXT
);

CREATE TABLE IF NOT EXISTS resi_item (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resi_id INTEGER REFERENCES resi(id) ON DELETE CASCADE,
    sku TEXT NOT NULL,
    varian INTEGER,  -- 10, 20, 50, 100
    quantity_ordered INTEGER NOT NULL,
    quantity_fulfilled INTEGER DEFAULT 0,
    UNIQUE (resi_id, sku, varian)
);

CREATE TABLE IF NOT EXISTS wadah (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nomor INTEGER UNIQUE NOT NULL,  -- 1, 2, 3, ...
    capacity INTEGER DEFAULT 10,
    is_active INTEGER DEFAULT 1,  -- 0/1 (SQLite boolean)
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS buffer_slot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wadah_id INTEGER REFERENCES wadah(id),
    slot_number INTEGER,  -- 1 sampai capacity
    sku TEXT,  -- NULL kalau slot kosong
    plastik_count INTEGER DEFAULT 0,
    first_plastik_at TEXT,
    last_plastik_at TEXT,
    is_overflow_of INTEGER REFERENCES buffer_slot(id),
    UNIQUE (wadah_id, slot_number)
);

CREATE TABLE IF NOT EXISTS plastik (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    barcode TEXT UNIQUE NOT NULL,
    sku TEXT NOT NULL,
    varian INTEGER,
    location_type TEXT,  -- buffer, in_transit, slot_aktif, packed, returned
    location_ref INTEGER,
    scanned_at TEXT,
    placed_at TEXT
);

CREATE TABLE IF NOT EXISTS harvester_task (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    buffer_slot_id INTEGER REFERENCES buffer_slot(id),
    target_resi_id INTEGER REFERENCES resi(id),
    sku TEXT,
    status TEXT,  -- pending, in_progress, done, cancelled
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT,  -- scan, setup_resi, harvest_pickup, harvest_dropoff, pack, undo, alert, add_wadah, wave_activated, wave_closed
    actor TEXT,  -- operator, harvester, packer, admin, system
    entity_type TEXT,
    entity_id INTEGER,
    payload TEXT,  -- JSON-encoded (SQLite tidak punya JSONB native)
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Index yang akan sering dipakai:
CREATE INDEX IF NOT EXISTS idx_resi_status ON resi(status);
CREATE INDEX IF NOT EXISTS idx_resi_item_sku ON resi_item(sku, varian);
CREATE INDEX IF NOT EXISTS idx_buffer_slot_sku ON buffer_slot(sku);
CREATE INDEX IF NOT EXISTS idx_plastik_barcode ON plastik(barcode);
CREATE INDEX IF NOT EXISTS idx_harvester_task_status ON harvester_task(status);
CREATE INDEX IF NOT EXISTS idx_event_log_created ON event_log(created_at);
```

---

## Functions inti

### Scan handler — `packing_router/scan_handler.py`

```python
def handle_scan_plastik(barcode: str, operator_id: str) -> ScanResult:
    """
    Event 1. Returns ScanResult berisi:
    - action: 'place_in_slot_aktif' | 'place_in_buffer_existing' | 'place_in_buffer_new'
    - target_label: string ditampilkan ke operator (e.g. "SLOT 23 (RESI ABCDE)")
    - target_slot_aktif_id atau target_buffer_slot_id
    - extra info (jumlah plastik existing, dll)
    """
```

Logika (di dalam `BEGIN IMMEDIATE TRANSACTION`):
1. Lookup plastik by barcode. Jika belum ada, create entry dengan `sku` &
   `varian` di-derive dari format barcode (lihat seksi Barcode).
2. SELECT resi WHERE `status='active'` AND EXISTS resi_item dengan sku &
   varian sama AND `quantity_fulfilled < quantity_ordered`. Order by
   `slot_aktif_number ASC` (deterministik).
3. Jika ada → return action `place_in_slot_aktif`, update plastik state,
   `UPDATE resi_item SET quantity_fulfilled = quantity_fulfilled + 1`,
   cek transisi `active → complete`.
4. Jika tidak → cek `buffer_slot` dengan sku yang sama:
   - Ada → return action `place_in_buffer_existing`, increment
     `plastik_count`, update `last_plastik_at`.
   - Belum ada → call `assign_buffer_slot(sku)` → return action
     `place_in_buffer_new`. Set `first_plastik_at` & `last_plastik_at`.

### Buffer management — `packing_router/buffer.py`

```python
def assign_buffer_slot(sku: str) -> BufferLocation:
    """
    Assign slot kosong di wadah aktif (least-full strategy: pilih wadah dengan
    slot kosong terbanyak; tiebreaker = nomor wadah terkecil).
    Raise BufferFullError jika tidak ada slot kosong di seluruh wadah aktif.
    """

def find_buffer_slot_for_sku(sku: str) -> Optional[BufferLocation]:
    """Cari slot yang sudah dipakai SKU ini. Jika ada multiple (overflow), return primary (yang is_overflow_of IS NULL)."""

def add_wadah(capacity: int = 10) -> Wadah:
    """
    Dynamic add wadah baru. Auto-increment nomor wadah.
    Auto-create capacity buffer_slot rows (semua kosong).
    Log event 'add_wadah'.
    """

def get_buffer_status() -> BufferStatus:
    """
    Return: total wadah aktif, total slot, slot terpakai, slot kosong, breakdown per wadah.
    """

def handle_buffer_overflow(sku: str) -> BufferLocation:
    """
    Saat slot SKU sticky sudah punya plastik_count == OVERFLOW_TRIGGER_COUNT
    (configurable) dan ALLOW_BUFFER_OVERFLOW = True:
    Assign slot baru, mark is_overflow_of = primary slot id.
    Display: "Wadah X Slot Y (overflow dari Wadah A Slot B)".
    """
```

### Setup resi handler — `packing_router/resi_setup.py`

```python
def handle_setup_resi_aktif(resi_id: int, slot_number: int) -> SetupResult:
    """
    Event 2. Saat resi di-setup ke Slot Aktif:
    1. Update resi.slot_aktif_number, status = 'active', setup_at = now.
    2. Untuk setiap resi_item, cek buffer_slot dengan sku & varian sama.
    3. Jika ada match → create harvester_task dengan status 'pending'.
       Jumlah task = min(buffer plastik_count, quantity_ordered - quantity_fulfilled).
    4. Return list of created tasks.
    """

def import_from_list_pesanan_sheet(batch_id: str) -> ImportResult:
    """
    Baca dari sheet 'LIST_PESANAN' (Google Sheet existing — bukan CSV file
    lokal). Filter row dengan Batch_ID == batch_id. Group by Nomor_Resi.
    Parse SKU pakai utils.parse_sku() untuk dapat (numeric_id, varian).
    Insert wave (1 batch = 6 wave × 50 resi), resi, resi_item.
    Activate wave pertama otomatis (panggil handle_setup_resi_aktif untuk
    50 resi pertama, slot 1-50).
    
    Auth Google Sheet: pakai service account dari config.json (existing —
    read-only). Sheet URL/ID juga dari config.json.
    """

def try_activate_next_wave() -> Optional[Wave]:
    """
    Dipanggil setelah setiap pack. Jika wave aktif punya ≥ WAVE_NEXT_THRESHOLD_PCT
    (default 90%) resi 'packed' → close wave aktif, activate wave berikutnya
    (status='pending' yang wave_number terkecil di batch yang sama),
    auto-setup 50 resi pertama ke Slot Aktif (slot fisik 1-50, reuse dari
    slot yang sudah di-release).
    """
```

### Harvester handler — `packing_router/harvester.py`

```python
def harvester_pickup_scan(barcode: str, harvester_id: str) -> HarvesterPickupResult:
    """
    Saat harvester scan plastik di buffer (sebelum ambil).
    Validasi: plastik ini benar dari buffer_slot yang ada di harvester_task
    aktif (status='pending'). Jika valid:
    - Decrement buffer_slot.plastik_count.
    - Update plastik.location_type='in_transit'.
    - Mark harvester_task.status='in_progress', started_at=now.
    Jika invalid: raise HarvesterMismatchError, log event 'alert'.
    """

def harvester_dropoff_scan(barcode: str, target_slot_aktif_number: int, harvester_id: str) -> HarvesterDropoffResult:
    """
    Saat harvester letakkan plastik di Slot Aktif.
    Validasi: barcode match dengan task in_progress (harvester_id sama),
    slot_aktif_number sesuai task.target_resi.slot_aktif_number.
    - Update plastik.location_type='slot_aktif', plastik.location_ref=resi_id.
    - Increment resi_item.quantity_fulfilled.
    - Mark harvester_task.status='done', completed_at=now.
    - Cek transisi resi 'active → complete' jika semua quantity_fulfilled == quantity_ordered.
    """
```

### Slot Aktif status & queue — `packing_router/reports.py`

```python
def get_slot_aktif_status() -> list[SlotStatus]:
    """
    Return 50 slot dengan: nomor (1-50), resi_id, nomor_resi, status
    (merah/kuning/hijau), missing_skus list, completed_at.
    Slot kosong (resi_id NULL) status = 'kosong'.
    """

def get_harvester_queue() -> list[HarvesterTask]:
    """Return semua harvester_task status='pending' dan 'in_progress', urutan FIFO (created_at ASC)."""

def get_buffer_aging_report() -> list[AgingItem]:
    """Buffer slot dengan first_plastik_at > BUFFER_AGING_HOURS yang lalu, urut by first_plastik_at ASC."""
```

### Reset & maintenance — `packing_router/maintenance.py`

```python
def cancel_resi(resi_id: int) -> None:
    """
    Saat resi di-cancel mid-flow:
    - Untuk setiap plastik di Slot Aktif resi tersebut, panggil
      handle_scan_plastik() ulang secara internal (tanpa scan fisik) — supaya
      plastik di-route balik ke buffer (atau ke resi aktif lain yang butuh).
    - Mark resi.status='cancelled', release slot_aktif_number.
    - Cancel semua harvester_task yang target_resi_id = resi_id (pending/
      in_progress) → mark cancelled.
    """

def undo_last_scan(operator_id: str, within_seconds: int = 30) -> UndoResult:
    """
    Rollback scan terakhir operator dalam window waktu tertentu.
    Cari event_log terakhir (event_type='scan', actor=operator_id) yang
    created_at > now - within_seconds detik.
    Berdasarkan payload event:
    - Jika action='place_in_slot_aktif': decrement resi_item.quantity_fulfilled,
      hapus state plastik (set location_type=NULL), revert resi status complete→active jika applicable.
    - Jika action='place_in_buffer_existing': decrement buffer_slot.plastik_count.
    - Jika action='place_in_buffer_new': decrement plastik_count, jika jadi 0
      → reset slot (set sku=NULL).
    Log event 'undo'.
    """
```

---

## Web dashboard (4 view)

**Stack baru** untuk repo ini: **Flask 3.x + Jinja2 + HTMX**.
Repo existing tidak punya web framework (cuma desktop CustomTkinter), jadi
ini introduce stack baru — OK karena packing_router berdiri terpisah, tidak
menggantikan `app.py` atau `run_qc.py`.

Dependencies disimpan di **`packing_router/requirements.txt` terpisah**
(jangan modifikasi `requirements.txt` di root).

Launcher: `packing_router/run.bat` → `python -m packing_router.web.app`
(Flask dev server di port 5000).

View:
1. **`/operator/scan`** — input barcode (auto-focus), tampilkan instruksi
   besar setelah scan, riwayat 5 scan terakhir, tombol Undo (aktif 30
   detik). HTMX swap untuk update riwayat tanpa reload.
2. **`/harvester/queue`** — daftar task pending & in_progress, double-scan
   flow (pickup → dropoff). Polling tiap 3 detik via HTMX.
3. **`/slot-aktif`** — grid 5×10 visual (50 slot), warna merah/kuning/
   hijau/abu-abu(kosong). Klik slot → modal detail SKU yang ada/kurang.
4. **`/admin`** — buffer aging report, wadah status, throughput per jam,
   tombol "Tambah Wadah", tombol "Activate Next Wave Manual", import
   batch dari `LIST_PESANAN`.

---

## Edge cases

1. **Buffer penuh** (semua wadah, semua slot terpakai dan tidak ada slot
   SKU-sama): tolak scan, alert: "Buffer penuh, tunggu harvest atau
   tambah wadah". Notify supervisor (log event 'alert').
2. **SKU sticky overflow** (1 SKU lebih dari capacity slot): default
   `ALLOW_BUFFER_OVERFLOW=True` → assign slot ke-2 dengan `is_overflow_of`
   filled. `find_buffer_slot_for_sku` return primary (yang
   `is_overflow_of IS NULL`).
3. **Mis-scan**: undo window 30 detik, rollback state via event_log
   payload (lihat `undo_last_scan`).
4. **Harvester double-scan mismatch**: alert, JANGAN update state,
   log error.
5. **Resi cancelled mid-flow**: function `cancel_resi` route plastik
   balik ke buffer (recursive scan internal).
6. **Race condition (concurrency strategy untuk SQLite)**:
   - PRAGMA `journal_mode=WAL` + `synchronous=NORMAL` saat connection init.
   - Semua operasi tulis (scan handler, assign_buffer_slot, harvester
     scan, setup resi) dibungkus `BEGIN IMMEDIATE TRANSACTION`.
   - Pada `sqlite3.OperationalError: database is locked` → retry dengan
     exponential backoff (3 attempts, base 50ms).
   - **NOTE**: SQLite tidak punya `SELECT FOR UPDATE` — `BEGIN IMMEDIATE`
     sudah cukup karena dia langsung acquire RESERVED lock yang block
     writer lain. Reader tetap jalan (WAL).
7. **Buffer aging**: cron job harian (Windows Task Scheduler atau script
   manual `python -m packing_router.cron.aging_check`) yang cek
   `first_plastik_at > BUFFER_AGING_HOURS jam`, kirim notifikasi
   (untuk MVP: print ke console + log event 'alert' — channel notif
   real ditentukan kemudian).

---

## Konfigurasi (`packing_router/config.py`)

```python
SLOTS_PER_WAVE = 50
RESIS_PER_BATCH = 300
DEFAULT_WADAH_COUNT = 5
SLOTS_PER_WADAH = 10
BUFFER_AGING_HOURS = 24
SLOT_KUNING_TIMEOUT_MIN = 15
ALLOW_BUFFER_OVERFLOW = True
OVERFLOW_TRIGGER_COUNT = 10  # plastik_count yang trigger overflow assignment
UNDO_WINDOW_SECONDS = 30
WAVE_NEXT_THRESHOLD_PCT = 90  # % packed sebelum auto-activate wave berikutnya
DB_PATH = "hasil/packing_router.db"
SQLITE_BUSY_RETRY_COUNT = 3
SQLITE_BUSY_RETRY_BASE_MS = 50
```

Override per env via `os.environ.get('PACKING_ROUTER_<KEY>', default)`.

---

## Integrasi

1. **Source data resi (BigSeller)**: BUKAN parse CSV file lokal. Baca
   dari Google Sheet `LIST_PESANAN` yang sudah di-populate Apps Script
   `code.gs` saat tim gudang upload. Fungsi:
   `import_from_list_pesanan_sheet(batch_id: str)`. Kolom yang dipakai:
   `Batch_ID, Nomor_Resi, SKU, Jumlah` (lihat README repo seksi "Schema
   sheet `LIST_PESANAN`"). Auth: service account JSON path dari
   `config.json` repo root (read-only) — keys: `gsheet_url`, `json_path`.

2. **`app.py` existing**:
   - JANGAN modifikasi.
   - SKU normalization saat ini = method `BotApp.extract_numeric_id_and_pcs`
     (`app.py:533`). Karena ini method instance Tkinter class, **JANGAN
     import**. Sebagai gantinya, copy regex logic-nya ke
     `packing_router/utils.py` sebagai pure function:
     ```python
     def parse_sku(sku: str) -> tuple[Optional[int], int]:
         """Return (numeric_id, pcs_per_paket). pcs_per_paket default 10."""
         sku = sku.strip()
         id_match = re.match(r'^\d+', sku)
         numeric_id = int(id_match.group()) if id_match else None
         pcs_match = re.search(r'(\d+)pcs', sku, re.IGNORECASE)
         pcs_per_paket = int(pcs_match.group(1)) if pcs_match else 10
         return numeric_id, pcs_per_paket
     ```

3. **Google Sheets log saat pack** (sheet `DATA_SALES`, **uppercase**):
   saat resi di-pack, append baris `(tanggal, ID master, total pcs)` via
   `gspread`. Auth pakai service account JSON dari `config.json`.
   Function ini ditulis di `packing_router/sheets_log.py`, BUKAN tambah
   ke `app.py`.

4. **Stok gudang**: di scope berikutnya. Untuk sekarang, asumsi semua
   plastik datang dari output weeding (produksi baru). Admin akan fetch
   stok gudang manual ke Slot Aktif (di luar logic module ini).

5. **Barcode di plastik**: assume sudah ada (HOG akan rollout barcode
   wajib di semua plastik). Format barcode wajib: `{ID}-{VARIAN}PCS-{SEQ4}`
   (e.g. `1446-10PCS-0001`). Parser di `utils.py`:
   ```python
   def parse_barcode(barcode: str) -> tuple[str, int, str]:
       """Return (sku_base, varian, seq). Raise BarcodeFormatError jika invalid."""
   ```
   Sub-tool generate barcode di scope berikutnya.

---

## Folder structure (target)

```
packing_router/
├── __init__.py
├── config.py
├── db.py                    # connection, schema init, WAL mode setup
├── models.py                # dataclass: ScanResult, BufferLocation, dll
├── exceptions.py            # BufferFullError, HarvesterMismatchError, BarcodeFormatError
├── utils.py                 # parse_sku, parse_barcode
├── scan_handler.py          # handle_scan_plastik
├── buffer.py                # assign_buffer_slot, add_wadah, dll
├── resi_setup.py            # handle_setup_resi_aktif, import_from_list_pesanan_sheet, try_activate_next_wave
├── harvester.py             # harvester_pickup_scan, harvester_dropoff_scan
├── reports.py               # get_slot_aktif_status, get_harvester_queue, get_buffer_aging_report
├── maintenance.py           # cancel_resi, undo_last_scan
├── sheets_log.py            # append packed log ke sheet DATA_SALES
├── requirements.txt         # Flask, jinja2, gspread (terpisah dari root)
├── run.bat                  # python -m packing_router.web.app
├── cron/
│   └── aging_check.py       # cron buffer aging
├── web/
│   ├── __init__.py
│   ├── app.py               # Flask entry, routes
│   ├── templates/
│   │   ├── base.html
│   │   ├── operator_scan.html
│   │   ├── harvester_queue.html
│   │   ├── slot_aktif.html
│   │   └── admin.html
│   └── static/
│       └── style.css
└── tests/
    ├── __init__.py
    ├── conftest.py          # fixture: temp DB
    ├── test_utils.py        # parse_sku, parse_barcode
    ├── test_scan_handler.py
    ├── test_buffer.py
    ├── test_resi_setup.py
    ├── test_harvester.py
    ├── test_maintenance.py  # cancel_resi, undo
    └── test_race_condition.py  # threading test 2 operator scan SKU sama
```

---

## Acceptance criteria

Module dianggap selesai kalau test case berikut pass:

- [ ] Operator scan plastik dengan SKU yang dibutuhkan resi aktif →
      diarahkan ke slot aktif yang benar
- [ ] Operator scan plastik dengan SKU yang tidak ada di resi aktif →
      diarahkan ke slot baru di buffer
- [ ] Operator scan plastik kedua dengan SKU yang sama (sudah ada di
      buffer) → diarahkan ke slot yang sama (sticky)
- [ ] Tambah wadah baru via dashboard `/admin` → langsung tersedia
      sebagai kapasitas tambahan tanpa restart
- [ ] Setup resi baru yang butuh SKU yang sudah ada di buffer →
      `harvester_task` otomatis tercipta dan muncul di queue
- [ ] Harvester double-scan flow validasi (pickup match SKU & task,
      dropoff match slot)
- [ ] Slot aktif berubah merah → hijau saat semua SKU resi terpenuhi
      (resi `status` `active → complete`)
- [ ] Slot hijau berubah kuning di UI setelah > 15 menit menunggu pack
- [ ] Undo last scan dalam 30 detik berfungsi (rollback ke 3 jenis aksi)
- [ ] Cancel resi route plastik balik ke buffer
- [ ] Buffer overflow (slot SKU penuh) auto-extend ke slot ke-2
- [ ] Buffer total penuh tolak scan + alert
- [ ] Race condition test: 2 thread scan SKU sama bersamaan via
      `threading.Thread` — tidak bikin duplicate slot assignment
      (`test_race_condition.py`)
- [ ] Import 1 batch dari sheet `LIST_PESANAN` (300 resi) split jadi 6
      wave dan activate wave pertama
- [ ] Aging report nampilkan plastik > 24 jam di buffer
- [ ] Wave transition: setelah ≥ 90% wave aktif `packed` → wave
      berikutnya auto-activate, slot 1-50 reused
- [ ] Logging `event_log` lengkap untuk audit trail (semua event_type)
- [ ] **Regression check** (eksplisit, harus PASS):
      - `git diff app.py qc_stasiun.py run_qc.py qc_seed.py duplicate_files.py updater.py code.gs test_qc_parser.py requirements.txt config.json README.md start.bat start_qc.bat install.bat` → empty (no modifications)
      - `python test_qc_parser.py` → exit 0
      - `python -c "import app"` → no error (smoke test app.py importable)
      - `python -c "import qc_stasiun"` → no error
      - `python -c "import run_qc"` → no error (kalau run_qc.py berisi top-level GUI launch, wrap dalam `if __name__ == '__main__'` check terlebih dahulu — kalau belum, skip test ini & document)

---

## Catatan untuk agent

- **Repo target**: `sortir_stiker_desain` existing (https://github.com/dennypa77/sortir_stiker_desain).
  Tambahkan sebagai folder package baru `packing_router/`. JANGAN modifikasi
  file root.
- **Repo `sortir-ganci`**: TIDAK ada hubungannya dengan project ini. Domain
  beda (QC ganci/gantungan kunci). Jangan disentuh sama sekali.
- **Database**: file terpisah `hasil/packing_router.db`. Jangan share dengan
  `qc_data.db`.
- **Web stack**: Flask + HTMX adalah introduction baru di repo ini, OK karena
  modul terpisah. Repo existing tetap desktop-only.
- **Prioritas**: correctness > performance > UX. Throughput target 1.500
  resi/hari realistis dengan SQLite biasa, tidak perlu caching layer agresif
  di iterasi pertama.
- **Setelah module berfungsi**, opsi enhancement fase berikutnya: barcode
  generator integrated, multi-station scan parallel (mungkin pindah ke
  Postgres saat scaling), mobile-friendly harvester UI (PWA), integrasi stok
  gudang.
