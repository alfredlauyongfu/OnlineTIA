# OnlineTIA

A Python pipeline that turns Blue Prism **Technical Infrastructure Assessment
(TIA)** Excel workbooks into Markdown assessment reports. It (1) extracts the
sheets of a customer TIA workbook into JSON, (2) calls an LLM to distil a
reference TIA workbook's sheets into a smaller per-topic knowledge set, (3)
ingests that knowledge into a Retrieval-Augmented Generation (RAG) store, then
(4) generates a per-customer assessment report grounded in both the customer's
own data and the RAG-retrieved reference recommendations.

## Architecture / data flow

`run.py` runs four stages in this order on every invocation:

```
                  ┌─────────────────────────────┐
                  │ 1. extract reference info   │  (idempotent: skipped if inbox empty)
                  │                             │
                  │  REFERENCE_TO_BE_LOADED_DIR │
   ReferenceTo  ──┤        ▶ convert ▶          ├─►  REFERENCE_JSON_DIR
   BeLoaded ─►    │        │                    │     │
                  │        │ on success         │     ▼ LLM per-sheet extract
                  │        ▼                    │     │
                  │  REFERENCE_LOADED_DIR       │     ▼
                  │                             │  REFERENCE_JSON_EXTRACTED_DIR
                  └─────────────────────────────┘     │
                                                     │
                  ┌─────────────────────────────┐    │
                  │ 2. sync RAG with reference  │ ◄──┘
                  │                             │
                  │  basenames match RAG? ──► skip
                  │  otherwise ──► delete-all + upload-all (POST /rag/ingest/...)
                  └─────────────────────────────┘

                  ┌─────────────────────────────┐
                  │ 3. primary + 4. TIA report  │  (per-file loop; skipped if INPUT empty)
                  │                             │
   INPUT_DIR ─►───┤  for each xlsx:             │
   (customer)     │    wipe INTERMEDIATE        │
                  │    convert ──► INTERMEDIATE_JSON_DIR (one file's sheets only)
                  │    LLM /rag/chat/completions with tags=["tia_reference"]
                  │    write TIA_<stem>_<ts>.md to OUTPUT_REPORT_DIR
                  │  finalize: PROCESSING ──► PROCESSED for converted xlsx
                  └─────────────────────────────┘
```

Each customer file is processed **independently and sequentially** — the
intermediate JSON store never holds more than one file's content at a time,
and each TIA report is based on exactly one input file.

## Modules

All source modules live under `online_tia/` for neatness; `run.py` is the
only Python file at the project root.

| File | What it does |
|------|-------------|
| `run.py` | CLI entry point. Runs all four stages in the order above. |
| `online_tia/reference_info_extractor.py` | Stage 1 orchestrator: reference Excel → JSON → LLM-extracted JSON. |
| `online_tia/excel_to_json.py` | `ExcelToJsonConverter` — Excel→JSON conversion + claim/processed move lifecycle. |
| `online_tia/reference_json_combiner.py` | `ReferenceJsonCombiner` — per-sheet LLM extraction via `/v1/chat/completions`. |
| `online_tia/rag_ingester.py` | `RagIngester` — list/register/upload/delete against `/rag/ingest/*`, with sync gate. |
| `online_tia/tia_generator.py` | `TiaReportGenerator` — generates the Markdown TIA via `/rag/chat/completions`. |
| `online_tia/logging_setup.py` | `configure_logging` + `bootstrap` (.env load + required-var check + logging). |

Three of the modules (`reference_info_extractor.py`, `rag_ingester.py`,
`tia_generator.py`) can also be invoked as standalone harnesses for testing
individual stages — each has a `main()` and `if __name__ == "__main__":`
block at the bottom.

## Prerequisites

- **Python 3.10+** (uses PEP 604 `int | str` annotations).
- **Network access** to:
  - `api-ai.ssnc.cloud` (chat-completions endpoint, used by the reference
    extraction stage).
  - `api-ai-us.ssnc-corp.cloud` (RAG endpoints, used by RAG sync + TIA
    generation stages).
- A valid **SS&C Cloud AI Gateway API key** and **User ID** with access to
  the configured model (default `global.anthropic.claude-sonnet-4-6`).
- A registered **use-case ID** for AI Gateway billing/governance.

No system-level dependencies (no SQL, no Docker, no compilation).

## Deployment / setup on a new Windows machine

PowerShell commands shown; bash/zsh equivalent should be obvious.

### 1. Clone / unzip the project somewhere

```powershell
cd C:\blueprism
git clone <repo url> OnlineTIA
cd OnlineTIA
```

(Or copy the project tree manually — there's no build artifact to fetch.)

### 2. Create and activate a virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If activation is blocked by execution policy:
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned` then retry.

### 3. Install Python dependencies

```powershell
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

This installs four packages: `openpyxl`, `python-dotenv`, `requests`, and
`pytest` (needed for running the test suite).

### 4. Create the working-directory tree

The pipeline expects ten directories to exist (it auto-creates output
subdirs when missing, but pre-creating everything avoids first-run noise).
Default layout:

```powershell
$base = "C:\blueprism\OnlineTIAWorkingDir"
$dirs = @(
    "InputCustomerResponseExcel",
    "IntermediateCustomerResponseJson",
    "Processing", "Processed",
    "OutputReport",
    "ReferenceToBeLoaded", "ReferenceLoaded",
    "ReferenceJson", "ReferenceJsonExtracted",
    "Logs"
)
foreach ($d in $dirs) { New-Item -ItemType Directory -Force "$base\$d" | Out-Null }
```

Change `$base` if you want a different root — make sure the `.env` paths
match.

### 5. Configure `.env`

```powershell
Copy-Item .env.example .env
```

Then open `.env` and:
- Paste the real value for `SSC_CLOUD_AIGATEWAY_API_KEY`,
  `SSC_CLOUD_AIGATEWAY_USER_ID`, and `USE_CASE_ID`.
- Adjust any path env vars if your working-dir root differs from the default.
- Optionally tune `LOG_LEVEL` (default `INFO`).

`.env` is gitignored by design — never commit it.

### 6. (Optional) Drop a reference workbook into the inbox

If you want the first run to actually populate RAG, drop a TIA reference
workbook (e.g. `Technical Infrastructure Assessment V2.6.4.xlsm`) into
`REFERENCE_TO_BE_LOADED_DIR`. Otherwise the reference stage is a
no-op and the RAG sync stage runs against whatever's already in the RAG
store at the gateway.

### 7. Run

```powershell
& .\.venv\Scripts\python.exe run.py
```

Expected: `=== run.py start ===` banner, four stage lines, exit 0. See
`Logs\logs.txt` for the full trace.

## Configuration reference

Every variable listed below is required in `.env` (the entry point's
`bootstrap()` call exits with code 2 if any are missing).

### Working-directory paths

| Variable | Purpose | Read by |
|----------|---------|---------|
| `INPUT_DIR` | Customer workbooks to be processed (xlsx/xlsm). | `run.py` |
| `INTERMEDIATE_JSON_DIR` | Per-sheet JSON output from customer conversion. Wiped before each customer file is processed. | `run.py`, `tia_generator.py` |
| `PROCESSING_DIR` | In-flight customer xlsx (claimed but not yet graduated). | `run.py` |
| `PROCESSED_DIR` | Customer xlsx graduates here after successful conversion. | `run.py` |
| `OUTPUT_REPORT_DIR` | Generated `TIA_<stem>_<timestamp>.md` reports. | `run.py`, `tia_generator.py` |
| `REFERENCE_TO_BE_LOADED_DIR` | Reference TIA workbooks waiting to be ingested. | `reference_info_extractor.py` |
| `REFERENCE_LOADED_DIR` | Reference xlsx graduates here after successful ingest. | `reference_info_extractor.py` |
| `REFERENCE_JSON_DIR` | Per-sheet JSON from the reference Excel conversion. Wiped each reference run. | `reference_info_extractor.py`, `reference_json_combiner.py` |
| `REFERENCE_JSON_EXTRACTED_DIR` | Per-sheet LLM-distilled JSON. Wiped each reference run. Source for RAG ingest. | `reference_json_combiner.py`, `run.py`, `rag_ingester.py` |
| `LOG_DIR` | Holds `logs.txt` (append-mode across runs). | all entry points |

### Logging

| Variable | Purpose |
|----------|---------|
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` (case-insensitive). Unknown values fall back to `INFO`. At `DEBUG` the RAG `listfiles` full JSON dump is included. |

### SS&C Cloud AI Gateway

| Variable | Purpose |
|----------|---------|
| `SSC_CLOUD_AIGATEWAY_API_KEY` | Auth — sent as `Authorization: Bearer ...` for `/v1/chat/completions`, and as `X-API-Key: ...` for all `/rag/...` endpoints. |
| `SSC_CLOUD_AIGATEWAY_USER_ID` | Sent as `X-User-Id` on `/v1/chat/completions` calls (reference extraction stage only). |
| `SSC_CLOUD_AIGATEWAY_BASE_URL` | Base for the OpenAI-compatible chat-completions endpoint (default `https://api-ai.ssnc.cloud/v1`). |
| `SSC_CLOUD_RAG_BASE_URL` | Base for the RAG endpoints (default `https://api-ai-us.ssnc-corp.cloud`). |
| `SSC_CLOUD_AIGATEWAY_MODEL` | Model name passed as `model` / `llm_name` in the LLM calls. |
| `USE_CASE_ID` | Sent as `X-Use-Case-Id` on `/v1/chat/completions` calls (reference extraction stage only). |

## Running individual stages

Each of these can be invoked directly for testing one stage in isolation:

```powershell
# Stage 1 only: reference Excel → per-sheet JSON → LLM-distilled JSON.
& .\.venv\Scripts\python.exe online_tia\reference_info_extractor.py

# Stage 2 only: sync RAG with whatever's currently in REFERENCE_JSON_EXTRACTED_DIR.
& .\.venv\Scripts\python.exe online_tia\rag_ingester.py

# Stages 3+4 in isolation: generate a TIA from whatever's currently in
# INTERMEDIATE_JSON_DIR. (Doesn't run the per-file isolation loop; uses
# whatever happens to be in that directory.)
& .\.venv\Scripts\python.exe online_tia\tia_generator.py
```

## User Guide

Once deployment is complete (see *Deployment / setup* above), day-to-day
usage is built around scheduling `run.py` and dropping files into the two
inbox directories. This section walks an operator through the model.

### Prerequisite

Before scheduling anything, confirm:

- `.env` has been copied from `.env.example` and filled in with valid
  SS&C Cloud credentials (Deployment step 5).
- All ten working directories from the *Configuration reference* tables
  exist on disk (Deployment step 4).
- The machine has network access to both gateway hosts
  (`api-ai.ssnc.cloud` and `api-ai-us.ssnc-corp.cloud`).

### Schedule `run.py` periodically

The pipeline is designed to be invoked on a fixed schedule — e.g. every
30 minutes — rather than run as a long-lived service. On Windows use
**Task Scheduler** to fire the venv-Python at `run.py` on whatever cadence
fits your turnaround target:

```
C:\blueprism\OnlineTIA\.venv\Scripts\python.exe C:\blueprism\OnlineTIA\run.py
```

Each invocation is a single short-lived process. If both inbox directories
are empty when it fires, the whole run is a no-op finishing in
milliseconds — so a tight schedule is safe.

### What happens on each run

Each `run.py` invocation does up to **two distinct things** in sequence,
each triggered only when its inbox has files. With both inboxes empty,
the run logs the skip lines and exits.

#### 1. Reference data ingest — triggered by files in `REFERENCE_TO_BE_LOADED_DIR`

This is how you populate the RAG knowledge base that the final report
draws on. Drop a TIA reference workbook (e.g. an internal TIA template
with scoring guidance, recommended configurations, severity rubrics)
into `REFERENCE_TO_BE_LOADED_DIR`. The next `run.py` will:

1. **Convert** each sheet of the workbook to a per-sheet JSON file in
   `REFERENCE_JSON_DIR`. This is a pure local Excel-to-JSON pass — every
   cell value is serialized, dates become ISO strings, and empty cells
   are dropped to keep the JSON compact. No LLM involved.

2. **Extract the useful content** from each sheet via an LLM call,
   writing the result as
   `extracted_<sheet>_<YYYYMMDD_HHMMSS>.json` in
   `REFERENCE_JSON_EXTRACTED_DIR`. Raw per-sheet JSON typically contains
   a lot of noise that isn't actionable for a customer report —
   instructions tabs, change logs, version history, unanswered template
   rows, header text appearing as data, duplicate scoring entries. A
   focused LLM prompt distils each sheet down to a structured JSON
   object of TIA-relevant facts (scoring tables, recommended
   configurations, severity guidance, etc.), discarding the noise.
   **One LLM call per sheet.**

3. **Upload** every freshly-extracted JSON to the SS&C Cloud RAG-as-a-
   Service, where each file is chunked, vectorized, and stored. This is
   the reference knowledge the final customer report retrieves from at
   generation time. The upload step is idempotent: if the local set of
   filenames already matches what's in RAG it skips entirely; if they
   differ (because a new reference workbook was ingested) the stale RAG
   entries are deleted and the fresh ones uploaded in their place.

After successful processing the source workbook is moved from
`REFERENCE_TO_BE_LOADED_DIR` into `REFERENCE_LOADED_DIR` so
it isn't re-processed on the next run.

#### 2. Customer report generation — triggered by files in `INPUT_DIR`

This is how you generate a Technical Infrastructure Assessment report
for a customer. Drop a completed customer TIA response workbook into
`INPUT_DIR`. The next `run.py` will, **for each workbook**:

1. **Convert** the workbook to per-sheet JSON in `INTERMEDIATE_JSON_DIR`
   (same Excel-to-JSON pass as the reference flow).

2. **Generate the TIA report** as a single LLM call that combines the
   customer's JSON data with relevant reference chunks retrieved from
   the RAG database. The LLM produces a Markdown document; the pipeline
   writes it to `OUTPUT_REPORT_DIR` as
   `TIA_<source_stem>_<YYYYMMDD_HHMMSS>.md`.

Each customer workbook is processed **independently and sequentially** —
`INTERMEDIATE_JSON_DIR` is wiped between files so each report is
grounded in exactly one customer's data. After successful processing the
source workbook graduates `INPUT_DIR → PROCESSING_DIR → PROCESSED_DIR`.

### Logs

Every operation is recorded in `LOG_DIR\logs.txt` in append mode, with
ISO-style timestamps on every line. The log captures: per-stage banners,
every per-file conversion outcome, every LLM call (initiation + success-
with-sizes-and-citations *or* failure-with-error), every RAG operation
(list / register / upload / delete), every wipe, and the final exit code
per `run.py` invocation. The file grows across runs — rotate it manually
or via your OS as needed.

Verbosity is controlled by **`LOG_LEVEL`** in `.env` — one of `DEBUG`,
`INFO`, `WARNING`, `ERROR`, `CRITICAL` (case-insensitive; unknown values
fall back to `INFO`). At `DEBUG` the RAG list-files response is also
dumped in full structured form, which is useful when debugging RAG ingest
state. For day-to-day operation `INFO` is the right setting.

## Outputs

- **TIA reports**: `OUTPUT_REPORT_DIR\TIA_<source_stem>_<YYYYMMDD_HHMMSS>.md`
  — one Markdown file per customer xlsx, with the source stem in the
  filename so per-file reports never collide.
- **Logs**: `LOG_DIR\logs.txt` — append-mode, timestamped, captures every
  stage banner, every LLM call (start / OK / FAILED), every RAG operation,
  and the wipe events.

## Tests

### Where the tests live

All tests sit in the `tests/` folder at the project root, one file per
source module plus a smoke file:

```
tests/
├── conftest.py                          # adds online_tia/ to sys.path
├── test_imports_smoke.py                # every module imports cleanly
├── test_excel_to_json.py
├── test_logging_setup.py
├── test_rag_ingester.py
├── test_reference_json_combiner.py
└── test_tia_generator.py
```

### Prerequisite

`pytest` is already listed in `requirements.txt` and gets installed in
Deployment step 3. No extra install is needed before running the suite.

### Run the full suite

From the project root, using the venv-Python:

```powershell
& .\.venv\Scripts\python.exe -m pytest -q
```

Expected output ends with `81 passed in ~2s` and exit code 0. If you see
a failure, the line immediately above the summary identifies the file
and test name.

### Common execution variations

All commands below assume `pytest` is invoked through the venv-Python the
same way as the full-suite command above. The leading
`& .\.venv\Scripts\python.exe -m` is omitted for readability.

| Goal | Command |
|------|---------|
| Verbose, one line per test | `pytest -v` |
| Stop on the first failure | `pytest -x` |
| Run one file | `pytest tests\test_excel_to_json.py` |
| Run tests whose name matches a substring | `pytest -k safe_name` |
| Run one specific (parametrized) test | `pytest tests\test_excel_to_json.py::test_safe_name` |
| Run one specific parametrized case | `pytest "tests\test_excel_to_json.py::test_safe_name[Orders (Q1)-Orders_Q1]"` |
| Don't capture stdout (see `print` output) | `pytest -s` |
| Show local variables on failure | `pytest -l` |
| Combine, e.g. verbose + stop on first failure | `pytest -vx` |

### What the suite covers

- **Pure logic for every module** — `safe_name`, cell-value serialization,
  the per-sheet header / blank-row handling, `wipe_output`, the full
  `process_one` → `finalize_to_processed_dir` lifecycle, `_resolve_level`
  parsing, `bootstrap` env-var validation, `_sheet_name_from_filename`,
  `_read_customer_content`, `_build_user_message`, `_build_output_path`
  filename format, `_sha256`, `_build_rag_config`.
- **HTTP-mocked behaviour for `rag_ingester`** — the sync gate's
  match-vs-mismatch decision, the delete-all-then-reupload path, empty
  source dir handling, and `_raise_for_status` / `list_files` HTTP-error
  + connection-error propagation.

What's **not** covered today: `_call_rag_chat` (tia_generator) and
`_call_llm` (reference_json_combiner). Their happy paths and error
handlers are currently exercised only by live `run.py` invocations
against the gateway; bringing them into the mocked-HTTP suite is a
worthwhile future addition.

### Offline guarantee

The suite never makes a real network call. `requests.get` and
`requests.post` are monkey-patched with `unittest.mock.patch` in the
RAG-ingester tests; Excel fixtures are built in memory via `openpyxl`
inside `tmp_path` directories per test. You can run the full suite on a
machine without VPN or gateway access.

## Troubleshooting

- **`Missing required env var(s) in .env: <name>`** — open `.env` and add
  the named variable. Most common after first checkout when `.env` was
  copied from `.env.example` but a placeholder was left blank.
- **`Cannot reach gateway at <url>`** — DNS/network issue. Verify the
  machine can resolve both `api-ai.ssnc.cloud` and
  `api-ai-us.ssnc-corp.cloud` (corporate VPN may be required).
- **`HTTP 404: Unsupported path`** — usually means
  `SSC_CLOUD_AIGATEWAY_BASE_URL` or `SSC_CLOUD_RAG_BASE_URL` is wrong.
  The base URL must NOT include `/chat/completions` or `/rag/ingest/...`
  — those path segments are appended by the code.
- **`No xlsx in <inbox>; skipping convert and extract stages`** — expected
  when the reference inbox is empty. To trigger reference processing, drop
  a workbook into `REFERENCE_TO_BE_LOADED_DIR`.
- **`--- stage: generate TIA report (skipped: no files in INPUT_DIR) ---`**
  — expected when no customer workbooks are present. Drop one into
  `INPUT_DIR` to trigger the per-file primary + TIA loop.
- **RAG sync repeatedly re-uploads everything** — every reference run
  produces fresh timestamped filenames, so the sync gate detects a
  mismatch and rotates. If you want to avoid the re-upload, don't re-run
  the reference stage (leave `REFERENCE_TO_BE_LOADED_DIR` empty).
