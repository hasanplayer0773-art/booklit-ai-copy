<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE TS>
<TS version="2.1" language="en_US">
<context>
    <name>ErrorTranslation</name>
    <message>
        <source>TextIntegrityError</source>
        <translation>⚠️ Unauthorized modification detected in extracted text — hash mismatch. Operation halted to protect content integrity.</translation>
    </message>
    <message>
        <source>MathExtractionError</source>
        <translation>⚠️ Failed to extract mathematical formulas from file — verify formula syntax in the original file.</translation>
    </message>
    <message>
        <source>COMOperationTimeout</source>
        <translation>⚠️ InDesign did not respond within timeout — a dialog box may be pending. Close InDesign and retry.</translation>
    </message>
    <message>
        <source>InDesignUnavailableError</source>
        <translation>⚠️ Cannot connect to InDesign — ensure it is installed and licensed on this computer, then retry.</translation>
    </message>
    <message>
        <source>InvalidBodyFrameGeometryError</source>
        <translation>⚠️ Invalid text frame dimensions — margin values overlap or produce negative space. Adjust margins.</translation>
    </message>
    <message>
        <source>ContentExceedsEstimateError</source>
        <translation>⚠️ Actual content exceeds initial page estimate — cost recalculation required.</translation>
    </message>
    <message>
        <source>MarginConfigError</source>
        <translation>⚠️ Invalid margin configuration — check margin values and ensure they are within allowed range.</translation>
    </message>
    <message>
        <source>StyleMappingError</source>
        <translation>⚠️ Required paragraph style not found in InDesign template — verify correct template is installed.</translation>
    </message>
    <message>
        <source>RTLTableUnsupportedError</source>
        <translation>⚠️ Installed InDesign version does not support Right-To-Left (RTL) tables — Middle East or World Ready edition required.</translation>
    </message>
    <message>
        <source>QuestionIndexError</source>
        <translation>⚠️ Database question indexing error — please retry.</translation>
    </message>
    <message>
        <source>BookletError</source>
        <translation>⚠️ An error occurred during booklet processing — please send a problem report for assistance.</translation>
    </message>
    <message>
        <source>PDFExtractionError</source>
        <translation>⚠️ Failed to extract content from PDF file — verify file is not corrupted or password-protected.</translation>
    </message>
    <message>
        <source>InvalidClassificationResponse</source>
        <translation>⚠️ Automatic content classification failed — please retry or check model settings.</translation>
    </message>
    <message>
        <source>EmbeddingError</source>
        <translation>⚠️ Failed to generate embedding vector — check internet connection and API settings.</translation>
    </message>
    <message>
        <source>PreflightBlockerError</source>
        <translation>⚠️ Unresolved preflight blockers prevent export — review preflight panel or record an explicit override.</translation>
    </message>
    <message>
        <source>FALLBACK_MESSAGE</source>
        <translation>⚠️ An unexpected error occurred — please send a problem report.</translation>
    </message>
</context>
<context>
    <name>AISettingsWidget</name>
    <message>
        <source>OpenRouter API Key</source>
        <translation>OpenRouter API Key</translation>
    </message>
    <message>
        <source>Show/hide API key</source>
        <translation>Show/hide API key</translation>
    </message>
    <message>
        <source>Save Key</source>
        <translation>Save Key</translation>
    </message>
    <message>
        <source>Model Name (Model)</source>
        <translation>Model Name (Model)</translation>
    </message>
    <message>
        <source>Save Model</source>
        <translation>Save Model</translation>
    </message>
    <message>
        <source>Test Connection</source>
        <translation>Test Connection</translation>
    </message>
    <message>
        <source>🔌 Test Connection</source>
        <translation>🔌 Test Connection</translation>
    </message>
    <message>
        <source>Interface Language</source>
        <translation>Interface Language</translation>
    </message>
    <message>
        <source>Arabic</source>
        <translation>Arabic</translation>
    </message>
    <message>
        <source>English</source>
        <translation>English</translation>
    </message>
    <message>
        <source>Click to test connection with specified model.</source>
        <translation>Click to test connection with specified model.</translation>
    </message>
    <message>
        <source>Testing connection...</source>
        <translation>Testing connection...</translation>
    </message>
    <message>
        <source>⏳ Testing...</source>
        <translation>⏳ Testing...</translation>
    </message>
    <message>
        <source>Model name is required — cannot be empty.</source>
        <translation>Model name is required — cannot be empty.</translation>
    </message>
    <message>
        <source>Invalid format — must be in the form &apos;provider/model&apos; (example: google/gemini-2.0-flash-001).</source>
        <translation>Invalid format — must be in the form &apos;provider/model&apos; (example: google/gemini-2.0-flash-001).</translation>
    </message>
    <message>
        <source>Enter API key first.</source>
        <translation>Enter API key first.</translation>
    </message>
    <message>
        <source>API key saved successfully.</source>
        <translation>API key saved successfully.</translation>
    </message>
    <message>
        <source>API key saved in Windows Credential Manager.</source>
        <translation>✓ API key saved in Windows Credential Manager.</translation>
    </message>
    <message>
        <source>No API key saved.</source>
        <translation>⚠ No API key saved.</translation>
    </message>
    <message>
        <source>Model saved:</source>
        <translation>✓ Model saved:</translation>
    </message>
</context>
<context>
    <name>PreflightPanel</name>
    <message>
        <source>Preflight Panel</source>
        <translation>Preflight Panel</translation>
    </message>
    <message>
        <source>Pre-COM Checks</source>
        <translation>Pre-COM Checks</translation>
    </message>
    <message>
        <source>Post-Generation Checks</source>
        <translation>Post-Generation Checks</translation>
    </message>
    <message>
        <source>Record Blocker Override</source>
        <translation>Record Blocker Override</translation>
    </message>
    <message>
        <source>No preflight issues detected.</source>
        <translation>No preflight issues detected.</translation>
    </message>
</context>
<context>
    <name>PreflightStatus</name>
    <message>
        <source>status_blockers</source>
        <translation>Blockers prevent proceeding — manual equation review or explicit override required.</translation>
    </message>
    <message>
        <source>status_override</source>
        <translation>Explicit override recorded for initial blockers — ready to start generation (final check pending).</translation>
    </message>
    <message>
        <source>status_tier1_clean</source>
        <translation>No known blockers so far (initial check) — final print-readiness confirmed after document generation.</translation>
    </message>
    <message>
        <source>status_tier2_failed</source>
        <translation>Post-generation final check failed — frame overflow or missing font issues prevent printing.</translation>
    </message>
    <message>
        <source>status_print_ready</source>
        <translation>Document is fully print-ready — passed both initial and final checks.</translation>
    </message>
</context>
<context>
    <name>LivePreview</name>
    <message>
        <source>Live Document Preview</source>
        <translation>Live Document Preview</translation>
    </message>
    <message>
        <source>Previous Page</source>
        <translation>Previous Page</translation>
    </message>
    <message>
        <source>Next Page</source>
        <translation>Next Page</translation>
    </message>
    <message>
        <source>Zoom In</source>
        <translation>Zoom In</translation>
    </message>
    <message>
        <source>Zoom Out</source>
        <translation>Zoom Out</translation>
    </message>
    <message>
        <source>Export Review PDF</source>
        <translation>Export Review PDF</translation>
    </message>
</context>
<context>
    <name>LanguageSelector</name>
    <message>
        <source>Language</source>
        <translation>Language</translation>
    </message>
    <message>
        <source>Arabic</source>
        <translation>العربية</translation>
    </message>
    <message>
        <source>English</source>
        <translation>English</translation>
    </message>
</context>
</TS>
