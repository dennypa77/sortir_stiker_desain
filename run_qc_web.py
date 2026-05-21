"""run_qc_web.py
Launcher Stasiun QC versi web (Flask + HTMX).

Menjalankan server lokal lalu membuka browser otomatis. Konfigurasi koneksi
Google Sheet dibaca dari config.json (sama dengan run_qc.py / app.py).

Penggunaan:
    python run_qc_web.py
    (atau double-click start_qc_web.bat)

Catatan: ini berjalan BERDAMPINGAN dengan run_qc.py (desktop). Tidak ada file
desktop yang diubah.
"""
import threading
import webbrowser

from qc_web.app import create_app, WEB_HOST, WEB_PORT


def main():
    app = create_app()
    url = f"http://{WEB_HOST}:{WEB_PORT}/"
    # Buka browser ~1 dtk setelah server siap.
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"Stasiun QC web berjalan di {url}  (Ctrl+C untuk berhenti)")
    # use_reloader=False supaya browser tidak kebuka 2x & timer tidak dobel.
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
