"""Auto-updater: sync file dari GitHub raw ke folder lokal.

Mekanisme:
1. Fetch ``update_manifest.txt`` dari server (line-per-line list file).
2. Untuk tiap file di manifest, fetch raw content + bandingkan dengan lokal.
3. Kalau berbeda → overwrite. Auto-create parent directory untuk subfolder
   (mis. ``packing_router/web/app.py``).

Kalau manifest gagal di-fetch (offline / 404), pakai ``FILES_TO_UPDATE``
fallback hardcoded.

Cache-bust: ``raw.githubusercontent.com`` punya cache 5 menit. Tiap fetch
append ``?_cb=<unix_ts>`` query untuk bypass CDN cache supaya commit baru
langsung kebaca tanpa nunggu cache expire.
"""
import os
import time
import urllib.request

BRANCH = os.environ.get("UPDATER_BRANCH", "main").strip() or "main"
BASE_URL = f"https://raw.githubusercontent.com/dennypa77/sortir_stiker_desain/{BRANCH}/"
MANIFEST_FILE = "update_manifest.txt"

# Fallback list (kompatibel dengan versi lama updater) kalau manifest gagal di-fetch
FILES_TO_UPDATE = [
    "app.py",
    "duplicate_files.py",
    "requirements.txt",
    "qc_stasiun.py",
    "qc_seed.py",
    "run_qc.py",
]


def _fetch_text(filename, timeout=10):
    """Fetch file dari BASE_URL. Return bytes atau raise.

    Append ``?_cb=<unix_ts>`` untuk bust CDN cache (raw.githubusercontent.com
    cache 5 menit by default — query unique per-run bypass cache)."""
    url = BASE_URL + filename.replace(" ", "%20").replace("\\", "/")
    url += ("&" if "?" in url else "?") + "_cb=" + str(int(time.time()))
    req = urllib.request.Request(url, headers={
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    response = urllib.request.urlopen(req, timeout=timeout)
    return response.read()


def _fetch_manifest():
    """Return list nama file dari manifest, atau None kalau gagal."""
    try:
        content = _fetch_text(MANIFEST_FILE).decode("utf-8")
    except Exception as e:
        print(f" -> Manifest tidak bisa di-fetch ({e}), pakai daftar default.")
        return None
    files = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        files.append(line)
    return files


def check_for_updates():
    print(f"Mencari pembaruan aplikasi dari server (branch: {BRANCH})...")
    files = _fetch_manifest()
    if files is None:
        files = FILES_TO_UPDATE

    has_updates = False
    failed = 0
    for filename in files:
        try:
            remote_content = _fetch_text(filename)

            local_content = b""
            if os.path.exists(filename):
                with open(filename, "rb") as f:
                    local_content = f.read()

            if remote_content != local_content:
                print(f" -> Mengunduh versi terbaru: {filename}...")
                parent = os.path.dirname(filename)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(filename, "wb") as f:
                    f.write(remote_content)
                has_updates = True

        except Exception as e:
            failed += 1
            # Hanya log warning kalau file ada di manifest tapi gagal — mungkin
            # belum di-push, file di-rename, atau koneksi putus.
            if failed <= 3:
                print(f" -> Peringatan: Gagal mengecek/mengunduh {filename}. ({e})")
            elif failed == 4:
                print(" -> ... (warning lain di-suppress)")

    if failed > 0:
        print(f" -> Total {failed} file gagal di-fetch. Mungkin offline atau file belum di-push ke server.")

    if has_updates:
        print("\nPembaruan berhasil diinstal!\n")
    else:
        print("Aplikasi Anda sudah versi terbaru!\n")


if __name__ == "__main__":
    check_for_updates()
    time.sleep(1)
