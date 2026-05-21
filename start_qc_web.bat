@echo off
echo ====================================================
echo Menjalankan Auto-Updater Bot Sortir Stiker...
echo ====================================================
python updater.py

echo ====================================================
echo Membuka Stasiun QC (Web)...
echo ====================================================
python run_qc_web.py
pause
