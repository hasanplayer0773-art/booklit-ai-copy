<!-- من الخطة الرئيسية Booklet_AI Desktop Master Plan -->

### [STEP-6.6] Bilingual UI (Arabic/English)

#### 1. Rationale

The application's error messages (STEP-6.2), Preflight messages (STEP-6.4), and general UI must be usable by operators who are more comfortable in Arabic, English, or need to switch based on context (e.g. a technical support conversation in English about a report generated in Arabic).

**Post-review correction:** the original draft specified only string externalization, without addressing two confirmed gaps: (1) swapping translated strings does not by itself change Qt's layout direction — an Arabic UI with LTR-oriented widget geometry is a real, distinct defect from wrong text; (2) no concrete translation mechanism was named, leaving it ambiguous whether this is a custom dictionary or Qt's own facility, which matters for PySide6 specifically.

#### 2. Architectural Placement

- UI strings are externalized via **Qt's built-in translation system (`QTranslator` + `.ts`/`.qm` files)**, the native mechanism for a PySide6 application — not a custom JSON dictionary reimplementing what Qt already provides. Static labels use `self.tr(...)` per Qt convention; the `.ts` source files are the single place translators edit text, compiled to `.qm` for runtime loading.
- **STEP-6.2's `ERROR_TRANSLATIONS` Arabic entries become the Arabic-locale `.ts` entries in this same mechanism** — not a separate parallel table. English equivalents are added as the English-locale entries for the same translation keys, so both are guaranteed to exist together (AC-6.6.2) rather than drifting independently.
- **Selected language drives both string lookup AND Qt's layout direction** — `QApplication.setLayoutDirection(Qt.RightToLeft)` for Arabic, `Qt.LeftToRight` for English, applied at the same point the language setting changes, not left to per-widget default inference. This is distinct from, and must not be confused with, document-content BiDi handling (STEP-2.2) — UI layout direction and document text direction are governed by entirely separate mechanisms and settings, and a change to one must never affect the other.
- Language selection is a persisted application-level setting (via the same `AppStore` mechanism already used for the AI model configuration in STEP-6.1), not a per-session or per-document choice.

#### 3. Acceptance Criteria

- **[AC-6.6.1]** Switching the UI language setting updates all static labels and any currently-displayed translated error message without requiring an application restart.
- **[AC-6.6.1b]** Switching the UI language setting to Arabic sets `QApplication.layoutDirection()` to `Qt.RightToLeft`, and to English sets it to `Qt.LeftToRight` — verified directly, not inferred from string content alone.
- **[AC-6.6.1c]** Changing the UI language setting never alters `document_content_hash` (STEP-6.3) or any `ContentBlock.raw_content` for the currently-open document — verified by confirming UI locale state and document-content state are structurally independent code paths.
- **[AC-6.6.2]** STEP-6.2's `ERROR_TRANSLATIONS` Arabic entries and their English equivalents are both present in the same `.ts`/`.qm` resource mechanism — verified by confirming no exception type's translation key has an Arabic entry without a corresponding English entry or vice versa.
- **[AC-6.6.3]** The selected UI language is persisted across application restarts.

---
