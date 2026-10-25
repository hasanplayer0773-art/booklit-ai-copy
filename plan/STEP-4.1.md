<!-- من الخطة الرئيسية Booklet_AI Desktop Master Plan -->

### [STEP-4.1] PyInstaller Build & Update Pipeline

#### 1. Components & Tooling

| Component | Tool | Rationale |
|---|---|---|
| Bundler | PyInstaller (`--onedir`, not `--onefile`) | `--onedir` avoids the multi-second temp-extraction delay of `--onefile` on every launch and simplifies partial-file updates. |
| Update Check | `httpx` GET against a static version manifest (JSON) | Lightweight polling, no dedicated update server needed initially — a static JSON file on any CDN/host is sufficient. |
| Update Delivery | Delta-free full-package replacement, staged in temp dir then swapped on relaunch | Simpler and more reliable than binary diffing for a Qt/PySide6 app; avoids partial-update corruption risk given this app's InDesign-adjacent reliability requirements. |
| Code Signing | Windows Authenticode (`signtool`) | Unsigned `.exe` triggers SmartScreen warnings that will alarm non-technical print-shop users — signing is a distribution requirement, not optional polish. |

#### 2. Technical Notes

- **`--onedir` over `--onefile` is a deliberate reliability choice**, not just a performance one: `--onefile` extracts to a temp directory on every launch, and if that extraction is interrupted (e.g. antivirus quarantine on a print-shop machine, common with unsigned/unfamiliar executables), the app can fail to start with an opaque error. `--onedir` avoids repeated extraction entirely once installed.
- **Version manifest schema is intentionally minimal** — `{"version": "1.2.0", "download_url": "...", "sha256": "...", "release_notes_url": "..."}`. The `sha256` field is mandatory: the updater verifies the downloaded package hash before staging it, applying the same "never trust unverified content" posture as STEP-1.1's text integrity hash, now applied to the update artifact itself.
- **Update failures never block app usage:** a failed version check or failed download simply skips the update prompt silently on that launch and retries next time — this is explicitly *not* Fail-Fast, since blocking a print shop from generating booklets because an update server is unreachable would be worse than running one version behind.
- **In-app InDesign/COM dependency is NOT bundled:** the installer must document Adobe InDesign as an external prerequisite (existing licensed install on the user's machine) — PyInstaller packages the Python/Qt application only, and `pywin32`'s COM bridge connects to whatever InDesign version is already installed. A startup precondition check (STEP-2.2 pattern) verifies InDesign is reachable via COM before allowing generation, rather than failing deep into a job.

#### 3. Declarative Definitions

```python
# app/utils/updater.py
import hashlib
import httpx
from typing import Optional
from pydantic import BaseModel, ConfigDict

class VersionManifest(BaseModel):
    model_config = ConfigDict(frozen=True)
    version: str
    download_url: str
    sha256: str
    release_notes_url: Optional[str] = None

class UpdateChecker:
    """
    Non-blocking update checker. Any failure (network, malformed manifest,
    hash mismatch) is logged and silently skipped — update availability
    is never a precondition for using the app.
    """
    def __init__(self, manifest_url: str, current_version: str):
        self.manifest_url = manifest_url
        self.current_version = current_version

    def check(self) -> Optional[VersionManifest]:
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(self.manifest_url)
                resp.raise_for_status()
                manifest = VersionManifest.model_validate(resp.json())
            if manifest.version != self.current_version:
                return manifest
            return None
        except Exception:
            return None  # silent skip — see Technical Notes

    def verify_download(self, file_bytes: bytes, expected_sha256: str) -> bool:
        actual = hashlib.sha256(file_bytes).hexdigest()
        return actual == expected_sha256
```

```python
# app/automation/indesign_precondition.py
from app.core.exceptions import BookletError

class InDesignUnavailableError(BookletError):
    """Raised when InDesign cannot be reached via COM at startup/pre-generation check."""

def verify_indesign_available() -> None:
    """
    Fail-Fast precondition check, run once before any generation job starts —
    not discovered mid-job. Attempts a lightweight COM handshake.
    """
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        app = win32com.client.Dispatch("InDesign.Application")
        _ = app.Name  # forces a live round-trip, not just object creation
    except Exception as exc:
        raise InDesignUnavailableError(
            "Adobe InDesign is not installed or not reachable via COM. "
            "Booklet_AI requires a licensed InDesign installation on this machine."
        ) from exc
    finally:
        pythoncom.CoUninitialize()
```

#### 4. Preconditions

- Code-signing certificate must be provisioned before any distribution build — unsigned builds are development-only.
- `verify_indesign_available()` must run at application startup, before the main window accepts a generation job.

#### 5. Acceptance Criteria

- **[AC-4.1.1]** A downloaded update package whose computed SHA-256 does not match the manifest's `sha256` field is discarded and never staged for install.
- **[AC-4.1.2]** Update-check failures (network, malformed manifest) never raise to the UI as errors and never block normal app usage.
- **[AC-4.1.3]** `verify_indesign_available()` raises `InDesignUnavailableError` at startup if InDesign is not reachable, before the user can begin a document job — never failing mid-generation for this reason.
- **[AC-4.1.4]** The packaged `--onedir` build launches without triggering antivirus quarantine on a clean Windows test machine when code-signed.

---

## PHASE 4 STATUS: CLOSED (STEP-4.1)

---

## PHASE 5: Print-Shop Cost & Margin Tooling
