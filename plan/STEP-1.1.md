<!-- من الخطة الرئيسية Booklet_AI Desktop Master Plan -->

### [STEP-1.1] Architectural Foundation, Data Models, and Text Integrity Protocol

#### 1. Components & Tooling

| Component | Tool | Rationale |
|---|---|---|
| Desktop UI framework | PySide6 (Qt6) | Decouples heavy processing via `QThread`; native `.exe` distribution — no localhost server, no port conflicts, no Node.js dependency for end users (print shops). |
| State management | `AppStore` + `threading.RLock` | Two-tier separation: lightweight Qt Signals (metadata, progress) vs. heavy document store (full extracted content), avoiding full-object deep copies on every UI update. |
| Data contract | Pydantic v2 (`frozen=True`) | Immutable, type-safe schema shared across extraction → AI classification → InDesign injection layers. |
| Text integrity | `unicodedata` + `hashlib` (SHA-256) | Canonical NFC-normalized hashing to detect any content mutation; Fail-Fast policy — no silent fallback on corrupted text. |
| InDesign automation | `pywin32` COM, STA worker thread | Isolated `QThread` with proper `CoInitialize`/`CoUninitialize` lifecycle; Smart Text Reflow disabled explicitly; atomic insert-style-release pattern to prevent stale object references. |
| Packaging | PyInstaller (initial), Nuitka (future optimization pass) | PyInstaller chosen first for full PySide6/pywin32 compatibility and faster iteration during development. |
| Secrets | `keyring` | OpenRouter API keys stored via OS credential manager (Windows Credential Manager) — never plaintext JSON. |
| Template format | `.idml` | Broadest InDesign version compatibility (CS6 → 2026), with `.indt` export as secondary option. |

#### 2. Why Desktop over Local-Web

- InDesign's COM API is only reliably accessible from a process running on the same machine as InDesign itself — there is no practical remote/networked control path. This is the primary technical driver, not just UX preference.
- Single-binary distribution avoids requiring Node.js, managing port conflicts (8000/5173), or a persistent background server for non-technical print-shop users.
- Desktop process ownership enables direct lifecycle control over InDesign: detecting unexpected crashes, memory management, and guaranteed temp-file cleanup on app exit.
- Frontend rewrite cost (React → PySide6/QSS) is accepted as justified: the design system (colors, spacing, dark Adobe-style theme) is already defined and porting it to QSS is more stable than maintaining a hybrid web-shell architecture.

#### 3. COM Threading & InDesign Lifecycle Management

- **Dedicated Worker Thread:** All InDesign interaction happens exclusively inside a `COMWorker(QThread)` — never on the main UI thread, never called ad hoc across threads.
- **STA Lifecycle:** `pythoncom.CoInitialize()` called at the start of the worker's `run()`; `pythoncom.CoUninitialize()` called inside a `finally` block to guarantee COM object release.
- **No COM pointers across threads:** Live InDesign objects (`Document`, `Page`, etc.) are never passed between threads. UI↔Worker communication is strictly file paths, primitive values, and Qt Signals/JSON payloads.
- **Reflow Paradox — Corrected Approach:** An earlier draft of this plan disabled `smartTextReflow` globally to prevent stale object references. This was wrong: with Smart Text Reflow off, InDesign never creates new pages or links frames when text overflows — the story silently enters Overset Text state past the first page, and any booklet longer than one page fails silently. The actual fix is **manual story threading**, not disabling reflow. This required a second correction beyond the initial threading fix: master page items (the header/footer/body frames inherited from `Booklet_Master`, per STEP-2.1) are **locked master items**, not free page objects — `newPage.textFrames` is not directly usable, and even if it were, `.item(0)` would resolve to whichever frame was created first on the master (the **header** frame, per STEP-2.1's construction order), silently pouring the entire body story into the header ribbon. The fix requires (a) a stable `label` on the body frame set once during master construction, and (b) explicit `.override()` to release that specific master item into an editable page-level object:
  ```javascript
  // Set once, during setupMasterSpread() in STEP-2.1 — after creating the
  // primary body text frame on the master (distinct from header/footer):
  bodyMasterFrame.label = "Booklet_Body_Story";

  // CORRECTION (round 1): an earlier version of this function returned the
  // first frame matching the label anywhere on the master spread. On a
  // facing-pages master, the spread has two pages (left and right) with
  // different margins (inside vs outside spine offset per STEP-2.1) —
  // returning the first match meant every new page, including right-hand
  // pages, silently inherited the LEFT page's body frame geometry,
  // producing wrong margins on every alternating page. The fix requires
  // matching side explicitly.
  //
  // CORRECTION (round 2): the round-1 fix used a strict equality check
  // (mPage.side !== side), which broke every non-facing-pages document.
  // A single-sided master spread has exactly one master page whose .side
  // is PageSideOptions.SINGLE_SIDED — never LEFT_HAND or RIGHT_HAND. Since
  // ensureFrameCapacity() passes newPage.side (which is also SINGLE_SIDED
  // for a non-facing document) as the side argument, the equality check
  // itself is actually still correct for THIS specific call pattern
  // (SINGLE_SIDED === SINGLE_SIDED matches). The remaining defect was in
  // setupMasterSpread()'s margin-side logic (fixed above), which treated
  // "not LEFT_HAND" as "must be RIGHT_HAND" and so applied asymmetric
  // margins to single-sided pages. This function's side-matching itself
  // was correctly generalizable once SINGLE_SIDED is treated as its own
  // valid value rather than an unexpected case — no change needed here
  // beyond confirming the equality-based match works for all three
  // PageSideOptions values, including SINGLE_SIDED.
  function findMasterFrameByLabelOnSide(masterSpread, labelName, side) {
      for (var i = 0; i < masterSpread.pages.count(); i++) {
          var mPage = masterSpread.pages.item(i);
          if (mPage.side !== side) continue;
          var frames = mPage.textFrames;
          for (var f = 0; f < frames.count(); f++) {
              if (frames.item(f).label === labelName) return frames.item(f);
          }
      }
      throw new Error("MasterFrameNotFoundError: no frame labeled '" + labelName + "' on the " + side + " master page.");
  }

  var MAX_PAGES_SAFETY = 500; // guard against runaway loops on malformed input

  function ensureFrameCapacity(doc, currentFrame, masterName) {
      var guard = 0;
      while (currentFrame.overflows && guard < MAX_PAGES_SAFETY) {
          var newPage = doc.pages.add(LocationOptions.AFTER, currentFrame.parentPage);
          newPage.appliedMaster = doc.masterSpreads.itemByName(masterName);

          // Locate the labeled master body frame ON THE SAME SIDE as the
          // new page, then override it to get an editable, page-level
          // frame object — item(0) is unsafe both because master items
          // are locked and because ordinal position does not guarantee
          // "the body frame" (header/footer frames exist on the same
          // master); side-matching is required because left/right master
          // pages have different margins.
          var masterBodyFrame = findMasterFrameByLabelOnSide(
              newPage.appliedMaster, "Booklet_Body_Story", newPage.side
          );
          var newFrame = masterBodyFrame.override(newPage);

          currentFrame.nextTextFrame = newFrame;
          currentFrame = newFrame;
          guard++;
      }
      if (guard >= MAX_PAGES_SAFETY) {
          throw new Error("PageOverflowSafetyError: exceeded " + MAX_PAGES_SAFETY + " pages — likely malformed input or infinite overflow loop.");
      }
      return currentFrame;
  }
  ```
  `ensureFrameCapacity()` is called after every block insertion that could push the story past the current frame's bottom bound — measured via `.overflows`, not assumed. Smart Text Reflow itself is left at its InDesign default (irrelevant once threading is explicit and manual); no global preference toggle is required or safe to rely on. **This labeling requirement is a precondition on STEP-2.1's master construction** — `setupMasterSpread()` must create an explicit body text frame (distinct from the header/footer frames already specified there) on **both** the left and right master pages, and assign the `"Booklet_Body_Story"` label to each — this precondition was previously stated but not reflected in STEP-2.1's actual construction code, which only created header/footer frames; STEP-2.1 must be read as requiring this body frame explicitly, not implicitly.
- **Atomic Insertion Pattern (Zero Batching):** Object references (`TextFrame`, `Paragraph`) are never cached across loop iterations — each insertion invalidates prior character offsets. Mandatory sequence per content block:
  1. `var start = story.insertionPoints[-1].index;`
  2. `story.insertionPoints[-1].contents = blockText + "\r";`
  3. `var end = story.insertionPoints[-1].index;`
  4. `story.characters.itemByRange(start, Math.max(start, end - 1)).applyParagraphStyle(targetStyle, false);`
  5. Discard temp variables, move to next block.
  
  Style application happens **immediately** after each insertion — never batched and applied in a later loop.

- **Modal Dialog Deadlock Prevention:** A missing font, a corrupt `.idml`, or any InDesign condition that would normally raise a UI dialog will instead cause a **headless COM automation session to hang indefinitely** — there is no user present to click the dialog, and no exception is raised, so `COMWorker` blocks forever with no timeout and no error. This is prevented at two independent levels:
  1. **InDesign-side suppression**, set immediately after connecting to the application, before opening any document:
     ```javascript
     app.scriptPreferences.userInteractionLevel = UserInteractionLevels.NEVER_INTERACT;
     ```
     This forces InDesign to resolve any would-be dialog with its default/safe choice (e.g. substitute missing fonts) instead of blocking on user input. **This alone is not sufficient** — `NEVER_INTERACT` suppresses dialogs but does not guarantee every possible hang condition is covered, and silent font substitution is itself a correctness problem (see the font-verification precondition in STEP-2.3's preconditions, which must run *before* generation rather than relying on this fallback).
  2. **Process-level isolation, not a thread-level watchdog.** **Correction to an earlier draft of this plan:** that draft implemented the watchdog using `threading.Thread` to execute the COM call itself in a second Python thread, joined with a timeout from the main thread. This directly violates this same document's own STA rule ("All InDesign interaction happens exclusively inside a dedicated `COMWorker(QThread)`") — COM objects created and used from an uninitialized or different apartment thread are not safely callable, and doing so risks `RPC_E_WRONG_THREAD`-class failures or undefined marshaling behavior rather than a clean timeout. A thread cannot safely run the COM call *and* be a safe thing to abandon on timeout at the same time. The corrected architecture separates these concerns entirely:
     - All COM/InDesign work runs in a **dedicated child process** (via Python's `multiprocessing`), never a thread within the main application process. This process performs its own `CoInitialize()`/`CoUninitialize()` lifecycle (STA rules are per-process-and-thread, and a dedicated child process gives a clean boundary to kill).
     - **Strict IPC contract, not arbitrary `job_fn`:** an earlier draft passed an arbitrary `job_fn` plus `args`/`kwargs` directly into `multiprocessing.Process(target=..., args=(...))`. Under Windows' `spawn` start method (the default and only safe option on Windows), the target and every argument must be picklable — closures, lambdas, bound methods holding Qt/COM objects, or any live object graph referencing non-picklable state will fail at process startup with no useful error. The corrected contract restricts what may cross the process boundary to a **fixed, top-level-importable command name plus primitive/file-path arguments only** — never a function object:
       ```python
       # app/automation/job_protocol.py
       from typing import Literal
       from pydantic import BaseModel, ConfigDict

       class COMJobRequest(BaseModel):
           """
           The ONLY data structure that may cross the process boundary into
           the COM worker. No function objects, no Qt objects, no COM
           references — job_command is dispatched via a fixed registry
           inside the child process, never transmitted as code.
           """
           model_config = ConfigDict(frozen=True)
           job_command: Literal["generate_booklet", "add_filler_pages", "verify_indesign"]
           payload_file_path: str  # JSON payload, per the file-based IPC pattern below
       ```
       The child process's entrypoint dispatches `job_command` to a registered top-level function via a plain dict lookup — the command name travels across the boundary, not the function itself.
     - The main application process communicates with this child only via a lightweight IPC channel (a `COMJobRequest` as above, plus primitive status messages) — never COM object references, consistent with this document's existing "no COM pointers across threads" rule, now also enforced across the process boundary.
     - A supervisor in the main process polls a **heartbeat/progress timestamp** the child process updates periodically (not a shared COM call) — if the heartbeat goes stale past the timeout, the supervisor terminates the child process.
     - **InDesign process ownership and orphan cleanup — the critical gap in an earlier draft.** That draft terminated only the Python child process on timeout (`process.terminate()`) and assumed this was sufficient recovery. **It is not.** InDesign.exe is a separate out-of-process COM server — killing the Python client does not terminate it. If InDesign itself is what's hung (e.g. blocked on a dialog condition `NEVER_INTERACT` didn't cover), it remains running and blocked after the Python child dies. The next job's child process then calls `Dispatch("InDesign.Application")`, silently **attaches to the same hung instance**, and hangs identically — making the entire supervisor mechanism ineffective at the one thing it exists to solve. The corrected design tracks and explicitly terminates the InDesign process itself:
       ```python
       # app/automation/com_supervisor.py
       import multiprocessing
       import time
       import psutil  # for reliable PID-based process management

       class COMOperationTimeout(BookletError):
           """Raised when the COM worker process's heartbeat goes stale."""

       class COMSupervisor:
           def __init__(self, heartbeat_timeout_seconds: float = 30.0):
               self.heartbeat_timeout = heartbeat_timeout_seconds
               self.process: multiprocessing.Process | None = None
               self.heartbeat = multiprocessing.Value("d", 0.0)
               # Shared with the child: records the InDesign.exe PID if this
               # job launched a new instance (vs. attaching to one already
               # running before the job started). 0 = not recorded / attached
               # to a pre-existing instance we do not own.
               self.indesign_pid = multiprocessing.Value("i", 0)

           def run_job(self, job_request: "COMJobRequest"):
               self.heartbeat.value = time.time()
               self.indesign_pid.value = 0
               self.process = multiprocessing.Process(
                   target=_com_worker_entrypoint,
                   args=(self.heartbeat, self.indesign_pid, job_request),
                   daemon=True,
               )
               self.process.start()

               while self.process.is_alive():
                   if time.time() - self.heartbeat.value > self.heartbeat_timeout:
                       self._terminate_and_cleanup()
                       raise COMOperationTimeout(
                           f"COM worker process unresponsive for {self.heartbeat_timeout}s "
                           f"— likely a blocked modal dialog or unresponsive InDesign instance. "
                           f"Worker and owned InDesign process terminated."
                       )
                   time.sleep(1.0)
               self.process.join()

           def _terminate_and_cleanup(self) -> None:
               """
               Two-stage cleanup: kill the Python child, THEN separately
               verify and kill the InDesign process it owned — killing the
               Python process alone is not recovery, per the failure mode
               described above.
               """
               if self.process is not None:
                   self.process.terminate()
                   self.process.join(timeout=5.0)

               owned_pid = self.indesign_pid.value
               if owned_pid != 0:
                   try:
                       indesign_proc = psutil.Process(owned_pid)
                       if indesign_proc.name().lower().startswith("indesign"):
                           indesign_proc.kill()  # OS-level TerminateProcess equivalent
                           indesign_proc.wait(timeout=10.0)
                   except psutil.NoSuchProcess:
                       pass  # already gone — fine
               # If indesign_pid.value was 0, this job attached to an
               # InDesign instance that was already running before the job
               # started — that instance is NOT automatically killed, since
               # it may not have been owned/launched by this application at
               # all (e.g. a user's own InDesign session). This is
               # surfaced to the user as an unrecoverable-state warning
               # requiring manual verification, never auto-killed blind.

       def _com_worker_entrypoint(heartbeat, indesign_pid, job_request: "COMJobRequest"):
           """
           Runs entirely inside the child process — its own CoInitialize/
           CoUninitialize lifecycle. Dispatches job_request.job_command via
           a fixed top-level registry (never a transmitted function object).
           Before connecting to InDesign, checks whether an instance is
           already running (pre-existing, not owned by us) versus needing
           to be launched (owned by this job, PID recorded for cleanup).
           """
           import pythoncom
           import win32com.client
           import psutil

           pythoncom.CoInitialize()
           try:
               heartbeat.value = time.time()

               pre_existing = any(
                   p.name().lower().startswith("indesign") for p in psutil.process_iter(["name"])
               )
               app = win32com.client.Dispatch("InDesign.Application")
               if not pre_existing:
                   # We are responsible for having launched this instance —
                   # record its PID so the supervisor can clean it up on timeout.
                   for p in psutil.process_iter(["name", "pid"]):
                       if p.info["name"].lower().startswith("indesign"):
                           indesign_pid.value = p.info["pid"]
                           break

               JOB_REGISTRY[job_request.job_command](app, job_request.payload_file_path, heartbeat)
           finally:
               pythoncom.CoUninitialize()

       # Fixed, top-level registry — job_command (a string) selects the
       # function; no function object ever crosses the process boundary.
       JOB_REGISTRY = {
           "generate_booklet": _run_generate_booklet,
           "add_filler_pages": _run_add_filler_pages,
           "verify_indesign": _run_verify_indesign,
       }
       ```
       `psutil` is added as a required dependency specifically for reliable cross-platform process inspection/termination (PID lookup by name, verified kill). If an InDesign instance was already running before the job started (i.e. not launched by this application), the supervisor does **not** kill it automatically on timeout — doing so could terminate a user's unrelated InDesign session — instead it surfaces an explicit "unrecoverable state, manual check required" warning to the user.
     - **Windows frozen-executable requirement:** since this application is packaged via PyInstaller (STEP-4.1), the `multiprocessing` entrypoint must include `multiprocessing.freeze_support()` guarded by `if __name__ == "__main__":` in the main entry script — required for `spawn`-based child processes to work correctly from a frozen `.exe`, per Python's own documented Windows/frozen-executable constraints.
     - After termination and cleanup, the supervisor launches a **fresh child process** for the next job. The previous job is reported to the user as failed (with the "InDesign unresponsive" message), never silently retried in the same process.
- **Safe Cross-Language Payload Passing (replacing raw `eval()`):** ExtendScript's `eval()` on a hand-serialized JSON string is fragile — escape-character handling for embedded quotes, backslashes, and newlines in Arabic text is error-prone, and large payloads (60-page booklets with tables/MathML) risk interpreter memory pressure. **Correction to an earlier draft of this plan:** that draft assumed ExtendScript has a native `JSON` object available and could call `JSON.parse()` directly — this is incorrect. ExtendScript runs on an ES3-era engine (used from CS6 through current 2025/2026 releases) with no built-in `JSON` global; calling `JSON.parse()` without first providing one throws `'JSON' is undefined` and halts the script immediately. The fix requires bundling the standard `json2.jsx` polyfill (Douglas Crockford's reference JSON implementation for ES3 environments) as an asset and including it explicitly before any JSON call:
  ```javascript
  #include "json2.jsx"

  var payloadFile = new File(payloadFilePath);
  payloadFile.open("r");
  payloadFile.encoding = "UTF-8";
  var rawJSON = payloadFile.read();
  payloadFile.close();
  var documentPayload = JSON.parse(rawJSON); // now safe — JSON provided by json2.jsx, never eval()
  ```
  `json2.jsx` must ship in `assets/scripts/lib/json2.jsx`, referenced via a path resolved relative to the executing script's own location (`$.fileName`), not a bare relative path — `#include` in ExtendScript does not reliably resolve arbitrary relative paths independent of the including script's directory. This must be treated as a mandatory dependency of every JSX entry point that receives a JSON payload, not an optional include. **Every worker function that receives structured data (`injectRTLTable`, `resolveAndApplyStyle`, etc.) must accept a payload file path and perform this read-and-parse itself** — passing nested structures (table data, style maps) directly as COM method arguments bypasses this safeguard entirely and is not a supported pattern in this architecture.
  This sidesteps manual escape-character handling entirely (the JSON parser handles it) and keeps large payloads out of the ExtendScript call-stack/argument-passing path.

#### 4. Two-Tier State Separation

- **Lightweight signals:** Qt Signals carry only primitives/metadata — `progress_updated(step: int, percent: float)`, `document_loaded(page_count: int, file_hash: str)`, `error_occurred(msg: str)`. Full document objects are never emitted via signals.
- **Heavy central store:** `DocumentStore`, protected by `threading.RLock` (or `QReadWriteLock`), holds the full extracted content tree (blocks, tables, pages). Widgets read on-demand slices — `store.get_page_slice(page_num)` — never a full deep copy on every update.

#### 5. Declarative Definitions

```python
# app/core/models.py
from typing import Literal, Dict, Any, List, Optional
from pydantic import BaseModel, ConfigDict, Field

BlockType = Literal[
    "chapter_header",
    "arabic_text",
    "english_text",
    "ministry_question",
    "math_formula",
    "chemistry_formula",
    "image_prompt",
    "list_item",
    "legacy_equation_placeholder"  # detected OLE/MathType equation, not converted — see STEP-1.2
]

class ContentBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    block_id: str
    block_type: BlockType
    raw_content: str
    content_hash: str
    source_position: Dict[str, Any] = Field(default_factory=dict)

class TableData(BaseModel):
    model_config = ConfigDict(frozen=True)

    table_id: str
    headers: List[str]
    rows: List[List[str]]
    source_bbox: Optional[List[float]] = None

class PageData(BaseModel):
    model_config = ConfigDict(frozen=True)

    page_number: int
    width: float
    height: float
    header_text: str = ""
    footer_text: str = ""
    content_blocks: List[ContentBlock] = Field(default_factory=list)
    tables: List[TableData] = Field(default_factory=list)

class BookletDocument(BaseModel):
    model_config = ConfigDict(frozen=True)

    doc_id: str
    file_path: str
    file_type: Literal["pdf", "docx"]
    pages: List[PageData] = Field(default_factory=list)
```

```python
# app/core/exceptions.py
class BookletError(Exception):
    """Base exception for Booklet_AI core."""

class TextIntegrityError(BookletError):
    """Raised when canonical text hash verification fails."""

class MathExtractionError(BookletError):
    """Raised when OMML/MathML/LaTeX conversion or XSLT transformation fails."""
```

```python
# app/core/text_integrity.py
import re
import unicodedata
import hashlib

# Fully protected ranges: tashkeel, tanween, superscript alef, hamzas/maddah
# \u064B-\u0652: Fathatan, Dammatan, Kasratan, Fatha, Damma, Kasra, Shadda, Sukun
# \u0670: Superscript Alef (الألف الخنجرية)
# \u0653-\u0655: Maddah, Hamza Above, Hamza Below
PROTECTED_DIACRITICS_REGEX = re.compile(r"[\u064B-\u0652\u0670\u0653-\u0655]")

# Invisible artifacts allowed to be stripped (control chars, zero-width, BOM)
STRIP_CONTROL_CHARS_REGEX = re.compile(
    r"[\u200E\u200F\u202A-\u202E\uFEFF\u200B]"
)

def normalize_presentation_forms(text: str) -> str:
    """
    Critical PDF-extraction safeguard: some PDFs (produced by tools not
    fully Unicode-aware for Arabic) store text using Arabic Presentation
    Forms glyphs (\\uFB50-\\uFDFF, \\uFE70-\\uFEFF) — visually-shaped,
    sometimes visually-reversed glyph codepoints — instead of logical-order
    base Arabic (\\u0600-\\u06FF). Without this step, compute_canonical_hash()
    would hash the corrupted Presentation Forms text successfully and pass
    Fail-Fast integrity checks on text that is already wrong before it ever
    reaches InDesign. NFKC decomposition maps compatibility characters
    (which includes most Presentation Forms) to their canonical base form,
    which is why NFKC — not NFC — is required here specifically for this
    normalization pass, ahead of the existing NFC hashing.
    """
    return unicodedata.normalize("NFKC", text)

def compute_canonical_hash(text: str) -> str:
    """
    Applies Presentation Forms normalization first (see
    normalize_presentation_forms), then NFC normalization, and strips only
    invisible/control characters, while strictly preserving diacritics,
    digits, punctuation, and original characters. Line breaks (\\r\\n, \\r)
    are normalized to \\n; leading/trailing whitespace per line is stripped,
    but internal \\n line breaks are preserved (cross-platform hash stability).
    """
    depresented = normalize_presentation_forms(text)
    normalized = unicodedata.normalize("NFC", depresented)
    cleaned = STRIP_CONTROL_CHARS_REGEX.sub("", normalized)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = "\n".join(line.strip() for line in cleaned.split("\n"))
    cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
```

**Important scope note:** NFKC normalization fixes *glyph-shape* corruption (Presentation Forms → logical base characters) but does **not** fix *visual character-order reversal*, which is a separate, extraction-layer problem handled in STEP-1.3's PDF spatial-ordering fix, not here. This function only guarantees that the hash is computed on logically-correct Unicode — it is not a substitute for correct reading-order extraction.

#### 6. Fail-Fast Text Integrity Policy

Given the material and educational cost of a misprinted booklet, the system applies a **hard halt** on any integrity mismatch — no silent fallback, no partial continuation.

- If the SHA-256 hash of the canonicalized source text does not match the canonicalized text about to be injected, the system immediately raises `TextIntegrityError`.
- Generation halts immediately; the UI surfaces a red audit dialog showing: page number, the corrupted block's text, and a character-level diff between source and extracted text — allowing manual correction before wasting paper/time.

#### 6a. User-Authorized Mutations (Editability Escape Hatch)

**Architectural gap this closes:** `frozen=True` plus strict hash comparison, as originally specified, cannot distinguish between *unauthorized corruption* (a bug, an encoding failure, AI accidentally rewriting text) and a *deliberate correction the user typed themselves* in the preview UI (fixing a typo, merging two blocks). Without an explicit mutation path, any legitimate user edit would trigger `TextIntegrityError` and halt generation — which is not the intended behavior of Fail-Fast; Fail-Fast exists to catch *unattributed* change, not to forbid all change.

- **Resolution:** a `ContentBlock` may only be mutated through a dedicated method, never by constructing a new block with edited `raw_content` directly:
  ```python
  # app/core/models.py (addition)
  from datetime import datetime, timezone

  class ContentBlock(BaseModel):
      model_config = ConfigDict(frozen=True)

      block_id: str
      block_type: BlockType
      raw_content: str
      content_hash: str
      source_position: Dict[str, Any] = Field(default_factory=dict)
      user_edited: bool = False
      edit_timestamp: Optional[str] = None

      def with_user_edit(self, new_content: str) -> "ContentBlock":
          """
          The ONLY sanctioned path to change raw_content after extraction.
          Recomputes content_hash from new_content and marks user_edited=True,
          so the new hash is trusted going forward — it is not compared
          against the original extraction hash. TextIntegrityError is
          reserved for hash mismatches on blocks where user_edited is False;
          once user-edited, the block's new hash IS the new source of truth.
          """
          from app.core.text_integrity import compute_canonical_hash
          return self.model_copy(update={
              "raw_content": new_content,
              "content_hash": compute_canonical_hash(new_content),
              "user_edited": True,
              "edit_timestamp": datetime.now(timezone.utc).isoformat(),
          })
  ```
- **Audit trail, not silent trust:** `user_edited=True` blocks are visually flagged in the UI preview (e.g. a small pencil icon) so the designer reviewing the final layout can see which blocks deviate from the original source — the system trusts the edit but never hides that it happened.
- **Scope boundary:** `with_user_edit()` is only reachable from the UI's manual preview/edit screen, never called by the AI classification layer (STEP-1.4) or any extraction code — those layers remain strictly read-only with respect to `raw_content`, preserving the Zero-Mutation guarantee for anything the user did not explicitly touch.

#### 7. Project Structure

```
Booklet_AI_Desktop/
├── app/
│   ├── ui/                 # MainWindow, custom widgets, theme.qss
│   ├── state/               # AppStore (Pydantic models + RLock)
│   ├── core/                 # PDF/DOCX parsers, models.py, text_integrity.py, exceptions.py
│   ├── ai/                   # OpenRouter client, JSON schema validation
│   ├── automation/           # COMWorker(QThread), InDesignBridge, JSX builders
│   └── utils/                 # keyring-based secrets, logging, temp file management
├── assets/                    # Icons, fonts, .idml templates, xslt/OMML2MML.XSL
├── configs/                    # Print/measurement settings (non-secret)
└── requirements.txt
```

`requirements.txt`: `PySide6, pywin32, PyMuPDF, python-docx, lxml, pydantic>=2.0, keyring, httpx`

#### 8. Acceptance Criteria

- **[AC-1.1.1]** A fully-diacritized Arabic text (e.g. "بِسْمِ اللَّهِ") produces an identical hash regardless of whether it is input as Decomposed (NFD) or Composed (NFC).
- **[AC-1.1.2]** Text containing invisible characters (`\u200B`, `\uFEFF`, `\u200E`) is fully cleaned without affecting diacritics or adjacent characters.
- **[AC-1.1.3]** Any *unauthorized* change to a single character, diacritic, or punctuation mark (i.e. one not made through `with_user_edit()`) raises `TextIntegrityError` immediately, halting the generation cycle (Fail-Fast).
- **[AC-1.1.4]** Qt Signal updates complete in under 2ms with constant memory usage — full Pydantic document objects are never passed through signals.
- **[AC-1.1.5]** Text containing Arabic Presentation Forms glyphs (`\uFB50-\uFDFF`, `\uFE70-\uFEFF`) is normalized via NFKC to logical base Arabic characters (`\u0600-\u06FF`) before hashing — a block extracted with Presentation Forms glyphs and one extracted with correctly logical-order Arabic containing the same semantic text produce the same hash.
- **[AC-1.1.6]** Calling `with_user_edit()` on a block produces a new block with `user_edited=True`, a hash matching the *new* content (not the original), and does not raise `TextIntegrityError` even though `raw_content` differs from the extraction-time value.
- **[AC-1.1.7]** `with_user_edit()` is unreachable from `app/ai/` and `app/core/*_extractor.py` modules — verified by import-boundary lint rule, not just code review.
- **[AC-1.1.8]** A generation job against a document referencing a missing font does not hang the application indefinitely, AND after a timeout, no orphaned `InDesign.exe` process that this job launched remains running — verified by checking process state after `_terminate_and_cleanup()`, not merely that the Python process exited.
- **[AC-1.1.9]** A payload containing embedded quotes, backslashes, and Arabic text with mixed diacritics round-trips correctly through the JSON file write (Python) → `JSON.parse()` read (JSX, via the `json2.jsx` polyfill) path with zero character corruption, verified against `compute_canonical_hash()` on both sides.
- **[AC-1.1.10]** All COM object creation and use occurs exclusively within the `COMSupervisor`'s child process — verified by confirming no `win32com`/`pythoncom` calls exist outside `com_supervisor.py`'s child-process entrypoint anywhere in the codebase (the main process and its threads never call COM APIs directly).
- **[AC-1.1.11]** A `COMJobRequest` is fully picklable and constructed only from primitive fields (`job_command` string literal, `payload_file_path` string) — verified by round-tripping it through Python's `pickle` module in a test, catching any future addition of a non-picklable field before it reaches a real Windows `spawn` failure.

---
