# Packing Router — Tutorial Lengkap

Sistem **Sort-to-Resi + Buffer SKU-sticky** untuk stasiun packing produk
Stiker Desain HOG (Heavy Object Group). Modul tambahan di repo
`sortir_stiker_desain` — berjalan **berdampingan** dengan `app.py` dan
`run_qc.py` existing (tidak menggantikan, tidak memodifikasi).

---

## Daftar Isi

1. [Latar Belakang & Konsep](#1-latar-belakang--konsep)
2. [Komponen Sistem](#2-komponen-sistem)
3. [Persyaratan & Instalasi](#3-persyaratan--instalasi)
4. [Setup Awal](#4-setup-awal)
5. [Menjalankan Aplikasi](#5-menjalankan-aplikasi)
6. [Workflow per Role](#6-workflow-per-role)
   - [6.1 Operator Sortir](#61-operator-sortir)
   - [6.2 Harvester](#62-harvester)
   - [6.3 Packer](#63-packer)
   - [6.4 Admin](#64-admin)
7. [Skenario End-to-End](#7-skenario-end-to-end)
8. [Format Barcode & SKU](#8-format-barcode--sku)
9. [Konfigurasi Lanjutan (Env Vars)](#9-konfigurasi-lanjutan-env-vars)
10. [Aging Report (Cron)](#10-aging-report-cron)
11. [Edge Cases & Troubleshooting](#11-edge-cases--troubleshooting)
12. [Testing](#12-testing)
13. [Struktur File](#13-struktur-file)
14. [Integrasi dengan Sistem Existing](#14-integrasi-dengan-sistem-existing)
15. [FAQ](#15-faq)

---

## 1. Latar Belakang & Konsep

### Masalah yang dipecahkan

HOG memproses ~1.500 resi/hari Stiker Desain dari marketplace. Setiap
resi rata-rata berisi 2-3 SKU. ~50% SKU di-fulfill dari stok gudang
(sudah ditandai stabilo di kertas resi), ~50% lagi dari produksi baru
(print → cutting → weeding).

**Bottleneck lama**: cutting & weeding lambat, packing menunggu —
picker buang waktu mencari plastik di matras lesehan. Plastik
berserakan, susah dicari per resi.

### Solusi: Inversi logika

Alih-alih packer mencari plastik, **sistem mengarahkan setiap plastik
ke tujuannya**. Operator scan barcode plastik → sistem kasih instruksi
besar di layar: *"LETAKKAN KE SLOT 23 (RESI XXX)"* atau *"LETAKKAN KE
WADAH 2 SLOT 5 (slot baru)"*.

### Dua rak fisik utama

#### Slot Aktif (50 slot)
Rak fisik dengan **50 slot bernomor**. Mapping 1:1 ke 50 resi yang
sedang aktif (1 wave). 1 batch BigSeller (300 resi) = 6 wave × 50 resi.

#### Buffer (N wadah × 10 slot)
Rak fisik dengan **N wadah configurable** (default 5, bisa tambah
anytime tanpa restart). Setiap wadah punya 10 slot bersekat. Aturan
**SKU-sticky**: 1 SKU = 1 slot di buffer, plastik dengan SKU sama
menumpuk di slot yang sama.

### Tiga role manusia

| Role | Tugas |
|---|---|
| **Operator Sortir** | Scan setiap plastik output weeding → letakkan sesuai instruksi sistem |
| **Harvester** | Pindahkan plastik dari Buffer ke Slot Aktif saat sistem buat task |
| **Packer** | Pack resi yang slot-nya sudah hijau (semua SKU lengkap) |
| **Admin** | Import batch, tambah wadah, monitor aging |

---

## 2. Komponen Sistem

### Database (SQLite)

File terpisah: `hasil/packing_router.db` (TIDAK numpang `qc_data.db`).
Auto-create saat first launch. WAL mode untuk concurrency.

8 tabel: `wave`, `resi`, `resi_item`, `wadah`, `buffer_slot`,
`plastik`, `harvester_task`, `event_log`.

### Web Dashboard (Flask + HTMX)

Server-rendered HTML, polling otomatis. Default port 5000.

| URL | Untuk |
|---|---|
| `/operator/scan` | Operator sortir |
| `/harvester/queue` | Harvester |
| `/slot-aktif` | Packer (grid 5×10 visual) |
| `/admin` | Admin |

### Modul Python

`packing_router/` package — `scan_handler`, `buffer`, `resi_setup`,
`harvester`, `maintenance`, `reports`, `sheets_log`, `cron`, `web`,
`tests`.

---

## 3. Persyaratan & Instalasi

### Persyaratan

- **OS**: Windows 10/11 (sama dengan repo existing)
- **Python**: 3.10+ (dites di Python 3.13)
- **Browser modern**: Chrome/Edge untuk dashboard
- **Barcode scanner USB** (atau ketik manual untuk testing)
- **Koneksi internet**: untuk integrasi Google Sheet (LIST_PESANAN
  import + DATA_SALES log) — modul tetap jalan offline kalau
  integrasi ini di-skip

### Install dependencies

Dari root repo:

```powershell
pip install -r packing_router\requirements.txt
```

Library yang di-install:
- `Flask` 3.x — web framework
- `Jinja2` 3.x — template engine
- `gspread` + `google-auth` — Google Sheets API
- `pytest` — untuk run unit test

> Catatan: `requirements.txt` di root repo (untuk app.py dan run_qc.py)
> **tidak** dimodifikasi. Modul ini punya `requirements.txt` sendiri di
> folder `packing_router/`.

### Verify install

```powershell
python -c "from packing_router.web.app import create_app; create_app(); print('OK')"
```

Output `OK` = install sukses, DB juga ke-create.

---

## 4. Setup Awal

### 4.1 Konfigurasi Google Sheet (opsional tapi disarankan)

Jika mau import batch otomatis dari sheet `LIST_PESANAN` dan
auto-append log `DATA_SALES`, pastikan `config.json` di root repo
sudah terisi:

```json
{
  "json_path": "C:\\path\\to\\service-account.json",
  "gsheet_url": "https://docs.google.com/spreadsheets/d/.../edit"
}
```

Sheet `LIST_PESANAN` dan `DATA_SALES` (uppercase) di Google Spreadsheet
harus sudah ada — sheet ini **shared dengan app.py existing**, sudah
dipopulate Apps Script `code.gs` saat tim gudang upload BigSeller.

> Modul ini hanya **baca** dari `LIST_PESANAN` (read-only) dan
> **append** ke `DATA_SALES`. Tidak overwrite atau delete.

### 4.2 First Launch — Auto Init

Jalankan satu kali untuk init DB & default 5 wadah:

```powershell
python -m packing_router.web.app
```

Saat first run, sistem akan:
1. Create `hasil/packing_router.db` dengan WAL mode aktif
2. Buat 8 tabel + index
3. Insert 5 wadah default (nomor 1-5), masing-masing 10 slot kosong
4. Log event `add_wadah` ke `event_log`

Konsol akan tampil:
```
 * Running on http://127.0.0.1:5000
```

Buka browser → `http://127.0.0.1:5000` → otomatis redirect ke
`/operator/scan`.

> Untuk stop, tekan Ctrl+C di terminal.

### 4.3 Setting Layout Rak Fisik

Sebelum mulai live, pastikan rak fisik di gudang sudah:

**Slot Aktif** (rak 50 slot):
- Susun 5 baris × 10 kolom, atau 10 baris × 5 kolom
- Tempelkan label nomor 1-50 di setiap slot
- Pastikan urutan slot fisik konsisten dengan numerik (mis. slot 1
  di pojok kiri-atas, slot 50 di pojok kanan-bawah)

**Buffer** (5 wadah × 10 slot):
- 5 wadah container plastik dengan label "WADAH 1" sampai "WADAH 5"
- Setiap wadah disekat 10 slot bernomor 1-10
- Tempelkan label besar di sisi luar agar terlihat jelas dari jarak
  scan

---

## 5. Menjalankan Aplikasi

### 5.1 Launcher

#### Cara 1: Batch file (paling mudah)

Double-click `packing_router\run.bat` dari Windows Explorer. Ini akan:
1. Pindah ke root repo
2. Run `python -m packing_router.web.app`

#### Cara 2: Manual via terminal

```powershell
cd "D:\Project\Bot Stiker V2"
python -m packing_router.web.app
```

#### Cara 3: Custom port / host

Lewat env var (set sebelum launch):

```powershell
$env:PACKING_ROUTER_WEB_HOST="0.0.0.0"   # bind ke semua interface (LAN access)
$env:PACKING_ROUTER_WEB_PORT="5050"
$env:PACKING_ROUTER_WEB_DEBUG="true"
python -m packing_router.web.app
```

`0.0.0.0` membuat app accessible dari device lain di jaringan lokal —
cocok untuk multi-station setup (operator pakai 1 PC, harvester
pakai 1 PC lain, dst).

### 5.2 Akses Dashboard

| URL | Pengguna | Tab browser disarankan |
|---|---|---|
| `http://localhost:5000/operator/scan` | Operator sortir | 1 tab fokus penuh |
| `http://localhost:5000/harvester/queue` | Harvester | 1 tab terbuka di smartphone/tablet harvester |
| `http://localhost:5000/slot-aktif` | Packer | 1 tab di monitor besar di stasiun packing |
| `http://localhost:5000/admin` | Admin/supervisor | Buka saat butuh (tambah wadah, import batch) |

> Tip: pasang shortcut Chrome/Edge dengan flag `--app=` ke setiap URL
> agar buka tanpa toolbar (kiosk-like). Cocok untuk PC stasiun.

---

## 6. Workflow per Role

> **Penting**: Operator pakai **2 mode** — Mode 1 (Scan Resi untuk setup ke
> slot aktif) dan Mode 2 (Scan Plastik untuk sortir output weeding). Resi
> tidak otomatis di-bulk-import — admin sync sheet dulu, lalu operator scan
> resi 1-by-1 saat fisik kertas resi datang.

### 6.1 Operator Sortir

**Posisi**: di dekat output weeding + meja terima kertas resi.
**Tugas**: 2 mode — setup resi yang fisik datang & sortir plastik output weeding.

#### Layar `/operator/scan`

2 panel input:
- **Mode 1 — Scan Resi**: untuk setup kertas resi marketplace ke Slot Aktif
- **Mode 2 — Scan Plastik**: untuk sortir plastik dari weeding

#### Mode 1 — Scan Resi (setup ke Slot Aktif)

Saat kertas resi marketplace fisik datang ke meja, **1-step**:

1. Scan/ketik nomor resi (mis. `SPXID060408319585`) di field "Mode 1"
2. Sistem cari di pool DB (yang sudah disync admin dari sheet `LIST_PESANAN`)
3. Auto-assign ke slot aktif kosong terkecil
4. Hasil:
   ```
   RESI SPXID0608... → SLOT 5
   Ambil dari Buffer dulu (1 SKU):
     SKU 445 (10pcs): ambil 1 pack dari WADAH 2 SLOT 3
   ```
   atau, kalau buffer kosong:
   ```
   RESI SPXID0608... → SLOT 5
   Belum ada SKU resi ini di buffer. Tunggu plastik di-scan lewat Mode 2.
   ```

#### SKU Sudah dari Stok Gudang (📦 Gudang button)

Tim packing **biasanya sudah memasukkan plastik ke polymailer dari stok
gudang** sebelum kertas resi sampai ke meja sortir (ditandai stabilo di
kertas resi).

Untuk tandai SKU yang sudah dari stok gudang, **klik tombol `📦 Gudang`
per-SKU** di kartu slot di dashboard:

1. Buka `/dashboard` di monitor
2. Cari kartu slot resi-nya (mis. SLOT 5 yang baru di-setup)
3. Buka details "Kurang (...)" → ada list SKU yang masih kurang
4. Klik tombol **`📦 Gudang`** di samping SKU yang ada stabilo-nya
5. Sistem set `prefilled_qty = quantity_ordered` untuk SKU itu, dan:
   - Cancel pending harvester task untuk SKU ini di resi ini (kalau ada)
   - SKU pindah dari list "Kurang" ke list "Stok Gudang ✓"
   - Kalau ini SKU terakhir yang missing → resi auto-complete (slot hijau)

Contoh: ResiA pesan SKUA dan SKUB.
- Setup resi → kedua SKU masuk daftar "Kurang"
- Klik `📦 Gudang` di SKUA → SKUA pindah ke "Stok Gudang ✓"
- SKUB tetap di daftar Kurang — perlu di-scan dari output weeding atau
  diambil dari buffer
- Setelah SKUB ter-fulfill (via scan plastik) → resi otomatis complete,
  siap di-pack

**Tombol Lepas (↺)**: di section "Stok Gudang ✓", ada tombol `↺ Lepas`
untuk un-mark (kalau salah klik). SKU balik ke daftar "Kurang", resi
revert ke status active kalau perlu.

> Kalau resi belum ada di pool: muncul error *"Resi belum ada di pool. Klik
> Sync Sheet di /admin"*. Admin yang harus klik Sync Sheet di /admin dulu.

#### Mode 2 — Scan Plastik (sortir output weeding)

Setiap plastik yang baru jadi dari cutting+weeding:

1. Ambil 1 plastik dari output weeding
2. Scan barcode di plastik (numeric ID, mis. `445`)
3. Layar tampilkan **kotak besar berwarna**:

   **Hijau** — `LETAKKAN KE SLOT 5 (RESI SPXID...)`
   - Ada resi di slot 5 yang butuh stiker 445 → langsung ke rak Slot Aktif

   **Kuning** — `LETAKKAN KE WADAH 2 SLOT 3 (sudah berisi 4 plastik)`
   - Tidak ada resi aktif yang butuh, tapi sudah ada slot buffer SKU sama → tumpuk

   **Biru** — `LETAKKAN KE WADAH 1 SLOT 7 (slot baru)`
   - SKU ini baru pertama → slot baru di buffer

4. Letakkan plastik secara fisik
5. Cursor otomatis kembali ke field — siap scan berikutnya

> 1 plastik = 1 pack 10pcs. Resi varian besar (20/50/100pcs) butuh
> 2/5/10 plastik scan. Sistem auto-hitung pack yang dibutuhkan.

#### Tombol Undo (window 30 detik)

Salah scan / salah letak? Klik tombol **Undo** di bawah field scan.

Sistem akan rollback action terakhir:
- Jika tadi masuk slot aktif → decrement fulfilled count
- Jika tadi masuk slot buffer existing → decrement plastik_count
- Jika tadi masuk slot buffer baru → reset slot tersebut

> Window default 30 detik (configurable via `UNDO_WINDOW_SECONDS`).
> Lewat window, undo akan tolak.

#### Riwayat 5 scan terakhir

Di bawah area instruksi tampil riwayat 5 scan terakhir operator (dari
`event_log`). Berguna untuk re-check kalau kelewat / lupa.

#### Kalau buffer penuh

Jika semua wadah aktif sudah penuh (semua slot terpakai dan tidak ada
SKU sama yang bisa ditumpuk), scan akan tolak dengan alert:
*"Buffer penuh, tunggu harvest atau tambah wadah"*. Lapor ke admin
untuk tambah wadah baru via `/admin`.

### 6.2 Harvester

**Posisi**: mobile — bisa pegang tablet/smartphone, fisik mondar-mandir
antara Buffer dan Slot Aktif. **Tugas**: ambil plastik dari buffer
yang sudah ada match-nya, bawa ke Slot Aktif.

#### Layar `/harvester/queue`

Tampilkan tabel task `pending` dan `in_progress` (FIFO). Auto-refresh
tiap 3 detik via HTMX.

#### Kolom tabel

| Kolom | Arti |
|---|---|
| # | Task ID |
| Status | `pending` (belum diambil) atau `in_progress` (lagi dibawa) |
| SKU | SKU yang harus dibawa |
| Ambil dari | `WADAH X SLOT Y` di Buffer |
| Bawa ke Slot | `SLOT N` di Slot Aktif |
| Resi | Nomor resi tujuan (untuk verifikasi visual) |

#### Double-Scan Flow

Setiap task butuh **2 kali scan** untuk validasi:

##### Step 1 — Pickup (di Buffer)

1. Ambil task paling atas di tabel (FIFO).
2. Pergi ke wadah/slot yang ditunjukkan, ambil 1 plastik dari tumpukan
   (FIFO — yang paling bawah dulu, supaya rotasi).
3. Di kolom **"1) Pickup di Buffer"**, scan barcode plastik tadi.
4. Sistem akan validasi:
   - Plastik ini benar dari buffer slot yang ada di task pending?
   - SKU match?
5. Jika valid:
   - Tampilkan konfirmasi hijau: `PICKUP OK — Task #N. Bawa ke SLOT N (Resi XXX)`
   - Task status: `pending` → `in_progress`
   - Plastik state: `buffer` → `in_transit`
   - Buffer slot count decremented (jika 0 → slot di-reset)

##### Step 2 — Dropoff (di Slot Aktif)

1. Bawa plastik fisik ke Slot Aktif yang ditunjukkan.
2. Letakkan di slot tersebut.
3. Di kolom **"2) Dropoff di Slot Aktif"**:
   - Isi field **"Slot #"** dengan nomor slot tujuan
   - Scan barcode plastik **lagi**
4. Sistem validasi:
   - Barcode ini punya task `in_progress`?
   - Slot # input match dengan task target?
5. Jika valid:
   - Tampilkan konfirmasi hijau: `DROPOFF OK — Task #N. Plastik ke SLOT N`
   - Plastik state: `in_transit` → `slot_aktif`
   - `resi_item.quantity_fulfilled` increment
   - Task status: `in_progress` → `done`
   - Cek transition resi: jika semua SKU lengkap → resi `complete`,
     slot di Slot Aktif berubah merah → hijau

#### Kalau scan mismatch

Sistem tolak dan tampilkan alert merah, tidak ada state yang berubah.
Cek ulang:
- Apakah barcode benar dari plastik yang di-pickup?
- Apakah slot # yang di-input sesuai?
- Apakah task masih `in_progress` (jangan-jangan harvester lain ambil)

#### Tip kerja efisien

- Ambil **multiple plastik sekaligus** dari buffer kalau task
  berurutan — pickup-pickup-pickup dulu, baru round trip ke slot
  aktif.
- Plastik `in_transit` masih ada di sistem, jadi aman ditaruh di
  troli sementara.

### 6.3 Packer

**Posisi**: di stasiun packing (seal & shipping). **Tugas**: pack
resi yang slot-nya sudah hijau, FIFO berdasarkan timestamp complete.

#### Layar `/slot-aktif`

Grid 5×10 visual dari 50 slot. Auto-refresh tiap 5 detik.

#### Warna slot

| Warna | Status | Arti |
|---|---|---|
| 🟥 **Merah** | `active` | Resi belum lengkap (ada SKU yang `quantity_fulfilled < quantity_ordered`). Jangan pack. |
| 🟩 **Hijau** | `complete` | Resi lengkap. Siap pack. |
| 🟨 **Kuning** | `complete` + > 15 menit | Sudah lama di-complete tapi belum di-pack. Prioritas (escalate). |
| ⬜ **Abu-abu** | `kosong` | Slot tanpa resi (belum di-setup). |

> Threshold kuning configurable via `SLOT_KUNING_TIMEOUT_MIN`.

#### Detail per slot

Klik card slot → expand `<details>` "Kurang (N)" — tampilkan SKU yang
masih kurang dan progress (mis. `1446-10PCS: 1/2` = baru 1 dari 2).

#### Tombol PACK

Tampil di slot hijau/kuning. Klik:
1. Resi state: `complete` → `packed`, `packed_at` ter-set
2. Slot fisik di-release (slot_aktif_number jadi NULL)
3. Plastik di slot tersebut state-nya jadi `packed`
4. Sistem cek `try_activate_next_wave` — jika wave aktif sudah ≥ 90%
   packed, wave berikutnya auto-activate → 50 resi baru di-setup ke
   slot yang baru saja di-release
5. (Opsional) trigger append ke sheet `DATA_SALES`

#### Tombol Cancel

Tampil di setiap slot terisi. Klik (akan tanya konfirmasi):
- Resi state: `cancelled`
- Plastik di slot itu di-route ulang via `handle_scan_plastik` internal
  — kalau ada resi aktif lain butuh SKU itu, plastik masuk slot situ.
  Kalau tidak, balik ke buffer.
- Harvester task pending/in_progress untuk resi ini di-cancel
- Plastik in-transit (jika ada) balik ke buffer asalnya

> Hati-hati: cancel = irreversible. Pastikan benar-benar resi-nya batal.

#### Workflow harian

1. Tongkrongi `/slot-aktif` di monitor besar.
2. Liat slot hijau yang muncul (FIFO by `completed_at`).
3. Buka resi fisik di rak, masukkan ke polymailer, seal.
4. Scan resi di **Stasiun QC** existing (`run_qc.py`) untuk verifikasi
   final sebelum shipping (workflow lama tetap jalan, parallel).
5. Klik PACK di dashboard Slot Aktif.

> Note: integrasi auto-pack saat QC approve di `run_qc.py` adalah
> scope berikutnya. Saat ini packer harus klik PACK manual.

### 6.4 Admin

**Posisi**: meja supervisor / komputer admin. **Tugas**: import batch,
tambah wadah, monitor aging, force-activate wave kalau perlu.

#### Layar `/admin`

3 section utama:

##### A. Buffer Status

Tabel breakdown per wadah: nomor, aktif, capacity, terpakai, kosong,
total plastik. Total agregat di atas tabel.

###### Tambah Wadah Baru

Form di bawah tabel: input capacity (default 10) → klik "+ Tambah
Wadah". Sistem akan:
1. Auto-increment nomor wadah (max + 1)
2. Insert wadah baru dengan capacity yang di-input
3. Auto-create N buffer_slot rows kosong
4. Log event `add_wadah`

> Tambah wadah = **runtime, no restart**. Operator langsung bisa scan
> ke wadah baru begitu form submit.

##### B. Aging Report

Tabel buffer slot dengan `first_plastik_at > BUFFER_AGING_HOURS` (default
24 jam). Kolom: wadah, slot, SKU, jumlah plastik, first scan, umur jam.

> Kalau ada item di tabel ini, artinya plastik nyangkut di buffer >
> 24 jam — kemungkinan resi-nya batal, SKU error, atau lupa di-harvest.
> Cek manual ke fisik buffer.

##### C. Throughput per Jam

List 12 jam terakhir, jumlah resi di-pack per jam. Untuk monitor
kinerja packer.

##### D. Sync Sheet (utama)

Tombol **Sync Sheet (LIST_PESANAN)** — **inilah cara utama** memasukkan
data resi ke sistem:

1. Tim gudang upload BigSeller via Apps Script `code.gs` (workflow lama
   tetap jalan) → row masuk ke sheet `LIST_PESANAN`
2. Admin klik **Sync Sheet** di dashboard
3. Sistem fetch SEMUA row dari sheet → upsert ke DB
4. Resi baru masuk dengan status `pending` (BELUM di slot aktif)
5. Resi yang sudah ada di DB di-skip (tidak overwrite)
6. Tampilkan summary: `Sync OK: N batch, M resi baru, K SKU items, S skip`

Setelah sync, operator yang setup resi ke slot aktif satu per satu
lewat **Mode 1 (Scan Resi)** di `/operator/scan`.

> Sync bisa dipanggil **berulang kali** sepanjang hari setiap kali tim
> gudang upload batch baru ke sheet — yang baru saja di-tambah
> ter-import, yang sudah ada di-skip.

##### E. Bulk Import (opsional, legacy)

Form input `Batch_ID` + tombol "Bulk Import" — pre-fill 50 slot aktif
sekaligus dari 1 batch tertentu.

> Workflow normal **TIDAK** pakai ini. Bulk import auto-set 50 resi ke
> slot aktif tanpa scan resi — cuma cocok kalau semua resi fisik sudah
> ada bersamaan (jarang di HOG yang resinya datang mencicil).

---

## 7. Skenario End-to-End

Run-through lengkap untuk pemahaman. Asumsi: 5 wadah default, 50 slot
aktif, 1 batch BigSeller berisi 300 resi.

### Step 1 — Pagi: Tim Gudang Upload BigSeller

Tim gudang export dari BigSeller → upload via menu `Kelola Gudang` di
Google Sheet → 300 row baru muncul di sheet `LIST_PESANAN`.

### Step 2 — Admin Sync Sheet ke DB

Admin buka `/admin` → klik tombol **Sync Sheet (LIST_PESANAN)**:
```
Sync OK: 1 batch, 300 resi baru, 750 SKU items, 0 skip.
```

DB sekarang punya 300 resi status `pending` — **belum** di-setup ke slot
aktif. Slot aktif masih semua kosong (abu-abu).

### Step 3 — Kertas Resi Datang Mencicil

Tim print/produksi mulai cetak resi. Kertas resi datang ke meja
operator satu per satu (atau berkelompok kecil).

### Step 4 — Operator Setup Resi

Operator scan/ketik nomor resi pertama yang datang (`SPXID0608...`):

```
RESI SPXID0608... → SLOT 1
Belum ada SKU resi ini di buffer. Tunggu plastik di-scan.
```

Slot 1 di `/slot-aktif` jadi 🔴 merah (resi terdaftar, belum lengkap).

Resi kedua datang → setup → SLOT 2. Resi ketiga → SLOT 3. Dst.

### Step 5 — Operator Scan Plastik

Bareng setup resi, plastik output weeding mulai keluar. Operator scan
barcode plastik `445`:
- Cek resi aktif: resi di SLOT 1 butuh stiker 445 (varian 50pcs = 5 pack)?
  - Kalau YA → instruksi: `LETAKKAN KE SLOT 1 (RESI SPXID0608...)`
  - Operator letakkan, fulfilled count 0 → 1 (masih kurang 4 pack)
- Scan plastik 445 lagi → SLOT 1 lagi → fulfilled 1 → 2
- ...sampai fulfilled 5 → resi SLOT 1 LENGKAP → 🔴 → 🟢

Sementara itu plastik 999 keluar (belum ada resi yang butuh):
- Cek slot aktif: tidak ada → cek buffer: belum ada → assign slot baru
- Instruksi: `LETAKKAN KE WADAH 1 SLOT 1 (slot baru)`
- Plastik kedua 999 → tumpuk di slot yang sama → plastik_count = 2

### Step 6 — Resi Baru Datang yang Butuh Plastik di Buffer

Resi keempat datang (`SPXID9999...`), berisi stiker 999 varian 20pcs (=
2 pack). Operator scan resi tersebut:

```
RESI SPXID9999... → SLOT 4
Ambil dari Buffer dulu (1 SKU):
  SKU 999 (20pcs): ambil 2 pack dari WADAH 1 SLOT 1
Task harvester sudah otomatis dibuat — lihat di /harvester/queue
```

Buffer sudah numpuk 2 plastik 999 — pas! Sistem otomatis buat 2
harvester task.

### Step 7 — Harvester Eksekusi Task

Harvester buka `/harvester/queue`:
```
#1 pending  SKU 999  Ambil dari WADAH 1 SLOT 1  Bawa ke SLOT 4
#2 pending  SKU 999  Ambil dari WADAH 1 SLOT 1  Bawa ke SLOT 4
```

Harvester ke Wadah 1 Slot 1, ambil 1 plastik. Scan di field "Pickup":
- Validasi OK → task #1 jadi `in_progress`, plastik jadi `in_transit`,
  buffer count 2 → 1

Bawa ke Slot 4. Scan di field "Dropoff" + input slot # 4:
- Validasi OK → resi item fulfilled 0 → 1 (masih butuh 1 lagi)

Ulang untuk plastik kedua. Setelah dropoff:
- Resi item fulfilled 1 → 2 ✓ (lengkap untuk SKU 999)
- Kalau resi tidak punya SKU lain → resi `complete` → SLOT 4 🟢

### Step 8 — Packer Pack Resi Hijau

Packer monitor `/slot-aktif`. Slot 1 dan Slot 4 hijau. Klik PACK
masing-masing:
- Slot 1: resi → `packed`, slot di-release → kosong abu-abu
- Slot 4: resi → `packed`, slot di-release → kosong abu-abu

Slot kosong nanti dipakai operator setup resi baru yang datang berikutnya.

### Step 9 — Akhir Hari

Admin lihat throughput di `/admin`:
```
2026-05-06 14:00 — 25 resi packed
2026-05-06 15:00 — 42 resi packed
...
```

Aging report: ada SKU 9999 di Wadah 3 Slot 7 (umur 30 jam).
Admin cek fisik — ternyata SKU ini batch lama yang tidak di-order
hari ini. Manual: ambil dari buffer, simpan di rak gudang.

---

## 8. Format Barcode & SKU

### Format Barcode di Plastik

**Format utama HOG**: numeric ID stiker desain saja.

```
445
1446
8888
```

**Format alternatif** (kalau HOG rollout dengan sequence):

```
{ID}-{VARIAN}PCS-{SEQ}
```

Contoh: `1446-10PCS-0001`, `431-50PCS-0042`, `8888-20PCS-9999`.

Parser di `utils.py:parse_barcode` terima keduanya, return `numeric_id` (string).

> 1 plastik = 1 pack 10pcs (standar HOG). Varian (10/20/50/100) **bukan**
> properti barcode plastik — itu properti SKU di kertas resi. Sistem
> auto-hitung berapa pack dibutuhkan per resi (mis. varian 50pcs = 5 pack).

### Format SKU

Sama dengan format SKU di sheet `LIST_PESANAN` existing:

```
{ID}-{NAMA-DESAIN}-{VARIAN}pcs
```

Contoh: `1446-RETRO-10pcs`, `431-FLOWER-50pcs`.

`utils.py:parse_sku` extract `(numeric_id, pcs_per_paket)` — sama
persis dengan logic di `app.py:533`.

> Modul ini **internal-store** SKU sebagai `{ID}-{VARIAN}PCS` (uppercase,
> tanpa nama desain) untuk match konsistensi antara barcode dan
> resi_item. Conversi dari format full ke internal dilakukan di
> `utils.derive_sku_full`.

---

## 9. Konfigurasi Lanjutan (Env Vars)

Semua konfigurasi di `config.py` bisa di-override via env var
`PACKING_ROUTER_<KEY>`:

```powershell
$env:PACKING_ROUTER_DEFAULT_WADAH_COUNT=8
$env:PACKING_ROUTER_SLOTS_PER_WADAH=12
$env:PACKING_ROUTER_SLOTS_PER_WAVE=60
$env:PACKING_ROUTER_BUFFER_AGING_HOURS=12
$env:PACKING_ROUTER_SLOT_KUNING_TIMEOUT_MIN=10
$env:PACKING_ROUTER_ALLOW_BUFFER_OVERFLOW=true
$env:PACKING_ROUTER_OVERFLOW_TRIGGER_COUNT=15
$env:PACKING_ROUTER_UNDO_WINDOW_SECONDS=60
$env:PACKING_ROUTER_WAVE_NEXT_THRESHOLD_PCT=85
$env:PACKING_ROUTER_DB_PATH="C:\custom\path\packing.db"
$env:PACKING_ROUTER_WEB_HOST=0.0.0.0
$env:PACKING_ROUTER_WEB_PORT=5050
$env:PACKING_ROUTER_WEB_DEBUG=true
$env:PACKING_ROUTER_LIST_PESANAN_SHEET=LIST_PESANAN
$env:PACKING_ROUTER_DATA_SALES_SHEET=DATA_SALES
$env:PACKING_ROUTER_SQLITE_BUSY_RETRY_COUNT=5
$env:PACKING_ROUTER_SQLITE_BUSY_RETRY_BASE_MS=100
```

Set sebelum launch. Untuk persistent, simpan di `start_packing.bat`
custom:

```bat
@echo off
set PACKING_ROUTER_DEFAULT_WADAH_COUNT=8
set PACKING_ROUTER_WEB_PORT=5050
cd /d "%~dp0"
python -m packing_router.web.app
```

> Default `DEFAULT_WADAH_COUNT=5` hanya berlaku saat **first init**.
> Setelah DB punya wadah, env var ini diabaikan — tambah wadah pakai
> tombol di `/admin`.

---

## 10. Aging Report (Cron)

Plastik yang nyangkut di buffer > 24 jam = sinyal masalah (resi batal,
SKU mismatch, atau workflow stuck). Set cron harian untuk cek otomatis.

### Manual Run

```powershell
python -m packing_router.cron.aging_check
```

Output kalau ada aging:
```
[2026-05-07 08:00:00] AGING ALERT — 2 buffer slot:
  Wadah 3 Slot 7 | SKU 9999-50PCS | 5 plastik | umur 30.5h | first_at 2026-05-05 ...
  Wadah 1 Slot 4 | SKU 7777-10PCS | 3 plastik | umur 26.0h | first_at 2026-05-06 ...
```

Exit code: 0 = OK (no aging), 1 = ada aging.

Setiap aging item juga di-log ke `event_log` (event_type=`alert`,
kind=`aging`) untuk audit trail.

### Schedule via Windows Task Scheduler

1. Buka Task Scheduler → Create Basic Task
2. Trigger: Daily, jam 08:00
3. Action: Start a program
   - Program: `python.exe` (atau full path)
   - Arguments: `-m packing_router.cron.aging_check`
   - Start in: `D:\Project\Bot Stiker V2`
4. (Optional) Action tambahan: kirim email kalau exit code != 0

> Channel notifikasi real (Slack/email/dll) belum di-include. Modul
> hanya print + log. Wrap dengan PowerShell script kalau butuh notif:
>
> ```powershell
> python -m packing_router.cron.aging_check 2>&1 | Out-File aging.log
> if ($LASTEXITCODE -ne 0) { Send-MailMessage ... }
> ```

---

## 11. Edge Cases & Troubleshooting

### Buffer penuh

**Gejala**: scan operator tolak dengan alert "Buffer penuh".

**Penyebab**: semua wadah aktif, semua slot terpakai, tidak ada slot
SKU sama yang bisa ditumpuk.

**Solusi cepat**:
1. Tambah wadah baru via `/admin` → tombol "+ Tambah Wadah"
2. ATAU tunggu harvester habiskan task pending di queue

### Slot SKU sticky overflow

**Gejala**: 1 SKU sudah punya banyak plastik (≥ `OVERFLOW_TRIGGER_COUNT`,
default 10) dan masih ada plastik baru SKU sama mau masuk.

**Default behavior** (`ALLOW_BUFFER_OVERFLOW=True`):
- Sistem assign slot ke-2 dengan `is_overflow_of` = primary slot id
- Display: `LETAKKAN KE WADAH X SLOT Y (overflow dari WADAH A SLOT B)`
- Find next time tetap ke primary

**Kalau set `False`**:
- Sistem akan tolak dengan `BufferFullError`
- Operator harus tunggu plastik di slot SKU ini di-harvest dulu

### Mis-scan operator

**Solusi**: klik tombol Undo di `/operator/scan` dalam 30 detik.
Lewat itu, undo akan tolak.

### Harvester scan mismatch

**Gejala**: scan pickup atau dropoff tampil error merah. State tidak
berubah (safe).

**Cek**:
- Pickup: barcode plastik sesuai task pending di queue?
- Dropoff: slot # match dengan task target?
- Jangan-jangan task sudah di-pickup harvester lain (cek queue di-refresh)

### Resi di-cancel mid-flow

Klik Cancel di card slot di `/slot-aktif`. Konfirmasi yes → sistem
auto-route plastik balik ke buffer (atau ke resi aktif lain yang
butuh SKU sama).

> Reverse-able: tidak ada. Cancel sudah final. Kalau masih butuh,
> harus re-import resi-nya manual.

### Race condition (2 operator scan SKU sama)

Sistem pakai `BEGIN IMMEDIATE TRANSACTION` SQLite — second writer
ngantri di lock. Kalau terjadi `SQLITE_BUSY`, retry 3× dengan
exponential backoff (50ms, 100ms, 200ms). Sangat jarang user lihat
error ini di production load (1.500 scan/hari = ~1 scan tiap 30 detik
average).

### Database locked (rare)

**Gejala**: error `sqlite3.OperationalError: database is locked`
saat scan, padahal sudah retry.

**Penyebab**: process lain (mis. SQLite browser GUI) megang lock
exclusive.

**Solusi**: tutup tool yang nge-lock DB, atau tingkatkan retry count:
```powershell
$env:PACKING_ROUTER_SQLITE_BUSY_RETRY_COUNT=10
```

### App tidak start — `ModuleNotFoundError: No module named 'flask'`

Install dependencies:
```powershell
pip install -r packing_router\requirements.txt
```

### App tidak start — `ImportError: gspread`

Hanya butuh kalau pakai integrasi Google Sheet (import batch atau
pack log). Untuk testing/dev tanpa Sheet, bisa skip — modul lain
tetap jalan.

### Reset DB (development only)

```powershell
Remove-Item hasil\packing_router.db*
python -m packing_router.web.app
```

DB akan auto-create ulang dengan default 5 wadah. **Hati-hati**: data
historis hilang.

---

## 12. Testing

### Run all tests

```powershell
pip install pytest
python -m pytest packing_router\tests
```

### Run specific suite

```powershell
python -m pytest packing_router\tests\test_buffer.py -v
python -m pytest packing_router\tests\test_race_condition.py -v
```

### 42 tests cover

| Test file | Coverage |
|---|---|
| `test_utils.py` | parse_sku, parse_barcode, derive_sku_full |
| `test_buffer.py` | assign, find, increment/decrement, overflow, add_wadah, status |
| `test_scan_handler.py` | 3 routing actions + transition resi → complete |
| `test_resi_setup.py` | setup resi, import LIST_PESANAN, wave transition 90% |
| `test_harvester.py` | pickup, dropoff, mismatch errors |
| `test_maintenance.py` | undo (3 actions), cancel resi, pack resi |
| `test_race_condition.py` | 2 thread scan SKU sama tanpa duplicate slot |

### Regression check (existing repo)

Dari root repo:
```powershell
python test_qc_parser.py        # 20 tests existing — semua harus tetap pass
python -c "import app"           # app.py tetap importable
python -c "import qc_stasiun"    # qc_stasiun.py tetap importable
python -c "import run_qc"        # run_qc.py tetap importable
```

Verifikasi tidak ada file root yang dimodifikasi:
```powershell
git diff --stat HEAD -- app.py qc_stasiun.py run_qc.py qc_seed.py duplicate_files.py updater.py code.gs test_qc_parser.py requirements.txt config.json README.md start.bat start_qc.bat install.bat
```

Output harus empty.

---

## 13. Struktur File

```
packing_router/
├── __init__.py
├── README.md                     # Dokumen ini
├── requirements.txt              # Flask, Jinja2, gspread, pytest
├── run.bat                       # Launcher Flask
├── config.py                     # Konstanta + override env
├── db.py                         # SQLite connection, schema, transaction
├── exceptions.py                 # BufferFullError, HarvesterMismatchError, dll
├── models.py                     # ScanResult, BufferLocation, dll dataclass
├── utils.py                      # parse_sku (port app.py:533), parse_barcode
├── scan_handler.py               # Event 1 — handle_scan_plastik
├── buffer.py                     # assign, find, add_wadah, overflow
├── resi_setup.py                 # Event 2 + import sheet + wave transition
├── harvester.py                  # pickup_scan, dropoff_scan
├── reports.py                    # slot_aktif_status, queue, aging
├── maintenance.py                # cancel_resi, undo, pack_resi
├── sheets_log.py                 # append_pack_log → DATA_SALES
├── cron/
│   ├── __init__.py
│   └── aging_check.py            # Cron harian buffer aging
├── web/
│   ├── __init__.py
│   ├── app.py                    # Flask routes (4 view + actions)
│   ├── static/
│   │   └── style.css
│   └── templates/
│       ├── base.html
│       ├── operator_scan.html
│       ├── harvester_queue.html
│       ├── slot_aktif.html
│       ├── admin.html
│       └── partials/
│           ├── _alert.html
│           ├── _scan_result.html
│           ├── _harvester_tasks.html
│           ├── _harvester_pickup_ok.html
│           ├── _harvester_dropoff_ok.html
│           ├── _harvester_alert.html
│           ├── _slot_grid.html
│           ├── _admin_buffer.html
│           └── _admin_import.html
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── test_utils.py
    ├── test_buffer.py
    ├── test_scan_handler.py
    ├── test_resi_setup.py
    ├── test_harvester.py
    ├── test_maintenance.py
    └── test_race_condition.py
```

---

## 14. Integrasi dengan Sistem Existing

### File root yang DIBACA (read-only)

| File | Tujuan |
|---|---|
| `config.json` | Auth Google Sheet — `json_path` dan `gsheet_url` |

### File root yang TIDAK disentuh

`app.py`, `qc_stasiun.py`, `run_qc.py`, `qc_seed.py`,
`duplicate_files.py`, `updater.py`, `code.gs`, `test_qc_parser.py`,
`requirements.txt`, `README.md`, `start.bat`, `start_qc.bat`,
`install.bat`.

### Logic yang di-port (BUKAN di-import)

- `parse_sku` di `utils.py` adalah port dari method
  `BotApp.extract_numeric_id_and_pcs` di `app.py:533`. Logic regex
  identik. Alasan port (bukan import): method tersebut tightly-coupled
  ke Tkinter class instance, tidak bisa standalone.

### Google Sheet shared

| Sheet | Akses packing_router |
|---|---|
| `LIST_PESANAN` | Read-only — fetch saat import batch |
| `DATA_SALES` | Append-only — log saat resi di-pack |
| `DATABASE_STIKER` | Tidak diakses (modul ini fokus stiker, bukan stok gudang) |
| `LOG_KELUAR` | Tidak diakses |
| `STOK_OPNAME` | Tidak diakses |

### Database

| DB | Akses packing_router |
|---|---|
| `hasil/packing_router.db` | **Owner** — full read/write |
| `hasil/qc_data.db` | **Tidak diakses** — DB QC station existing |

### Update via auto-updater

Modul `packing_router/` belum di-include di `updater.py`
`FILES_TO_UPDATE`. Untuk auto-sync dari GitHub, tambahkan path
folder ke `updater.py` (FILE INI dikecualikan dari aturan "tidak
modifikasi" karena memang harus di-edit untuk include modul baru) —
atau update manual via `git pull`.

> Kalau mau pakai auto-updater dengan ini, modifikasi `updater.py`
> bukan otomatis dilakukan oleh modul ini — itu keputusan eksplisit
> admin.

---

## 15. FAQ

**Q: Apa beda antara Slot Aktif dan Buffer?**
A: Slot Aktif = mapping 1:1 ke resi yang sedang aktif (50 slot).
Buffer = penyimpanan sementara plastik yang belum match resi aktif
(N wadah × 10 slot, SKU-sticky).

**Q: Kenapa buffer SKU-sticky? Kenapa tidak FIFO biasa?**
A: Karena setiap resi butuh beberapa pcs SKU sama (mis. 50 pcs varian
10pcs = 5 plastik). Kalau plastik tersebar di banyak slot, harvester
harus mondar-mandir ambil dari banyak tempat. SKU-sticky = 1 trip
ambil semua dari 1 slot.

**Q: 1 SKU > 10 plastik di buffer, bagaimana?**
A: Default `ALLOW_BUFFER_OVERFLOW=True` → assign slot ke-2 dengan
`is_overflow_of` = slot pertama. Display: "Wadah 2 Slot 5 (overflow
dari Wadah 1 Slot 7)". `find_buffer_slot_for_sku` always return
primary, jadi tumpukan tetap di slot pertama selama belum penuh.

**Q: Berapa wadah maksimal? Berapa slot per wadah maksimal?**
A: Tidak ada hard limit di code. Realistic constraint = ruang fisik
gudang. Tambah wadah dynamic via `/admin` (tidak perlu restart).

**Q: Apakah modul ini menggantikan `run_qc.py`?**
A: TIDAK. `run_qc.py` (Stasiun QC) tetap jalan terpisah — quality gate
sebelum shipping. Packing Router fokus di logistic plastik dari
weeding → packing. QC verifikasi item benar setelah packing.

**Q: Apakah modul ini menggantikan `app.py`?**
A: TIDAK. `app.py` tetap untuk tim print (sortir + cetak PDF). Modul
ini fokus untuk tim sortir (output weeding) + harvester + packer.

**Q: Multi-station support?**
A: YES via Flask host `0.0.0.0` dan akses LAN. Tapi semua station
share 1 DB SQLite di server. Untuk scaling > 5 station, mungkin
perlu pindah ke Postgres (scope berikutnya).

**Q: Mobile-friendly?**
A: Layout responsive lewat CSS sederhana, tapi belum di-optimize untuk
mobile. Harvester yang pakai tablet/smartphone bisa, tapi kemungkinan
ada elemen yang kekecilan. Mobile-first redesign = scope berikutnya.

**Q: Apa harus barcode di plastik wajib?**
A: Ya. Skema dasar `{ID}-{VARIAN}PCS-{SEQ}` (e.g. `1446-10PCS-0001`).
Manual entry juga bisa (ketik di field), tapi defeat tujuan otomasi.

**Q: Apa terjadi kalau Google Sheet down?**
A: Modul tetap jalan untuk scan dan harvester flow (offline-capable).
Yang gagal cuma:
- `import_from_list_pesanan_sheet` (tidak bisa fetch batch baru)
- `append_pack_log` (log DATA_SALES tertunda — bisa retry manual)

**Q: Berapa lama 1 wave habis?**
A: Realistic 30-60 menit untuk 50 resi (rata-rata 2-3 SKU per resi =
100-150 plastik per wave). Di throughput 1.500 plastik/hari = ~6
menit per plastik (operator + harvester + packer paralel).

**Q: Apa harvester role bisa digabung dengan operator/packer?**
A: Bisa, kalau orangnya cukup. Modul tidak enforce role separation.
1 orang bisa scan operator + buka tab harvester + pack — tapi efisiensi
turun karena harus pindah-pindah konteks.

**Q: Stok gudang yang sudah ditandai stabilo, gimana?**
A: **Sudah disupport** dengan tombol `📦 Gudang` per-SKU di kartu slot
di dashboard. Setelah resi di-setup ke slot, operator klik tombol
`📦 Gudang` di samping SKU yang ada stabilo-nya → sistem set
`prefilled_qty = quantity_ordered` dan **tidak minta plastik fisik**
untuk SKU itu (juga cancel pending harvester task SKU itu). SKU pindah
ke section "Stok Gudang ✓" (info di slot card). Kalau ini SKU terakhir
yang kurang, resi auto-complete (hijau, siap pack). Tombol `↺ Lepas`
ada di section "Stok Gudang ✓" untuk un-mark kalau salah klik.

---

## Pengembangan Berikutnya (Phase 2+)

- Integrasi auto-pack saat QC approve (`run_qc.py` ↔ Packing Router)
- Sub-tool generate barcode plastik (cetak QR/Code128 batch)
- Multi-station scan parallel (mungkin pindah ke Postgres)
- Mobile-first redesign untuk harvester (PWA)
- Integrasi stok gudang (auto-tag SKU yang sudah di-fulfill dari rak)
- Notifikasi real-time (Slack webhook untuk aging alert)
- Pass rate analytics per harvester / per shift
- Auto-updater integration (sync modul dari GitHub)

---

## Lisensi & Kontak

Internal tool PT Heavy Object Group, unit Stickitup. Repository:
[github.com/dennypa77/sortir_stiker_desain](https://github.com/dennypa77/sortir_stiker_desain).

Untuk bug report atau request enhancement, hubungi tim engineering
atau buat issue di GitHub repo.
