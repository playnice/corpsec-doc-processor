"""
Teamwork.sg Discovery Script
Navigates the site step-by-step, captures screenshots and HTML snippets
at each stage so we can identify the exact CSS selectors to use.

Usage:
    python tools/discover_teamwork.py
    python tools/discover_teamwork.py --step 6    (skip to a specific step)

All screenshots saved to: data/Screenshots/discovery/
"""

import logging
import os
import sys
import time
from pathlib import Path

# Allow imports from project root (parent of tools/)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

load_dotenv(_PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("discover")

OUTPUT_DIR = _PROJECT_ROOT / "data" / "Screenshots" / "discovery"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def save(page, label, dump_html_selector=None):
    """Save a screenshot and optionally dump HTML for a selector."""
    ts = time.strftime("%H%M%S")
    ss_path = OUTPUT_DIR / f"{ts}_{label}.png"
    page.screenshot(path=str(ss_path), full_page=True)
    logger.info("Screenshot: %s", ss_path)

    if dump_html_selector:
        try:
            html = page.locator(dump_html_selector).first.inner_html()
            html_path = OUTPUT_DIR / f"{ts}_{label}.html"
            html_path.write_text(html, encoding="utf-8")
            logger.info("HTML dump: %s", html_path)
        except Exception as e:
            logger.warning("Could not dump HTML for '%s': %s", dump_html_selector, e)


def main():
    start_step = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[1] == "--step" else 0

    company_id = os.getenv("TEAMWORK_COMPANY_ID", "")
    username = os.getenv("TEAMWORK_USERNAME", "")
    password = os.getenv("TEAMWORK_PASSWORD", "")
    login_url = os.getenv("TEAMWORK_LOGIN_URL", "https://login.teamwork.sg/")

    if not all([company_id, username, password]):
        logger.error("Set TEAMWORK_COMPANY_ID, TEAMWORK_USERNAME, TEAMWORK_PASSWORD in .env")
        sys.exit(1)

    logger.info("=== Teamwork.sg Discovery Script ===")
    logger.info("Screenshots will be saved to: %s", OUTPUT_DIR)
    logger.info("Browser will open visibly so you can watch.\n")

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=False)
    context = browser.new_context(viewport={"width": 1920, "height": 1080})
    page = context.new_page()
    page.set_default_timeout(30_000)

    try:
        # --- Step 0: Login ---
        logger.info(">>> Step 0: LOGIN")
        page.goto(login_url, wait_until="networkidle")
        save(page, "step0_login_page")

        page.fill('input[name="client_id"]', company_id)
        page.fill('input[name="uname"]', username)
        page.fill('input[name="upsd"]', password)
        page.click("#target_submit_btn")
        page.wait_for_url("**/dashboard**", timeout=30_000)
        save(page, "step1_dashboard", dump_html_selector="body")
        logger.info("Logged in successfully.\n")

        if start_step > 2:
            logger.info("Skipping to step %d...", start_step)

        # --- Step 2: Click Records ---
        if start_step <= 2:
            logger.info(">>> Step 2: RECORDS sidebar")
            # Dump sidebar HTML to find exact selectors
            save(page, "step2_before_records", dump_html_selector=".left_col, #sidebar-menu, .nav.side-menu, .sidebar, nav")
            input("Press Enter to click 'Records'...")

            records = page.locator('a:has-text("Records")').first
            records.click()
            time.sleep(1)
            save(page, "step2_records_expanded", dump_html_selector=".left_col, #sidebar-menu, .nav.side-menu, .sidebar, nav")

        # --- Step 3: Click Companies ---
        if start_step <= 3:
            logger.info(">>> Step 3: COMPANIES sub-menu")
            input("Press Enter to click 'Companies'...")

            companies = page.locator('a:has-text("Companies")').first
            companies.click()
            page.wait_for_load_state("networkidle")
            time.sleep(1)
            save(page, "step3_companies_page", dump_html_selector="body")

        # --- Step 4: Search ---
        if start_step <= 4:
            logger.info(">>> Step 4: SEARCH BAR")
            save(page, "step4_search_area", dump_html_selector=".dataTables_filter, .search-form, form, .toolbar")

            test_company = input("Enter a company name to search (or press Enter to skip): ").strip()
            if test_company:
                # Strip entity suffixes for better search
                words = test_company.split()
                entity_suffixes = {"pte", "ltd", "sdn", "bhd", "inc", "corp", "co",
                                   "limited", "private", "company", "incorporated",
                                   "llc", "llp", "lp", "plc"}
                while words and words[-1].lower().rstrip(".,") in entity_suffixes:
                    words.pop()
                search_term = " ".join(words).strip().rstrip(",.")
                logger.info("Search term (entity stripped): '%s'", search_term)

                search_box = page.locator('#datatable_filter input[type="search"]').first
                search_box.fill(search_term)
                time.sleep(2)  # DataTables filters on keyup
                save(page, "step4_search_results", dump_html_selector="#datatable, .dataTables_wrapper")

        # --- Step 5: Click View ---
        if start_step <= 5:
            logger.info(">>> Step 5: VIEW button in Action column")
            logger.info("Using selector: #datatable tbody tr a[href*='view_company']")
            save(page, "step5_before_view")
            input("Press Enter to click the first 'View' button...")

            # Target View button specifically in the datatable (not other "View" links on page)
            view_btn = page.locator('#datatable tbody tr a[href*="view_company"]').first
            view_btn.click()
            page.wait_for_load_state("networkidle")
            time.sleep(1)
            save(page, "step5_company_profile", dump_html_selector="body")

        # --- Step 6: Files tab ---
        if start_step <= 6:
            logger.info(">>> Step 6: FILES tab")
            save(page, "step6_before_files_tab", dump_html_selector=".nav-tabs, [role='tablist'], .tabs")
            input("Press Enter to click 'Files' tab...")

            files_tab = page.locator('a:has-text("Files")').first
            files_tab.click()
            page.wait_for_load_state("networkidle")
            time.sleep(1)
            save(page, "step6_files_tab", dump_html_selector="body")

        # --- Step 7: Upload button ---
        if start_step <= 7:
            logger.info(">>> Step 7: UPLOAD button")
            save(page, "step7_before_upload")
            input("Press Enter to click 'Upload' button...")

            # Multiple elements match "Upload" — hidden per-row buttons exist.
            # Find the first visible one (top-right Upload button).
            candidates = page.locator('a:has-text("Upload"), button:has-text("Upload")').all()
            clicked = False
            for i, btn in enumerate(candidates):
                vis = btn.is_visible()
                tag = btn.evaluate("el => el.tagName")
                href = btn.get_attribute("href") or ""
                cls = btn.get_attribute("class") or ""
                logger.info("  Candidate[%d] <%s> visible=%s class='%s' href='%s'", i, tag, vis, cls[:50], href[:60])
                if vis and not clicked:
                    btn.click()
                    clicked = True
            if not clicked:
                logger.error("No visible Upload button found!")
            else:
                page.wait_for_load_state("networkidle")
                time.sleep(1)
            save(page, "step7_upload_form", dump_html_selector="form, .modal, .modal-content, body")

        # --- Step 8: Choose file ---
        if start_step <= 8:
            logger.info(">>> Step 8: CHOOSE FILE")
            logger.info("Capturing upload form HTML for selector discovery...")
            save(page, "step8_upload_form_detail", dump_html_selector="form, .modal-body, .upload-form, body")

            # Dump all input/select/textarea/button elements
            fields = page.locator("input, select, textarea, button").all()
            logger.info("\nForm fields found:")
            for i, field in enumerate(fields):
                try:
                    vis = field.is_visible()
                    tag = field.evaluate("el => el.tagName")
                    name = field.get_attribute("name") or ""
                    ftype = field.get_attribute("type") or ""
                    placeholder = field.get_attribute("placeholder") or ""
                    fid = field.get_attribute("id") or ""
                    classes = field.get_attribute("class") or ""
                    value = field.get_attribute("value") or ""
                    onclick = field.get_attribute("onclick") or ""
                    logger.info(
                        "  [%d] <%s> visible=%s name='%s' type='%s' id='%s' "
                        "placeholder='%s' class='%s' value='%s' onclick='%s'",
                        i, tag, vis, name, ftype, fid, placeholder,
                        classes[:60], value[:40], onclick[:60],
                    )
                except Exception:
                    pass

            # Look for file input specifically
            file_inputs = page.locator('input[type="file"]').all()
            logger.info("\nFile inputs found: %d", len(file_inputs))
            for i, fi in enumerate(file_inputs):
                vis = fi.is_visible()
                name = fi.get_attribute("name") or ""
                fid = fi.get_attribute("id") or ""
                accept = fi.get_attribute("accept") or ""
                logger.info("  file_input[%d] visible=%s name='%s' id='%s' accept='%s'", i, vis, name, fid, accept)

            # Try selecting a test file
            test_file = input("Enter full path to a test PDF (or press Enter to skip file upload): ").strip()
            if test_file and Path(test_file).exists():
                file_input = page.locator('input[type="file"]').first
                file_input.set_input_files(test_file)
                time.sleep(2)
                save(page, "step8_file_selected")
                logger.info("File selected successfully.")
            elif test_file:
                logger.warning("File not found: %s", test_file)

        # --- Step 9: Description field ---
        if start_step <= 9:
            logger.info(">>> Step 9: DESCRIPTION FIELD")
            save(page, "step9_before_description")

            # Look for description-like fields
            desc_selectors = [
                'textarea[name*="desc" i]', 'input[name*="desc" i]',
                'textarea[placeholder*="escription"]', 'input[placeholder*="escription"]',
                'textarea#description', 'input#description',
                'textarea', 'input[type="text"]',
            ]
            for sel in desc_selectors:
                matches = page.locator(sel).all()
                for j, m in enumerate(matches):
                    vis = m.is_visible()
                    name = m.get_attribute("name") or ""
                    fid = m.get_attribute("id") or ""
                    ph = m.get_attribute("placeholder") or ""
                    logger.info("  desc match '%s'[%d] visible=%s name='%s' id='%s' placeholder='%s'",
                                sel, j, vis, name, fid, ph)

            test_desc = input("Enter test description (or press Enter to skip): ").strip()
            if test_desc:
                # Try the most specific selectors first
                filled = False
                for sel in desc_selectors[:6]:
                    try:
                        el = page.locator(sel).first
                        if el.is_visible(timeout=1000):
                            el.fill(test_desc)
                            filled = True
                            logger.info("Filled description using selector: %s", sel)
                            break
                    except Exception:
                        continue
                if not filled:
                    logger.warning("Could not find a visible description field to fill.")
                time.sleep(1)
                save(page, "step9_description_filled")

        # --- Step 10: Resolution Date ---
        if start_step <= 10:
            logger.info(">>> Step 10: RESOLUTION DATE FIELD")
            save(page, "step10_before_date")

            # Look for date-like fields
            date_selectors = [
                'input[name*="resolution" i]', 'input[name*="date" i]',
                'input[placeholder*="ate"]', 'input[type="date"]',
                'input.datepicker', 'input.date-picker',
                'input[class*="date" i]',
            ]
            for sel in date_selectors:
                matches = page.locator(sel).all()
                for j, m in enumerate(matches):
                    vis = m.is_visible()
                    name = m.get_attribute("name") or ""
                    fid = m.get_attribute("id") or ""
                    ph = m.get_attribute("placeholder") or ""
                    cls = m.get_attribute("class") or ""
                    logger.info("  date match '%s'[%d] visible=%s name='%s' id='%s' placeholder='%s' class='%s'",
                                sel, j, vis, name, fid, ph, cls[:60])

            test_date = input("Enter test date in dd/mm/yyyy e.g. 15/03/2026 (or press Enter to skip): ").strip()
            if test_date:
                filled = False
                for sel in date_selectors:
                    try:
                        el = page.locator(sel).first
                        if el.is_visible(timeout=1000):
                            el.click()
                            # Set value via JS to avoid datepicker intercepting keystrokes
                            el.evaluate("(el, val) => { el.value = val; }", test_date)
                            el.dispatch_event("input")
                            el.dispatch_event("change")
                            page.keyboard.press("Escape")
                            time.sleep(0.5)
                            page.keyboard.press("Tab")
                            filled = True
                            logger.info("Filled date using selector: %s", sel)
                            break
                    except Exception:
                        continue
                if not filled:
                    logger.warning("Could not find a visible date field to fill.")
                time.sleep(1)
                save(page, "step10_date_filled")

        # --- Step 11: Category ---
        if start_step <= 11:
            logger.info(">>> Step 11: CATEGORY FIELD")
            save(page, "step11_before_category")

            # Dump the doc_category <select> options from the page
            cat_select = page.locator('select[name*="doc_category"]').first
            try:
                options_data = cat_select.evaluate("""
                    el => Array.from(el.options).map(o => ({value: o.value, text: o.textContent.trim()}))
                """)
                logger.info("  Category <select> options (%d):", len(options_data))
                for o in options_data:
                    logger.info("    value='%s' text='%s'", o["value"], o["text"])
            except Exception as e:
                logger.warning("  Could not read <select> options: %s", e)

            test_cat = input("Enter test category e.g. Directors' Written Resolution (or press Enter to skip): ").strip()
            if test_cat:
                # Use fuzzy word-overlap matching (same as teamwork_uploader)
                from teamwork_uploader import TeamworkUploader
                val, label, score = TeamworkUploader._best_category_match(test_cat)
                logger.info("  Fuzzy match: '%s' → '%s' (value=%s, score=%.2f)", test_cat, label, val, score)

                if score >= 0.15 and val:
                    # Set value directly via JS on hidden <select>, trigger Select2 change
                    page.evaluate("""
                        ([selectSel, optionValue]) => {
                            const sel = document.querySelector(selectSel);
                            if (!sel) return;
                            for (const opt of sel.options) {
                                if (opt.value === optionValue) {
                                    opt.selected = true;
                                    break;
                                }
                            }
                            $(sel).trigger('change');
                        }
                    """, ['select[name*="doc_category"]', val])
                    time.sleep(0.5)
                    logger.info("  Category set via JS: '%s'", label)
                else:
                    logger.warning("  No good match found (score=%.2f).", score)

                save(page, "step11_category_filled")

        # --- Step 12: Save ---
        if start_step <= 12:
            logger.info(">>> Step 12: SAVE BUTTON")
            save(page, "step12_before_save")

            # Look for Save/Submit buttons
            save_selectors = [
                'button:has-text("Save")', 'input[type="submit"]',
                'button[type="submit"]', 'a.btn:has-text("Save")',
                'button:has-text("Submit")', 'button:has-text("Upload")',
            ]
            for sel in save_selectors:
                matches = page.locator(sel).all()
                for j, m in enumerate(matches):
                    vis = m.is_visible()
                    tag = m.evaluate("el => el.tagName")
                    text = (m.text_content() or "")[:40]
                    cls = m.get_attribute("class") or ""
                    fid = m.get_attribute("id") or ""
                    logger.info("  save match '%s'[%d] <%s> visible=%s text='%s' id='%s' class='%s'",
                                sel, j, tag, vis, text, fid, cls[:60])

            confirm = input("Type 'save' to click Save, or press Enter to skip: ").strip().lower()
            if confirm == "save":
                # Find and click the first visible Save button
                for sel in save_selectors:
                    try:
                        matches = page.locator(sel).all()
                        for m in matches:
                            if m.is_visible():
                                m.click()
                                logger.info("Clicked Save using selector: %s", sel)
                                page.wait_for_load_state("networkidle")
                                time.sleep(2)
                                save(page, "step12_after_save")
                                break
                        else:
                            continue
                        break
                    except Exception as e:
                        logger.warning("Save click error with '%s': %s", sel, e)
                        continue

        logger.info("\n=== Discovery complete! ===")
        logger.info("Screenshots saved to: %s", OUTPUT_DIR)
        logger.info("Review the screenshots and HTML dumps to identify exact selectors.")
        input("\nPress Enter to close the browser...")

    except Exception:
        logger.exception("Discovery failed at current step")
        save(page, "discovery_error")
        input("Press Enter to close browser after error...")

    finally:
        context.close()
        browser.close()
        pw.stop()


if __name__ == "__main__":
    main()
