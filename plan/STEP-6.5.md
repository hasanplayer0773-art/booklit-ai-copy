<!-- من الخطة الرئيسية Booklet_AI Desktop Master Plan -->

### [STEP-6.5] Quick PDF Export for Review

#### 1. Rationale

Reviewing a document by opening InDesign is slow relative to a quick visual sanity check. This step adds a lightweight export path that produces a reviewable PDF directly from the already-extracted and classified content, without invoking `COMSupervisor` or InDesign at all.

**Post-review correction — confirmed defect in the original tooling assumption:** the original draft implied this path could be built from the existing stack (PyMuPDF, python-docx, lxml). This is not viable: PyMuPDF is an extraction/manipulation library, not a typesetting engine — it has no support for Arabic glyph shaping, bidirectional (BiDi) text ordering, or MathML rendering. Attempting to lay out raw `ContentBlock` text directly through PyMuPDF would produce unshaped, incorrectly-ordered Arabic strings, which is worse than useless for a review tool whose entire purpose is visual sanity-checking of Arabic content. A real typesetting/rendering step is required.

#### 2. Tooling Decision

**Post-follow-up-review correction — two confirmed defects in the original tooling claim:** independent reviewers confirmed (1) the original draft's description of WeasyPrint as "pure Python (no bundled browser engine required)" is misleading — WeasyPrint binds via CFFI to native C libraries (Pango, Cairo, HarfBuzz, GLib), which are not present on a stock Windows machine and must be explicitly bundled with the PyInstaller build, a real packaging cost the original draft did not surface; (2) WeasyPrint has no built-in MathML rendering capability, so `math_formula` blocks (stored as MathML per STEP-1.2) cannot reach a review PDF through WeasyPrint alone — a dedicated math-rendering stage is required in addition to WeasyPrint, not instead of it.

- **WeasyPrint** is retained as the correct choice for the *text/table/BiDi* portion of this feature — it renders HTML+CSS to PDF using Pango for text shaping and HarfBuzz for glyph shaping, both of which handle Arabic (and other non-Latin, BiDi) scripts correctly out of the box, which remains a genuine and necessary improvement over attempting this with PyMuPDF. **It is smaller than a Chromium-based alternative** (which would add several hundred megabytes), but it is not dependency-free — its native library requirements are addressed explicitly in Preconditions below, not glossed over.
- **A dedicated math-rendering stage is added ahead of WeasyPrint in the pipeline**, since WeasyPrint itself cannot render MathML: `math_formula` blocks' MathML content is rendered to SVG (or PNG as a fallback) via a small, focused MathML-to-image step — several lightweight libraries can do this without requiring a full browser engine (e.g. converting MathML to a renderable form the review pipeline can embed as an image within the WeasyPrint-generated HTML/PDF). The resulting SVG/PNG is embedded as an `<img>` within the HTML template WeasyPrint consumes, so the review PDF's math rendering is a distinct sub-step, not an assumed capability of WeasyPrint itself.
- **Corrected pipeline:** `ContentBlock`(`math_formula`).mathml → math renderer → SVG/PNG fragment → embedded into the HTML template → WeasyPrint → review PDF. Non-math blocks (`arabic_text`, `english_text`, `ministry_question`, `list_item`, `TableData`) map directly to HTML as before, with `dir="rtl"`/`dir="ltr"` applied per STEP-2.2's direction rules — mirrored here for review-path consistency with the real InDesign path, even though exact typography will differ.
- **This remains explicitly a review artifact, not a production-equivalent one** — the simplified HTML/CSS style mapping is not required to match `.idml` template fidelity (STEP-2.3), only to be legible and correctly directionally-ordered.

#### 3. Architectural Placement

- This is explicitly **not** a substitute for the final InDesign-generated output — it is a distinct, lower-fidelity rendering path used only for quick review before committing to the full InDesign generation job. It must be visually and functionally distinguishable in the UI from the final print-ready export so it is never mistaken for, or accidentally sent to print as, the final deliverable.
- Because it does not use InDesign, this path does not have access to the `.idml` template's paragraph styles (STEP-2.3) — it uses a separate, simplified style mapping sufficient for review legibility, not final typography fidelity. This distinction must be visible to the user (e.g. a persistent "معاينة / PREVIEW" marker on the exported PDF), so the simplified rendering is never confused with final print typography.
- STEP-1.1's text-integrity check (`compute_canonical_hash()`) is run against the content feeding this export path, exactly as it would be for the InDesign path — a review PDF is not exempt from the Zero-Mutation guarantee just because it isn't the final artifact.

#### 4. Preconditions

- **WeasyPrint's native dependencies (Pango, Cairo, HarfBuzz, GLib — commonly distributed together via the MSYS2/GTK runtime on Windows) must be explicitly bundled into the PyInstaller `--onedir` build** (STEP-4.1), with the application's startup path configured (e.g. via `os.add_dll_directory()`) to resolve these local binaries before the first WeasyPrint import — this is a required, non-trivial addition to STEP-4.1's packaging scope, not an assumption that `pip install weasyprint` alone makes the feature deployable on a clean Windows machine. This must be verified on a Windows test machine with no developer toolchain installed (mirroring AC-4.1.4's existing clean-machine testing discipline), not just on a development machine that happens to already have these libraries present.
- The chosen math-rendering library/approach for MathML→SVG/PNG conversion must be confirmed compatible with the MathML STEP-1.2 already produces (via `OMML2MML.XSL`) before implementation — this is a one-time compatibility verification, analogous to STEP-1.2's own XSLT-licensing precondition.
- The HTML template used for this export must apply explicit `dir="rtl"`/`dir="ltr"` per block according to `block_type`, mirroring STEP-2.2's direction rules — it must not rely on browser/renderer default direction inference.

#### 5. Acceptance Criteria

- **[AC-6.5.1]** Generating a quick review PDF never invokes `COMSupervisor`, `win32com`, or `pythoncom` — verified by the same import-boundary check used elsewhere in this phase.
- **[AC-6.5.2]** Every quick review PDF carries a visible marker distinguishing it from the final InDesign-generated output — verified by checking the marker is present on every page of the generated file.
- **[AC-6.5.3]** Quick review PDF generation succeeds using only the data already present in `DocumentStore` after classification — it does not require the `.idml` template or any InDesign-side style resource to be available.
- **[AC-6.5.4]** A block containing Arabic text with diacritics renders in the review PDF with correct glyph shaping and right-to-left character order — verified visually/structurally against a known-correct fixture, proving the WeasyPrint/Pango/HarfBuzz path (not raw PyMuPDF text placement) is actually what produced the output.
- **[AC-6.5.5]** A `math_formula` block's MathML content is actually rendered as a visible mathematical expression (not omitted, not shown as raw MathML markup, not blank) in the review PDF, oriented left-to-right regardless of surrounding Arabic content direction — verified against the corrected pipeline's math-rendering stage specifically, not assumed as a WeasyPrint built-in capability.
- **[AC-6.5.7]** A `legacy_equation_placeholder` block appears in the review PDF as a clearly visible labeled gap (e.g. "معادلة قديمة — تحتاج معالجة يدوية") at its original position — never omitted, never rendered as blank space or raw source text.
- **[AC-6.5.6]** The packaged `.exe` (STEP-4.1's `--onedir` build) successfully generates a review PDF on a clean Windows test machine with no Python or GTK/Pango development environment installed — verified as a distinct test from running the feature in a development environment, closing the gap where a developer's machine already having these native libraries present could mask a packaging failure.

---
