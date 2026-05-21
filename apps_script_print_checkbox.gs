/**
 * ADD-ON: TOMBOL (CHECKBOX) MULAI/SELESAI PRINT — PERMINTAAN_RESTOCK
 *
 * Tujuan: memindahkan langkah tim PRINT dari app.py (Python) ke Google Sheet,
 * supaya tim print & gudang cukup kerja di sheet PERMINTAAN_RESTOCK saja.
 *
 * Kenapa file terpisah & berbasis NAMA kolom (bukan posisi):
 *   Sheet PERMINTAAN_RESTOCK yang asli ternyata punya kolom tambahan
 *   (Jml_Lbr, Varian) sehingga posisi kolom GESER dari skrip lama. Add-on ini
 *   mencari kolom lewat JUDUL di baris 1 — jadi tahan walau kolom dipindah.
 *   Logika approve→LOG_MASUK milik skrip Anda yang sudah jalan TIDAK disentuh.
 *
 * Alur baru tim print (di sheet):
 *   1. Ketik nama di kolom "Print_Operator".
 *   2. Centang "Mulai_Print"  → Status jadi "in_progress" + "Tanggal_Mulai_Print" terisi.
 *   3. Selesai cetak, centang "Selesai_Print" → Status jadi "menunggu_approval".
 *   (Lalu gudang isi Jumlah_Aktual_Gudang + centang Approve seperti biasa.)
 *
 * CARA PASANG (sekali saja):
 *   A. Extensions → Apps Script. Klik "+" di samping "Files" → Script →
 *      beri nama "PrintCheckbox" → paste seluruh isi file ini → Save (Ctrl+S).
 *   B. Jalankan fungsi setup sekali: pilih fungsi `setupPrintCheckboxColumns`
 *      di dropdown atas, klik Run. (Akan minta izin pertama kali → Allow.)
 *      → Ini menambah 2 kolom checkbox + pewarnaan Status.
 *   C. Pasang trigger: ikon jam (Triggers) → Add Trigger:
 *        - Function: restockPrintOnEdit
 *        - Event source: From spreadsheet
 *        - Event type: On edit
 *      → Save. (Trigger ini terpisah & tidak bentrok dgn onEdit Anda.)
 */

var RP_SHEET_NAME = "PERMINTAAN_RESTOCK";
var RP_COL_MULAI = "Mulai_Print";
var RP_COL_SELESAI = "Selesai_Print";

/** Baca baris 1 → map { judulKolom : nomorKolom (1-based) }. */
function rpHeaderMap_(sheet) {
  var lastCol = sheet.getLastColumn();
  if (lastCol < 1) return {};
  var hdr = sheet.getRange(1, 1, 1, lastCol).getValues()[0];
  var map = {};
  for (var i = 0; i < hdr.length; i++) {
    var name = String(hdr[i] || "").trim();
    if (name) map[name] = i + 1;
  }
  return map;
}

/**
 * SETUP (jalankan SEKALI dari editor):
 *   - Tambah kolom "Mulai_Print" & "Selesai_Print" di paling kanan kalau belum ada.
 *   - Pasang validasi checkbox + warna pembeda.
 *   - Bonus: conditional formatting kolom Status biar enak dibaca.
 */
function setupPrintCheckboxColumns() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(RP_SHEET_NAME);
  if (!sheet) {
    SpreadsheetApp.getUi().alert('Sheet "' + RP_SHEET_NAME + '" tidak ditemukan.');
    return;
  }

  var map = rpHeaderMap_(sheet);
  var added = [];

  // Tambah kolom yang belum ada, di paling kanan.
  [RP_COL_MULAI, RP_COL_SELESAI].forEach(function (colName) {
    if (!map[colName]) {
      var newCol = sheet.getLastColumn() + 1;
      sheet.getRange(1, newCol).setValue(colName);
      map[colName] = newCol;
      added.push(colName);
    }
  });

  var nMulai = map[RP_COL_MULAI];
  var nSelesai = map[RP_COL_SELESAI];
  var nStatus = map["Status"];

  // Header style untuk 2 kolom baru
  sheet.getRange(1, nMulai).setFontWeight("bold").setBackground("#dbeafe").setHorizontalAlignment("center");
  sheet.getRange(1, nSelesai).setFontWeight("bold").setBackground("#dcfce7").setHorizontalAlignment("center");
  sheet.setFrozenRows(1);

  // Validasi checkbox + warna sel (baris 2..1000)
  var cb = SpreadsheetApp.newDataValidation().requireCheckbox().build();
  sheet.getRange(2, nMulai, 999, 1).setDataValidation(cb).setBackground("#eff6ff").setHorizontalAlignment("center");
  sheet.getRange(2, nSelesai, 999, 1).setDataValidation(cb).setBackground("#f0fdf4").setHorizontalAlignment("center");
  sheet.setColumnWidth(nMulai, 95);
  sheet.setColumnWidth(nSelesai, 105);

  // Bonus: conditional formatting kolom Status (warna per status) — biar
  // tim cukup "lihat sheet" untuk tahu posisi tiap permintaan.
  if (nStatus) {
    var statusRange = sheet.getRange(2, nStatus, 999, 1);
    var rules = sheet.getConditionalFormatRules().filter(function (r) {
      // buang rule lama yang menyasar kolom Status (biar idempotent)
      var ranges = r.getRanges();
      for (var i = 0; i < ranges.length; i++) {
        if (ranges[i].getColumn() === nStatus) return false;
      }
      return true;
    });
    function ruleText(text, bg) {
      return SpreadsheetApp.newConditionalFormatRule()
        .whenTextEqualTo(text).setBackground(bg).setRanges([statusRange]).build();
    }
    rules.push(ruleText("pending", "#fff3cd"));            // kuning muda
    rules.push(ruleText("in_progress", "#cfe2ff"));        // biru muda
    rules.push(ruleText("menunggu_approval", "#ffe0b2"));  // oranye muda
    rules.push(ruleText("approved", "#d1e7dd"));           // hijau
    rules.push(ruleText("rejected", "#f8d7da"));           // merah
    rules.push(ruleText("dibatalkan", "#e2e3e5"));         // abu
    sheet.setConditionalFormatRules(rules);
  }

  SpreadsheetApp.getUi().alert(
    "Setup selesai",
    (added.length ? "Kolom ditambah: " + added.join(", ") + ".\n" : "Kolom checkbox sudah ada (di-refresh).\n") +
    "\nLangkah berikutnya: pasang trigger 'restockPrintOnEdit' (ikon jam → Add Trigger → On edit).\n\n" +
    "Alur print di sheet:\n" +
    "1. Ketik nama di Print_Operator.\n" +
    "2. Centang Mulai_Print → in_progress.\n" +
    "3. Centang Selesai_Print → menunggu_approval.",
    SpreadsheetApp.getUi().ButtonSet.OK
  );
}

/**
 * TRIGGER (pasang sbg installable On-edit). Hanya menangani 2 checkbox print.
 * Tidak menyentuh kolom/aksi lain — aman berdampingan dgn onEdit Anda.
 */
function restockPrintOnEdit(e) {
  try {
    if (!e || !e.range) return;
    var sheet = e.range.getSheet();
    if (sheet.getName() !== RP_SHEET_NAME) return;

    var row = e.range.getRow();
    if (row < 2) return;
    var col = e.range.getColumn();

    var map = rpHeaderMap_(sheet);
    var nMulai = map[RP_COL_MULAI];
    var nSelesai = map[RP_COL_SELESAI];
    // Hanya bereaksi pada kolom Mulai_Print / Selesai_Print.
    if (col !== nMulai && col !== nSelesai) return;
    if (e.range.getValue() !== true) return; // hanya saat dicentang TRUE

    var nStatus = map["Status"];
    var nTglMulai = map["Tanggal_Mulai_Print"];
    var nOperator = map["Print_Operator"];
    var nCatatan = map["Catatan"];
    if (!nStatus) return; // tanpa kolom Status, tidak bisa apa-apa

    var ss = e.source;
    var tz = ss.getSpreadsheetTimeZone();
    var now = Utilities.formatDate(new Date(), tz, "yyyy-MM-dd HH:mm:ss");
    var status = String(sheet.getRange(row, nStatus).getValue() || "").toLowerCase().trim();

    function note(msg) { if (nCatatan) sheet.getRange(row, nCatatan).setValue(msg); }

    // ===== MULAI_PRINT dicentang =====
    if (col === nMulai) {
      if (status === "in_progress") return; // sudah mulai (idempotent)
      if (status && status !== "pending") {
        e.range.setValue(false);
        note('Mulai di-skip: status saat ini "' + status + '" (hanya "pending" yang bisa dimulai).');
        return;
      }
      sheet.getRange(row, nStatus).setValue("in_progress");
      if (nTglMulai && !sheet.getRange(row, nTglMulai).getValue()) {
        sheet.getRange(row, nTglMulai).setValue(now);
      }
      var opName = nOperator ? String(sheet.getRange(row, nOperator).getValue() || "").trim() : "";
      if (!opName) {
        note("Sudah in_progress — jangan lupa isi kolom Print_Operator (nama Anda).");
        ss.toast("Status → in_progress. Isi nama di Print_Operator ya.", "Mulai cetak", 5);
      } else {
        ss.toast("Status → in_progress oleh " + opName + ".", "Mulai cetak", 4);
      }
      return;
    }

    // ===== SELESAI_PRINT dicentang =====
    if (col === nSelesai) {
      if (status === "menunggu_approval") return; // idempotent
      if (status !== "in_progress") {
        e.range.setValue(false);
        note('Selesai di-skip: status saat ini "' + status + '" (centang Mulai_Print dulu).');
        return;
      }
      sheet.getRange(row, nStatus).setValue("menunggu_approval");
      ss.toast("Status → menunggu_approval. Menunggu verifikasi gudang.", "Selesai cetak", 5);
      return;
    }
  } catch (err) {
    try {
      Logger.log("restockPrintOnEdit error: " + err + "\n" + (err.stack || ""));
    } catch (e2) {}
  }
}
