# rut-miit-profile-mcp

MCP server for the [RUT (MIIT)](https://www.miit.ru) student portal — timetable, grades, portfolio.

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

| Tool | Description |
|------|-------------|
| `miit_list_institutes` | All 11 institutes at MIIT |
| `miit_find_group` | Search group by code |
| `miit_get_timetable` | Full schedule (days, lessons, teachers, rooms) |
| `miit_get_student_context` | Lab report context (group, semester, disciplines) |
| `miit_set_current_group` | Set active group + auto-infer next year |
| `miit_get_current_group` | Show stored group, history, next-year suggestion |
| `miit_sync_cabinet` | Login + extract profile, grades, portfolio → cache |
| `miit_get_profile` | Personal data (cache) |
| `miit_get_grades` | All grades with filters (cache) |
| `miit_get_portfolio` | Portfolio items (cache) |
| `miit_upload_portfolio` | Upload file to portfolio |
| `miit_logout` | Clear session |

Cache-first: `miit_sync_cabinet` runs headless browser once (~19s). All reads are instant from JSON.

## Files

```
server.py              MCP server (12 tools)
cabinet.py             Headless Playwright scraper + cache API
test_sync.py           Integration test
credentials.json       Login/password (gitignored)
```
