"""
MIIT Cabinet Module — headless browser automation for the student portal.
Sync-first architecture: one browser session extracts everything into a JSON cache,
then all read operations serve from cache without touching the browser.

Uses Playwright Python API (sync) in headless mode.
"""
import json
import os
import re
import time
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

BASE_URL = "https://rut-miit.ru/cabinet"
LOGIN_URL = f"{BASE_URL}/hello/login.jsp"

_DIR = os.path.dirname(os.path.abspath(__file__))
CREDS_FILE = os.path.join(_DIR, "credentials.json")
STATE_FILE = os.path.join(_DIR, "browser_state.json")
CACHE_FILE = os.path.join(_DIR, "cabinet_cache.json")

# ═══════════════════════════════════════════════════════════════════
# Grade text parser (pure function, no browser needed)
# ═══════════════════════════════════════════════════════════════════

TYPE_PATTERNS = [
    r"Текущий\s*контроль\s*\([^)]+\)",
    r"Курсовой\s+проект",
    r"Курсовая\s+работа",
    r"Экзамен",
    r"Зачет",
    r"Зачёт",
]
TYPE_RE = "(" + "|".join(TYPE_PATTERNS) + ")"
GRADE_RE = r"(\d|зачёт|зачет|Зачёт|Зачет)"
TEACHER_RE = r"([А-ЯЁ][а-яёА-ЯЁ\s\.\-,]+?)"


def parse_grades_text(text):
    """Parse grades from body text content (ADF concatenated format)."""
    grades = []
    sem_blocks = re.split(r"(\d)\s*-\s*й\s+семестр", text)
    current_sem = None

    for chunk in sem_blocks:
        chunk = chunk.strip()
        if re.match(r"^\d$", chunk):
            current_sem = chunk
            continue
        if not current_sem or len(chunk) < 10:
            continue

        chunk = re.sub(r"Вид\s*аттестации\s*Преподаватель\s*Оценка\s*Документы", "", chunk)
        records = [r.strip() for r in chunk.split("\xa0") if r.strip()]
        current_discipline = None

        for record in records:
            if len(record) < 5:
                continue
            if record in ("Семестр", "Дисциплина", "Вид аттестации", "Оценка", "Документы", "- Все -"):
                continue
            if re.match(r"^[А-ЯЁ][а-яё]+\s+\d{4}-\d{4}$", record):
                continue

            type_match = re.search(TYPE_RE, record)
            if type_match:
                prefix = record[:type_match.start()].strip()
                suffix = record[type_match.start():]

                if prefix and re.match(r"^[А-ЯЁA-Z].+", prefix) and len(prefix) > 3:
                    current_discipline = prefix

                grade_match = re.match(TYPE_RE + TEACHER_RE + GRADE_RE + r"\s*$", suffix)
                if grade_match:
                    atype = grade_match.group(1).strip()
                    teacher = grade_match.group(2).strip()
                    grade = grade_match.group(3).strip()
                    teacher = re.sub(r"\s*к\.\w+\.\w+\.?,?\s*(доц\.?|проф\.?)?\s*$", "", teacher).strip()

                    grades.append({
                        "semester": current_sem,
                        "discipline": current_discipline or prefix or "",
                        "type": atype,
                        "teacher": teacher,
                        "grade": grade,
                    })

    return grades


def parse_profile_text(text):
    """Extract profile fields from page innerText (tab-separated ADF format)."""
    data = {}
    # Normalize whitespace: keep tabs as field separators, collapse spaces
    text = re.sub(r"[ \xa0]+", " ", text)
    patterns = {
        "full_name": r"([А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+)(?=\s*изменить|\s*$)",
        "tab_number": r"Табельный\s+номер\s+(\d+)",
        "gender": r"Пол\s+(Мужской|Женский)",
        "birth_date": r"Дата\s+рождения\s+(.+?)\s+Возраст",
        "age": r"Возраст\s+(\d+)",
        "birth_place": r"Место\s+рождения\s+(.+?)\s+Гражданство",
        "citizenship": r"Гражданство\s+(.+?)\s+СНИЛС",
        "snils": r"СНИЛС\s+([\d\-]+)",
        "inn": r"ИНН\s+(\d+)",
        "email": r"(\d+@edu\.rut-miit\.ru)",
    }
    for field, pattern in patterns.items():
        m = re.search(pattern, text)
        if m:
            data[field] = m.group(1).strip()
    return data


# ═══════════════════════════════════════════════════════════════════
# Cache helpers
# ═══════════════════════════════════════════════════════════════════

def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_cache(data):
    data["cached_at"] = datetime.now(timezone.utc).isoformat()
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════
# Cabinet session (thin — only sync + upload)
# ═══════════════════════════════════════════════════════════════════

class CabinetSession:
    """Headless browser session — only used during sync() and upload()."""

    def __init__(self, headless=True):
        self.headless = headless
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._creds = self._load_credentials()

    def _load_credentials(self):
        if os.path.exists(CREDS_FILE):
            with open(CREDS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _ensure_browser(self):
        if self._browser is not None:
            return
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self.headless,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        storage_state = None
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    storage_state = json.load(f)
            except Exception:
                pass
        self._context = self._browser.new_context(
            storage_state=storage_state,
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0",
        )
        self._page = self._context.new_page()

    def _save_state(self):
        if self._context:
            state = self._context.storage_state()
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)

    def _adf_click(self, text_contains):
        self._page.evaluate(
            """(text) => {
                const links = document.querySelectorAll('a.sideMenuLink');
                for (const l of links) { if (l.textContent.includes(text)) { l.click(); return true; } }
                const tabs = document.querySelectorAll('[role="tab"]');
                for (const t of tabs) { if (t.textContent.includes(text)) { t.click(); return true; } }
                return false;
            }""",
            text_contains,
        )

    def _wait_adf(self, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            busy = self._page.evaluate(
                """() => { const el = document.querySelector('.p_AFBusy, .x1ol'); return el && window.getComputedStyle(el).display !== 'none'; }"""
            )
            if not busy:
                time.sleep(1)
                return True
            time.sleep(0.3)
        time.sleep(1)
        return True

    # ═══════════════════════════════════════════════════════════════
    # Login
    # ═══════════════════════════════════════════════════════════════

    def _login(self):
        """Internal: ensure authenticated. Returns error dict or None."""
        self._ensure_browser()
        login = self._creds.get("login", "")
        password = self._creds.get("password", "")
        if not login or not password:
            return {"status": "error", "message": "No credentials."}

        try:
            self._page.goto(LOGIN_URL, wait_until="networkidle", timeout=30000)
        except Exception:
            pass

        try:
            self._page.fill("#login", login)
            self._page.fill("#password", password)
            self._page.click("button[type='submit']")
            self._page.wait_for_url("**/cabinet/**", timeout=15000)
        except PlaywrightTimeout:
            if "/cabinet/" in self._page.url and "login" not in self._page.url:
                self._save_state()
                return None  # already logged in
            return {"status": "error", "message": "Login failed."}

        self._save_state()
        return None

    # ═══════════════════════════════════════════════════════════════
    # SYNC — fetch everything in one browser session
    # ═══════════════════════════════════════════════════════════════

    def sync(self):
        """Open browser once, collect all data, save to cache, close browser."""
        err = self._login()
        if err:
            return err

        cache = {}

        # Helper: click a side-menu link by visible text (retries with multiple strategies)
        def nav_to(section):
            # Strategy 1: ADF side menu
            found = self._page.evaluate(
                """(text) => {
                    for (const l of document.querySelectorAll('a.sideMenuLink, a[data-afr-tlen]')) {
                        if (l.textContent.includes(text)) { l.click(); return true; }
                    }
                    return false;
                }""", section)
            if found:
                self._wait_adf()
                return True
            # Strategy 2: Playwright role
            try:
                self._page.get_by_role("link", name=section).click(timeout=3000)
                self._wait_adf()
                return True
            except Exception:
                pass
            return False

        def click_tab(tab_name):
            found = self._page.evaluate(
                """(text) => {
                    for (const t of document.querySelectorAll('[role="tab"]')) {
                        if (t.textContent.includes(text)) { t.click(); return true; }
                    }
                    return false;
                }""", tab_name)
            if found:
                self._wait_adf(timeout=6)
                return True
            try:
                self._page.get_by_role("tab", name=tab_name).click(timeout=3000)
                self._wait_adf(timeout=6)
                return True
            except Exception:
                return False

        # 1. Profile
        nav_to("Обо мне")
        text = self._page.evaluate("() => document.body.innerText || ''")
        cache["profile"] = parse_profile_text(text)
        if "full_name" not in cache["profile"]:
            cache["profile"]["full_name"] = self._page.evaluate(
                "() => { const t = document.querySelector('.toolbar'); return t ? t.textContent.trim() : ''; }"
            )

        # 2. Navigate to Education section
        nav_to("Моё обучение")

        # 2a. Education options
        options = self._page.evaluate(
            """() => {
                const selects = document.querySelectorAll('select');
                for (const s of selects) {
                    const prev = s.previousElementSibling;
                    if (prev && prev.textContent.includes('Обучение')) {
                        return Array.from(s.options).map(o => ({value: o.value, text: o.textContent.trim(), selected: o.selected}));
                    }
                }
                return [];
            }"""
        )
        cache["education_options"] = options
        cache["current_education"] = next((o for o in options if o.get("selected")), options[0] if options else None)

        # 3. Grades
        click_tab("Результаты сессий")
        text = self._page.evaluate("() => document.body.textContent || ''")
        cache["grades"] = parse_grades_text(text)

        # 4. Portfolio
        click_tab("Портфолио")
        text = self._page.evaluate("() => document.body.textContent || ''")
        cache["portfolio"] = [] if "Данные не найдены" in text else []

        self.close()
        save_cache(cache)
        return {
            "status": "ok",
            "cached": cache["profile"].get("full_name", "unknown"),
            "grades_count": len(cache.get("grades", [])),
            "education": cache.get("current_education", {}).get("text", "") if cache.get("current_education") else "",
        }

    # ═══════════════════════════════════════════════════════════════
    # Upload (still needs live browser)
    # ═══════════════════════════════════════════════════════════════

    def upload_portfolio(self, file_path, description=""):
        """Upload a file to portfolio. Uses fresh browser session."""
        if not os.path.exists(file_path):
            return {"status": "error", "message": f"File not found: {file_path}"}

        err = self._login()
        if err:
            return err

        try:
            self._adf_click("Моё обучение")
            self._wait_adf()
            self._adf_click("Портфолио")
            self._wait_adf(timeout=6)

            clicked = self._page.evaluate(
                """() => { for (const b of document.querySelectorAll('button')) { if (b.textContent.includes('Добавить')) { b.click(); return true; } } return false; }"""
            )
            if not clicked:
                return {"status": "error", "message": "Add button not found."}

            self._wait_adf(timeout=4)

            if description:
                try:
                    self._page.fill("textarea, input[type='text']", description, timeout=5000)
                except PlaywrightTimeout:
                    pass

            self._page.locator("input[type='file']").set_input_files(file_path, timeout=5000)
            self._wait_adf(timeout=3)

            saved = self._page.evaluate(
                """() => { for (const b of document.querySelectorAll('button')) { if (b.textContent.includes('Сохранить') || b.textContent.includes('Загрузить') || b.textContent.includes('Добавить')) { b.click(); return true; } } return false; }"""
            )
            self._wait_adf(timeout=4)

            return {"status": "ok" if saved else "warning", "message": "Uploaded" if saved else "Saved as draft"}
        finally:
            self.close()

    def logout(self):
        if self._page:
            try:
                self._page.goto(f"{BASE_URL}/adfAuthentication?logout=true&end_url=./", wait_until="commit", timeout=10000)
            except Exception:
                pass
        self.close()
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)
        return {"status": "ok"}

    def close(self):
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._browser = None
        self._page = None
        self._context = None
        self._playwright = None


# ═══════════════════════════════════════════════════════════════════
# Public API — cache-first, no browser on reads
# ═══════════════════════════════════════════════════════════════════

def sync_cabinet():
    """Run a full sync: browser → cache. Returns status dict."""
    cab = CabinetSession(headless=True)
    return cab.sync()


def get_profile():
    """Read profile from cache."""
    cache = load_cache()
    if not cache:
        return {"status": "no_cache", "message": "No cache. Run miit_sync_cabinet first."}
    return {"status": "ok", "profile": cache.get("profile", {})}


def get_grades(semester=None, discipline=None, attestation_type=None):
    """Read grades from cache with optional filters."""
    cache = load_cache()
    if not cache:
        return {"status": "no_cache", "message": "No cache. Run miit_sync_cabinet first."}
    grades = cache.get("grades", [])
    if semester:
        grades = [g for g in grades if semester in str(g.get("semester", ""))]
    if discipline:
        grades = [g for g in grades if discipline.lower() in g.get("discipline", "").lower()]
    if attestation_type:
        grades = [g for g in grades if attestation_type.lower() in g.get("type", "").lower()]
    return {"status": "ok", "grades": grades, "total": len(grades)}


def get_portfolio():
    """Read portfolio from cache."""
    cache = load_cache()
    if not cache:
        return {"status": "no_cache", "message": "No cache. Run miit_sync_cabinet first."}
    return {"status": "ok", "portfolio": cache.get("portfolio", []), "total": len(cache.get("portfolio", []))}


def get_education():
    """Read education options from cache."""
    cache = load_cache()
    if not cache:
        return {"status": "no_cache", "message": "No cache. Run miit_sync_cabinet first."}
    return {"status": "ok", "options": cache.get("education_options", []), "current": cache.get("current_education")}


def upload_portfolio(file_path, description=""):
    """Upload a file — requires live browser."""
    cab = CabinetSession(headless=True)
    return cab.upload_portfolio(file_path, description)


def logout_cabinet():
    """Clear session and cache."""
    cab = CabinetSession(headless=True)
    return cab.logout()
