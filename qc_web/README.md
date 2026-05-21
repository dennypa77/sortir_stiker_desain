# Stasiun QC — Web (Flask + HTMX)

Frontend HTML untuk Stasiun QC, pengganti UI desktop (`qc_stasiun.py` /
`run_qc.py`). **Berjalan berdampingan** — file desktop tidak diubah. Logika
bisnis, database, dan koneksi Google Sheet di-*import* langsung dari
`qc_stasiun.py` (tanpa duplikasi).

## Menjalankan

```bash
pip install -r requirements.txt        # butuh flask (sudah ditambahkan)
python run_qc_web.py                    # atau double-click start_qc_web.bat
```

Browser terbuka otomatis ke `http://127.0.0.1:5057/`.

Prasyarat sama dengan desktop:
1. `config.json` berisi `gsheet_url` + `json_path` (set via app.py → tab
   "Koneksi Gudang").
2. Sheet `LIST_PESANAN` ada di spreadsheet.

## Alur (sama dengan desktop)

1. **Idle** → scan barcode resi polymailer.
2. **Sesi QC** → checklist SKU yang diharapkan + scan tiap pack (atau ketik SKU
   manual lalu Enter). Item non-stiker pakai tombol **Visual Confirm**.
3. **Approve & Seal** (aktif setelah semua item terverifikasi) / **Reject**
   (dengan alasan) / **Batalkan** (sesi tetap `in_progress`).
4. Hasil ditulis ke `LIST_PESANAN` + sheet **Hasil QC**, audit ke SQLite lokal.

## Perbedaan teknis dari desktop

| Desktop | Web |
|---|---|
| beep `winsound` | Web Audio API (`HX-Trigger` → `beep()` di base.html) |
| TTS `gTTS`+`pygame` | `speechSynthesis` browser (lang id-ID) |
| dialog modal tkinter | modal HTML + HTMX |
| Manual Entry dialog | langsung ketik di input scan lalu Enter |

State antar-request stateless: `session_id` dibawa di URL/form, progress selalu
dibaca ulang dari DB (akurat & tahan refresh).

## Struktur

```
qc_web/
  app.py                 # create_app() + semua route
  templates/
    base.html            # header, audio, TTS, fokus scanner
    idle.html / qc_session.html
    partials/            # _idle, _session, _checklist, _approve, _scan_response, _stats
  static/style.css
```
