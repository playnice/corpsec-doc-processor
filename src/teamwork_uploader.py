"""
Teamwork.sg Uploader module.
Uses Playwright browser automation to upload documents to teamwork.sg.

teamwork.sg is a Corporate Secretarial System with no public API,
so we automate the web UI via a real browser to handle reCAPTCHA v3,
CSRF tokens, and session management automatically.

Upload workflow:
  0. Login
  1. Dashboard
  2. Records (left sidebar menu)
  3. Companies
  4. Search company name
  5. Click View on matching row
  6. Go to Files tab
  7. Click Upload button
  8. Choose file
  9. Fill Description (document content/subject)
  10. Fill Resolution Date
  11. Select Category from dropdown
  12. Click Save
"""

import logging
import re
import time
from pathlib import Path

from playwright.sync_api import (
    sync_playwright, Browser, BrowserContext, Page,
    TimeoutError as PwTimeout,
)

import config
from ai_analyzer import DocumentMetadata

logger = logging.getLogger(__name__)

# Timeouts in milliseconds
NAV_TIMEOUT = 30_000
ACTION_TIMEOUT = 15_000
UPLOAD_TIMEOUT = 60_000


def _strip_entity_type(company_name: str) -> str:
    """Strip legal entity suffixes (Pte, Ltd, Sdn, Bhd, etc.) for search purposes."""
    # Only strip common legal form abbreviations, not generic words like "company"
    _SEARCH_STRIP = {
        "pte", "ltd", "pte.", "ltd.", "sdn", "bhd", "sdn.", "bhd.",
        "inc", "inc.", "corp", "corp.", "co", "co.",
        "llc", "llp", "lp", "plc", "l.p.", "l.p",
    }
    words = company_name.split()
    while words and words[-1].lower().rstrip(".,") in _SEARCH_STRIP:
        words.pop()
    return " ".join(words).strip().rstrip(",.")


class TeamworkUploader:
    """Client for uploading documents to teamwork.sg via browser automation."""

    def __init__(self):
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._logged_in = False

        self._login_url = config.TEAMWORK_LOGIN_URL
        self._company_id = config.TEAMWORK_COMPANY_ID
        self._username = config.TEAMWORK_USERNAME
        self._password = config.TEAMWORK_PASSWORD
        self._headless = config.TEAMWORK_HEADLESS
        self._screenshots_dir = config.TEAMWORK_SCREENSHOTS_DIR

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _ensure_browser(self) -> Page:
        """Launch browser and return the active page (lazy init)."""
        if self._page and not self._page.is_closed():
            return self._page

        logger.info("Launching browser (headless=%s)...", self._headless)
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self._headless)
        self._context = self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        self._page = self._context.new_page()
        self._page.set_default_timeout(NAV_TIMEOUT)
        return self._page

    def close(self) -> None:
        """Shut down the browser cleanly."""
        try:
            if self._page:
                self._page.close()
        except Exception:
            pass
        try:
            if self._context:
                self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        finally:
            self._page = None
            self._context = None
            self._browser = None
            self._playwright = None
            self._playwright = None
            self._logged_in = False

    # ------------------------------------------------------------------
    # Step 0: Login
    # ------------------------------------------------------------------

    def _login(self) -> bool:
        """Log in to teamwork.sg. Returns True on success."""
        if self._logged_in:
            return True

        if not self._company_id or not self._username or not self._password:
            logger.warning(
                "Teamwork.sg credentials not configured — set "
                "TEAMWORK_COMPANY_ID, TEAMWORK_USERNAME, TEAMWORK_PASSWORD in .env"
            )
            return False

        page = self._ensure_browser()

        try:
            logger.info("[Step 0] Logging in to %s", self._login_url)
            page.goto(self._login_url, wait_until="networkidle")

            page.fill('input[name="client_id"]', self._company_id)
            page.fill('input[name="uname"]', self._username)
            page.fill('input[name="upsd"]', self._password)

            # Click login (triggers reCAPTCHA v3 automatically in the real browser)
            page.click("#target_submit_btn")

            # Wait for dashboard
            page.wait_for_url("**/dashboard**", timeout=NAV_TIMEOUT)
            logger.info("[Step 0] Login successful — reached dashboard.")
            self._save_screenshot("step0_dashboard")
            self._logged_in = True
            return True

        except PwTimeout:
            logger.error("Login timed out — check credentials or network.")
            self._save_screenshot("step0_login_timeout")
            return False
        except Exception:
            logger.exception("Login failed")
            self._save_screenshot("step0_login_error")
            return False

    # ------------------------------------------------------------------
    # Step 2-3: Navigate to Records > Companies
    # ------------------------------------------------------------------

    def _navigate_to_companies(self) -> bool:
        """Click Records in sidebar, then Companies sub-menu."""
        page = self._page
        if not page:
            return False

        try:
            # Step 2: Click "Records" in the left sidebar to expand sub-menu
            logger.info("[Step 2] Clicking Records in sidebar...")
            records_link = page.locator(
                '#sidebar-menu a:has-text("Records"), '
                '.nav.side-menu a:has-text("Records"), '
                '.sidebar a:has-text("Records"), '
                'a:has-text("Records")'
            ).first
            records_link.click(timeout=ACTION_TIMEOUT)
            time.sleep(1)

            # Step 3: Click "Companies" sub-menu item
            logger.info("[Step 3] Clicking Companies...")
            companies_link = page.locator(
                'a:has-text("Companies")'
            ).first
            companies_link.click(timeout=ACTION_TIMEOUT)
            page.wait_for_load_state("networkidle")
            logger.info("[Step 3] Navigated to Companies listing.")
            self._save_screenshot("step3_companies")
            return True

        except PwTimeout:
            logger.error("Could not navigate to Records > Companies.")
            self._save_screenshot("step2_3_nav_error")
            return False

    # ------------------------------------------------------------------
    # Step 4-5: Search company and click View
    # ------------------------------------------------------------------

    def _search_and_view_company(self, company_name: str) -> bool:
        """Search for a company and click the View button in the Action column."""
        page = self._page
        if not page:
            return False

        try:
            # Step 4: Type company name (without entity suffix) into DataTables search
            search_term = _strip_entity_type(company_name)
            logger.info("[Step 4] Searching for: %s (from: %s)", search_term, company_name)

            search_box = page.locator('#datatable_filter input[type="search"]').first
            search_box.click(timeout=ACTION_TIMEOUT)
            search_box.fill(search_term)
            # DataTables filters on keyup, wait for table to re-render
            time.sleep(2)
            self._save_screenshot("step4_search_results")

            # Step 5: Find the matching row and click its View button
            logger.info("[Step 5] Looking for matching company row...")

            # Find all visible rows in the datatable
            rows = page.locator('#datatable tbody tr').all()
            if not rows:
                logger.error("No rows found in search results.")
                self._save_screenshot("step5_no_rows")
                return False

            # Find the best matching row by company name text
            target_row = None
            # Normalize: strip dots so "Pte Ltd" matches "PTE. LTD."
            def _norm(s: str) -> str:
                return " ".join(s.replace(".", "").lower().split())

            norm_company = _norm(company_name)
            norm_search = _norm(search_term)
            for row in rows:
                row_text = _norm(row.text_content() or "")
                if norm_company in row_text or norm_search in row_text:
                    target_row = row
                    break

            if not target_row:
                # Fallback: just use the first row
                logger.warning("No exact match found, using first result row.")
                target_row = rows[0]

            # Click the View button within this specific row
            view_btn = target_row.locator('a[href*="view_company"]').first
            view_btn.click(timeout=ACTION_TIMEOUT)
            page.wait_for_load_state("networkidle")
            logger.info("[Step 5] Opened company profile.")
            self._save_screenshot("step5_company_profile")
            return True

        except PwTimeout:
            logger.error("Could not find company: %s", company_name)
            self._save_screenshot("step4_5_search_error")
            return False

    # ------------------------------------------------------------------
    # Step 6: Go to Files tab
    # ------------------------------------------------------------------

    def _go_to_files_tab(self) -> bool:
        """Click the Files tab on the company profile page."""
        page = self._page
        if not page:
            return False

        try:
            logger.info("[Step 6] Clicking Files tab...")
            files_tab = page.locator(
                'a:has-text("Files"), '
                'li a:has-text("Files"), '
                '.nav-tabs a:has-text("Files"), '
                '[role="tab"]:has-text("Files")'
            ).first
            files_tab.click(timeout=ACTION_TIMEOUT)
            page.wait_for_load_state("networkidle")
            time.sleep(1)
            logger.info("[Step 6] Files tab opened.")
            self._save_screenshot("step6_files_tab")
            return True

        except PwTimeout:
            logger.error("Could not find Files tab.")
            self._save_screenshot("step6_files_tab_error")
            return False

    # ------------------------------------------------------------------
    # Step 7: Click Upload button
    # ------------------------------------------------------------------

    def _click_upload_button(self) -> bool:
        """Click the visible Upload button on the Files tab."""
        page = self._page
        if not page:
            return False

        try:
            logger.info("[Step 7] Clicking Upload button...")
            # Multiple elements match "Upload" — hidden per-row buttons exist.
            # Iterate to find the first visible one (top-right Upload button).
            candidates = page.locator(
                'a:has-text("Upload"), button:has-text("Upload")'
            ).all()
            for btn in candidates:
                if btn.is_visible():
                    btn.click(timeout=ACTION_TIMEOUT)
                    page.wait_for_load_state("networkidle")
                    time.sleep(1)
                    logger.info("[Step 7] Upload form opened.")
                    self._save_screenshot("step7_upload_form")
                    return True

            logger.error("No visible Upload button found.")
            self._save_screenshot("step7_no_visible_upload")
            return False

        except PwTimeout:
            logger.error("Could not find Upload button.")
            self._save_screenshot("step7_upload_error")
            return False

    # ------------------------------------------------------------------
    # Step 8: Choose file
    # ------------------------------------------------------------------

    def _choose_file(self, file_path: Path) -> bool:
        """Set the file in the file input (hidden behind the 'Choose a file' button)."""
        page = self._page
        if not page:
            return False

        try:
            logger.info("[Step 8] Selecting file: %s", file_path.name)

            file_input = page.locator('input[type="file"]').first

            # If the file input is hidden, try clicking the visible drop zone / button first
            if not file_input.is_visible(timeout=2000):
                drop_zone = page.locator(
                    ':has-text("Choose a file"), '
                    ':has-text("choose a file"), '
                    ':has-text("drag it here"), '
                    '.dropzone, .file-upload, [class*="dropzone"]'
                ).first
                if drop_zone.is_visible(timeout=3000):
                    drop_zone.click()
                    time.sleep(1)

            # Set the file (works even if the input is hidden)
            file_input = page.locator('input[type="file"]').first
            file_input.set_input_files(str(file_path), timeout=ACTION_TIMEOUT)
            time.sleep(2)
            logger.info("[Step 8] File selected.")
            self._save_screenshot("step8_file_selected")
            return True

        except PwTimeout:
            logger.error("Could not set file input.")
            self._save_screenshot("step8_file_error")
            return False

    # ------------------------------------------------------------------
    # Step 9: Fill Description
    # ------------------------------------------------------------------

    def _fill_description(self, description: str) -> bool:
        """Fill the Description field with the document content/subject."""
        page = self._page
        if not page or not description:
            return True  # Not an error, just nothing to fill

        try:
            logger.info("[Step 9] Filling description: %s", description[:80])
            desc_field = page.locator(
                'textarea[name*="desc"], input[name*="desc"], '
                'textarea[name*="Desc"], input[name*="Desc"], '
                'textarea[placeholder*="escription"], input[placeholder*="escription"], '
                'textarea#description, input#description'
            ).first
            desc_field.fill(description, timeout=ACTION_TIMEOUT)
            logger.info("[Step 9] Description filled.")
            return True

        except PwTimeout:
            logger.warning("Could not find Description field — skipping.")
            self._save_screenshot("step9_desc_not_found")
            return True  # Non-fatal

    # ------------------------------------------------------------------
    # Step 10: Fill Resolution Date
    # ------------------------------------------------------------------

    def _fill_resolution_date(self, date_str: str | None) -> bool:
        """Fill the Resolution Date field (expects dd/mm/yyyy)."""
        page = self._page
        if not page or not date_str:
            logger.info("[Step 10] No date provided — skipping.")
            return True

        try:
            # Convert YYYY-MM-DD → DD/MM/YYYY for teamwork.sg
            parts = date_str.split("-")
            if len(parts) == 3 and len(parts[0]) == 4:
                formatted = f"{parts[2]}/{parts[1]}/{parts[0]}"
            else:
                formatted = date_str
            logger.info("[Step 10] Setting resolution date: %s → %s", date_str, formatted)

            # Set the value entirely via JS — do NOT click/focus the field
            # (clicking opens a datepicker that intercepts all keystrokes)
            result = page.evaluate("""
                (dateValue) => {
                    // Find resolution date input
                    const el = document.querySelector(
                        'input[name*="resolution_date"], input[id*="resolution_date"]'
                    );
                    if (!el) return {found: false};

                    // Set value directly
                    el.value = dateValue;

                    // Fire native events so form validation picks it up
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));

                    // Close any jQuery datepicker that might be open
                    try {
                        if (typeof $ !== 'undefined') {
                            $('.datepicker, .ui-datepicker').hide();
                            $(el).datepicker('hide');
                        }
                    } catch(e) {}

                    // Close any Bootstrap datepicker
                    try {
                        if (typeof $ !== 'undefined') {
                            $(el).datepicker('update', dateValue);
                            $(el).datepicker('hide');
                        }
                    } catch(e) {}

                    return {found: true, value: el.value};
                }
            """, formatted)

            logger.info("[Step 10] JS result: %s", result)
            time.sleep(0.5)
            self._save_screenshot("step10_date_filled")
            return True

        except Exception as exc:
            logger.warning("[Step 10] Could not set resolution date: %s", exc)
            self._save_screenshot("step10_date_error")
            return True  # Non-fatal

    # ------------------------------------------------------------------
    # Step 11: Select Category
    # ------------------------------------------------------------------

    # Known category options on teamwork.sg (value → label)
    _CATEGORIES: dict[str, str] = {
        "24": "ACRA Lodgements",
        "31": "Agreement",
        "29": "Annual Report",
        "27": "Business Profile",
        "11": "Directors' Written Resolution/ Minutes of BODM",
        "26": "Financial Statements",
        "30": "IRAS Stamp Duty",
        "12": "Members' Written Resolution / Minutes of AGM/EGM",
        "10": "Permanent records",
        "16": "Register of Allotments",
        "18": "Register of Auditors",
        "19": "Register of Charges",
        "23": "Register of Data Protection Officers",
        "13": "Register of Directors",
        "14": "Register of Members",
        "21": "Register of Nominee Directors",
        "22": "Register of Nominee Shareholders",
        "20": "Register of Registrable Controllers",
        "15": "Register of Secretaries",
        "17": "Register of Transfer",
        "25": "Share Certificates",
        "9":  "Shares Full Payment",
        "28": "Statutory forms",
    }

    @staticmethod
    def _matching_categories(doc_type: str, threshold: float = 0.30, max_results: int = 3) -> list[tuple[str, str, float]]:
        """Return categories scoring above *threshold*, sorted best-first.

        Each entry is (value, label, score). At most *max_results* returned.
        """
        def _normalise(text: str) -> set[str]:
            return {w.lower().strip("'/,") for w in re.split(r"[\s/]+", text) if len(w) > 1}

        doc_words = _normalise(doc_type)
        matches = []
        for val, label in TeamworkUploader._CATEGORIES.items():
            label_words = _normalise(label)
            overlap = len(doc_words & label_words)
            max_len = max(len(doc_words), len(label_words), 1)
            score = overlap / max_len
            if score >= threshold:
                matches.append((val, label, score))
        matches.sort(key=lambda x: x[2], reverse=True)
        return matches[:max_results]

    # Keep for backward compat (discovery script uses it)
    @staticmethod
    def _best_category_match(doc_type: str) -> tuple[str, str, float]:
        matches = TeamworkUploader._matching_categories(doc_type, threshold=0.0)
        return matches[0] if matches else ("", "", 0.0)

    def _select_category(self, category: str | None) -> bool:
        """Match the document type to all relevant categories and select them
        on the hidden <select multiple> via JS (avoids Select2 search quirks)."""
        page = self._page
        if not page or not category:
            logger.info("[Step 11] No category provided — skipping.")
            return True

        try:
            matches = self._matching_categories(category)
            logger.info("[Step 11] Category input: '%s'", category)

            # Categories cannot be blank — always select at least the best match
            if not matches:
                logger.info("[Step 11] No matches above threshold — falling back to best match.")
                best = self._best_category_match(category)
                if best[0]:
                    matches = [best]
                else:
                    logger.warning("[Step 11] Could not find any category match at all.")
                    return True

            values = [m[0] for m in matches]
            for val, label, score in matches:
                logger.info("[Step 11]   → '%s' (value=%s, score=%.2f)", label, val, score)

            # Find doc_category <select> elements
            selects = page.locator('select[name*="doc_category"]').all()
            logger.info("[Step 11] Found %d doc_category <select> elements", len(selects))

            if not selects:
                logger.warning("[Step 11] No doc_category <select> found on page.")
                self._save_screenshot("step11_no_select")
                return True

            # Select all matching option values via JS, trigger Select2 update
            result = page.evaluate("""
                (optionValues) => {
                    const selects = document.querySelectorAll('select[name*="doc_category"]');
                    let setCount = 0;
                    selects.forEach(sel => {
                        for (const opt of sel.options) {
                            if (optionValues.includes(opt.value)) {
                                opt.selected = true;
                                setCount++;
                            }
                        }
                        if (typeof $ !== 'undefined') {
                            $(sel).trigger('change');
                        } else if (typeof jQuery !== 'undefined') {
                            jQuery(sel).trigger('change');
                        } else {
                            sel.dispatchEvent(new Event('change', {bubbles: true}));
                        }
                    });
                    return {selectCount: selects.length, setCount: setCount};
                }
            """, values)

            logger.info("[Step 11] JS result: %s", result)
            time.sleep(0.5)
            self._save_screenshot("step11_category_selected")
            return True

        except Exception as exc:
            logger.warning("[Step 11] Could not select category: %s", exc)
            self._save_screenshot("step11_category_error")
            return True  # Non-fatal

    # ------------------------------------------------------------------
    # Step 12: Click Save
    # ------------------------------------------------------------------

    def _click_save(self) -> bool:
        """Click the visible Save button to complete the upload."""
        page = self._page
        if not page:
            return False

        try:
            logger.info("[Step 12] Clicking Save...")

            # Dismiss any open datepicker/popups via JS first
            page.evaluate("""
                () => {
                    // Hide jQuery UI datepicker
                    try { $('.datepicker, .ui-datepicker, .datepicker-dropdown').hide(); } catch(e) {}
                    try { $.datepicker._hideDatepicker(); } catch(e) {}
                    // Hide Bootstrap datepicker
                    try { $('.datepicker').datepicker('hide'); } catch(e) {}
                    // Remove any overlays
                    try { $('.modal-backdrop, .datepicker-backdrop').remove(); } catch(e) {}
                    // Blur any focused input
                    try { document.activeElement.blur(); } catch(e) {}
                }
            """)
            time.sleep(0.5)

            # Scroll to the bottom so Save button becomes visible
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(0.5)
            self._save_screenshot("step12_before_save")

            # Dump nearby HTML for debugging
            save_area_html = page.evaluate("""
                () => {
                    // Look for any element containing "Save" text near the bottom
                    const all = document.querySelectorAll('a, button, input[type="submit"], input[type="button"]');
                    const info = [];
                    for (const el of all) {
                        const text = (el.textContent || el.value || '').trim();
                        if (text.toLowerCase().includes('save') || text.toLowerCase().includes('back')) {
                            info.push({
                                tag: el.tagName,
                                type: el.type || '',
                                text: text.substring(0, 50),
                                value: el.value || '',
                                className: el.className || '',
                                id: el.id || '',
                                href: el.href || '',
                                visible: el.offsetParent !== null,
                                onclick: el.getAttribute('onclick') || ''
                            });
                        }
                    }
                    return info;
                }
            """)
            logger.info("[Step 12] Save/Back elements found: %s", save_area_html)

            # Broad selector: any clickable element with "Save" text
            candidates = page.locator(
                'button:has-text("Save"), '
                'a:has-text("Save"), '
                'input[type="submit"][value*="ave"], '
                'input[type="button"][value*="ave"], '
                '[role="button"]:has-text("Save")'
            ).all()

            logger.info("[Step 12] Found %d Save candidates", len(candidates))
            clicked = False
            for i, btn in enumerate(candidates):
                try:
                    tag = btn.evaluate("el => el.tagName")
                    text = (btn.text_content() or "").strip()[:30]
                    cls = btn.get_attribute("class") or ""
                    href = btn.get_attribute("href") or ""
                    logger.info("  Save candidate[%d] <%s> text='%s' class='%s' href='%s'",
                                i, tag, text, cls[:60], href[:60])

                    btn.scroll_into_view_if_needed(timeout=3000)
                    time.sleep(0.3)
                    if btn.is_visible():
                        btn.click(timeout=ACTION_TIMEOUT)
                        clicked = True
                        logger.info("  → Clicked candidate[%d]", i)
                        break
                except Exception as e:
                    logger.info("  → candidate[%d] not clickable: %s", i, str(e)[:80])
                    continue

            # Fallback: click Save via JavaScript (searches ALL element types)
            if not clicked:
                logger.info("[Step 12] Trying JS click fallback...")
                js_clicked = page.evaluate("""
                    () => {
                        const all = document.querySelectorAll('a, button, input[type="submit"], input[type="button"]');
                        for (const el of all) {
                            const text = (el.textContent || el.value || '').trim();
                            if (text === 'Save' && el.offsetParent !== null) {
                                el.click();
                                return 'clicked: ' + el.tagName + '.' + el.className;
                            }
                        }
                        return false;
                    }
                """)
                if js_clicked:
                    clicked = True
                    logger.info("[Step 12] Clicked Save via JS: %s", js_clicked)

            if not clicked:
                logger.error("[Step 12] No Save button found at all.")
                self._save_screenshot("step12_no_save_btn")
                return False

            # Wait for save to complete
            page.wait_for_load_state("networkidle", timeout=UPLOAD_TIMEOUT)
            time.sleep(2)

            # Check for success
            success = page.locator(
                '.alert-success, .success-message, '
                '[class*="success"]'
            ).first
            if success.is_visible(timeout=5000):
                logger.info("[Step 12] Upload saved successfully!")
                self._save_screenshot("step12_success")
                return True

            # Check for errors
            error = page.locator('.alert-danger, .alert-error, .error-message').first
            if error.is_visible(timeout=2000):
                error_text = error.text_content() or "Unknown error"
                logger.error("[Step 12] Save error: %s", error_text[:200])
                self._save_screenshot("step12_save_error")
                return False

            # No clear indicator — assume success
            logger.warning("[Step 12] Save completed (no explicit confirmation).")
            self._save_screenshot("step12_no_confirmation")
            return True

        except PwTimeout:
            logger.error("[Step 12] Save timed out.")
            self._save_screenshot("step12_save_timeout")
            return False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upload_document(
        self,
        file_path: Path,
        metadata: DocumentMetadata | None,
    ) -> bool:
        """
        Upload a document to teamwork.sg via the 12-step browser workflow.

        Steps:
            0. Login
            1. Dashboard (after login)
            2. Click Records in sidebar
            3. Click Companies
            4. Search for company
            5. Click View
            6. Go to Files tab
            7. Click Upload
            8. Choose file
            9. Fill Description (document content)
            10. Fill Resolution Date
            11. Select Category (fuzzy-matched from document type)
            12. Click Save

        Returns True on success.
        """
        if not self._company_id:
            logger.warning("Teamwork.sg not configured — skipping upload.")
            return False

        company_name = metadata.company_name if metadata else None
        if not company_name:
            logger.error("No company name — cannot upload without knowing the company.")
            return False

        try:
            # Step 0-1: Login → Dashboard
            if not self._login():
                return False

            # Step 2-3: Records → Companies
            if not self._navigate_to_companies():
                return False

            # Step 4-5: Search → View
            if not self._search_and_view_company(company_name):
                return False

            # Step 6: Files tab
            if not self._go_to_files_tab():
                return False

            # Step 7: Upload button
            if not self._click_upload_button():
                return False

            # Step 8: Choose file
            if not self._choose_file(file_path):
                return False

            # Step 9: Description (document content/subject)
            description = ""
            if metadata and metadata.document_content:
                description = metadata.document_content
            self._fill_description(description)

            # Step 10: Resolution Date
            resolution_date = metadata.document_date if metadata else None
            self._fill_resolution_date(resolution_date)

            # Step 11: Category (fuzzy-matched from AI-extracted document type)
            doc_type = metadata.document_type if metadata else None
            self._select_category(doc_type)

            # Step 12: Save
            return self._click_save()

        except Exception:
            logger.exception("Unexpected error during upload workflow")
            self._save_screenshot("workflow_error")
            return False

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def _save_screenshot(self, label: str) -> None:
        """Save a debug screenshot for troubleshooting."""
        if not self._page or self._page.is_closed():
            return
        try:
            self._screenshots_dir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            path = self._screenshots_dir / f"{ts}_{label}.png"
            self._page.screenshot(path=str(path), full_page=True)
            logger.info("Screenshot saved: %s", path)
        except Exception:
            logger.debug("Could not save screenshot", exc_info=True)
