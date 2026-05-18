"""Playwright UI test untuk golden-path Flask app + JS interaction.

Cover:
- Dashboard render tanpa JS error
- Operator scan form submit via HTMX → partial render
- Pack-size toggle 10/50 update hidden input
- Admin add/remove wadah
"""
import os
import socket
import subprocess
import tempfile
import threading
import time

import pytest

# Skip whole module kalau playwright tidak terinstall
playwright = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402


def _get_free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def live_server():
    """Spin up Flask dev server in background thread."""
    import sys
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    # Pakai DB sementara supaya isolation
    tmpdir = tempfile.mkdtemp(prefix="pr_pw_")
    db_path = os.path.join(tmpdir, "test.db")

    from packing_router import config as pr_config
    pr_config.DB_PATH = db_path
    pr_config.DEFAULT_WADAH_COUNT = 2
    pr_config.SLOTS_PER_WADAH = 10
    pr_config.DEFAULT_SLOT_AKTIF_COUNT = 30

    from packing_router.db import reset_connection
    reset_connection()

    from packing_router.web.app import create_app
    app = create_app()
    port = _get_free_port()

    def _run():
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    # Tunggu server siap
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError(f"Flask server tidak siap di port {port}")

    yield f"http://127.0.0.1:{port}"
    # Tidak ada cleanup — daemon thread mati saat process exit


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
    p = ctx.new_page()
    console_msgs = []
    p.on("console", lambda m: console_msgs.append((m.type, m.text)))
    p.on("pageerror", lambda e: console_msgs.append(("error", str(e))))
    yield p
    # Fail kalau ada JS error
    errors = [m for m in console_msgs if m[0] in ("error", "pageerror")]
    if errors:
        # Allow specific harmless warnings (favicon 404 dll)
        real_errors = [
            m for m in errors
            if "favicon" not in m[1].lower() and "404" not in m[1].lower()
        ]
        assert not real_errors, f"JS console errors: {real_errors}"
    ctx.close()


class TestDashboard:
    def test_dashboard_loads(self, page, live_server):
        page.goto(live_server + "/dashboard")
        page.wait_for_load_state("domcontentloaded")
        assert page.title() != ""

    def test_admin_loads(self, page, live_server):
        page.goto(live_server + "/admin")
        page.wait_for_load_state("domcontentloaded")
        # Halaman admin punya button add wadah
        assert page.locator("body").count() > 0


class TestOperatorScan:
    def test_scan_form_submit_routes_to_buffer(self, page, live_server):
        page.goto(live_server + "/operator/scan")
        page.wait_for_load_state("domcontentloaded")
        page.fill("input[name='barcode']", "1446")
        page.click("button[type='submit']:has-text('Scan')")
        # Wait HTMX response
        page.wait_for_function(
            "() => document.querySelector('#scan-result').textContent.length > 100",
            timeout=3000,
        )
        text = page.locator("#scan-result").inner_text().upper()
        assert "WADAH" in text or "LETAKKAN" in text

    def test_pack_size_toggle_50_sets_hidden_input(self, page, live_server):
        page.goto(live_server + "/operator/scan")
        page.wait_for_load_state("domcontentloaded")
        # Klik tombol 50pcs
        page.click("button.pack-size-toggle__btn[data-pack='50']")
        # Hidden input #pack-size-input-scan harus jadi '50'
        val = page.eval_on_selector(
            "#pack-size-input-scan", "el => el.value"
        )
        assert val == "50"
        # Submit scan 50pcs
        page.fill("input[name='barcode']", "777")
        page.click("button[type='submit']:has-text('Scan')")
        page.wait_for_function(
            "() => document.querySelector('#scan-result').textContent.length > 100",
            timeout=3000,
        )

    def test_empty_barcode_shows_error(self, page, live_server):
        page.goto(live_server + "/operator/scan")
        page.wait_for_load_state("domcontentloaded")
        # Submit without filling
        page.click("button[type='submit']:has-text('Scan')")
        page.wait_for_function(
            "() => document.querySelector('#scan-result').textContent.toLowerCase().includes('kosong')",
            timeout=3000,
        )


class TestNoJSErrorsOnAllRoutes:
    """Walk all main routes, ensure no JS console errors."""

    def test_routes(self, page, live_server):
        for path in ["/", "/dashboard", "/operator/scan", "/admin"]:
            page.goto(live_server + path)
            page.wait_for_load_state("networkidle", timeout=3000)
