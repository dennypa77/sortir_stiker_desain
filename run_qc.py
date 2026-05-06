"""
run_qc.py
Standalone launcher untuk Stasiun QC.

Membaca konfigurasi koneksi Google Sheet dari config.json (sama yang dipakai app.py),
lalu buka QcStasiunWindow sebagai window utama. Tutup window = exit aplikasi.

Penggunaan:
    python run_qc.py
    (atau double-click start_qc.bat)

Prerequisites:
    1. config.json sudah di-set via app.py tab "Koneksi Gudang" (gsheet_url + json_path).
    2. Sheet "LIST_PESANAN" sudah dibuat di spreadsheet.
    3. Minimal 1 operator sudah di-seed:
         python qc_seed.py add-operator --name "Nama"
"""

import os
import sys
import json
import queue
import tempfile
import threading

import customtkinter as ctk
from tkinter import messagebox

from gtts import gTTS
import pygame

from google.oauth2.service_account import Credentials
import gspread

from qc_stasiun import QcStasiunWindow, init_db

CONFIG_FILE = "config.json"


class QcLauncherRoot(ctk.CTk):
    """Hidden root window. Cuma untuk host TTS queue + parent QcStasiunWindow."""

    def __init__(self):
        super().__init__()
        self.title("QC Launcher")
        self.geometry("1x1+0+0")
        self.withdraw()  # sembunyikan root window

        self.speech_queue = queue.Queue()
        try:
            pygame.mixer.init()
        except Exception as e:
            print(f"[Warning] pygame.mixer.init gagal: {e}")

        self.tts_thread = threading.Thread(target=self._tts_worker, daemon=True)
        self.tts_thread.start()

    def _tts_worker(self):
        while True:
            text = self.speech_queue.get()
            if text is None:
                break
            temp_path = None
            try:
                tts = gTTS(text=text, lang="id")
                temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
                temp_path = temp_file.name
                temp_file.close()
                tts.save(temp_path)
                pygame.mixer.music.load(temp_path)
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    pygame.time.Clock().tick(10)
                pygame.mixer.music.unload()
            except Exception as e:
                print(f"[TTS Error] {e}")
            finally:
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass
                self.speech_queue.task_done()

    def speak(self, text):
        self.speech_queue.put(text)


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def connect_spreadsheet(cfg):
    """Authenticate ke Google Sheets pakai config existing. Raise kalau gagal."""
    url = cfg.get("gsheet_url", "").strip()
    jpath = cfg.get("json_path", "").strip()

    if not url:
        raise RuntimeError(
            "config.json belum punya 'gsheet_url'.\n\n"
            "Buka app.py → tab 'Koneksi Gudang' → isi URL spreadsheet & "
            "Test Koneksi dulu, baru jalankan run_qc.py."
        )
    if not jpath or not os.path.exists(jpath):
        raise RuntimeError(
            f"File JSON credential tidak ditemukan:\n{jpath}\n\n"
            "Set ulang path-nya via app.py tab 'Koneksi Gudang'."
        )

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(jpath, scopes=scopes)
    client = gspread.authorize(creds)

    if "spreadsheets/d/" in url:
        ss = client.open_by_url(url)
    else:
        ss = client.open_by_key(url)

    # Verifikasi sheet LIST_PESANAN ada
    try:
        ss.worksheet("LIST_PESANAN")
    except Exception:
        raise RuntimeError(
            "Sheet 'LIST_PESANAN' belum dibuat di spreadsheet.\n\n"
            "Buat sheet baru bernama 'LIST_PESANAN'. Header akan auto-set saat "
            "tim gudang upload pertama via Apps Script v7.0."
        )

    return ss


def main():
    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("blue")

    # Pastikan DB QC siap
    try:
        init_db()
    except Exception as e:
        # Tidak ada root sama sekali — pakai messagebox standalone
        import tkinter as tk
        tk_root = tk.Tk()
        tk_root.withdraw()
        messagebox.showerror("DB Error", f"Gagal init database QC:\n{e}")
        tk_root.destroy()
        sys.exit(1)

    cfg = load_config()

    # Buat hidden launcher root dulu (provider TTS untuk QC window)
    launcher = QcLauncherRoot()

    # Authenticate sheet
    try:
        spreadsheet = connect_spreadsheet(cfg)
    except Exception as e:
        messagebox.showerror("Gagal Koneksi Sheet", str(e), parent=launcher)
        launcher.destroy()
        sys.exit(1)

    # Buka QC window
    try:
        qc_window = QcStasiunWindow(launcher, spreadsheet)
    except Exception as e:
        messagebox.showerror("Gagal Buka Stasiun QC", str(e), parent=launcher)
        launcher.destroy()
        sys.exit(1)

    # Tutup QC window = keluar app. wait_window block sampai window destroyed,
    # lalu mainloop berhenti.
    launcher.wait_window(qc_window)

    # Cleanup
    try:
        launcher.speech_queue.put(None)  # signal TTS thread to exit
    except Exception:
        pass
    try:
        launcher.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    main()
