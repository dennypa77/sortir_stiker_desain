@echo off
echo ====================================================
echo  Alat Otomatis Upload Pembaruan ^& Naikkan Versi
echo ====================================================
echo.

:: 1. Menaikkan versi di app.py
python increment_version.py

echo.
:: 2. Masukkan semua perubahan ke Git
git add .

:: 3. Minta deskripsi update
set /p desc="Masukkan info update (contoh: 'perbaikan bug A'): "

:: 4. Commit dan Push
git commit -m "%desc%"
echo.
echo Sedang mengunggah (push) ke GitHub...
git push origin main

echo.
echo ====================================================
echo  SUKSES! Aplikasi versi terbaru berhasil diunggah.
echo  Tim Anda akan otomatis mendapatkan update ini.
echo ====================================================
pause
