import os
import re
import math
import json
import shutil
import threading
import queue
from gtts import gTTS
import pygame
import tempfile
import winsound
from datetime import datetime
from collections import defaultdict
from time import sleep
import customtkinter as ctk
import tkinter.filedialog as fd
from tkinter import messagebox
from openpyxl import load_workbook, Workbook
from PyPDF2 import PdfReader, PdfWriter
from PyPDF2.errors import PdfReadError

from google.oauth2.service_account import Credentials
import gspread

CONFIG_FILE = "config.json"

class BotApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Bot Sortir Stiker & Gudang v2.1")
        self.geometry("850x700")
        
        self.config_data = self.load_config()
        
        # Tabs Utama
        self.tabview = ctk.CTkTabview(self)
        self.tabview.pack(padx=20, pady=20, fill="both", expand=True)
        
        self.tab1 = self.tabview.add("Koneksi Gudang")
        self.tab2 = self.tabview.add("Pengaturan File")
        self.tab3 = self.tabview.add("Eksekusi & Log")
        self.tab4 = self.tabview.add("Scanner Resi Gudang")
        
        self.setup_tab_koneksi()
        self.setup_tab_file()
        self.setup_tab_eksekusi()
        self.setup_tab_scanner()

        self.gs_client = None
        self.spreadsheet = None

        self.scanner_db = None
        self.scanner_stock = None
        self.speech_queue = queue.Queue()
        
        # Init pygame mixer for TTS
        pygame.mixer.init()
        
        self.tts_thread = threading.Thread(target=self.tts_worker, daemon=True)
        self.tts_thread.start()

    def tts_worker(self):
        while True:
            text = self.speech_queue.get()
            if text is None:
                break
            try:
                tts = gTTS(text=text, lang='id')
                temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.mp3')
                temp_file.close()
                tts.save(temp_file.name)
                pygame.mixer.music.load(temp_file.name)
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    pygame.time.Clock().tick(10)
                pygame.mixer.music.unload()
                os.remove(temp_file.name)
            except Exception as e:
                print("TTS Error:", e)
            finally:
                self.speech_queue.task_done()

    def speak(self, text):
        self.speech_queue.put(text)

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def save_config(self):
        with open(CONFIG_FILE, "w") as f:
            json.dump(self.config_data, f, indent=4)

    # --- TAB 1: Koneksi Gudang ---
    def setup_tab_koneksi(self):
        ctk.CTkLabel(self.tab1, text="Pengaturan Google Spreadsheet", font=("Segoe UI", 16, "bold")).pack(pady=10)
        
        self.url_frame = ctk.CTkFrame(self.tab1)
        self.url_frame.pack(fill="x", padx=20, pady=5)
        ctk.CTkLabel(self.url_frame, text="URL Spreadsheet:").pack(side="left", padx=10)
        self.entry_url = ctk.CTkEntry(self.url_frame, width=400)
        self.entry_url.pack(side="left", padx=10, fill="x", expand=True)
        self.entry_url.insert(0, self.config_data.get("gsheet_url", ""))

        self.json_frame = ctk.CTkFrame(self.tab1)
        self.json_frame.pack(fill="x", padx=20, pady=5)
        ctk.CTkLabel(self.json_frame, text="File JSON Credentials:").pack(side="left", padx=10)
        self.entry_json = ctk.CTkEntry(self.json_frame, width=300)
        self.entry_json.pack(side="left", padx=10, fill="x", expand=True)
        self.entry_json.insert(0, self.config_data.get("json_path", ""))
        self.btn_browse_json = ctk.CTkButton(self.json_frame, text="Cari JSON", command=self.browse_json)
        self.btn_browse_json.pack(side="left", padx=10)

        self.btn_test_conn = ctk.CTkButton(self.tab1, text="Test & Simpan Koneksi", command=self.test_connection)
        self.btn_test_conn.pack(pady=20)
        
        self.lbl_conn_status = ctk.CTkLabel(self.tab1, text="Status Koneksi: Belum Dites", text_color="gray")
        self.lbl_conn_status.pack()

    def browse_json(self):
        path = fd.askopenfilename(filetypes=[("JSON Files", "*.json")])
        if path:
            self.entry_json.delete(0, 'end')
            self.entry_json.insert(0, path)

    def test_connection(self):
        url = self.entry_url.get()
        jpath = self.entry_json.get()
        self.config_data["gsheet_url"] = url
        self.config_data["json_path"] = jpath
        self.save_config()
        
        if not url or not os.path.exists(jpath):
            self.lbl_conn_status.configure(text="Error: URL kosong atau JSON tidak ditemukan", text_color="red")
            return
            
        try:
            scopes = ["https://www.googleapis.com/auth/spreadsheets"]
            creds = Credentials.from_service_account_file(jpath, scopes=scopes)
            self.gs_client = gspread.authorize(creds)
            
            if "spreadsheets/d/" in url:
                self.spreadsheet = self.gs_client.open_by_url(url)
            else:
                self.spreadsheet = self.gs_client.open_by_key(url)
                
            # Test Read target sheets, make sure they exist
            self.spreadsheet.worksheet("DATABASE_STIKER")
            self.spreadsheet.worksheet("LOG_KELUAR")
                
            self.lbl_conn_status.configure(text="Berhasil Terhubung & Sheet Ditemukan!", text_color="green")
        except Exception as e:
            error_msg = str(e) if str(e) else repr(e)
            self.lbl_conn_status.configure(text=f"Gagal Terhubung: {error_msg[:60]}", text_color="red")

    # --- TAB 2: Pengaturan File ---
    def setup_tab_file(self):
        ctk.CTkLabel(self.tab2, text="Pengaturan Path File", font=("Segoe UI", 16, "bold")).pack(pady=10)
        
        self.excel_frame = ctk.CTkFrame(self.tab2)
        self.excel_frame.pack(fill="x", padx=20, pady=5)
        ctk.CTkLabel(self.excel_frame, text="Data Pesanan (.xlsx):").pack(side="left", padx=10)
        self.entry_excel = ctk.CTkEntry(self.excel_frame)
        self.entry_excel.pack(side="left", padx=10, fill="x", expand=True)
        self.entry_excel.insert(0, self.config_data.get("excel_path", ""))
        ctk.CTkButton(self.excel_frame, text="Browse", width=80, command=lambda: self.browse_path("excel")).pack(side="left", padx=10)

        self.master_frame = ctk.CTkFrame(self.tab2)
        self.master_frame.pack(fill="x", padx=20, pady=5)
        ctk.CTkLabel(self.master_frame, text="Folder Master PDF:").pack(side="left", padx=10)
        self.entry_master = ctk.CTkEntry(self.master_frame)
        self.entry_master.pack(side="left", padx=10, fill="x", expand=True)
        self.entry_master.insert(0, self.config_data.get("master_path", ""))
        ctk.CTkButton(self.master_frame, text="Browse", width=80, command=lambda: self.browse_path("master")).pack(side="left", padx=10)

        self.hot_frame = ctk.CTkFrame(self.tab2)
        self.hot_frame.pack(fill="x", padx=20, pady=5)
        ctk.CTkLabel(self.hot_frame, text="Hot Folder (Hasil):").pack(side="left", padx=10)
        self.entry_hot = ctk.CTkEntry(self.hot_frame)
        self.entry_hot.pack(side="left", padx=10, fill="x", expand=True)
        self.entry_hot.insert(0, self.config_data.get("hot_path", ""))
        ctk.CTkButton(self.hot_frame, text="Browse", width=80, command=lambda: self.browse_path("hot")).pack(side="left", padx=10)
        
        ctk.CTkButton(self.tab2, text="Simpan Path", command=self.save_paths).pack(pady=20)

    def browse_path(self, ptype):
        if ptype == "excel":
            path = fd.askopenfilename(filetypes=[("Excel Files", "*.xlsx")])
            if path:
                self.entry_excel.delete(0, 'end')
                self.entry_excel.insert(0, path)
        elif ptype == "master":
            path = fd.askdirectory()
            if path:
                self.entry_master.delete(0, 'end')
                self.entry_master.insert(0, path)
        elif ptype == "hot":
            path = fd.askdirectory()
            if path:
                self.entry_hot.delete(0, 'end')
                self.entry_hot.insert(0, path)

    def save_paths(self):
        self.config_data["excel_path"] = self.entry_excel.get()
        self.config_data["master_path"] = self.entry_master.get()
        self.config_data["hot_path"] = self.entry_hot.get()
        self.save_config()
        messagebox.showinfo("Sukses", "Path berhasil disimpan!")

    # --- TAB 3: Eksekusi & Log ---
    def setup_tab_eksekusi(self):
        self.btn_frame = ctk.CTkFrame(self.tab3, fg_color="transparent")
        self.btn_frame.pack(pady=10)

        self.btn_start = ctk.CTkButton(self.btn_frame, text="MULAI PROSES", command=self.start_thread, height=40, font=("Segoe UI", 14, "bold"))
        self.btn_start.pack(side="left", padx=10)

        self.btn_open_output = ctk.CTkButton(self.btn_frame, text="Buka Folder Output", command=self.open_output_folder, height=40, font=("Segoe UI", 14, "bold"), fg_color="#6c757d", hover_color="#5a6268")
        self.btn_open_output.pack(side="left", padx=10)

        self.progress = ctk.CTkProgressBar(self.tab3)
        self.progress.pack(fill="x", padx=20, pady=5)
        self.progress.set(0)
        
        # Sub-tab untuk Log Eksekusi Utama dan Daftar Gudang Ready
        self.sub_tabview = ctk.CTkTabview(self.tab3)
        self.sub_tabview.pack(fill="both", expand=True, padx=20, pady=5)
        self.sub_tab_log = self.sub_tabview.add("Log Eksekusi")
        self.sub_tab_gudang = self.sub_tabview.add("Daftar Ready Gudang")

        self.legend_frame = ctk.CTkFrame(self.sub_tab_log, fg_color="transparent")
        self.legend_frame.pack(fill="x", padx=5, pady=(5, 5))
        
        ctk.CTkLabel(self.legend_frame, text="● Ambil Gudang", text_color="#28a745", font=("Segoe UI", 12, "bold")).pack(side="left", padx=10)
        ctk.CTkLabel(self.legend_frame, text="● Cetak Seluruhnya", text_color="#17a2b8", font=("Segoe UI", 12, "bold")).pack(side="left", padx=10)
        ctk.CTkLabel(self.legend_frame, text="● Error/Gagal", text_color="#dc3545", font=("Segoe UI", 12, "bold")).pack(side="left", padx=10)

        # Log Textbox Utama (dengan warna tag_config)
        self.textbox = ctk.CTkTextbox(self.sub_tab_log, state="disabled")
        self.textbox.pack(fill="both", expand=True, padx=5, pady=5)
        self.textbox.tag_config("hijau", foreground="#28a745")    # Mengambil gudang FULL
        self.textbox.tag_config("kuning", foreground="#ffc107")   # Parsial
        self.textbox.tag_config("merah", foreground="#dc3545")    # Error / Gagal
        self.textbox.tag_config("cyan", foreground="#17a2b8")     # Cetak murni
        self.textbox.tag_config("info", foreground="#adb5bd")   # Info Standar

        # Log Textbox Khusus Daftar Gudang
        self.textbox_gudang = ctk.CTkTextbox(self.sub_tab_gudang, state="disabled", text_color="#28a745")
        self.textbox_gudang.pack(fill="both", expand=True, padx=5, pady=5)

    def log_gui(self, message, color_tag="info"):
        self.textbox.configure(state="normal")
        self.textbox.insert("end", f"{message}\n", color_tag)
        self.textbox.see("end")
        self.textbox.configure(state="disabled")

    def log_gudang_ready(self, message):
        self.textbox_gudang.configure(state="normal")
        self.textbox_gudang.insert("end", f"{message}\n")
        self.textbox_gudang.see("end")
        self.textbox_gudang.configure(state="disabled")

    # --- TAB 4: Scanner Resi Gudang ---
    def setup_tab_scanner(self):
        ctk.CTkLabel(self.tab4, text="Pemindai Barcode Resi", font=("Segoe UI", 16, "bold")).pack(pady=10)
        
        btn_load = ctk.CTkButton(self.tab4, text="Muat/Perbarui Data Scanner", command=self.confirm_load_scanner_data)
        btn_load.pack(pady=5)
        
        self.lbl_scanner_status = ctk.CTkLabel(self.tab4, text="Status: Belum Memuat Data", text_color="orange")
        self.lbl_scanner_status.pack()

        # Input form sangat besar untuk di scan
        self.entry_scan = ctk.CTkEntry(self.tab4, placeholder_text="Arahkan kursor kesini, lalu scan barcode", height=50, font=("Segoe UI", 20))
        self.entry_scan.pack(fill="x", padx=40, pady=20)
        self.entry_scan.bind("<Return>", self.on_scan)

        # Log Textbox untuk hasil scan
        self.log_scan = ctk.CTkTextbox(self.tab4, state="disabled")
        self.log_scan.pack(fill="both", expand=True, padx=20, pady=10)
        self.log_scan.tag_config("info", foreground="#adb5bd")
        self.log_scan.tag_config("hijau", foreground="#28a745")
        self.log_scan.tag_config("merah", foreground="#dc3545")

    def print_scan_log(self, msg, color="info"):
        self.log_scan.configure(state="normal")
        self.log_scan.insert("end", f"{msg}\n", color)
        self.log_scan.see("end")
        self.log_scan.configure(state="disabled")

    def confirm_load_scanner_data(self):
        answer = messagebox.askyesno("Pengingat Update Sales", "Apakah Anda sudah memastikan 'Upload Data Sales' telah di-update di Google Sheet secara manual sebelum memuat data scanner?")
        if answer:
            threading.Thread(target=self.load_scanner_data, daemon=True).start()

    def load_scanner_data(self):
        self.lbl_scanner_status.configure(text="Status: Sedang memuat data... Mohon tunggu", text_color="orange")
        self.print_scan_log("\n[INFO] Memulai sinkronisasi data gudang & pesanan...", "info")
        
        # Test Connection 
        if not self.spreadsheet:
            self.test_connection()
            if not self.spreadsheet:
                self.print_scan_log("ERROR: Gagal terhubung ke Google Sheets.", "merah")
                self.lbl_scanner_status.configure(text="Status: Gagal Terhubung", text_color="red")
                return
                
        excel_file = self.config_data.get("excel_path", "")
        if not excel_file or not os.path.exists(excel_file):
            self.print_scan_log("ERROR: File Pesanan Excel tidak valid di tab Pengaturan File.", "merah")
            self.lbl_scanner_status.configure(text="Status: Excel tidak valid", text_color="red")
            return

        try:
            # Load GS
            ws_db = self.spreadsheet.worksheet("DATABASE_STIKER")
            db_records = ws_db.get_all_values()
            stock_dict = {}
            for row in db_records[1:]:
                if len(row) >= 8:
                    sku_id = str(row[0]).strip()
                    try: stok_saat_ini = int(row[7])
                    except ValueError: stok_saat_ini = 0
                    stock_dict[sku_id] = stok_saat_ini
            self.scanner_stock = stock_dict

            # Load Excel
            wb = load_workbook(excel_file, data_only=True)
            ws = wb.active
            scan_db = defaultdict(list)
            for row_idx in range(2, ws.max_row + 1):
                resi_val = ws.cell(row_idx, 1).value
                sku_val = ws.cell(row_idx, 2).value
                jml_val = ws.cell(row_idx, 3).value
                if not sku_val or not jml_val or not resi_val: continue
                
                try: jumlah_pesanan = int(jml_val)
                except ValueError: continue
                
                original_sku = str(sku_val).strip()
                numeric_id, pcs_per_paket = self.extract_numeric_id_and_pcs(original_sku)
                if not numeric_id: continue
                
                total_pcs_needed = pcs_per_paket * jumlah_pesanan
                scan_db[str(resi_val).strip()].append({
                    "sku": original_sku,
                    "numeric_id": numeric_id,
                    "total_pcs_needed": total_pcs_needed
                })
                
            self.scanner_db = scan_db
            
            self.print_scan_log(f"Berhasil! Dimuat {len(self.scanner_db)} Resi Unik dari Pesanan.", "hijau")
            self.lbl_scanner_status.configure(text="Status: Siap! Arahkan kursor dan Scan Barcode.", text_color="green")
            self.speak("Data siap digunakan")
        except Exception as e:
            self.print_scan_log(f"ERROR: {e}", "merah")
            self.lbl_scanner_status.configure(text="Status: Error Gagal Muat Data", text_color="red")

    def on_scan(self, event):
        resi = self.entry_scan.get().strip()
        self.entry_scan.delete(0, 'end') # Clear
        
        if not resi:
            return
            
        try:
            winsound.Beep(1500, 150)
        except:
            pass
            
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.print_scan_log(f"\n[{timestamp}] > Barcode Terbaca: {resi}", "info")
        
        if self.scanner_db is None or self.scanner_stock is None:
            self.print_scan_log("Proses ditolak. Harap 'Muat/Perbarui Data Scanner' terlebih dahulu.", "merah")
            self.speak("Harap muat data terlebih dahulu")
            return

        if resi not in self.scanner_db:
            self.print_scan_log(f"Resi '{resi}' tidak ditemukan pada file Excel pesanan.", "merah")
            self.speak("Resi tidak ditemukan")
            return

        items = self.scanner_db[resi]
        self.print_scan_log(f"Ditemukan {len(items)} SKU dalam resi ini.", "info")
        
        ready_count = sum(1 for item in items if self.scanner_stock.get(item['numeric_id'], 0) >= item['total_pcs_needed'])
        
        if ready_count == 0:
            self.speak("Semua kosong")
        
        for item in items:
            num_id = item['numeric_id']
            original_sku = item['sku']
            needed = item['total_pcs_needed']
            current_gudang = self.scanner_stock.get(num_id, 0)
            
            if current_gudang >= needed:
                self.print_scan_log(f"✔ SKU {num_id} ({needed} pcs) MENCUKUPI (Stok: {current_gudang})", "hijau")
                self.speak(f"{num_id} ready")
            else:
                sisa_produksi = needed
                msg = f"SKU {num_id} ({needed} pcs) STOK TIDAK CUKUP (Stok: {current_gudang}, Cetak Full: {sisa_produksi})"
                self.print_scan_log(f"❌ {msg}", "merah")
                if ready_count > 0:
                    self.speak(f"{num_id} kosong")

    def open_output_folder(self):
        hot_folder = self.config_data.get("hot_path", "")
        if hot_folder and os.path.exists(hot_folder):
            os.startfile(hot_folder)
        else:
            messagebox.showwarning("Peringatan", "Folder output belum diatur atau tidak ditemukan.")

    def start_thread(self):
        self.btn_start.configure(state="disabled")
        
        # Bersihkan textbox
        self.textbox.configure(state="normal")
        self.textbox.delete("1.0", "end")
        self.textbox.configure(state="disabled")
        
        self.textbox_gudang.configure(state="normal")
        self.textbox_gudang.delete("1.0", "end")
        self.textbox_gudang.configure(state="disabled")
        
        self.progress.set(0)
        
        # Test Connection 
        if not self.spreadsheet:
            self.test_connection()
            if not self.spreadsheet:
                self.log_gui("ERROR: Gagal terhubung ke Google Sheets. Cek pengaturan.", "merah")
                self.btn_start.configure(state="normal")
                return

        threading.Thread(target=self.run_process, daemon=True).start()

    def run_process(self):
        self.log_gui("[INFO] Memulai Proses...", "info")
        try:
            self.main_logic()
        except Exception as e:
            self.log_gui(f"[ERROR FATAL] Terjadi kesalahan: {str(e)}", "merah")
        finally:
            self.btn_start.configure(state="normal", text="MULAI KEMBALI")

    def clear_hotfolder(self, hot_folder):
        self.log_gui("[INFO] Membersihkan HOT_FOLDER dari file lama...", "info")
        try:
            for item in os.listdir(hot_folder):
                full_path = os.path.join(hot_folder, item)
                if os.path.isdir(full_path):
                    if item.lower() == "log":
                        continue
                    # Hapus folder Batch sebelumnya jika ada
                    if item.lower().startswith("batch"):
                        shutil.rmtree(full_path, ignore_errors=True)
                elif os.path.isfile(full_path) and item.lower().endswith('.pdf'):
                    os.remove(full_path)
            self.log_gui("Pembersihan selesai.", "info")
        except Exception as e:
            self.log_gui(f"Error membersihkan HOT_FOLDER: {e}", "merah")

    def create_file_cache(self, master_folder):
        self.log_gui("[INFO] Membuat cache daftar file dari MASTER_FOLDER...", "info")
        cache = defaultdict(list)
        try:
            for filename in os.listdir(master_folder):
                if filename.lower().endswith(".pdf"):
                    match = re.match(r'^\d+', filename)
                    if match:
                        name_key = match.group(0)
                        cache[name_key].append(os.path.join(master_folder, filename))
            return cache
        except FileNotFoundError:
            self.log_gui(f"Error: MASTER_FOLDER tidak ditemukan.", "merah")
            return None

    def extract_numeric_id_and_pcs(self, sku):
        id_match = re.match(r'^\d+', sku.strip())
        numeric_id = id_match.group(0) if id_match else None
        pcs_match = re.search(r'(\d+)pcs', sku, re.IGNORECASE)
        pcs = int(pcs_match.group(1)) if pcs_match else 1
        return numeric_id, pcs

    def main_logic(self):
        hot_folder = self.config_data.get("hot_path", "")
        master_folder = self.config_data.get("master_path", "")
        excel_file = self.config_data.get("excel_path", "")

        if not all([hot_folder, master_folder, excel_file]):
            self.log_gui("ERROR: Semua path (Excel, Master, Hot) harus diisi.", "merah")
            return

        if not os.path.exists(excel_file):
            self.log_gui(f"ERROR: File Excel tidak ditemukan: {excel_file}", "merah")
            return

        self.clear_hotfolder(hot_folder)

        # Build paths untuk LOG lokal
        LOG_BERHASIL_DIR = os.path.join(hot_folder, "log", "berhasil")
        LOG_GAGAL_DIR = os.path.join(hot_folder, "log", "gagal")
        LOG_PERINGATAN_DIR = os.path.join(hot_folder, "log", "peringatan")
        os.makedirs(LOG_BERHASIL_DIR, exist_ok=True)
        os.makedirs(LOG_GAGAL_DIR, exist_ok=True)
        os.makedirs(LOG_PERINGATAN_DIR, exist_ok=True)

        file_cache = self.create_file_cache(master_folder)
        if file_cache is None: return

        # Load Stock from GS
        self.log_gui("[INFO] Mengunduh data dari DATABASE_STIKER...", "info")
        ws_db = self.spreadsheet.worksheet("DATABASE_STIKER")
        db_records = ws_db.get_all_values()
        
        stock_dict = {}
        for row in db_records[1:]:
            if len(row) >= 8:
                sku_id = str(row[0]).strip()
                try: stok_saat_ini = int(row[7])
                except ValueError: stok_saat_ini = 0
                stock_dict[sku_id] = stok_saat_ini

        ws_log = self.spreadsheet.worksheet("LOG_KELUAR")

        # Compile Excel rows into Task List
        self.log_gui("[INFO] Membaca & Mengurutkan pesanan Excel (Sort by SKU)...", "info")
        wb = load_workbook(excel_file)
        ws = wb.active
        
        task_list = []
        fail_logs = []
        
        for row_idx in range(2, ws.max_row + 1):
            resi_val = ws.cell(row_idx, 1).value
            sku_val = ws.cell(row_idx, 2).value
            jml_val = ws.cell(row_idx, 3).value
            
            if not sku_val or not jml_val: continue
                
            try:
                jumlah_pesanan = int(jml_val)
                original_sku = str(sku_val).strip()
            except ValueError:
                fail_logs.append((str(sku_val), str(jml_val), f"Gagal - 'Jumlah' harus angka"))
                continue
                
            numeric_id, pcs_per_paket = self.extract_numeric_id_and_pcs(original_sku)
            if not numeric_id:
                fail_logs.append((original_sku, str(jml_val), "Gagal - Tidak ada ID (angka awal) terdeteksi."))
                continue

            total_pcs_needed = pcs_per_paket * jumlah_pesanan

            task_list.append({
                'resi': str(resi_val) if resi_val else "-",
                'sku': original_sku,
                'numeric_id': numeric_id,
                'total_pcs_needed': total_pcs_needed,
                'jumlah_pesanan': jumlah_pesanan,
                'pcs_per_paket': pcs_per_paket
            })

        if not task_list:
            self.log_gui("Data kosong atau tidak valid.", "merah")
            return

        # SORTING by Numeric ID
        task_list.sort(key=lambda x: int(x['numeric_id']))

        success_logs = []
        warnings_log = []
        logs_keluar_to_append = []

        total_tasks = len(task_list)
        today_str = datetime.now().strftime("%Y-%m-%d")

        used_filenames = defaultdict(int)
        def get_next_filename(task_id: int, design_id: str, suffix: str = "") -> str:
            sanitized_id = re.sub(r'[\\/*?:"<>|]', "-", design_id)
            base_key = f"{sanitized_id}{suffix}"
            used_filenames[base_key] += 1
            if used_filenames[base_key] == 1: return f"{base_key}.pdf"
            else: return f"{base_key} - Copy ({used_filenames[base_key] - 1}).pdf"

        batches = {
            '10pcs': {'base_dir': os.path.join(hot_folder, "Batch 10"), 'number': 1, 'count': 0, 'dir': os.path.join(hot_folder, "Batch 10", "Batch_1")},
            '50pcs': {'base_dir': os.path.join(hot_folder, "Batch 50"), 'number': 1, 'count': 0, 'dir': os.path.join(hot_folder, "Batch 50", "Batch_1")}
        }
        os.makedirs(batches['10pcs']['dir'], exist_ok=True)
        os.makedirs(batches['50pcs']['dir'], exist_ok=True)

        for i, task in enumerate(task_list):
            self.progress.set((i) / total_tasks)
            
            numeric_id = task['numeric_id']
            resi_val = task['resi']
            total_pcs_needed = task['total_pcs_needed']
            original_sku = task['sku']

            stok_gudang = stock_dict.get(numeric_id, 0)

            if stok_gudang >= total_pcs_needed:
                # 1. Full terpenuhi gudang
                ambil_terpenuhi = total_pcs_needed
                sisa_produksi = 0
                stock_dict[numeric_id] -= ambil_terpenuhi
                
                logs_keluar_to_append.append([today_str, numeric_id, ambil_terpenuhi, f"Resi: {resi_val} | Full"])
                self.log_gui(f"● Resi {resi_val} (SKU {numeric_id}): {total_pcs_needed} pcs", "hijau")
                
                self.log_gudang_ready(f"GUDANG - Resi: {resi_val} | SKU: {numeric_id} | Jumlah: {ambil_terpenuhi} pcs")
                success_logs.append((f"Tugas-{(i+1):03d}", numeric_id, total_pcs_needed, f"Tersedia dari gudang ({ambil_terpenuhi})"))
                
            else:
                sisa_produksi = total_pcs_needed
                self.log_gui(f"● Resi {resi_val} (SKU {numeric_id}): {total_pcs_needed} pcs", "cyan")
                
            # PROSES CETAK / DUPLIKAT
            if sisa_produksi > 0:
                found_paths = file_cache.get(numeric_id, [])
                if not found_paths:
                    fail_logs.append((original_sku, f"Sisa Produksi: {sisa_produksi}", "Gagal - Master PDF tidak ada."))
                    self.log_gui(f"❌ (SKU {numeric_id}) File Master tidak ditemukan!", "merah")
                    continue
                
                optimal_candidates = [p for p in found_paths if "versioptimal" in os.path.basename(p).lower()]
                standard_candidates = [p for p in found_paths if p not in optimal_candidates]
                
                found_file = None
                version_type = "standard"
                if optimal_candidates:
                    optimal_candidates.sort(key=len)
                    found_file = optimal_candidates[0]
                    version_type = "optimal"
                    if len(optimal_candidates) > 1: warnings_log.append((numeric_id, "", "Duplikat versi optimal ditemukan."))
                elif standard_candidates:
                    standard_candidates.sort(key=len)
                    found_file = standard_candidates[0]
                    if len(standard_candidates) > 1: warnings_log.append((numeric_id, "", "Duplikat standar ditemukan."))
                
                batch_size_per_page = 100.0 if version_type == 'optimal' else 50.0
                num_pages_to_print = int(math.ceil(sisa_produksi / batch_size_per_page))
                
                pcs_type = '10pcs' if task['pcs_per_paket'] <= 20 else '50pcs'
                batch_info = batches[pcs_type]

                # Check Batch Folder Logic
                if batch_info['count'] >= 20: 
                    batch_info['number'] += 1
                    batch_info['count'] = 0
                    batch_info['dir'] = os.path.join(batch_info['base_dir'], f"Batch_{batch_info['number']}")
                    os.makedirs(batch_info['dir'], exist_ok=True)

                try:
                    for _ in range(num_pages_to_print):
                        out_filename = get_next_filename((i+1), numeric_id, "-PRODUK" if version_type != 'optimal' else "-VERSIOPTIMAL")
                        # Taruh di dalam folder Batch
                        out_path = os.path.join(batch_info['dir'], out_filename)
                        
                        with open(found_file, "rb") as infile:
                            reader = PdfReader(infile)
                            writer = PdfWriter()
                            if len(reader.pages) > 0:
                                writer.add_page(reader.pages[0])
                                with open(out_path, "wb") as outfile:
                                    writer.write(outfile)
                                    
                    keterangan = f"Sukses Cetak {num_pages_to_print} lbr (Dilimpahkan ke {pcs_type}/Batch_{batch_info['number']})"
                    success_logs.append((f"Tugas-{(i+1):03d}", numeric_id, sisa_produksi, keterangan))
                    
                    batch_info['count'] += 1
                except Exception as e:
                    fail_logs.append((original_sku, str(sisa_produksi), f"Gagal Ekstrak - {e}"))
                    self.log_gui(f"❌ Error ekstrak SKU {numeric_id}: {e}", "merah")

        # PUSH SHEETS
        if logs_keluar_to_append:
            self.log_gui(f"\n[INFO] Mengirim data stok keluar ke LOG_KELUAR...", "info")
            ws_log.append_rows(logs_keluar_to_append)

        # WRITE LOG EXCEL LOCAL
        timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        if success_logs:
            wb_success = Workbook()
            ws_success = wb_success.active
            ws_success.append(["ID Tugas", "ID Desain", "Produksi (Pcs/Lbr)", "Keterangan"])
            for lg in success_logs: ws_success.append(lg)
            wb_success.save(os.path.join(LOG_BERHASIL_DIR, f"berhasil_{timestamp}.xlsx"))

        if fail_logs:
            wb_fail = Workbook()
            ws_fail = wb_fail.active
            ws_fail.append(["SKU", "Jumlah Target", "Keterangan"])
            for lg in fail_logs: ws_fail.append(lg)
            wb_fail.save(os.path.join(LOG_GAGAL_DIR, f"gagal_{timestamp}.xlsx"))

        if warnings_log:
            wb_warn = Workbook()
            ws_warn = wb_warn.active
            ws_warn.append(["SKU", "Jml", "Keterangan"])
            for lg in warnings_log: ws_warn.append(lg)
            wb_warn.save(os.path.join(LOG_PERINGATAN_DIR, f"peringatan_{timestamp}.xlsx"))

        self.progress.set(1.0)
        self.log_gui("\n[SELESAI] Proses telah selesai.", "info")

if __name__ == "__main__":
    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("blue")
    app = BotApp()
    app.mainloop()
