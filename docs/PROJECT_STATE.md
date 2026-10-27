# Booklet_AI Desktop — Project State & Architecture Reference

> **Single Source of Truth for Project Architecture, Execution, and State**  
> *Generated for new contributors, maintainers, and AI pair-programming sessions.*  
> *All entries derived strictly from git history, codebase tree, test suites, and automation scripts.*

---

## 1. Purpose

Booklet_AI Desktop is an automated publishing pipeline for Iraqi secondary school academic booklets (الملزمات الدراسية). It ingests source manuscripts in DOCX or PDF format, classifies their educational elements (e.g. ministerial exam questions, mathematical formulas, RTL tables, chapter headers), and automates Adobe InDesign via COM/ExtendScript to produce press-ready `.indd` documents and print-ready `.pdf` booklets with zero source text mutation (verified by canonical SHA-256 hash integrity).

---

## 2. Architecture & Pipeline

```
[DOCX / PDF Source]
       │
       ▼
 [Extraction] ─────────► docx_extractor.py / pdf_extractor.py
       │                 (Extracts text, OMML math, tables, images, metadata)
       ▼
 [Integrity] ──────────► text_integrity.py (Canonical SHA-256 hash validation)
       │
       ▼
[Classification] ──────► RuleBasedClassifier (deterministic) or OpenRouterClient (LLM)
       │                 (Identifies ministry questions, formulas, headers, tables)
       ▼
[Payload Build] ───────► generation_pipeline.py + math_renderer.py
       │                 (Builds payload.json, renders MathML to inline SVG)
       ▼
[COM Supervisor] ──────► com_supervisor.py (Isolated Windows child process)
       │                 (Controls InDesign COM, tracks heartbeat, enforces timeouts)
       ▼
[ExtendScript / JSX] ──► generate_booklet.jsx
       │                 ├─ build_master_spread.jsx (Margins, headers, footers)
       │                 ├─ inject_tables_and_math.jsx (RTL tables, inline SVG math)
       │                 └─ apply_question_callout.jsx (Right-side bar callouts)
       ▼
[Press Output] ────────► Master .indd Document + High-Resolution Print .pdf
```

### Key Files by Pipeline Stage

1. **Document Extraction & Integrity**
   - `src/app/core/docx_extractor.py`: Extracts paragraphs, character runs, OMML equations (converted to MathML via XSLT), legacy OLE equations, and tables from `.docx` files.
   - `src/app/core/pdf_extractor.py`: PyMuPDF-based text and image extractor for `.pdf` manuscripts.
   - `src/app/core/text_integrity.py`: Canonical unicode normalization and SHA-256 hashing to guarantee zero mutation.
   - `src/app/core/models.py`: Core Pydantic data models (`BookletDocument`, `ContentBlock`, `PageData`, `TableData`).
   - `src/assets/xslt/OMML2MML.XSL`: Microsoft XSLT stylesheet converting Office Math Markup Language (OMML) to MathML.

2. **Block Classification**
   - `src/app/ai/classifier.py`: `RuleBasedClassifier` (regex patterns for Arabic ministerial questions, headers, formulas) and LLM classification orchestrator.
   - `src/app/ai/client.py`: `OpenRouterClient` for cloud-based LLM classification calls.
   - `src/app/ai/schemas.py`: Pydantic response models for block classification.

3. **Payload Building & Math Rendering**
   - `src/app/core/math_renderer.py`: Pure-Python MathML-to-SVG renderer using `ziamath` and `ziafont` (`config.svg2 = False` for InDesign compatibility, ASCII minus normalization).
   - `src/app/automation/generation_pipeline.py`: Assembles the JSON payload (`build_payload()`), orchestrates SVG generation, and submits generation jobs.
   - `src/app/automation/geometry.py`: Page geometries, margins, column calculations, bleed, and trim sizes.
   - `src/app/automation/styles_schema.py`: `TypographyStylesConfig` defining font families, point sizes, leading, and math scaling.
   - `src/app/automation/pagination_sync.py`: Imposition calculations (e.g. saddle-stitch padding to multiples of 4).

4. **InDesign Automation & Process Supervision**
   - `src/app/automation/com_supervisor.py`: Manages an isolated child process using `win32com.client` to execute InDesign commands safely without blocking or hanging the main process.
   - `src/app/automation/indesign_precondition.py`: Validates InDesign installation and COM interface reachability.
   - `src/app/automation/job_protocol.py`: IPC protocol schema between Python and ExtendScript.

5. **ExtendScript (JSX) Automation Subroutines**
   - `src/assets/scripts/generate_booklet.jsx`: Main entry point inside InDesign; sets up document, coordinates master spreads, story threading, and PDF export.
   - `src/assets/scripts/build_master_spread.jsx`: Generates master pages, headers, footers, page numbering, margins, and bleed bounds.
   - `src/assets/scripts/inject_tables_and_math.jsx`: Injects RTL tables with repeating headers/shading, and places inline SVG math with vertical spacing (`MATH_SPACE_BEFORE_PT = 10.0`, `MATH_SPACE_AFTER_PT = 8.0`).
   - `src/assets/scripts/apply_question_callout.jsx`: Applies ministerial question callouts using paragraph border (3 pt right-edge bar only) and shading tint (8%) with padding offsets.
   - `src/assets/scripts/payload_loader.jsx`: Reads and deserializes `payload.json` inside ExtendScript.
   - `src/assets/scripts/preflight_styles.jsx`: Verifies required paragraph styles exist in the template before injection.

---

## 3. How to Run

### Required Environment
- **Operating System**: Windows 10 or Windows 11 (64-bit).
- **Python**: Python 3.14 (or >= 3.11 64-bit).
- **Adobe InDesign**: Adobe InDesign (verified with InDesign 2026), **Middle East / World-Ready localized build** (mandatory for RTL story direction and RTL tables).
- **Dependencies**: Install pinned packages via `pip install -r src/requirements.txt`.

### Execution Commands

1. **Manual Smoke Test (Generates sample 3-page booklet in InDesign)**:
   ```powershell
   # Full run creating .indd and .pdf via InDesign COM:
   python tools/manual_smoke_test.py

   # Build payload only (without invoking InDesign):
   python tools/manual_smoke_test.py --build-only

   # Custom output directory:
   python tools/manual_smoke_test.py --output-dir "build/smoke_test"
   ```

2. **Real DOCX Processing (Deterministic CLI with --no-ai)**:
   ```powershell
   # Process real DOCX using rule-based classification and generate booklet:
   python tools/generate_from_docx.py path/to/document.docx --out build/my_booklet --no-ai

   # With saddle-stitch binding imposition and custom footer:
   python tools/generate_from_docx.py path/to/document.docx --out build/my_booklet --no-ai --binding saddle_stitch --footer-text "الفصل الأول"

   # Dry-run payload and math SVG extraction only:
   python tools/generate_from_docx.py path/to/document.docx --out build/my_booklet --no-ai --build-only
   ```

3. **Running the Test Suite**:
   ```powershell
   # PowerShell:
   $env:PYTHONPATH="src"; pytest src/tests -q

   # Bash / Command Prompt:
   PYTHONPATH=src pytest src/tests -q
   ```

---

## 4. Step History (STEP-7.0 → Latest)

| Step | Commit Hash | One-Line Description |
|---|---|---|
| **STEP-7.0b** | `0d1b785` | Untrack cached pycache files, update `.gitignore`, and correct `test_step_6_8.py` JSX script path. |
| **STEP-7.1** | `11df353` | Implement InDesign COM supervisor, generation pipeline, ExtendScript JSX automation scripts, and manual smoke test. |
| **STEP-7.1** | `7ffb22e` | Refine COM supervisor error handling, exceptions, and `generate_booklet.jsx` script flow. |
| **STEP-7.1** | `4311e85` | Consolidate ExtendScript JSX files cleanly under `src/assets/scripts/` and remove root duplicate folder. |
| **STEP-7.1a** | `aa39b06` | Harden COM supervisor child process dispatch, win32com error mapping, and static contract tests. |
| **STEP-7.1b** | `9ba3338` | Wire table injection payload generation, enhance `inject_tables_and_math.jsx`, and update smoke tests. |
| **STEP-7.1c** | `45b47f9` | Fix `generate_booklet.jsx` page creation loop, story threading, and PDF preset export handling. |
| **STEP-7.1d / 7.1e** | `8656284` | Enforce RTL BiDi paragraph direction and pad saddle-stitch page counts to exact multiples of 4. |
| **STEP-7.2** | `fbc37ae` | Extract real Word (.docx) content, convert OMML equations to MathML via XSLT, and create `generate_from_docx.py`. |
| **STEP-7.2** | `588c3f7` | Add build and temporary generation directories to `.gitignore`. |
| **STEP-7.2b** | `eeb4982` | Implement inline equation extraction, paragraph splitting, and Zero-Mutation canonical hash tracking. |
| **STEP-7.2c** | `35e0fb5` | Refine `test_step_7_2.py` assertions and equation boundary edge cases. |
| **STEP-7.3** | `16b2f05` | End-to-end integration: DOCX extraction → rule-based/AI classification → InDesign payload generation. |
| **STEP-7.4** | `4b46a94` | Render `math_formula` blocks as inline SVGs via `ziamath`/`ziafont`; insert anchored rectangles with `FitOptions.FRAME_TO_CONTENT`. |
| **STEP-7.4b** | `b676e75` | Add `@requires_xslt` test skip marker for environments without `OMML2MML.XSL`; validate root XML element in `math_renderer.py`. |
| **STEP-7.4d** | `0dece17` | Fix `ziamath` greedy minus corruption by replacing ASCII `-` with `\u2212` in the element tree before rendering. |
| **STEP-7.4e** | `7b913f0` | Disable SVG2 (`zm.config.svg2 = False`) so InDesign imports `<path>` glyphs, and reset rectangle stroke/fill to None. |
| **STEP-7.4f** | `8c60116` | Format ministry-question callouts with right-side bar only (`Top/Bottom/LeftLineWeight = 0`, right weight 3.0) and padding offsets. |
| **STEP-7.4g** | `324aab1` | Add vertical spacing around inline equations (`MATH_SPACE_BEFORE_PT = 10.0`, `MATH_SPACE_AFTER_PT = 8.0`) to prevent table collisions. |

---

## 5. Verification Status: Real InDesign vs Unit-Tested Only

### Verified in Real InDesign (Manual Runs on InDesign 2026 MENA)
- **DOCX → PDF Generation Pipeline**: Full automated flow from `.docx` input to `.indd` generation and high-resolution `.pdf` export via `tools/generate_from_docx.py`.
- **RTL Arabic Tables**: Right-to-left column ordering, repeating header rows, alternating row tints, and cell padding.
- **Ministry Question Callouts**: Right-side leading border bar (3.0 pt primary color) with light background tint (8%) and proper internal padding offsets (no rectangular box outline).
- **Inline SVG Math Formulas**: Vector rendering of MathML expressions (fractions, binomial expansions, Greek characters, powers) placed inline as anchored rectangles without missing glyphs or default black frame borders.
- **Equation Vertical Spacing**: Paragraph spacing (`spaceBefore = 10.0 pt`, `spaceAfter = 8.0 pt`) preventing equations from colliding with preceding tables or succeeding text.
- **Saddle-Stitch Imposition**: Automatic page count calculation and blank page insertion to enforce multiples of 4 for booklet binding.
- **Master Spread Construction**: Running headers, footers with page numbering, margins, and 3mm bleed limits.

### Unit-Tested Only (Mocked COM / Static Contract / In-Memory)
- **PySide6 UI Panels**: `DashboardPanel`, `BatchQueuePanel`, `PreflightPanel`, `AISettingsWidget`, `LivePreviewWidget`, `LanguageSelectorWidget` (tested via pytest offscreen/mock fixtures).
- **COM Process Supervisor Resiliency**: Subprocess timeouts, heartbeat monitoring, and emergency InDesign process termination (`COMSupervisor`).
- **OpenRouter LLM Integration**: Live HTTP API calling, token tracking, retry mechanisms, and error translation (tested with mock HTTP responses).
- **Vector Search & Semantic Deduplication**: `sqlite-vec` embeddings and `question_indexer.py` similarity clustering.
- **Preflight & Verification Engines**: Font glyph coverage analysis, CMYK gamut verification, and overset text detection (`preflight.py`, `post_generation_preflight.py`).
- **SQLite Database & State Persistence**: Schema migrations (001–005), job history recording, and undo/redo stacks (`job_history.py`, `undo_redo.py`).
- **PDF Manuscript Extraction**: PyMuPDF-based text and image extraction (`pdf_extractor.py`).
- **Review PDF Sheet Generation**: Multi-page thumbnail contact sheet generator (`review_pdf.py`).

### Unverified (External / Physical Constraints)
- **Commercial Press Output**: Physical offset printing and mechanical folding/trimming on industrial saddle-stitch binding machinery. *(unverified)*
- **Non-MENA InDesign Localizations**: Behavior on non-Middle-East InDesign installations lacking `TableDirectionOptions.RIGHT_TO_LEFT_DIRECTION` or Arabic World-Ready Composers. *(unverified)*
- **In-Place Auto-Updater**: Live manifest fetching, binary download, and signature verification against production CDN servers (`updater.py`). *(unverified)*

---

## 6. Modules Present in Code but Not Wired to a Running UI

The backend and standalone UI widgets are extensively developed and covered by automated tests, but the application currently lacks an integrated main window (`MainWindow`) orchestrating them into a unified desktop application.

1. **Desktop Main Window & Shell**:
   - `src/main.py`: Headless startup script. Initializes logging, checks updates, validates InDesign COM, and sets up i18n, but does **not** instantiate a `QApplication` event loop or launch a main GUI window.
   - `src/app/ui/dashboard_panel.py` (`DashboardPanel`): Standalone dashboard widget for job submission; not embedded into a main application window.
   - `src/app/ui/batch_panel.py` (`BatchQueuePanel`): Batch queue manager widget; not connected to a parent window.
   - `src/app/ui/preflight_panel.py` (`PreflightPanel`): Interactive preflight issue viewer; not wired into generation flow.
   - `src/app/ui/ai_settings.py` (`AISettingsWidget`): API key and model selection widget; not exposed via a settings dialog.
   - `src/app/ui/live_preview.py` (`LivePreviewWidget`): WeasyPrint HTML/CSS live preview widget; not mounted in any active view.
   - `src/app/ui/problem_report.py` (`ProblemReportDialog`): Error reporting and redaction dialog; not wired to global uncaught exception handlers.

2. **PDF Manuscript Extraction**:
   - `src/app/core/pdf_extractor.py`: PDF extraction logic exists and is unit-tested, but is not exposed in `tools/generate_from_docx.py` or any desktop workflow.

3. **Semantic Question Deduplication & Vector Search**:
   - `src/app/ai/embedding_client.py`: Local vector embedding client using `sqlite-vec`.
   - `src/app/ai/question_indexer.py`: Indexes ministerial questions to detect duplicates across years; not exposed to CLI or UI tools.

4. **Database, History & Undo/Redo**:
   - `src/app/state/undo_redo.py`: Command pattern undo/redo stack; not connected to GUI menu actions.
   - `src/app/state/job_history.py` & `src/app/utils/db.py`: SQLite persistence for historical generation jobs; not bound to a user-facing history panel.
   - `src/app/state/document_recovery.py` & `src/app/utils/auto_recovery.py`: Periodic snapshot and crash recovery engine; not connected to startup restoration prompts.

5. **Preflight & Verification Reporting**:
   - `src/app/core/preflight.py` & `src/app/core/post_generation_preflight.py`: Preflight engines for text overflow and glyph validation; executed in test fixtures but not displayed in the preflight GUI.
   - `src/app/core/review_pdf.py`: PDF review sheet generator; not wired to UI export actions.

6. **Commercial Printing Cost & Pricing**:
   - `src/app/core/pricing_engine.py` & `src/app/core/pricing_models.py`: Page-count and color-coverage printing cost calculators; completely headless.

7. **Batch Queue Automation**:
   - `src/app/automation/batch_queue.py`: Background job queue; not connected to GUI queue controls.
   - `src/app/automation/job_retry.py`: Exponential backoff retry engine; not wired to UI retry triggers.
