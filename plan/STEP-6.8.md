<!-- من الخطة الرئيسية Booklet_AI Desktop Master Plan -->

### [STEP-6.8] Optional Page Numbering Style

#### 1. Rationale

Different Iraqi curricula/schools may expect Arabic-Indic numerals (١٢٣) rather than Western Arabic numerals (123) for printed page numbers.

**Post-review correction — confirmed API assumption error:** the original draft assumed InDesign's "page-number numbering-style property" alone would control digit *shape* (١٢٣ vs. 123). Adobe's InDesign object model distinguishes numbering **style** (the counting/section-numbering scheme itself) from **digit rendering** (which script's glyphs are drawn for the digits) — these are governed by different properties. The corrected spec below names the actual digit-shaping property explicitly rather than conflating the two.

#### 2. Architectural Placement

- This is a configuration value read at the same point STEP-2.1's `SpecialCharacters.AUTO_PAGE_NUMBER` is applied to the footer master frame. The corrected implementation sets the **digit-shaping property** (InDesign's Arabic/Hindi/Farsi digit-display mode, exposed on the relevant text/numbering preferences object — verified against the specific InDesign scripting API version this project targets before implementation, since exact property naming can shift across InDesign releases) **in addition to**, not instead of, leaving `SpecialCharacters.AUTO_PAGE_NUMBER`'s own automatic-numbering mechanism (STEP-2.1, AC-2.1.2) completely unchanged. Digit shape is a *display* concern layered on top of the existing auto-numbering marker — it must never be implemented as hand-generated digit text replacing the marker, which would break AC-2.1.2's automatic-update guarantee.
- Stored as a per-document setting (part of the same configuration payload that carries `MarginConfig`/`PageGeometry` from STEP-2.1 to the JSX layer), not a global application setting — different documents in the same batch (STEP-6.7) may reasonably use different numbering styles.

#### 3. Preconditions

- Before implementation, the exact InDesign scripting property controlling digit-shaping (as opposed to numbering style/scheme) must be confirmed against the specific InDesign version(s) this project targets — this is a one-time verification task analogous to STEP-1.2's OMML2MML.XSL licensing-verification precondition, not an architectural blocker, but it must be resolved before STEP-6.8 code is written, not discovered during implementation.
- The digit-shaping setting must only ever affect generated `AUTO_PAGE_NUMBER` markers — it must never be applied globally in a way that could alter digit rendering within ordinary source text, question numbers, dates, or mathematical content, all of which fall under STEP-1.1's Zero-Mutation guarantee and must never have their digit glyphs silently substituted.

#### 4. Acceptance Criteria

- **[AC-6.8.1]** Selecting Arabic-Indic numbering produces page numbers rendered as ١٢٣-style digits in the generated document; selecting Western Arabic numbering produces 123-style digits — both via InDesign's native digit-shaping property applied to the existing `AUTO_PAGE_NUMBER` marker, not via hand-inserted digit text and not via a numbering-*style*/section-scheme property being mistaken for a digit-shape property.
- **[AC-6.8.2]** Changing this setting does not alter or disable `SpecialCharacters.AUTO_PAGE_NUMBER`'s existing automatic-update behavior (AC-2.1.2) — page numbers still update correctly as pages are added via `ensureFrameCapacity()` (STEP-1.1) or `addBlankFillerPages()` (STEP-2.1), regardless of which numbering style is selected.
- **[AC-6.8.3]** Two documents processed in the same batch (STEP-6.7) with different numbering-style settings each produce output matching their own setting — verified this is a per-document, not per-application-session, value.
- **[AC-6.8.4]** Selecting Arabic-Indic digit rendering never changes the digit glyphs of any ordinary source-text content, question numbers, or dates elsewhere in the document — verified by confirming `document_content_hash` (STEP-6.3) for the non-page-number content is unaffected by this setting.

---
