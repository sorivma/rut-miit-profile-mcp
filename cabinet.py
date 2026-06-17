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
# Parsers (pure functions, no browser needed)
# ═══════════════════════════════════════════════════════════════════

_TYPE_PATTERNS = [
    r"Текущий\s*контроль\s*\([^)]+\)",
    r"Курсовой\s+проект",
    r"Курсовая\s+работа",
    r"Экзамен",
    r"Зачет",
    r"Зачёт",
]
TYPE_RE = "(" + "|".join(_TYPE_PATTERNS) + ")"
FULL_GRADE = re.compile(
    TYPE_RE
    + r"([А-ЯЁ][а-яёА-ЯЁ\s\.\-,]+?)"
    + r"(\d|зачёт|зачет|Зачёт|Зачет)"
    + r"\s*$"
)


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

        chunk = re.sub(
            r"Вид\s*аттестации\s*Преподаватель\s*Оценка\s*Документы",
            "", chunk,
        )
        # Split on nbsp BEFORE normalizing (nbsp is the ADF record separator)
        records = [r.strip() for r in chunk.split("\xa0") if r.strip()]
        current_discipline = None

        for record in records:
            # Normalize whitespace in each record
            record = re.sub(r"[ \xa0]+", " ", record).strip()
            if len(record) < 5:
                continue
            if record in ("Семестр", "Дисциплина", "Вид аттестации",
                          "Оценка", "Документы", "- Все -"):
                continue
            if re.match(r"^[А-ЯЁ][а-яё]+\s+\d{4}-\d{4}$", record):
                continue

            m = re.search(TYPE_RE, record)
            if not m:
                continue

            prefix = record[:m.start()].strip()
            suffix = record[m.start():]

            if prefix and re.match(r"^[А-ЯЁA-Z].+", prefix) and len(prefix) > 3:
                current_discipline = prefix

            gm = FULL_GRADE.search(suffix)
            if gm:
                atype = gm.group(1).strip()
                teacher = gm.group(2).strip()
                grade = gm.group(3).strip()
                teacher = re.sub(
                    r"\s*к\.\w+\.\w+\.?,?\s*(доц\.?|проф\.?)?\s*$",
                    "", teacher,
                ).strip()
                grades.append({
                    "semester": current_sem,
                    "discipline": current_discipline or prefix or "",
                    "type": atype,
                    "teacher": teacher,
                    "grade": grade,
                })

    return grades


def parse_profile_text(text):
    """Extract profile fields from page innerText."""
    text = re.sub(r"[ \xa0]+", " ", text)
    data = {}
    # Try to find full name near "Табельный номер" or after "Обо мне"
    for pattern in [
        r"([А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+)\s+изменить",
        r"([А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+)\s+Табельный",
    ]:
        m = re.search(pattern, text)
        if m:
            data["full_name"] = m.group(1).strip()
            break
    if "full_name" not in data:
        m = re.search(r"([А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+)", text)
        if m:
            data["full_name"] = m.group(1).strip()

    field_patterns = {
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
    for field, pattern in field_patterns.items():
        m = re.search(pattern, text)
        if m:
            data[field] = m.group(1).strip()
    return data


def parse_study_plan_text(text):
    """Parse study plan tab — specialty, department, qualification, PDFs."""
    text = re.sub(r"[ \xa0]+", " ", text)
    data = {}
    patterns = {
        "specialty_code": r"Код\s+специальности\s*(\d{2}\.\d{2}\.\d{2})",
        "abbreviation": r"Аббревиатура\s*([А-ЯЁ]+)",
        "form": r"Форма\s+обучения\s*(\S+)",
        "qualification": r"Квалификация\s*(\S+)",
    }
    for field, pattern in patterns.items():
        m = re.search(pattern, text)
        if m:
            data[field] = m.group(1).strip()

    # Department: try proper format
    m = re.search(r"Кафедра\s+(Кафедра\s*[«\"][^»\"]+[»\"])\s", text)
    if m:
        data["department"] = m.group(1).strip()
    else:
        m = re.search(r"Кафедра\s+([А-ЯЁ][А-ЯЁа-яё\s\-]+?)\s", text)
        if m:
            data["department"] = m.group(1).strip()

    pdfs = re.findall(r"\b([А-ЯЁа-яё][А-ЯЁа-яё\s\-().,\d]+\.pdf)", text, re.IGNORECASE)
    data["documents"] = [p.strip() for p in pdfs if len(p.strip()) > 5 and not any(w in p.lower() for w in ("личный кабинет", "выход", "перезагрузка"))]

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
# Cabinet session
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
                return None
            return {"status": "error", "message": "Login failed."}

        self._save_state()
        return None

    # ═══════════════════════════════════════════════════════════════
    # Navigation helpers
    # ═══════════════════════════════════════════════════════════════

    def _nav_to(self, section):
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
        try:
            self._page.get_by_role("link", name=section).click(timeout=3000)
            self._wait_adf()
            return True
        except Exception:
            pass
        return False

    def _click_tab(self, tab_name):
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
            pass
        return False

    def _get_education_options(self):
        """Get education options — find the select with institute-like options."""
        return self._page.evaluate(
            """() => {
                const selects = document.querySelectorAll('select');
                for (const s of selects) {
                    if (s.options.length <= 1) continue;
                    const firstOpt = s.options[0].textContent.trim();
                    // Education select has options like "Институт ... УВП-171 (01.09.2025 очная)"
                    if (firstOpt.includes('Институт') || /\d{4}/.test(firstOpt)) {
                        return Array.from(s.options).map(o => ({
                            value: o.value,
                            text: o.textContent.trim(),
                            selected: o.selected,
                        }));
                    }
                }
                return [];
            }"""
        )

    def _get_inner_text(self):
        """Get visible text only (ignores hidden elements, scripts, styles)."""
        return self._page.evaluate("() => document.body.innerText || ''")

    def _get_text_content(self):
        """Get all text content including hidden elements (for ADF data extraction)."""
        return self._page.evaluate("() => document.body.textContent || ''")

    def _get_tab_text_content(self):
        """Get textContent of the currently visible tab panel only."""
        return self._page.evaluate(
            """() => {
                const tabs = document.querySelectorAll('[role="tabpanel"]');
                for (const t of tabs) {
                    if (window.getComputedStyle(t).display !== 'none') {
                        return t.textContent || '';
                    }
                }
                return document.body.textContent || '';
            }"""
        )

    def _extract_disciplines(self):
        """Extract disciplines data from the visible tab panel DOM."""
        return self._page.evaluate(
            """() => {
                const tabs = document.querySelectorAll('[role="tabpanel"]');
                let panel = null;
                for (const t of tabs) {
                    if (window.getComputedStyle(t).display !== 'none') {
                        panel = t;
                        break;
                    }
                }
                if (!panel) return [];
                
                const disciplines = [];
                let currentSemester = null;
                let currentDisc = null;
                const seenDiscs = new Set();
                
                for (const el of panel.querySelectorAll('*')) {
                    const tag = el.tagName;
                    const text = (el.childNodes.length === 1 && el.childNodes[0].nodeType === 3)
                        ? el.textContent.trim() : '';
                    const href = el.href || '';
                    
                    // Semester header
                    if (tag === 'SPAN' && /\\d-й\\s+семестр/.test(text)) {
                        const m = text.match(/(\\d)-й\\s+семестр/);
                        if (m) currentSemester = m[1];
                        continue;
                    }
                    
                    // Section ONLY from SPAN with font-weight bold and medium/large size
                    if (tag === 'SPAN' && text && text.length > 5 && text.length < 120) {
                        const style = el.getAttribute('style') || '';
                        if (/font-size\\s*:\\s*(?:medium|large|1[4-9]px|[2-9]\\dpx)/i.test(style) || el.closest('[style*="font-size"]')) {
                            if (/[Дд]исциплин|[Пп]рактик|[Гг]осударствен|[Фф]акульт/.test(text)) {
                                currentDisc = null;
                                continue;
                            }
                        }
                    }
                    
                    // Discipline name: DIV with cyrillic capital, 10-150 chars, not a filter/header
                    if (tag === 'DIV' && text && text.length > 8 && text.length < 150 && /^[А-ЯЁ]/.test(text)) {
                        if (/^(?:Семестр|Вид занятий|Кафедра|Документы|Данные не найдены|Код специальности|Аббревиатура|Форма обучения|Институт|Квалификация|Факультет)/.test(text)) continue;
                        if (/^(?:Диссертация|Дифференцированный|Экзамен|Зачет|Курсов[ао]|Лекция|Практическое|Производственная|Самостоятельная|Текущий|Учебная)/.test(text)) continue;
                        if (/\.(?:pdf|docx?|xlsx?|pptx?)/i.test(text)) continue;
                        if (/^(?:[-—]|\\d+[\\s.)])/.test(text)) continue;
                        if (/^(?:Рабочая|Аннотация|ОМ|Программа|Календарный|Описание)/.test(text)) continue;
                        
                        const key = text.trim();
                        if (!seenDiscs.has(key)) {
                            seenDiscs.add(key);
                            currentDisc = {
                                "semester": currentSemester,
                                "discipline": key,
                                "documents": [],
                                "department": null,
                            };
                            disciplines.push(currentDisc);
                        }
                        continue;
                    }
                    
                    // Links
                    if (tag === 'A' && href && currentDisc) {
                        const linkText = el.textContent.trim();
                        if (!linkText || linkText.length < 2) continue;
                        
                        // Department links
                        if (href.includes('/depts/')) {
                            currentDisc.department = linkText;
                            continue;
                        }
                        
                        // Document links
                        if (href.includes('/content/') && /\\.(pdf|docx?|xlsx?|pptx?)$/i.test(href.split('?')[0])) {
                            currentDisc.documents.push({
                                "name": linkText,
                                "url": href
                            });
                            continue;
                        }
                    }
                }
                
                return disciplines;
            }"""
        )

    # ═══════════════════════════════════════════════════════════════
    # SYNC
    # ═══════════════════════════════════════════════════════════════

    def sync(self):
        """Open browser once, collect all data, save to cache, close browser."""
        err = self._login()
        if err:
            return err

        cache = {}

        # 1. Profile
        self._nav_to("Обо мне")
        text = self._get_inner_text()
        cache["profile"] = parse_profile_text(text)
        if "full_name" not in cache["profile"]:
            cache["profile"]["full_name"] = self._page.evaluate(
                "() => { const t = document.querySelector('.toolbar'); return t ? t.textContent.trim() : ''; }"
            )

        # 2. Education section
        self._nav_to("Моё обучение")
        # Retry education options — ADF may need extra time to render the select
        options = []
        for _ in range(3):
            options = self._get_education_options()
            if options:
                break
            time.sleep(2)
        cache["education_options"] = options
        cache["current_education"] = next(
            (o for o in options if o.get("selected")),
            options[0] if options else None,
        )

        # 3. Grades — use body textContent (must extract before visiting other tabs)
        self._click_tab("Результаты сессий")
        text = self._get_text_content()
        cache["grades"] = parse_grades_text(text)

        # 4. Study plan
        self._click_tab("Учебный план")
        text = self._get_inner_text()
        cache["study_plan"] = parse_study_plan_text(text)

        # 5. Disciplines (DOM-based)
        self._click_tab("Дисциплины")
        cache["disciplines"] = self._extract_disciplines()

        # 6. Portfolio
        self._click_tab("Портфолио")
        text = self._get_text_content()
        cache["portfolio"] = parse_portfolio_text(text)

        # 7. Contracts
        self._nav_to("Мои договоры")
        text = self._get_inner_text()
        cache["contracts_available"] = "Данные не найдены" not in text

        # 8. Orders
        self._nav_to("Мои приказы")
        text = self._get_inner_text()
        cache["orders_available"] = "Данные не найдены" not in text

        self.close()
        save_cache(cache)

        return {
            "status": "ok",
            "cached": cache["profile"].get("full_name", "unknown"),
            "grades_count": len(cache.get("grades", [])),
            "disciplines_count": len(cache.get("disciplines", [])),
            "portfolio_count": len(cache.get("portfolio", [])),
            "education": (cache.get("current_education") or {}).get("text", ""),
        }

    # ═══════════════════════════════════════════════════════════════
    # Upload portfolio
    # ═══════════════════════════════════════════════════════════════

    def upload_portfolio(self, file_path, description=""):
        if not os.path.exists(file_path):
            return {"status": "error", "message": f"File not found: {file_path}"}

        err = self._login()
        if err:
            return err

        try:
            self._nav_to("Моё обучение")
            self._click_tab("Портфолио")

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

    # ═══════════════════════════════════════════════════════════════
    # Document download
    # ═══════════════════════════════════════════════════════════════

    def fetch_document(self, url, output_path=None):
        """Download a document from the cabinet using browser session cookies.
        Works for PDF, DOCX, XLSX, etc. — any file accessible in the cabinet.
        Returns {"status": "ok", "path": "...", "size": N} or error.
        """
        import base64
        from urllib.parse import unquote, urlparse

        err = self._login()
        if err:
            return err

        if output_path is None:
            parsed = urlparse(url)
            name = unquote(parsed.path).split("/")[-1]
            if not name or "." not in name:
                name = "document.pdf"
            output_path = os.path.join(_DIR, name)

        try:
            # Use browser fetch with credentials to get the file
            result = self._page.evaluate(
                """async (url) => {
                    try {
                        const resp = await fetch(url, {credentials: 'include'});
                        if (!resp.ok) return {error: 'HTTP ' + resp.status};
                        const blob = await resp.blob();
                        return new Promise((resolve) => {
                            const reader = new FileReader();
                            reader.onloadend = () => resolve({data: reader.result});
                            reader.onerror = () => resolve({error: 'read error'});
                            reader.readAsDataURL(blob);
                        });
                    } catch (e) {
                        return {error: e.message};
                    }
                }""", url)

            if result.get("error"):
                return {"status": "error", "message": result["error"]}

            data_url = result.get("data", "")
            if "," in data_url:
                raw = base64.b64decode(data_url.split(",", 1)[1])
                with open(output_path, "wb") as f:
                    f.write(raw)
                return {"status": "ok", "path": output_path, "size": len(raw)}

            return {"status": "error", "message": "No data received"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

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


def parse_portfolio_text(text):
    """Parse portfolio tab — returns items when data exists."""
    if "Данные не найдены" in text:
        return []

    items = []
    records = [r.strip() for r in text.split("\xa0") if r.strip()]
    current_item = None
    skip = {"№", "Описание достижения", "Файлы", "Добавить элемент портфолио",
            "Учебная работа", "Произвольное достижение", "Публикация",
            "Кнопка открывает всплывающее окно"}

    for record in records:
        record = record.strip()
        if not record or record in skip or len(record) < 2:
            if any(w in record for w in skip):
                continue
            continue

        m = re.match(r"^(\d+)\s+(.+)", record)
        if m and len(m.group(2)) > 3:
            if current_item:
                items.append(current_item)
            current_item = {"number": int(m.group(1)), "description": m.group(2), "files": []}
            continue

        if current_item and re.search(r"\.(pdf|docx?|xlsx?|pptx?|jpg|png)", record, re.I):
            current_item["files"].append(record)

    if current_item:
        items.append(current_item)
    return items


# ═══════════════════════════════════════════════════════════════════
# Public API — cache-first, no browser on reads
# ═══════════════════════════════════════════════════════════════════

def sync_cabinet():
    cab = CabinetSession(headless=True)
    return cab.sync()


def get_profile():
    cache = load_cache()
    if not cache:
        return {"status": "no_cache", "message": "No cache. Run miit_sync_cabinet first."}
    profile = cache.get("profile", {})
    return {"status": "ok", "profile": profile}


def get_grades(semester=None, discipline=None, attestation_type=None):
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
    cache = load_cache()
    if not cache:
        return {"status": "no_cache", "message": "No cache. Run miit_sync_cabinet first."}
    items = cache.get("portfolio", [])
    return {"status": "ok", "portfolio": items, "total": len(items)}


def get_disciplines(semester=None, discipline=None):
    cache = load_cache()
    if not cache:
        return {"status": "no_cache", "message": "No cache. Run miit_sync_cabinet first."}
    discs = cache.get("disciplines", [])
    if semester:
        discs = [d for d in discs if semester in str(d.get("semester", ""))]
    if discipline:
        discs = [d for d in discs if discipline.lower() in d.get("discipline", "").lower()]
    return {"status": "ok", "disciplines": discs, "total": len(discs)}


def get_study_plan():
    cache = load_cache()
    if not cache:
        return {"status": "no_cache", "message": "No cache. Run miit_sync_cabinet first."}
    return {"status": "ok", "study_plan": cache.get("study_plan", {})}


def get_education():
    cache = load_cache()
    if not cache:
        return {"status": "no_cache", "message": "No cache. Run miit_sync_cabinet first."}
    return {
        "status": "ok",
        "options": cache.get("education_options", []),
        "current": cache.get("current_education"),
    }


def upload_portfolio(file_path, description=""):
    cab = CabinetSession(headless=True)
    return cab.upload_portfolio(file_path, description)


def fetch_document(url, output_path=None):
    """Download a cabinet document by URL. Uses browser session for auth.
    Returns {"status": "ok", "path": "...", "size": N}."""
    cab = CabinetSession(headless=True)
    try:
        return cab.fetch_document(url, output_path)
    finally:
        cab.close()


def logout_cabinet():
    cab = CabinetSession(headless=True)
    return cab.logout()
