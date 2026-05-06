import urllib.request
import os
import time

# Daftar file inti yang akan selalu disinkronkan dari GitHub
FILES_TO_UPDATE = [
    "app.py",
    "duplicate_files.py",
    "requirements.txt",
    "qc_stasiun.py",
    "qc_seed.py"
]

BASE_URL = "https://raw.githubusercontent.com/dennypa77/sortir_stiker_desain/main/"

def check_for_updates():
    print("Mencari pembaruan aplikasi dari server...")
    has_updates = False
    
    for filename in FILES_TO_UPDATE:
        url = BASE_URL + filename.replace(" ", "%20")
        try:
            # Download file secara langsung (Raw)
            response = urllib.request.urlopen(url, timeout=10)
            remote_content = response.read()
            
            # Baca file lokal
            local_content = b""
            if os.path.exists(filename):
                with open(filename, 'rb') as f:
                    local_content = f.read()
                    
            # Jika ada perbedaan isi (kode berubah), timpa dengan yang baru
            if remote_content != local_content:
                print(f" -> Mengunduh versi terbaru: {filename}...")
                with open(filename, 'wb') as f:
                    f.write(remote_content)
                has_updates = True
                
        except Exception as e:
            print(f" -> Peringatan: Gagal mengecek/mengunduh {filename}. ({e})")
            print(" -> Mungkin tidak ada koneksi internet. Menggunakan versi lokal saat ini.")
            
    if has_updates:
        print("\nPembaruan berhasil diinstal! Menjalankan aplikasi...")
        # Jika requirements berubah, kita mungkin perlu beritahu pengguna (opsional)
    else:
        print("Aplikasi Anda sudah versi terbaru!\n")

if __name__ == "__main__":
    check_for_updates()
    time.sleep(1)
