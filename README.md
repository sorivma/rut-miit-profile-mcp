# rut-miit-profile-mcp

MCP server for the [RUT (MIIT)](https://www.miit.ru) student portal — timetable, grades, disciplines, documents.

## Data Model

The server reads two independent data sources:

### 1. Public timetable (miit.ru/timetable)
- **Institutes** — 11 institutes with course/group counts
- **Groups** — searchable by code, with institute, course, specialty
- **Schedule** — weekly timetable: days → lessons (time, type, subject, lecturers, rooms)
- **Student context** — auto-extracted disciplines + teachers from current timetable

### 2. Personal Cabinet (rut-miit.ru/cabinet) — requires credentials
- **Profile** — full_name, tab_number, gender, birth_date, age, birth_place, citizenship, SNILS, INN, email
- **Grades** — all semesters: discipline, attestation type (Экзамен/Зачет/Курсовой проект/Текущий контроль), teacher, grade
- **Disciplines** — per semester: discipline name, department (abbreviation), document URLs (syllabus PDF, annotation PDF, course materials DOCX)
- **Study plan** — specialty code (09.04.01), abbreviation (УВП), form (очная), qualification (Магистр), department, PDF documents
- **Education** — all student's education entries (multiple programs/degrees)
- **Contracts & Orders** — availability flags

## Install

```bash
pip install playwright
python -m playwright install chromium
```

Copy `credentials.json.example` → `credentials.json` and fill your login.

## MCP config

```json
{
  "mcp": {
    "miit-timetable": {
      "type": "local",
      "command": ["python", "./server.py"],
      "enabled": true,
      "timeout": 60000
    }
  }
}
```

## Tools

| Tool | Source | Description |
|------|--------|-------------|
| `miit_list_institutes` | timetable | All institutes with IDs and group counts |
| `miit_find_group` | timetable | Search group by code across all institutes |
| `miit_get_timetable` | timetable | Full schedule: days, lessons, teachers, rooms |
| `miit_get_student_context` | timetable | Lab report context: group, semester, disciplines, teachers |
| `miit_set_current_group` | state | Set active group + auto-infer next year |
| `miit_get_current_group` | state | Stored group, change history, next-year prediction |
| `miit_sync_cabinet` | cabinet 🔐 | Login + extract ALL cabinet data → cache (~30s) |
| `miit_login` | cabinet 🔐 | Alias for miit_sync_cabinet |
| `miit_get_profile` | cache | Personal data: name, tab#, SNILS, INN, etc. |
| `miit_get_grades` | cache | All grades with filters (semester, discipline, type) |
| `miit_get_disciplines` | cache | Disciplines with departments + document URLs |
| `miit_get_study_plan` | cache | Specialty code, qualification, department, PDFs |
| `miit_get_portfolio` | cache | Portfolio items |
| `miit_upload_portfolio` | cabinet 🔐 | Upload file to portfolio |
| `miit_fetch_document` | cabinet 🔐 | Download PDF/DOCX from cabinet by URL |
| `miit_logout` | — | Clear browser session and cache |

🔐 = requires credentials.json, opens headless browser

## Workflow

```
miit_sync_cabinet          ← 1. Authenticate + extract everything (~30s)
  ├─ miit_get_profile       ← 2. Read profile from cache (instant)
  ├─ miit_get_grades        ← 3. Read grades from cache (instant)
  ├─ miit_get_disciplines   ← 4. Read disciplines + doc URLs (instant)
  ├─ miit_get_study_plan    ← 5. Read study plan (instant)
  └─ miit_fetch_document    ← 6. Download syllabus/materials by URL (~5s)
```

## Files

```
server.py              MCP server (16 tools)
cabinet.py             Headless Playwright scraper + cache API
test_sync.py           Integration test
credentials.json       Login/password (gitignored)
cabinet_cache.json     Cached cabinet data (gitignored)
browser_state.json     Browser cookies (gitignored)
state.json             Active group + history
```
