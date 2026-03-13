# DSAF — Dynamic Survey Automation Framework

A full-stack Python/Flask application for automating Japanese survey platforms (rsch.jp).

---

## Quick Start

```bash
cd dsaf

# 1. Create and activate virtual environment
python -m venv venv

# Windows
venv\Scripts\activate
# macOS / Linux
# source venv/bin/activate

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Install Playwright browser binaries
playwright install chromium

# 4. Copy and edit environment variables
cp .env.example .env
# Edit .env: set FLASK_SECRET_KEY and optionally PROXY_LIST

# 5. Start the server
python run.py
```

Open **http://localhost:5010** in your browser.

---

## Project Structure

```
dsaf/
├── app/
│   ├── __init__.py          # Flask app factory (create_app)
│   ├── config.py            # Dev / Prod / Test config classes
│   ├── extensions.py        # Flask-SocketIO singleton
│   ├── exceptions.py        # Custom exception hierarchy
│   ├── models/
│   │   ├── survey_map.py    # SurveyMap, SurveyPage, Question dataclasses
│   │   ├── pattern.py       # Pattern, AnswerStrategy, TimingConfig
│   │   └── run_result.py    # RunResult dataclass
│   ├── services/
│   │   ├── browser_service.py   # Playwright context management + anti-detection
│   │   ├── proxy_service.py     # Proxy pool with round-robin and cooldown
│   │   ├── mapper_service.py    # MapperService + BranchingMapperService
│   │   ├── pattern_service.py   # Pattern CRUD
│   │   └── executor_service.py  # Core automation engine
│   ├── routes/
│   │   ├── mapper.py        # /api/mapper/* (12 endpoints)
│   │   ├── configurator.py  # /api/config/* (6 endpoints)
│   │   └── executor.py      # /api/executor/* (5 endpoints)
│   └── templates/
│       ├── base.html        # Bootstrap 5.3 dark + Alpine.js + Socket.IO
│       ├── dashboard.html   # Overview of maps, patterns, recent runs
│       ├── mapper.html      # Interactive mapping & branch discovery UI
│       ├── configurator.html# Pattern editor (question tree + answer strategies)
│       └── executor.html    # Batch run launcher with live log stream
├── static/
│   ├── css/app.css          # Custom dark-theme styles
│   └── js/
│       ├── mapper.js        # mapperApp() Alpine function
│       ├── configurator.js  # configuratorApp() Alpine function
│       └── executor.js      # executorApp() Alpine function + Socket.IO
├── data/
│   ├── maps/                # survey_map JSON files (schema v1.1)
│   ├── patterns/            # pattern JSON files (schema v1.1)
│   └── results/             # run_result JSON files
├── tests/
│   ├── test_mapper.py       # Fingerprint + honeypot + question extraction
│   ├── test_executor.py     # Fresh context, timing, honeypot skip, batch stop
│   └── test_pattern_service.py  # Validation warnings + CRUD
├── .env.example
├── .gitignore
├── requirements.txt
└── run.py
```

---

## Three-Phase Workflow

### Phase 1 — Mapper

Navigate to `/mapper`.

1. Enter the survey URL (e.g. `https://rsch.jp/survey/xxx?uid=TESTUID`).
2. Click **Start Mapping** — a visible browser window opens.
3. For each survey page, click **Scan Page** to extract questions.
4. Fill in any answers you wish to record, then click **Record & Proceed**.
5. After completing the survey, click **Finalize Map**.

For branch discovery, start a **Discovery Session**, explore each branch path, and use the **Coverage** panel to see how many option combinations remain unexplored.

Generated files are stored in `data/maps/<survey_id>.json`.

---

### Phase 2 — Configurator

Navigate to `/configurator`.

1. Select a survey map from the dropdown.
2. The question tree loads on the left. Click any question to configure its answer strategy:
   - **fixed** — always use a specific value
   - **random_option** — pick a random option from the detected list
   - **random_from_list** — pick from a comma-separated list you define
   - **text_from_list** — type a random string from a list (for text inputs)
3. Fill in pattern metadata (name, UID pool, timing, etc.).
4. Click **Validate** to check for missing or invalid answer strategies.
5. Click **Save Pattern**.

Generated files are stored in `data/patterns/<pattern_id>.json`.

---

### Phase 3 — Executor

Navigate to `/executor`.

1. Select a survey map and pattern.
2. Set the run count, concurrency level, and (optionally) a proxy URL.
3. Click **Run** to confirm and launch the batch.
4. Watch the live log stream and progress bar as runs complete.
5. Click **Export Results** to download `results_<batch_id>.json`.

---

## JSON Schemas

### Survey Map (schema v1.1)

```json
{
  "schema_version": "1.1",
  "survey_id": "<slug>",
  "base_url": "https://rsch.jp/survey/...",
  "url_params": {},
  "created_at": "<ISO 8601>",
  "pages": [
    {
      "page_id": "<slug>",
      "page_index": 0,
      "url_pattern": "",
      "page_fingerprint": "<SHA-256 hex>",
      "page_type": "questions | login | confirmation | complete",
      "questions": [...],
      "navigation": { "submit_button_text": "次へ", "submit_selector": "...", "method": "submit" },
      "branching_hints": []
    }
  ],
  "branch_tree": {},
  "discovery_sessions": [],
  "coverage_stats": {}
}
```

### Pattern (schema v1.1)

```json
{
  "schema_version": "1.1",
  "pattern_id": "<slug>",
  "pattern_name": "...",
  "linked_survey_id": "<survey_id>",
  "uid_pool": ["UID001", "UID002"],
  "uid_strategy": "sequential | random",
  "answers": {
    "<page_id>": {
      "<q_id>": {
        "strategy": "fixed | random_option | random_from_list | text_from_list",
        "value": "...",
        "values": ["...", "..."]
      }
    }
  },
  "timing": {
    "min_total_seconds": 30,
    "max_total_seconds": 120,
    "page_delay_min": 2.0,
    "page_delay_max": 6.0,
    "typing_delay_per_char_ms": [50, 150]
  }
}
```

---

## Anti-Detection Notes

- Every run uses a **fresh, isolated browser context** (`new_context()`). Cookies and storage never persist between runs.
- **22 real Japanese browser user agents** are rotated randomly per context.
- A 7-patch **stealth init script** is injected into every page:
  - Removes `navigator.webdriver`
  - Fakes `navigator.plugins` (3 standard Chrome plugins)
  - Sets `navigator.languages` to `['ja', 'ja-JP', 'en-US']`
  - Injects `window.chrome.runtime`
  - Sets `Notification.permission` to `'default'`
  - Adds ±1 RGB noise to canvas `getImageData` results
  - Removes Playwright-specific window properties
- **Honeypot fields** (hidden via CSS or positioned off-screen) are detected during mapping and **never filled** during execution.
- All clicks use randomized mouse trajectories; all typing uses per-character delays with occasional simulated typos and corrections.
- Page timing is governed by `TimingConfig.min_total_seconds` / `max_total_seconds` to ensure natural session durations.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `FLASK_ENV` | `development` | Config profile (`development`, `production`, `testing`) |
| `FLASK_SECRET_KEY` | `dev-secret-key` | Flask session secret (change in production) |
| `DATA_DIR` | `./data` | Root directory for JSON data files |
| `DEFAULT_HEADLESS` | `true` | `false` to show browser windows during execution |
| `MAX_CONCURRENCY` | `3` | Maximum parallel browser contexts |
| `PROXY_LIST` | _(empty)_ | Comma-separated proxy URLs |
| `LOG_LEVEL` | `INFO` | Python logging level |

---

## Running Tests

```bash
pip install pytest pytest-asyncio
pytest tests/ -v
```

Tests use mocked Playwright pages and temporary filesystem directories. No live network calls are required.

---

## Extending the Framework

- **New survey platform**: Subclass `MapperService`, override `scan_current_page()`, and update `NEXT_BUTTON_TEXTS_JA` / `COMPLETE_PAGE_SIGNALS` as needed.
- **New answer strategy**: Add a case to `AnswerStrategy.strategy` in `models/pattern.py`, implement resolution in `ExecutorService._resolve_answer_value()`, and add UI controls in `configurator.html`.
- **New anti-detection patch**: Append to `STEALTH_SCRIPT` in `browser_service.py`.
