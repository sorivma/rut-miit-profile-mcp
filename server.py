#!/usr/bin/env python3
"""
MIIT Timetable MCP Server (stateful)
Fetches and parses the MIIT (RUT) university timetable.
Remembers current group, tracks history, auto-infers next-year group.

Tools:
  - miit_list_institutes       → list all institutes
  - miit_find_group            → search group by code
  - miit_get_timetable         → get schedule (uses stored group if none given)
  - miit_get_student_context   → get lab report context (uses stored group if none given)
  - miit_set_current_group     → set active group + infer next year
  - miit_get_current_group     → show stored group, history, and next-year suggestion
"""
import json
import sys
import re
import io
import os
from datetime import datetime, timezone
import urllib.request
import urllib.error
from html.parser import HTMLParser

# Force UTF-8 for stdin/stdout on Windows
if sys.platform == "win32":
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

BASE_URL = "https://www.miit.ru"
TIMETABLE_URL = f"{BASE_URL}/timetable"

# Lazy import for cabinet module (Playwright dependency)
_cabinet_imported = False

def _ensure_cabinet():
    global _cabinet_imported
    if not _cabinet_imported:
        import cabinet as _cab
        globals()["_cab"] = _cab
        _cabinet_imported = True
    return globals()["_cab"]

# State file — lives alongside this script
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")


# ═══════════════════════════════════════════════════════════════════
# State management
# ═══════════════════════════════════════════════════════════════════

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "current_group": None,
        "set_at": None,
        "group_history": [],
    }


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_group_code(code):
    """
    Parse a MIIT group code like 'УВП-171' into prefix, course, subgroup.
    Pattern: {PREFIX}-{COURSE}{SUBGROUP}
      УВП-171 → prefix='УВП', course=1, subgroup=71
      ВВП-111 → prefix='ВВП', course=1, subgroup=11
      ОММу-221 → prefix='ОММу', course=2, subgroup=21
    """
    code = code.strip().upper()
    m = re.match(r"^([А-ЯЁ]+)-(\d)(\d+)$", code)
    if not m:
        return None
    return {
        "prefix": m.group(1),
        "course": int(m.group(2)),
        "subgroup": int(m.group(3)),
    }


def infer_next_year_group(catalog, current_code):
    """
    Given current group code, find the same prefix with course+1 in catalog.
    Returns the best match (same subgroup if exists, otherwise first match).
    """
    parsed = parse_group_code(current_code)
    if not parsed:
        return None

    next_course = parsed["course"] + 1
    candidates = []

    for inst_id, inst_data in catalog.items():
        for course, groups in inst_data.get("courses", {}).items():
            for g in groups:
                gp = parse_group_code(g["code"])
                if (gp and gp["prefix"] == parsed["prefix"]
                        and gp["course"] == next_course):
                    candidates.append({
                        "group_code": g["code"],
                        "timetable_id": g["id"],
                        "institute_id": inst_id,
                        "institute_name": inst_data["name"],
                        "course": course,
                        "specialty": g["specialty"],
                        "subgroup": gp["subgroup"],
                    })

    if not candidates:
        return None

    # Prefer same subgroup number
    same_sub = [c for c in candidates if c["subgroup"] == parsed["subgroup"]]
    if same_sub:
        return same_sub[0]
    return candidates[0]


# ═══════════════════════════════════════════════════════════════════
# HTML Parser for catalog page
# ═══════════════════════════════════════════════════════════════════

class CatalogParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.institutes = {}
        self._current_institute = None
        self._current_course = None
        self._in_header = False
        self._in_course_name = False
        self._in_group_link = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        cls = attrs.get("class", "")

        if tag == "div" and "info-block" in cls:
            inst_id = attrs.get("id", "")
            if inst_id:
                self._current_institute = inst_id
                if inst_id not in self.institutes:
                    self.institutes[inst_id] = {"name": inst_id, "courses": {}}

        elif tag == "div" and "info-block__header" in cls and self._current_institute:
            self._in_header = True

        elif tag == "span" and "text-form__item-name" in cls:
            self._in_course_name = True
            self._course_text = ""

        elif tag == "a" and self._current_institute:
            href = attrs.get("href", "")
            m = re.match(r"/timetable/(\d+)", href)
            if m:
                self._timetable_id = m.group(1)
                self._group_title = attrs.get("title", "")
                self._in_group_link = True
                self._group_code = ""

    def handle_endtag(self, tag):
        if tag == "div" and self._in_header:
            self._in_header = False

        elif tag == "span" and self._in_course_name:
            self._in_course_name = False
            self._current_course = self._course_text.strip()
            inst = self.institutes.get(self._current_institute)
            if inst is not None and self._current_course:
                inst["courses"].setdefault(self._current_course, [])

        elif tag == "a" and self._in_group_link:
            self._in_group_link = False
            inst = self.institutes.get(self._current_institute)
            if inst is not None and self._current_course:
                group = {
                    "code": self._group_code.strip(),
                    "id": self._timetable_id,
                    "specialty": self._group_title.strip(),
                }
                inst["courses"][self._current_course].append(group)
            self._timetable_id = None
            self._group_code = ""

    def handle_data(self, data):
        if self._in_header:
            text = data.strip()
            if text and len(text) > 2:
                inst = self.institutes.get(self._current_institute)
                if inst is not None:
                    inst["name"] = text
        elif self._in_course_name:
            self._course_text += data
        elif self._in_group_link:
            self._group_code += data


def fetch_catalog():
    req = urllib.request.Request(TIMETABLE_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8")
    parser = CatalogParser()
    parser.feed(html)
    return parser.institutes


def fetch_timetable_page(group_id):
    url = f"{TIMETABLE_URL}/{group_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8")

    m = re.search(r"window\._timetableData\s*=\s*(\[[\s\S]*?\]);", html)
    if not m:
        return None, None, None, None

    timetable_data = json.loads(m.group(1))

    semester = None
    sm = re.search(r"<h5>([^<]+)</h5>", html)
    if sm:
        semester = sm.group(1).strip()

    date_range = None
    dm = re.search(r"с\s+(\d{2}\.\d{2}\.\d{4})\s+по\s+(\d{2}\.\d{2}\.\d{4})", html)
    if dm:
        date_range = f"{dm.group(1)} – {dm.group(2)}"

    group_name = None
    gm = re.search(r"Расписание учебной группы\s+([\w\-]+)", html)
    if gm:
        group_name = gm.group(1)

    return timetable_data, semester, date_range, group_name


def find_group_in_catalog(institutes, group_code):
    group_code = group_code.strip().upper()
    results = []
    for inst_id, inst_data in institutes.items():
        for course, groups in inst_data.get("courses", {}).items():
            for g in groups:
                if group_code in g["code"].upper():
                    results.append({
                        "institute_id": inst_id,
                        "institute_name": inst_data["name"],
                        "course": course,
                        "group_code": g["code"],
                        "timetable_id": g["id"],
                        "specialty": g["specialty"],
                    })
    return results


def build_schedule(timetable_data):
    days = []
    for day in timetable_data:
        entries = []
        for slot in day.get("timeSlots", []):
            for event in slot.get("events", []):
                entry = {
                    "time": f"{slot.get('slotStartDisplay','')} – {slot.get('slotEndDisplay','')}",
                    "type": event.get("textTitle", ""),
                    "subject": event.get("text", ""),
                    "lecturers": [
                        {
                            "short_name": l.get("shortFio", ""),
                            "full_name": l.get("fullFio", ""),
                            "description": l.get("description", ""),
                        }
                        for l in event.get("lecturers", [])
                    ],
                    "rooms": [
                        {"name": r.get("name", ""), "hint": r.get("hint", "")}
                        for r in event.get("rooms", [])
                    ],
                    "note": event.get("noteText"),
                }
                entries.append(entry)
        days.append({
            "date": day.get("hisdate"),
            "day": day.get("dayDisplay"),
            "date_display": day.get("dayDateDisplay"),
            "is_past": day.get("pastDay", False),
            "lessons": entries,
        })
    return days


# ═══════════════════════════════════════════════════════════════════
# Tool definitions
# ═══════════════════════════════════════════════════════════════════

TOOLS = [
    {
        "name": "miit_list_institutes",
        "description": "List all institutes/academies at RUT MIIT university. Returns id, name, courses_count and groups_count for each institute. Use this to discover what institutes exist before searching for groups.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "miit_find_group",
        "description": "Search for a student group by code (e.g. 'ВВП-111', 'УВП-311') across all institutes. Returns list of matches with: institute_id, institute_name, course, group_code, timetable_id, specialty.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "group_code": {
                    "type": "string",
                    "description": "Group code to search for, e.g. 'ВВП-111', 'УВП-311'",
                },
            },
            "required": ["group_code"],
        },
    },
    {
        "name": "miit_set_current_group",
        "description": "Set the student's active group. The server stores this as default for other tools and auto-infers the next-year group. Returns current_group, institute, course, specialty, timetable_id, and next_year_group suggestion.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "group_code": {
                    "type": "string",
                    "description": "Group code, e.g. 'УВП-171'",
                },
            },
            "required": ["group_code"],
        },
    },
    {
        "name": "miit_get_current_group",
        "description": "Get stored group state: current_group, set_at timestamp, group_history (all previously set groups with institute/course/specialty), next_year_group auto-inference. Refreshes next-year prediction against live catalog.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "miit_get_timetable",
        "description": "Get the full weekly schedule for a student group. Returns group name, semester, date_range, timetable_id, and days[]. Each day contains date, day name, date_display, is_past flag, and lessons[]. Each lesson: time range, type (Лекция/Практика/...), subject, lecturers[{short_name, full_name, description}], rooms[{name, hint}], note. Uses stored group if no args given.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "group_code": {
                    "type": "string",
                    "description": "Group code, e.g. 'ВВП-111'. Optional — uses stored group if omitted.",
                },
                "timetable_id": {
                    "type": "string",
                    "description": "Numeric timetable ID. Optional — auto-resolved from group_code.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "miit_get_student_context",
        "description": "Get student context for lab reports/coursework: institute name, institute_id, group, course, specialty, timetable_id, semester, date_range, and disciplines[{subject, type, teachers[]}]. Disciplines and teachers are extracted from the current timetable. Uses stored group if group_code not given.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "group_code": {
                    "type": "string",
                    "description": "Student group code, e.g. 'ВВП-111'. Optional — uses stored group if omitted.",
                },
            },
            "required": [],
        },
    },
    # ═══════════════════════════════════════════════════════════════
    # Cabinet — Personal Account (ЛК РУТ МИИТ)
    # ═══════════════════════════════════════════════════════════════
    {
        "name": "miit_sync_cabinet",
        "description": "AUTH & SYNC ALL CABINET DATA. Opens headless browser, logs into rut-miit.ru/cabinet, extracts everything into cabinet_cache.json. Extracts: profile (9 fields), grades (all semesters), disciplines (with department + document URLs), study plan (specialty code, department, PDFs), education options, contracts/orders availability. Takes ~25-30s. Run this FIRST before any miit_get_* or miit_fetch_document.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "miit_login",
        "description": "Alias for miit_sync_cabinet. Authenticate and sync all cabinet data in one call.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "login": {"type": "string", "description": "Login email (optional, uses credentials.json)"},
                "password": {"type": "string", "description": "Password (optional, uses credentials.json)"},
            },
            "required": [],
        },
    },
    {
        "name": "miit_logout",
        "description": "Clear browser session and delete cached cabinet data.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "miit_get_profile",
        "description": "STUDENT PROFILE from cabinet cache. Returns: full_name, tab_number, gender, birth_date, age, birth_place, citizenship, snils, inn, email (@edu.rut-miit.ru). Requires prior miit_sync_cabinet.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "miit_get_grades",
        "description": "ALL GRADES from cabinet cache. Each grade: semester (1/2/3/4), discipline, type (Экзамен/Зачет/Курсовой проект/Курсовая работа/Текущий контроль with subtype), teacher (full name), grade (5/4/3/зачёт). Supports filters: semester, discipline (partial match), attestation_type. Requires prior miit_sync_cabinet.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "semester": {"type": "string", "description": "Semester number: '1', '2', '3', '4'"},
                "discipline": {"type": "string", "description": "Partial discipline name filter"},
                "attestation_type": {"type": "string", "description": "Type filter: Зачет, Экзамен, Курсовой проект, Курсовая работа, Текущий контроль"},
            },
            "required": [],
        },
    },
    {
        "name": "miit_get_disciplines",
        "description": "DISCIPLINES WITH DOCUMENTS from cabinet cache. Each discipline: semester (1-4), discipline name, department (abbreviation like ЦТУТП, ИЯ, УТБиИС), documents[{name, url}] — syllabus PDF, annotation PDF, course materials DOCX. Filter by semester or discipline name. These document URLs can be downloaded via miit_fetch_document. Requires prior miit_sync_cabinet.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "semester": {"type": "string", "description": "Semester: '1', '2', '3', '4'"},
                "discipline": {"type": "string", "description": "Partial discipline name filter"},
            },
            "required": [],
        },
    },
    {
        "name": "miit_get_study_plan",
        "description": "STUDY PLAN from cabinet cache. Returns: specialty_code (e.g. 09.04.01), abbreviation (e.g. УВП), form (очная/...), qualification (Магистр/Бакалавр/...), department (full name), documents[] — syllabus PDFs, educational program PDFs, calendar schedule PDFs. Requires prior miit_sync_cabinet.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "miit_get_portfolio",
        "description": "PORTFOLIO items from cabinet cache. Returns list of {number, description, files[]}. Empty if no portfolio entries exist. Requires prior miit_sync_cabinet.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "miit_upload_portfolio",
        "description": "UPLOAD a file to the student's portfolio in the live cabinet. Opens browser, logs in, uploads file. Takes file_path (absolute path required) and optional description.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to the file to upload"},
                "description": {"type": "string", "description": "Optional description for the portfolio entry"},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "miit_fetch_document",
        "description": "DOWNLOAD a document from the cabinet by URL. Use this to fetch syllabus PDFs, course materials DOCX, or any file whose URL appears in miit_get_disciplines or miit_get_study_plan. Opens browser with auth session, downloads the file, saves to disk. Returns the local file path and size in bytes. The downloaded file can then be read by the agent to extract text/content.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full document URL from disciplines or study_plan cache (document.url field)"},
                "output_path": {"type": "string", "description": "Optional: absolute path where to save the file. Auto-generated from URL if omitted."},
            },
            "required": ["url"],
        },
    },
]


# ═══════════════════════════════════════════════════════════════════
# MCP JSON-RPC Handlers
# ═══════════════════════════════════════════════════════════════════

def handle_initialize(params, msg_id):
    return {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "miit-timetable-mcp", "version": "2.0.0"},
    }


def handle_tools_list(params, msg_id):
    return {"tools": TOOLS}


def resolve_group(arguments):
    """Resolve group_code from arguments or stored state. Returns error tuple if not found."""
    group_code = arguments.get("group_code", "").strip()
    if not group_code:
        state = load_state()
        group_code = state.get("current_group")
        if not group_code:
            return None, {"content": [{"type": "text", "text": "No group_code provided and no current group stored. Use miit_set_current_group first."}]}
    return group_code, None


def handle_tools_call(params, msg_id):
    tool_name = params.get("name", "")
    arguments = params.get("arguments", {})

    try:
        # ── miit_list_institutes ──
        if tool_name == "miit_list_institutes":
            catalog = fetch_catalog()
            result = []
            for inst_id, inst_data in catalog.items():
                total_groups = sum(len(groups) for groups in inst_data.get("courses", {}).values())
                result.append({
                    "id": inst_id,
                    "name": inst_data["name"],
                    "courses_count": len(inst_data.get("courses", {})),
                    "groups_count": total_groups,
                })
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

        # ── miit_find_group ──
        elif tool_name == "miit_find_group":
            group_code = arguments.get("group_code", "")
            if not group_code:
                return {"content": [{"type": "text", "text": "Error: group_code is required"}]}
            catalog = fetch_catalog()
            results = find_group_in_catalog(catalog, group_code)
            if not results:
                return {"content": [{"type": "text", "text": f"Group '{group_code}' not found."}]}
            return {"content": [{"type": "text", "text": json.dumps(results, ensure_ascii=False, indent=2)}]}

        # ── miit_set_current_group ──
        elif tool_name == "miit_set_current_group":
            group_code = arguments.get("group_code", "").strip()
            if not group_code:
                return {"content": [{"type": "text", "text": "Error: group_code is required"}]}

            catalog = fetch_catalog()
            groups = find_group_in_catalog(catalog, group_code)
            if not groups:
                return {"content": [{"type": "text", "text": f"Group '{group_code}' not found in MIIT catalog."}]}

            g = groups[0]
            state = load_state()
            old_group = state.get("current_group")
            ts = now_iso()

            state["current_group"] = g["group_code"]
            state["set_at"] = ts

            # Track history
            history_entry = {
                "group": g["group_code"],
                "set_at": ts,
                "institute": g["institute_name"],
                "course": g["course"],
                "specialty": g["specialty"],
            }
            if old_group and old_group != g["group_code"]:
                history_entry["previous_group"] = old_group
            state["group_history"].append(history_entry)

            # Auto-infer next year's group
            next_group = infer_next_year_group(catalog, g["group_code"])
            if next_group:
                state["next_year_group"] = next_group["group_code"]
                state["next_year_info"] = {
                    "group_code": next_group["group_code"],
                    "timetable_id": next_group["timetable_id"],
                    "institute": next_group["institute_name"],
                    "course": next_group["course"],
                    "specialty": next_group["specialty"],
                }
            else:
                state.pop("next_year_group", None)
                state.pop("next_year_info", None)

            save_state(state)

            result = {
                "current_group": state["current_group"],
                "institute": g["institute_name"],
                "course": g["course"],
                "specialty": g["specialty"],
                "timetable_id": g["timetable_id"],
            }
            if "next_year_group" in state:
                result["next_year_group"] = state["next_year_group"]
                result["next_year_info"] = state["next_year_info"]
            else:
                result["next_year_group"] = None
                result["next_year_note"] = "No next-year group found with same prefix in catalog."

            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

        # ── miit_get_current_group ──
        elif tool_name == "miit_get_current_group":
            state = load_state()
            if not state.get("current_group"):
                return {"content": [{"type": "text", "text": json.dumps({"current_group": None, "note": "No group set. Use miit_set_current_group to set one."}, ensure_ascii=False, indent=2)}]}

            result = {
                "current_group": state["current_group"],
                "set_at": state["set_at"],
                "group_history": state["group_history"],
            }
            if state.get("next_year_group"):
                result["next_year_group"] = state["next_year_group"]
                result["next_year_info"] = state.get("next_year_info")

            # Also refresh next-year inference using current catalog
            catalog = fetch_catalog()
            next_group = infer_next_year_group(catalog, state["current_group"])
            if next_group:
                result["next_year_group_refreshed"] = next_group["group_code"]
                state["next_year_group"] = next_group["group_code"]
                state["next_year_info"] = {
                    "group_code": next_group["group_code"],
                    "timetable_id": next_group["timetable_id"],
                    "institute": next_group["institute_name"],
                    "course": next_group["course"],
                    "specialty": next_group["specialty"],
                }
                save_state(state)

            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

        # ── miit_get_timetable ──
        elif tool_name == "miit_get_timetable":
            group_code, err = resolve_group(arguments)
            if err:
                return err

            timetable_id = arguments.get("timetable_id", "")

            if not timetable_id:
                catalog = fetch_catalog()
                results = find_group_in_catalog(catalog, group_code)
                if not results:
                    return {"content": [{"type": "text", "text": f"Group '{group_code}' not found."}]}
                timetable_id = results[0]["timetable_id"]

            data, semester, date_range, group_name = fetch_timetable_page(timetable_id)
            if data is None:
                return {"content": [{"type": "text", "text": f"No timetable data found for ID {timetable_id}."}]}

            schedule = {
                "group": group_name,
                "semester": semester,
                "date_range": date_range,
                "timetable_id": str(timetable_id),
                "days": build_schedule(data),
            }
            return {"content": [{"type": "text", "text": json.dumps(schedule, ensure_ascii=False, indent=2)}]}

        # ── miit_get_student_context ──
        elif tool_name == "miit_get_student_context":
            group_code, err = resolve_group(arguments)
            if err:
                return err

            catalog = fetch_catalog()
            groups = find_group_in_catalog(catalog, group_code)
            if not groups:
                return {"content": [{"type": "text", "text": f"Group '{group_code}' not found."}]}

            g = groups[0]
            data, semester, date_range, group_name = fetch_timetable_page(g["timetable_id"])

            if data is None:
                context = {
                    "institute": g["institute_name"],
                    "institute_id": g["institute_id"],
                    "group": g["group_code"],
                    "course": g["course"],
                    "specialty": g["specialty"],
                    "timetable_id": g["timetable_id"],
                    "semester": semester,
                    "disciplines": [],
                    "note": "No timetable data available",
                }
            else:
                disciplines = {}
                for day in data:
                    for slot in day.get("timeSlots", []):
                        for event in slot.get("events", []):
                            subj = event.get("text", "").strip()
                            if subj and subj not in disciplines:
                                teachers = []
                                for lec in event.get("lecturers", []):
                                    name = lec.get("shortFio") or lec.get("fullFio", "")
                                    desc = lec.get("description", "")
                                    teachers.append(f"{name} ({desc})" if desc else name)
                                disciplines[subj] = {
                                    "subject": subj,
                                    "type": event.get("textTitle", ""),
                                    "teachers": teachers,
                                }
                context = {
                    "institute": g["institute_name"],
                    "institute_id": g["institute_id"],
                    "group": g["group_code"],
                    "course": g["course"],
                    "specialty": g["specialty"],
                    "timetable_id": g["timetable_id"],
                    "semester": semester,
                    "date_range": date_range,
                    "disciplines": list(disciplines.values()),
                }

            return {"content": [{"type": "text", "text": json.dumps(context, ensure_ascii=False, indent=2)}]}

        # ── miit_sync_cabinet (replaces miit_login) ──
        elif tool_name == "miit_sync_cabinet" or tool_name == "miit_login":
            cab = _ensure_cabinet()
            result = cab.sync_cabinet()
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

        # ── miit_logout ──
        elif tool_name == "miit_logout":
            cab = _ensure_cabinet()
            result = cab.logout_cabinet()
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

        # ── miit_get_profile (cache) ──
        elif tool_name == "miit_get_profile":
            cab = _ensure_cabinet()
            result = cab.get_profile()
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

        # ── miit_get_grades (cache) ──
        elif tool_name == "miit_get_grades":
            cab = _ensure_cabinet()
            result = cab.get_grades(
                semester=arguments.get("semester"),
                discipline=arguments.get("discipline"),
                attestation_type=arguments.get("attestation_type"),
            )
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

        # ── miit_get_disciplines (cache) ──
        elif tool_name == "miit_get_disciplines":
            cab = _ensure_cabinet()
            result = cab.get_disciplines(
                semester=arguments.get("semester"),
                discipline=arguments.get("discipline"),
            )
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

        # ── miit_get_study_plan (cache) ──
        elif tool_name == "miit_get_study_plan":
            cab = _ensure_cabinet()
            result = cab.get_study_plan()
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

        # ── miit_get_portfolio (cache) ──
        elif tool_name == "miit_get_portfolio":
            cab = _ensure_cabinet()
            result = cab.get_portfolio()
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

        # ── miit_upload_portfolio (live browser) ──
        elif tool_name == "miit_upload_portfolio":
            cab = _ensure_cabinet()
            result = cab.upload_portfolio(
                file_path=arguments.get("file_path", ""),
                description=arguments.get("description", ""),
            )
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

        # ── miit_fetch_document (live browser) ──
        elif tool_name == "miit_fetch_document":
            cab = _ensure_cabinet()
            result = cab.fetch_document(
                url=arguments.get("url", ""),
                output_path=arguments.get("output_path"),
            )
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

        else:
            return {"content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}]}

    except Exception as e:
        return {"content": [{"type": "text", "text": f"Error: {str(e)}"}], "isError": True}


METHOD_HANDLERS = {
    "initialize": handle_initialize,
    "tools/list": handle_tools_list,
    "tools/call": handle_tools_call,
}


def send_response(msg_id, result):
    response = {"jsonrpc": "2.0", "id": msg_id, "result": result}
    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def send_error(msg_id, code, message):
    response = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    sys.stderr.write("[miit-timetable-mcp] Server starting...\n")
    sys.stderr.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params", {})

        if msg_id is None:
            if method == "notifications/initialized":
                sys.stderr.write("[miit-timetable-mcp] Client initialized.\n")
                sys.stderr.flush()
            continue

        handler = METHOD_HANDLERS.get(method)
        if handler:
            try:
                result = handler(params, msg_id)
                send_response(msg_id, result)
            except Exception as e:
                sys.stderr.write(f"[miit-timetable-mcp] Handler error: {e}\n")
                sys.stderr.flush()
                send_error(msg_id, -32000, str(e))
        else:
            send_error(msg_id, -32601, f"Method not found: {method}")


if __name__ == "__main__":
    main()
