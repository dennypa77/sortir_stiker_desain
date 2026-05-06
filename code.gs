/**
 * SISTEM MANAJEMEN GUDANG STIKER - V7.0
 *
 * File ini adalah Google Apps Script yang harus di-paste ke editor Apps Script
 * di Google Spreadsheet (Extensions → Apps Script). File ini disimpan di repo
 * sebagai referensi versi dan dokumentasi.
 *
 * Tambahan v7.0 (dari v6.0):
 *   - Auto-populate sheet LIST_PESANAN saat upload BigSeller export.
 *     Deteksi kolom by header keyword (resi/tracking/awb, sku, jumlah/qty,
 *     tanggal/date), fallback ke positional 3-col / 4-col seperti v6.0.
 *   - Auto-generate Batch_ID format YYYY-MM-DD-Bn (auto-increment per hari).
 *   - Auto-set header LIST_PESANAN saat sheet pertama kali diisi.
 *   - Auto-detect marketplace dari prefix resi (Shopee/SPX/JNT/JNE/dll).
 *   - Menu item baru: "Hapus LIST_PESANAN > 10 Hari" untuk maintenance.
 *
 * Cara deploy:
 *   1. Buka Google Spreadsheet → Extensions → Apps Script.
 *   2. Hapus seluruh isi Code.gs di editor, paste isi file ini.
 *   3. Save (Ctrl+S). Reload spreadsheet.
 *   4. Menu "📦 Kelola Gudang" akan punya 4 item.
 *
 * Sheet yang dipakai:
 *   - DATA_SALES        (existing) — trend penjualan, summary harian, rolling 30 hari.
 *   - DATABASE_STIKER   (existing) — master SKU + stok gudang.
 *   - STOK_OPNAME       (existing) — input stok fisik untuk sync opname.
 *   - LOG_KELUAR        (existing) — log barang keluar (di-write app.py Python).
 *   - LIST_PESANAN      (BARU)     — detail per-resi untuk Stasiun QC.
 */

const LIST_PESANAN_HEADER = [
  "Batch_ID", "Uploaded_At", "Nomor_Resi", "SKU", "Jumlah",
  "Marketplace", "Status", "QC_Operator", "QC_Completed_At", "QC_Notes"
];

const MARKETPLACE_PREFIXES = {
  'SPXID': 'Shopee Express', 'SPX': 'Shopee Express',
  'SHPE': 'Shopee', 'SHP': 'Shopee',
  'JNT': 'J&T Express', 'JT': 'J&T Express',
  'JNE': 'JNE', 'TKP': 'Tokopedia',
  'IDE': 'ID Express', 'SAP': 'SAP Express'
};

const PURGE_DAYS = 10;

function onOpen() {
  const ui = SpreadsheetApp.getUi();
  ui.createMenu('📦 Kelola Gudang')
      .addItem('1. Upload Data Sales (Excel/CSV)', 'showUploadDialog')
      .addSeparator()
      .addItem('2. Jalankan Sinkronisasi Opname', 'syncOpnameToDatabase')
      .addSeparator()
      .addItem('3. Reset & Ringkas Data Sales', 'manageDataSales')
      .addSeparator()
      .addItem('4. Hapus LIST_PESANAN > ' + PURGE_DAYS + ' Hari', 'purgeOldListPesanan')
      .addToUi();
}

/* ============================================================
 *  OPNAME (UNCHANGED dari v6.0)
 *  Memindahkan stok fisik dari STOK_OPNAME ke kolom Adj Opname
 *  di DATABASE_STIKER (Hard Sync).
 * ============================================================ */
function syncOpnameToDatabase() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const dbSheet = ss.getSheetByName("DATABASE_STIKER");
  const soSheet = ss.getSheetByName("STOK_OPNAME");
  if (!dbSheet || !soSheet) {
    SpreadsheetApp.getUi().alert("Error: Tab DATABASE_STIKER atau STOK_OPNAME tidak ditemukan!");
    return;
  }
  const dbData = dbSheet.getDataRange().getValues();
  const soData = soSheet.getDataRange().getValues();
  if (soData.length < 2) {
    SpreadsheetApp.getUi().alert("Data di STOK_OPNAME kosong!");
    return;
  }
  const physicalMap = {};
  for (let i = 1; i < soData.length; i++) {
    const sku = soData[i][1].toString().trim();
    const physicalQty = parseFloat(soData[i][2]);
    if (sku && !isNaN(physicalQty)) physicalMap[sku] = physicalQty;
  }
  const newAdjValues = [];
  for (let j = 1; j < dbData.length; j++) {
    const idMaster = dbData[j][0].toString().trim();
    const stokBerjalan = parseFloat(dbData[j][5]) || 0;
    const adjLama = parseFloat(dbData[j][6]) || 0;
    if (physicalMap.hasOwnProperty(idMaster)) {
      newAdjValues.push([physicalMap[idMaster] - stokBerjalan]);
    } else {
      newAdjValues.push([adjLama]);
    }
  }
  if (newAdjValues.length > 0) {
    dbSheet.getRange(2, 7, newAdjValues.length, 1).setValues(newAdjValues);
    SpreadsheetApp.getUi().alert("✅ Opname Berhasil! Stok fisik telah disinkronkan ke Database secara permanen.");
  }
}

/* ============================================================
 *  DIALOG UPLOAD (UI sidebar — UNCHANGED dari v6.0,
 *  hanya update teks deskripsi)
 * ============================================================ */
function showUploadDialog() {
  const html = `
    <!DOCTYPE html>
    <html>
      <head>
        <link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet">
        <style>
          body { font-family: sans-serif; padding: 20px; background-color: #f8fafc; }
          .drop-zone { border: 2px dashed #cbd5e1; border-radius: 8px; padding: 40px; text-align: center; transition: 0.3s; background: white; cursor: pointer; }
          .drop-zone:hover { border-color: #3b82f6; background-color: #eff6ff; }
        </style>
      </head>
      <body>
        <div class="max-w-md mx-auto text-center">
          <h2 class="text-lg font-bold mb-2 text-gray-800">Upload Data Sales / Pesanan</h2>
          <p class="text-xs text-gray-500 mb-4">Akan update DATA_SALES (trend). Jika file ada kolom <b>Nomor Resi</b>, sekaligus tambah ke LIST_PESANAN untuk QC.</p>
          <div id="dropZone" class="drop-zone" onclick="document.getElementById('fileInput').click()">
            <p id="fileNameDisplay" class="text-gray-400 text-sm">Klik atau seret file Excel/CSV</p>
            <input type="file" id="fileInput" accept=".csv, .xlsx" style="display:none">
          </div>
          <button id="uploadBtn" class="w-full mt-4 bg-blue-600 text-white py-2 rounded-lg font-bold hover:bg-blue-700 disabled:opacity-50" disabled>
            PROSES DATA
          </button>
          <div id="status" class="mt-4 text-sm font-medium"></div>
        </div>
        <script>
          const fileInput = document.getElementById('fileInput');
          const uploadBtn = document.getElementById('uploadBtn');
          const status = document.getElementById('status');
          const fileNameDisplay = document.getElementById('fileNameDisplay');
          fileInput.onchange = (e) => {
            if (e.target.files.length > 0) {
              fileNameDisplay.innerText = e.target.files[0].name;
              fileNameDisplay.classList.add('text-blue-600', 'font-bold');
              uploadBtn.disabled = false;
            }
          };
          uploadBtn.onclick = () => {
            const file = fileInput.files[0];
            const reader = new FileReader();
            status.innerHTML = '<p class="text-blue-500 animate-pulse">Sedang memproses...</p>';
            uploadBtn.disabled = true;
            reader.onload = (e) => {
              const content = e.target.result.split(',')[1];
              google.script.run
                .withSuccessHandler((msg) => {
                  status.innerHTML = '<p class="text-green-600 whitespace-pre-line">' + msg + '</p>';
                  setTimeout(() => google.script.host.close(), 6000);
                })
                .withFailureHandler((err) => {
                  status.innerHTML = '<p class="text-red-600">Error: ' + err + '</p>';
                  uploadBtn.disabled = false;
                })
                .processUploadedFile(content, file.name);
            };
            reader.readAsDataURL(file);
          };
        </script>
      </body>
    </html>
  `;
  const userInterface = HtmlService.createHtmlOutput(html).setTitle('Upload Data').setWidth(400);
  SpreadsheetApp.getUi().showSidebar(userInterface);
}

/* ============================================================
 *  PROCESS UPLOAD — Updated v7.0 untuk dual-write LIST_PESANAN
 * ============================================================ */
function processUploadedFile(base64Content, fileName) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const salesSheet = ss.getSheetByName("DATA_SALES");
  if (!salesSheet) throw new Error("Tab DATA_SALES tidak ditemukan!");

  const decoded = Utilities.base64Decode(base64Content);
  let rawData;
  if (fileName.toLowerCase().endsWith('.csv')) {
    const blob = Utilities.newBlob(decoded, "text/csv", fileName);
    rawData = Utilities.parseCsv(blob.getDataAsString());
  } else {
    try {
      const blob = Utilities.newBlob(decoded, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", fileName);
      const fileResource = { name: 'Temp_' + new Date().getTime(), mimeType: 'application/vnd.google-apps.spreadsheet' };
      let tempFile = Drive.Files.create
        ? Drive.Files.create(fileResource, blob)
        : Drive.Files.insert({title: fileResource.name, mimeType: fileResource.mimeType}, blob, {convert: true});
      const tempSs = SpreadsheetApp.openById(tempFile.id);
      rawData = tempSs.getSheets()[0].getDataRange().getValues();
      Drive.Files.remove(tempFile.id);
    } catch (e) {
      throw new Error("Gagal konversi file: " + e.toString());
    }
  }

  if (!rawData || rawData.length < 2) return "File kosong atau hanya berisi header.";

  const tz = ss.getSpreadsheetTimeZone();
  const todayStr = Utilities.formatDate(new Date(), tz, "yyyy-MM-dd");
  const uploadedAtStr = Utilities.formatDate(new Date(), tz, "yyyy-MM-dd HH:mm:ss");

  // Header detection (case-insensitive, partial keyword match)
  const header = rawData[0].map(h => String(h).toLowerCase().trim());
  const idxResi = findColumnIdx(header, ['resi', 'tracking', 'awb']);
  const idxSku  = findColumnIdx(header, ['sku', 'kode']);
  const idxQty  = findColumnIdx(header, ['jumlah', 'qty', 'quant', 'pcs']);
  const idxTgl  = findColumnIdx(header, ['tanggal', 'date']);
  const numCols = rawData[0].length;

  const listSheet = ss.getSheetByName("LIST_PESANAN");
  let batchId = null;
  if (listSheet && idxResi >= 0) {
    ensureListPesananHeader(listSheet);
    batchId = getNextBatchId(listSheet, todayStr);
  }

  const processedSales = [];
  const processedListPesanan = [];

  for (let i = 1; i < rawData.length; i++) {
    const row = rawData[i];
    let tgl, sku, qty, resi = "";

    // Prefer header-based detection; fallback ke logic positional v6.0
    if (idxSku >= 0 && idxQty >= 0) {
      sku  = row[idxSku] ? row[idxSku].toString().replace(/\s/g, "") : "";
      qty  = parseFloat(row[idxQty]) || 0;
      tgl  = (idxTgl >= 0) ? (parseAnyDate(row[idxTgl]) || todayStr) : todayStr;
      resi = (idxResi >= 0 && row[idxResi]) ? row[idxResi].toString().trim() : "";
    } else if (numCols >= 4) {
      tgl = parseAnyDate(row[0]) || todayStr;
      sku = row[2] ? row[2].toString().replace(/\s/g, "") : "";
      qty = parseFloat(row[3]) || 0;
    } else {
      tgl = todayStr;
      sku = row[1] ? row[1].toString().replace(/\s/g, "") : "";
      qty = parseFloat(row[2]) || 0;
    }
    if (!sku || qty === 0) continue;

    const idMatch  = sku.match(/^(\d+)/);
    const idMaster = idMatch ? idMatch[1] : sku;
    const pcsMatch = sku.toLowerCase().match(/-(\d+)pcs/);
    const multiplier = pcsMatch ? parseInt(pcsMatch[1]) : 1;
    const tglFinal = (tgl instanceof Date) ? Utilities.formatDate(tgl, tz, "yyyy-MM-dd") : tgl;

    // (1) DATA_SALES (existing — multiplier × qty = total pcs)
    processedSales.push([tglFinal, idMaster, multiplier * qty]);

    // (2) LIST_PESANAN (BARU di v7.0) — hanya kalau ada resi
    if (resi && batchId) {
      processedListPesanan.push([
        batchId, uploadedAtStr, resi, sku, qty,
        detectMarketplace(resi), 'pending', '', '', ''
      ]);
    }
  }

  if (processedSales.length > 0) {
    salesSheet.getRange(salesSheet.getLastRow() + 1, 1, processedSales.length, 3).setValues(processedSales);
  }
  if (processedListPesanan.length > 0 && listSheet) {
    listSheet.getRange(listSheet.getLastRow() + 1, 1, processedListPesanan.length, LIST_PESANAN_HEADER.length).setValues(processedListPesanan);
  }

  const salesResult = manageDataSales();
  let result = "✅ " + salesResult;
  if (processedListPesanan.length > 0) {
    result += `\n+ ${processedListPesanan.length} pesanan masuk LIST_PESANAN sebagai batch ${batchId}.`;
  } else if (idxResi < 0) {
    result += `\n(Tidak ada kolom resi terdeteksi — LIST_PESANAN tidak diupdate.)`;
  } else if (!listSheet) {
    result += `\n⚠️ Sheet LIST_PESANAN belum dibuat — pesanan tidak diteruskan ke QC.`;
  }
  return result;
}

/* ============================================================
 *  DATA_SALES MAINTENANCE (UNCHANGED dari v6.0)
 *  Aggregate per (tanggal, ID Master), drop > 30 hari.
 * ============================================================ */
function parseAnyDate(input) {
  if (input instanceof Date) return input;
  if (!isNaN(input) && typeof input === 'number') return new Date((input - 25569) * 86400 * 1000);
  const str = input.toString().trim();
  const dmy = str.match(/^(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{2,4})/);
  if (dmy) {
    let y = parseInt(dmy[3]);
    if (y < 100) y += 2000;
    return new Date(y, parseInt(dmy[2]) - 1, parseInt(dmy[1]));
  }
  const p = new Date(str);
  return isNaN(p.getTime()) ? null : p;
}

function manageDataSales() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName("DATA_SALES");
  const data = sheet.getDataRange().getValues();
  const header = ["Tanggal", "ID Master", "Total Pcs"];
  const today = new Date();
  const limitDate = new Date();
  limitDate.setDate(today.getDate() - 31);
  limitDate.setHours(0,0,0,0);
  const aggregated = {};
  if (data.length > 1) data.shift();
  data.forEach(row => {
    const rowDate = new Date(row[0]);
    if (isNaN(rowDate.getTime()) || rowDate < limitDate) return;
    const tglKey = Utilities.formatDate(rowDate, ss.getSpreadsheetTimeZone(), "yyyy-MM-dd");
    const key = `${tglKey}_${row[1]}`;
    if (aggregated[key]) {
      aggregated[key][2] += parseFloat(row[2]) || 0;
    } else {
      aggregated[key] = [tglKey, row[1].toString(), parseFloat(row[2]) || 0];
    }
  });
  const finalRows = Object.values(aggregated).sort((a,b) => b[0].localeCompare(a[0]));
  sheet.clearContents();
  sheet.getRange(1, 1, 1, header.length).setValues([header]);
  if (finalRows.length > 0) {
    sheet.getRange(2, 1, finalRows.length, 3).setValues(finalRows);
  }
  return `Total histori sales: ${finalRows.length} baris harian.`;
}

/* ============================================================
 *  LIST_PESANAN HELPERS (BARU di v7.0)
 * ============================================================ */
function ensureListPesananHeader(sheet) {
  const firstRow = sheet.getRange(1, 1, 1, LIST_PESANAN_HEADER.length).getValues()[0];
  const isEmpty = firstRow.every(c => c === "" || c === null);
  const matchesHeader = firstRow.every((c, i) => String(c).trim() === LIST_PESANAN_HEADER[i]);
  if (isEmpty) {
    sheet.getRange(1, 1, 1, LIST_PESANAN_HEADER.length).setValues([LIST_PESANAN_HEADER]);
    sheet.getRange(1, 1, 1, LIST_PESANAN_HEADER.length).setFontWeight("bold").setBackground("#e5e7eb");
    sheet.setFrozenRows(1);
  } else if (!matchesHeader) {
    Logger.log("Header LIST_PESANAN sudah ada tapi berbeda dari skema v7.0. Tidak diubah otomatis.");
  }
}

function getNextBatchId(sheet, todayStr) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return `${todayStr}-B1`;
  const colA = sheet.getRange(2, 1, lastRow - 1, 1).getValues();
  let maxN = 0;
  const prefix = todayStr + '-B';
  for (let i = 0; i < colA.length; i++) {
    const v = String(colA[i][0] || '');
    if (v.startsWith(prefix)) {
      const n = parseInt(v.substring(prefix.length)) || 0;
      if (n > maxN) maxN = n;
    }
  }
  return `${todayStr}-B${maxN + 1}`;
}

function findColumnIdx(headerArr, keywords) {
  for (let i = 0; i < headerArr.length; i++) {
    const h = String(headerArr[i]).toLowerCase().trim();
    for (const kw of keywords) {
      if (h.indexOf(kw) >= 0) return i;
    }
  }
  return -1;
}

function detectMarketplace(resi) {
  const r = String(resi).trim().toUpperCase();
  const prefixes = Object.keys(MARKETPLACE_PREFIXES).sort((a, b) => b.length - a.length);
  for (const p of prefixes) {
    if (r.startsWith(p)) return MARKETPLACE_PREFIXES[p];
  }
  return 'Unknown';
}

/* ============================================================
 *  PURGE LIST_PESANAN > N HARI (BARU di v7.0)
 *  Maintenance manual untuk jaga ukuran sheet.
 * ============================================================ */
function purgeOldListPesanan() {
  const ui = SpreadsheetApp.getUi();
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName('LIST_PESANAN');
  if (!sheet) {
    ui.alert('Sheet LIST_PESANAN tidak ditemukan.');
    return;
  }
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) {
    ui.alert('LIST_PESANAN kosong, tidak ada yang dihapus.');
    return;
  }

  const cutoff = new Date();
  cutoff.setDate(cutoff.getDate() - PURGE_DAYS);
  cutoff.setHours(0, 0, 0, 0);
  const cutoffStr = Utilities.formatDate(cutoff, ss.getSpreadsheetTimeZone(), "yyyy-MM-dd");

  const resp = ui.alert(
    'Konfirmasi Hapus',
    `Akan menghapus pesanan di LIST_PESANAN yang di-upload sebelum ${cutoffStr} (lebih dari ${PURGE_DAYS} hari).\n\nLanjutkan?`,
    ui.ButtonSet.YES_NO
  );
  if (resp !== ui.Button.YES) return;

  const data = sheet.getDataRange().getValues();
  const newRows = [data[0]]; // keep header
  let removed = 0;
  for (let i = 1; i < data.length; i++) {
    const uploadedAt = data[i][1];
    let dt = (uploadedAt instanceof Date) ? uploadedAt : new Date(String(uploadedAt));
    if (dt && !isNaN(dt.getTime()) && dt >= cutoff) {
      newRows.push(data[i]);
    } else {
      removed++;
    }
  }
  sheet.clearContents();
  if (newRows.length > 0) {
    sheet.getRange(1, 1, newRows.length, newRows[0].length).setValues(newRows);
    sheet.getRange(1, 1, 1, newRows[0].length).setFontWeight("bold").setBackground("#e5e7eb");
    sheet.setFrozenRows(1);
  }
  ui.alert(`✅ Berhasil. Dihapus ${removed} baris pesanan lama (>${PURGE_DAYS} hari). Tersisa ${newRows.length - 1} pesanan aktif.`);
}
