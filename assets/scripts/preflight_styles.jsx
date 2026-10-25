/*
    preflight_styles.jsx — Template Style Preflight Entry Point
    STEP-2.3

    Payload-based entry point for the template style preflight check.
    Called once at document-load time to verify all required paragraph
    styles exist in the .idml template BEFORE content generation begins.

    Usage (from Python COM bridge via payload_loader pattern):
        var payload = loadPayloadFromFile(payloadFilePath);
        var result = preflightTemplateStyles(payload.required_styles);
        writeResultToFile(resultFilePath, result);

    The payload is produced by template_preflight.build_preflight_payload()
    and contains { "required_styles": ["StyleA", "StyleB", ...] }.

    The result is read by template_preflight.check_preflight_result()
    and contains either:
        { "status": "ok" }
    or:
        { "status": "error", "missing_styles": ["MissingStyle", ...] }

    Dependencies:
        - lib/json2.jsx (JSON polyfill for ExtendScript)
        - payload_loader.jsx (file-based IPC)
        - apply_question_callout.jsx (provides preflightTemplateStyles)

    STEP-1.1 reference: COM threading, modal suppression
*/

// ─── Includes ─────────────────────────────────────────────────────────
// NOTE: These #include directives are resolved relative to the file
// location at runtime. The calling Python code must ensure the scripts
// directory is set as the active scripts folder.
//
// #include "lib/json2.jsx"
// #include "payload_loader.jsx"
// #include "apply_question_callout.jsx"


/**
 * Main entry point for the style preflight check.
 *
 * Reads the payload from a JSON file (passed via script args),
 * runs the preflight check, and writes the result to a JSON file.
 *
 * @param {string} payloadFilePath - Path to the input JSON payload.
 * @param {string} resultFilePath - Path to write the result JSON.
 */
function runPreflightCheck(payloadFilePath, resultFilePath) {
    var payload = loadPayloadFromFile(payloadFilePath);

    if (!payload.required_styles || !payload.required_styles.length) {
        throw new Error(
            "PreflightError: payload missing 'required_styles' array. " +
            "Expected output from template_preflight.build_preflight_payload()."
        );
    }

    var result = preflightTemplateStyles(payload.required_styles);
    writeResultToFile(resultFilePath, result);
}
