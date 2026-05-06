# Sortir Stiker & Stasiun QC HOG

Aplikasi internal **PT Heavy Object Group (HOG)** unit bisnis **Stickitup** untuk
otomatisasi proses produksi stiker custom die-cut. Mencakup tiga stasiun kerja:
**tim print**, **tim gudang**, dan **tim packing/QC** — semua di-orkestrasikan
dari satu codebase Python + Google Sheet bersama.

---

## Daftar Isi

1. [Latar Belakang Bisnis](#latar-belakang-bisnis)
2. [Komponen Aplikasi](#komponen-aplikasi)
3. [Persyaratan Sistem](#persyaratan-sistem)
4. [Instalasi](#instalasi)
5. [Konfigurasi Awal](#konfigurasi-awal)
6. [Aplikasi Sortir & Cetak (`app.py`)](#aplikasi-sortir--cetak-apppy)
7. [Stasiun QC (`run_qc.py`)](#stasiun-qc-run_qcpy)
8. [Apps Script Google Sheet (`code.gs`)](#apps-script-google-sheet-codegs)
9. [Workflow Harian Tim](#workflow-harian-tim)
10. [Struktur File Project](#struktur-file-project)
11. [Auto-Updater](#auto-updater)
12. [Database SQLite QC](#database-sqlite-qc)
13. [Troubleshooting](#troubleshooting)

---

## Latar Belakang Bisnis

HOG mengoperasikan unit Stickitup yang fokus jualan stiker desain kustom di
marketplace e-commerce, dengan ~8.000 SKU di katalog. Produknya dijual dalam
varian **10pcs**, **20pcs**, **50pcs**, dan **100pcs** per resi.

**Standardisasi gudang:** semua stok disimpan dalam pack 10pcs. Varian
20/50/100 pcs di marketplace = ambil 2/5/10 pack dari rak. Jadi rumus
sederhana: `packs_needed = total_pcs ÷ 10`.

**Masalah yang diselesaikan:**
- Tim print: pesanan Excel BigSeller dengan ribuan baris harus di-sort + cetak
  PDF master + skip yang sudah ada di stok gudang.
- Tim gudang: cek cepat apakah resi tertentu bisa dipenuhi 100% dari stok atau
  perlu cetak ulang sebagian.
- Tim packing/QC: verifikasi setiap polymailer berisi SKU yang benar sebelum
  di-seal — ribuan desain dengan banyak yang mirip rentan salah ambil pada
  volume tinggi.

---

## Komponen Aplikasi

| Komponen | File | Fungsi |
|---|---|---|
| **App utama** | `app.py` | Window CustomTkinter dengan 4 tab: koneksi sheet, setup path, eksekusi sortir/cetak, scanner resi gudang |
| **Stasiun QC** | `run_qc.py` + `start_qc.bat` | Window standalone untuk QC packing — scan resi & verifikasi SKU isi polymailer |
| **CLI seed (dormant)** | `qc_seed.py` | Manajemen operator QC via command-line (saat ini fitur login operator dihilangkan, tapi CLI tetap tersedia) |
| **Apps Script** | `code.gs` | Code Google Sheet — upload BigSeller, opname, populate sheet `LIST_PESANAN` |
| **Auto-updater** | `updater.py` | Sync file Python dari GitHub raw setiap kali start |
| **Modul utility** | `qc_stasiun.py`, `duplicate_files.py` | Module support untuk QC dan dedup file |
| **Launcher** | `start.bat`, `start_qc.bat`, `install.bat` | Batch script Windows |

Semua aplikasi GUI dibangun pakai **CustomTkinter** (Tkinter modern) — desktop
aplikasi Windows, bukan web app.

---

## Persyaratan Sistem

- **OS:** Windows 10 / 11 (script pakai `winsound` & `os.startfile` Windows-spesifik)
- **Python:** 3.10 atau lebih baru (dites di Python 3.13)
- **Internet:** untuk Google Sheets API + auto-updater
- **Storage:** ~50 MB untuk app + ratusan MB untuk hasil cetak PDF

### Library Python (di `requirements.txt`)

```
customtkinter
gspread
google-auth
PyPDF2
openpyxl
gTTS
pygame
```

Semua dependency standar pip, tidak ada hardware-spesifik selain
`winsound` (builtin Windows) dan `pygame` untuk audio playback.

---

## Instalasi

### 1. Clone repo

```bash
git clone https://github.com/dennypa77/sortir_stiker_desain
cd sortir_stiker_desain
```

### 2. Install dependency

Double-click `install.bat`, atau dari terminal:

```bash
pip install -r requirements.txt
```

### 3. Siapkan Google Service Account JSON

Buat service account di [Google Cloud Console](https://console.cloud.google.com/),
enable **Google Sheets API**, generate JSON credential, lalu **share** Google
Spreadsheet target dengan email service account tersebut (role Editor).

Simpan file JSON di lokasi aman — nanti path-nya akan diisi di app.

### 4. Siapkan Google Spreadsheet

Buat spreadsheet baru dengan sheet-sheet berikut (case-sensitive):

| Sheet | Header (row 1) |
|---|---|
| `DATABASE_STIKER` | ID Master \| Nama Desain \| ... \| Stok (kolom A & H) |
| `LOG_KELUAR` | Tanggal \| ID Master \| Pcs Keluar \| Keterangan |
| `DATA_SALES` | Tanggal \| ID Master \| Total Pcs |
| `STOK_OPNAME` | (opsional, untuk sync stok fisik) |
| `LIST_PESANAN` | (header otomatis ter-set saat upload BigSeller pertama) |

### 5. Paste Apps Script

Buka spreadsheet → **Extensions** → **Apps Script** → hapus isi default →
paste seluruh isi file `code.gs` dari repo → save (Ctrl+S) → reload spreadsheet.
Menu `Kelola Gudang` akan muncul di toolbar.

---

## Konfigurasi Awal

Setelah instalasi, jalankan `app.py` (atau `start.bat`) untuk konfigurasi
pertama kali:

### Tab "Koneksi Gudang"
1. Paste **URL Google Spreadsheet** (atau cuma ID).
2. Klik **Cari JSON** → pilih file service account `.json`.
3. Klik **Test & Simpan Koneksi** — kalau berhasil tampil hijau "Berhasil
   Terhubung & Sheet Ditemukan".

Konfigurasi disimpan otomatis ke `config.json` di root project.

### Tab "Pengaturan File"
- **Data Pesanan (.xlsx):** path ke Excel BigSeller export untuk tim print.
- **Folder Master PDF:** folder berisi file desain `.pdf` (filename diawali
  ID numerik, contoh `431-RETRO.pdf`).
- **Hot Folder (Hasil):** folder output cetak. Di-clear setiap kali "Mulai
  Proses" (kecuali subfolder `log/` yang tetap diarsipkan).

Klik **Simpan Path**.

---

## Aplikasi Sortir & Cetak (`app.py`)

Window utama dengan 4 tab. Run via `python app.py` atau `start.bat`.

### Tab 1 — Koneksi Gudang
Auth ke Google Sheet pakai service account JSON. Cuma perlu di-set sekali —
selanjutnya app baca dari `config.json` setiap launch.

### Tab 2 — Pengaturan File
Browser file/folder untuk path Excel pesanan, folder master PDF, hot folder
output. Path persisted di `config.json`.

### Tab 3 — Eksekusi & Log

Tab utama untuk **tim print**. Langkah:

1. **Opsi Pra-Eksekusi**:
   - Checkbox **"Tulis otomatis ke LOG_KELUAR & kurangi stok gudang"**:
     - **AKTIF** (default): stok yang terpenuhi gudang dicatat ke sheet
       `LOG_KELUAR`, stok berkurang otomatis.
     - **NONAKTIF**: tidak menulis ke sheet, operator harus potong stok manual.

2. **MULAI PROSES**:
   - Aplikasi baca file Excel pesanan, sort by SKU numeric_id ascending.
   - Untuk tiap pesanan:
     - Cek stok di sheet `DATABASE_STIKER`.
     - Jika cukup → catat ke `LOG_KELUAR` (jika opsi aktif), tampilkan di
       tab "Daftar Ready Gudang".
     - Jika tidak cukup → cari file PDF di Master Folder, ekstrak halaman
       sesuai jumlah dibutuhkan, tulis ke Hot Folder dalam batch:
       - **Batch 10** untuk varian 10/20pcs (50 pcs per lembar).
       - **Batch 50** untuk varian 50/100pcs (100 pcs per lembar kalau ada
         versi optimal, default 50).
       - Tiap batch maks 20 file → buat sub-folder `Batch_1`, `Batch_2`, dst.

3. **Log:**
   - Tab "Log Eksekusi" — running log dengan color coding:
     - **Hijau**: ambil dari gudang full
     - **Cyan**: cetak murni (stok tidak cukup)
     - **Kuning**: parsial / peringatan
     - **Merah**: error / file master tidak ditemukan
   - Tab "Daftar Ready Gudang" — daftar resi yang bisa dipenuhi gudang.

4. **Output:**
   - PDF cetak di Hot Folder, dikelompokkan per batch.
   - File log Excel di `hot_folder/log/{berhasil,gagal,peringatan}/`
     dengan timestamp.
   - Klik **Buka Folder Output** untuk akses cepat.

### Tab 4 — Scanner Resi Gudang

Tab untuk **tim gudang**. Workflow:

1. Klik **Muat/Perbarui Data Scanner** — confirm "sudah upload data sales?"
   → app fetch DATABASE_STIKER + Excel pesanan ke memory.
2. Arahkan kursor ke field scan.
3. Scan barcode resi (atau ketik manual) → tekan Enter.
4. App tampilkan tiap SKU di resi tersebut + status:
   - "✔ MENCUKUPI (Stok: N)" — hijau, audio "ready"
   - "✗ STOK TIDAK CUKUP" — merah, audio "kosong"
5. Tim gudang langsung tahu: ambil dari rak (full) atau setor ke tim print
   untuk cetak ulang.

Audio feedback pakai gTTS (perlu internet) — kata "ready" / "kosong" / "Resi
tidak ditemukan" / "Semua kosong".

---

## Stasiun QC (`run_qc.py`)

Window terpisah untuk **tim packing/QC**. Quality gate antara packing dan
shipping — verifikasi setiap polymailer berisi SKU yang benar sebelum
di-seal.

### Cara Run

Prerequisite: koneksi sheet sudah dites di `app.py` (config.json sudah ada).

```bash
python run_qc.py
```

Atau double-click `start_qc.bat` (jalankan auto-updater dulu, lalu QC window).

### Flow Operator

1. Window terbuka langsung di state **Idle** — kursor auto-focus di field
   scan resi.
2. Polymailer di tangan (belum di-seal). Scan barcode resi → field di-clear,
   app fetch data resi dari sheet `LIST_PESANAN`.
3. Window pindah ke state **Resi Loaded** — checklist 3-kolom muncul,
   tampilkan semua SKU yang ada di resi tersebut.
4. Untuk tiap SKU di checklist:
   - Stiker: scan **1 pack** saja → status berubah jadi "✔ SKU SESUAI".
     Reminder kekurangan pack ditampilkan bold merah di card kalau jumlah
     scan < target pack (informatif, bukan enforcement).
   - Non-stiker (banner/stamp/dll): klik tombol **Visual Confirm** di card.
5. Setelah semua SKU verified, tombol **APPROVE & SEAL** aktif → klik →
   row di sheet `LIST_PESANAN` ter-update kolom `Status=qc_approved`,
   `QC_Operator=<nama PC>`, `QC_Completed_At=<timestamp>`.
6. Window kembali ke Idle untuk resi berikutnya.

Kalau ada masalah (SKU salah, kekurangan, dll): klik **REJECT** → pilih
reason + notes → status di sheet `qc_rejected`.

### Tombol-tombol

| Tombol | Fungsi |
|---|---|
| **APPROVE & SEAL** | Mark resi sebagai QC approved. Aktif setelah semua SKU verified. |
| **REJECT** | Reject resi dengan dropdown alasan (SKU salah / kurang / lebih / pack rusak / item non-stiker tidak ada / lainnya) + textarea notes |
| **Manual Entry SKU** | Buka dialog input SKU manual (untuk barcode rusak) — tanpa PIN |
| **Batalkan, Kembali** | Tutup sesi tanpa approve/reject — sesi tetap tersimpan sebagai `in_progress`, bisa di-resume dengan scan resi yang sama |
| **Refresh Data** (header) | Fetch ulang data dari sheet `LIST_PESANAN` |

### Layout Checklist (3-kolom Grid)

Tiap card berisi:
- Status icon (◯ pending / ✔ verified / □ menunggu visual confirm)
- ID besar (contoh "ID 1446")
- SKU full kecil di bawah (contoh "1446-20PCS")
- Status text bold (BELUM DI-SCAN / ✔ SKU SESUAI / dll)
- Detail counter (Butuh N pack • scan Mx)
- **KURANG N PACK** bold merah — kalau scan < target

Layout 3 kolom supaya banyak item muat dalam 1 layar tanpa scroll banyak.

### Behavior Scan

| Skenario | Reaksi |
|---|---|
| Scan SKU benar pertama kali | Beep tinggi (1500Hz), card jadi "✔ SKU SESUAI" |
| Scan SKU benar lagi (sudah verified) | Beep tinggi, counter naik, status tetap SESUAI |
| Scan SKU yang tidak ada di resi | Beep rendah (400Hz), label merah "✗ SKU X TIDAK ADA di resi", TTS "SKU tidak sesuai" |
| Manual entry | Sama dengan scan biasa, tapi di-tag `manual_entry` di activity log |

### Edge Cases

- **Resi tidak ditemukan di sheet** → label merah "Resi tidak ditemukan" + TTS, harap pastikan tim gudang sudah upload BigSeller export hari itu.
- **Resi sudah pernah approved** → label kuning warning, tidak boleh di-QC ulang.
- **Sesi pending (window crash / restart)** → scan resi yang sama lagi → app auto-resume dari progress sebelumnya.
- **Sheet update gagal** → status QC tetap tersimpan di SQLite lokal, dialog error muncul. Klik Refresh Data lalu coba lagi.

### Stats Hari Ini

Footer window menampilkan: total resi diproses, jumlah approved, rejected,
pass rate, dan jumlah pending di sheet. Update otomatis setelah tiap
approve/reject.

---

## Apps Script Google Sheet (`code.gs`)

Apps Script v7.0 di-paste ke editor Apps Script di Google Spreadsheet
(Extensions → Apps Script). Menyediakan menu `Kelola Gudang` di toolbar
spreadsheet dengan 4 item:

### 1. Upload Data Sales (Excel/CSV)

Tim gudang upload sekali per hari (atau per batch). Apps Script:
1. Parse file Excel/CSV (deteksi kolom by header keyword: resi/tracking/awb,
   sku/kode, jumlah/qty/pcs, tanggal/date — fallback ke positional).
2. Append ke sheet `DATA_SALES` per (tanggal, ID master, total pcs) untuk
   trend analytics.
3. **Jika file punya kolom resi**, sekaligus append ke sheet `LIST_PESANAN`
   sebagai batch baru (auto-generate `Batch_ID` format `YYYY-MM-DD-Bn`).
4. `LIST_PESANAN` jadi single source of truth untuk Stasiun QC.

Dialog summary: "✅ N pesanan masuk LIST_PESANAN sebagai batch
2026-05-06-B2"

### 2. Jalankan Sinkronisasi Opname

Sinkronisasi stok fisik dari sheet `STOK_OPNAME` ke kolom Adj Opname di
`DATABASE_STIKER`. Pakai logic delta = fisik − stok berjalan, hard sync
permanen.

### 3. Reset & Ringkas Data Sales

Aggregate `DATA_SALES` per (tanggal, ID master), drop row > 30 hari. Maintenance
manual untuk jaga ukuran sheet.

### 4. Hapus LIST_PESANAN > 10 Hari

Maintenance manual untuk hapus row pesanan yang `Uploaded_At` lebih dari 10
hari yang lalu. Dialog konfirmasi sebelum hapus.

### Schema sheet `LIST_PESANAN`

| Kolom | Type | Diisi oleh | Contoh |
|---|---|---|---|
| Batch_ID | string | Apps Script | `2026-05-06-B1` |
| Uploaded_At | datetime | Apps Script | `2026-05-06 09:15:00` |
| Nomor_Resi | string | Apps Script (dari file) | `SPXID060155202261` |
| SKU | string | Apps Script (dari file) | `431-RETRO-10PCS` |
| Jumlah | int | Apps Script (dari file) | `1` |
| Marketplace | string | Auto-detect dari prefix resi | `Shopee Express` |
| Status | string | QC app | `pending` / `qc_approved` / `qc_rejected` |
| QC_Operator | string | QC app | `<COMPUTERNAME>` |
| QC_Completed_At | datetime | QC app | `2026-05-06 14:22:30` |
| QC_Notes | string | QC app | reject reason + notes |

Header otomatis ter-set saat upload pertama kalau sheet kosong.

### Auto-detect Marketplace

Berdasarkan prefix nomor resi:

| Prefix | Marketplace |
|---|---|
| SPXID, SPX | Shopee Express |
| SHPE, SHP | Shopee |
| JNT, JT | J&T Express |
| JNE | JNE |
| TKP | Tokopedia |
| IDE | ID Express |
| SAP | SAP Express |

Prefix lain → `Unknown`.

---

## Workflow Harian Tim

Multi-batch concurrent — 3 tim bisa bekerja paralel di batch yang berbeda.

```
                  ┌────────────────────────────────────┐
                  │     Google Spreadsheet             │
                  │  ┌──────────────────────────────┐  │
                  │  │ DATA_SALES (trend)           │  │
                  │  │ DATABASE_STIKER (stok)       │  │
                  │  │ LOG_KELUAR (audit)           │  │
                  │  │ LIST_PESANAN (QC source)     │  │
                  │  └──────────────────────────────┘  │
                  └─────┬─────────┬─────────┬──────────┘
                        │         │         │
              ┌─────────▼──┐ ┌────▼──┐ ┌────▼──────────┐
              │ Tim Gudang │ │ Tim   │ │ Tim Packing/  │
              │ Upload &   │ │ Print │ │ QC            │
              │ Scanner    │ │       │ │               │
              └────────────┘ └───────┘ └───────────────┘
```

### Pagi (Tim Gudang)

1. Export pesanan dari BigSeller (Excel/CSV).
2. Buka spreadsheet → menu `Kelola Gudang` → **Upload Data Sales** → pilih
   file → submit.
3. Apps Script append ke `DATA_SALES` (trend) + `LIST_PESANAN` (untuk QC).
4. Drop file `data.xlsx` ke folder shared (Google Drive sync atau local) yang
   dipakai tim print.

### Siang (Tim Print)

1. Buka `app.py` → tab "Eksekusi & Log".
2. Pastikan opsi LOG_KELUAR sesuai kebutuhan (default AKTIF).
3. Klik **MULAI PROSES**.
4. App auto-sort + cetak PDF batch ke Hot Folder.
5. Tim print eksekusi cetak, isi pesanan ke polymailer (belum seal).

### Siang–Sore (Tim Gudang)

1. Buka `app.py` → tab "Scanner Resi Gudang".
2. Klik **Muat/Perbarui Data Scanner**.
3. Scan tiap resi yang masuk dari produksi → app cek apakah stok cukup atau
   harus cetak ulang.

### Sore (Tim Packing/QC)

1. Buka `start_qc.bat` (atau `python run_qc.py`).
2. Window QC terbuka di state Idle.
3. Untuk setiap polymailer (belum seal):
   - Scan barcode resi → checklist muncul.
   - Scan tiap SKU stiker 1x untuk verifikasi.
   - Klik Visual Confirm untuk item non-stiker.
   - Klik APPROVE → seal polymailer → kirim.
4. Sheet `LIST_PESANAN` ter-update real-time per resi.

### Multi-Batch Skenario

- Batch B1 di-upload pagi, tim print garap dulu.
- Tim packing baru sampai batch B1 setelah jam 14.
- Batch B2 di-upload tim gudang jam 13 → append ke sheet, batch B1 yang
  belum di-QC tidak terganggu.
- Tim print bisa lanjut B2, tim packing tetap di B1.
- Sheet menyimpan semua batch concurrent — filter by `Batch_ID` atau
  `Status` untuk visibility.

---

## Struktur File Project

```
sortir_stiker_desain/
├── app.py                       # Aplikasi utama (4 tab)
├── duplicate_files.py           # Standalone script dedup file PDF
├── duplicate_files - stable.py  # Backup versi stabil
├── qc_stasiun.py                # Module QC (DB layer + UI window)
├── qc_seed.py                   # CLI seed operator (dormant)
├── run_qc.py                    # Standalone launcher Stasiun QC
├── test_qc_parser.py            # Unit test parser SKU
├── code.gs                      # Apps Script v7.0 (paste ke editor GS)
├── updater.py                   # Auto-updater dari GitHub raw
├── requirements.txt             # Pip dependencies
├── install.bat                  # Install dependencies
├── start.bat                    # Run updater + app.py
├── start_qc.bat                 # Run updater + run_qc.py
├── config.json                  # Konfigurasi user (gsheet_url, paths)
├── data.xlsx                    # File Excel pesanan (lokal)
├── README.md                    # Dokumen ini
├── .gitignore                   # Git exclusions
└── hasil/                       # Output folder (gitignored)
    ├── Batch 10/
    ├── Batch 50/
    ├── log/
    │   ├── berhasil/
    │   ├── gagal/
    │   └── peringatan/
    └── qc_data.db               # SQLite database QC
```

---

## Auto-Updater

`updater.py` jalan setiap kali `start.bat` atau `start_qc.bat` dieksekusi.
Dia fetch versi terbaru dari `https://raw.githubusercontent.com/dennypa77/sortir_stiker_desain/main/`
untuk file-file kunci:

- `app.py`
- `duplicate_files.py`
- `requirements.txt`
- `qc_stasiun.py`
- `qc_seed.py`
- `run_qc.py`

Kalau ada perbedaan dengan versi lokal → overwrite versi lokal. Kalau
gagal fetch (offline / 404) → pakai versi lokal saja, log warning.

Untuk menambah file ke daftar auto-update, edit `FILES_TO_UPDATE` di
`updater.py` lalu commit & push ke GitHub.

---

## Database SQLite QC

Stasiun QC menyimpan audit trail di `hasil/qc_data.db` (SQLite). Schema:

| Tabel | Isi |
|---|---|
| `qc_operators` | Daftar operator (saat ini dormant — login operator dihilangkan) |
| `qc_sessions` | Tiap sesi QC (1 sesi = 1 resi yang di-QC) — status, batch, timestamp, reject reason |
| `qc_session_progress` | Per-SKU progress dalam sesi — target pack, scan count, visual confirm |
| `qc_activity_log` | Semua event QC: scan_match, scan_mismatch, manual_entry, approve, reject, dll |

DB di-init otomatis saat `run_qc.py` first launch. Auto-migrate schema kalau
deteksi versi lama (operator_id NOT NULL → nullable). Backup file lama
disimpan di `hasil/qc_data.db.bak_<timestamp>`.

Untuk re-enable operator login nanti, restore dari git history commit
sebelum operator dihapus, plus seed ulang via `qc_seed.py`.

---

## Troubleshooting

### "Belum ada operator" / login screen muncul
Ini behavior versi lama. Update ke versi terbaru via `start.bat`. Versi
sekarang langsung masuk Idle tanpa login.

### `RuntimeError: main thread is not in main loop`
Sudah diperbaiki di commit `9749dc3`. Update via auto-updater.

### Sheet LIST_PESANAN tidak ditemukan
Buat sheet kosong bernama `LIST_PESANAN` di spreadsheet (case-sensitive).
Header akan auto-set saat tim gudang upload pertama via Apps Script.

### Apps Script tidak punya menu "Kelola Gudang"
- Pastikan `code.gs` sudah di-paste lengkap di Apps Script editor.
- Reload spreadsheet (browser refresh).
- Jika masih tidak muncul, buka Apps Script editor → `Run` function `onOpen`
  manual sekali (akan minta authorization).

### Connection refused / quota exceeded di gspread
Google Sheets API limit: 60 read req/min per user, 100 write req/min.
QC station tidak akan kena limit untuk kerja normal (1 read per scan
resi, 1 write per approve/reject). Kalau kena, tunggu 1 menit lalu klik
**Refresh Data**.

### File Excel pesanan tidak terbaca / kolom hilang
Pastikan format `data.xlsx` punya 3 kolom dengan header `Nomor Resi |
SKU | Jumlah` (case-sensitive). Apps Script lebih fleksibel (header
keyword detection), tapi `app.py` Tab 3 dan Tab 4 expect format ini.

### Audio feedback tidak bunyi
- `winsound.Beep`: bawaan Windows, harus jalan tanpa setup.
- `gTTS speak`: butuh internet aktif. Kalau offline, voice tidak keluar
  tapi visual feedback (warna + text) tetap jalan.

### Auto-updater gagal 404
Berarti file Python belum di-push ke GitHub. Kalau terjadi, beri tahu
admin yang punya akses repo.

### Stasiun QC reject "DB locked" / "PermissionError"
Migration error di Windows. Tutup semua proses Python, jalankan ulang
`run_qc.py`. Kalau persisten, hapus manual `hasil/qc_data.db` (data lama
sudah di-backup di `hasil/qc_data.db.bak_*`).

---

## Pengembangan Berikutnya (Phase 2+)

Yang sudah di-skip dari MVP, bisa di-tambah nanti kalau perlu:

- Pick-to-light di gudang (LED per rak per SKU)
- Multi-station QC paralel dengan dispatch resi otomatis
- Foto evidence otomatis saat reject (webcam capture)
- Integrasi BigSeller API langsung (skip upload manual)
- Print verification sticker (struk QC pass)
- Pass rate analytics per packer / per shift
- Re-enable operator login + supervisor PIN (kode dorman, tinggal restore)

---

## Lisensi & Kontak

Internal tool PT Heavy Object Group. Repository:
[github.com/dennypa77/sortir_stiker_desain](https://github.com/dennypa77/sortir_stiker_desain).

Untuk pertanyaan atau bug report, hubungi tim engineering atau buat issue di
GitHub repo.
