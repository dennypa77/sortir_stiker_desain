@echo off
echo ====================================================
echo Menjalankan Auto-Updater Bot Sortir Stiker...
echo ====================================================
py -3.13 updater.py

echo ====================================================
echo Membuka Stasiun QC...
echo ====================================================
py -3.13 run_qc.py
pause
