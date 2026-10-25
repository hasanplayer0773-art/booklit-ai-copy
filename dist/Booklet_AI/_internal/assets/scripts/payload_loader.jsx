/*
    payload_loader.jsx — Unified payload loading pattern for ExtendScript

    Every JSX entry point that receives structured data from Python MUST
    use this pattern: read a JSON file → parse via json2.jsx polyfill.
    This avoids:
    - Fragile manual escape-character handling (raw eval())
    - Large payloads in the ExtendScript call-stack/argument path
    - Character corruption of Arabic text with mixed diacritics

    Usage in worker scripts:
        #include "lib/json2.jsx"
        #include "payload_loader.jsx"

        // payloadFilePath is passed as a script argument from Python
        var data = loadPayloadFromFile(payloadFilePath);
        // data is now a parsed JavaScript object

    The path to json2.jsx is resolved relative to this script's own
    location ($.fileName), not a bare relative path — #include in
    ExtendScript does not reliably resolve arbitrary relative paths
    independent of the including script's directory.

    STEP-1.1 reference: §3, "Safe Cross-Language Payload Passing"
*/

// Ensure json2.jsx is loaded (provides JSON.parse / JSON.stringify)
// Note: The #include directive above must be used by the calling script.
// This function assumes JSON global is available.

/**
 * Load and parse a JSON payload file.
 *
 * @param {string} filePath - Absolute path to the JSON payload file.
 * @returns {Object} Parsed JavaScript object from the JSON file.
 * @throws {Error} If the file cannot be opened, read, or parsed.
 */
function loadPayloadFromFile(filePath) {
    if (typeof JSON === "undefined") {
        throw new Error(
            "PayloadLoaderError: JSON global is not defined. " +
            "Ensure json2.jsx is #included before payload_loader.jsx."
        );
    }

    var payloadFile = new File(filePath);

    if (!payloadFile.exists) {
        throw new Error(
            "PayloadLoaderError: file not found — " + filePath
        );
    }

    payloadFile.encoding = "UTF-8";
    var opened = payloadFile.open("r");

    if (!opened) {
        throw new Error(
            "PayloadLoaderError: cannot open file — " + filePath +
            " (error: " + payloadFile.error + ")"
        );
    }

    var rawJSON = payloadFile.read();
    payloadFile.close();

    if (!rawJSON || rawJSON.length === 0) {
        throw new Error(
            "PayloadLoaderError: file is empty — " + filePath
        );
    }

    try {
        var parsed = JSON.parse(rawJSON);
        return parsed;
    } catch (e) {
        throw new Error(
            "PayloadLoaderError: JSON parse failed — " + e.message +
            " (file: " + filePath + ")"
        );
    }
}

/**
 * Write a result object back to a JSON file (for IPC responses).
 *
 * Used by worker scripts to send structured results back to Python
 * (e.g. preflight reports, error details) via the file-based IPC
 * pattern rather than COM return values.
 *
 * @param {string} filePath - Absolute path for the output JSON file.
 * @param {Object} data - JavaScript object to serialize.
 * @throws {Error} If the file cannot be created or written.
 */
function writeResultToFile(filePath, data) {
    if (typeof JSON === "undefined") {
        throw new Error(
            "PayloadLoaderError: JSON global is not defined. " +
            "Ensure json2.jsx is #included before payload_loader.jsx."
        );
    }

    var resultFile = new File(filePath);
    resultFile.encoding = "UTF-8";
    var opened = resultFile.open("w");

    if (!opened) {
        throw new Error(
            "PayloadLoaderError: cannot create output file — " + filePath +
            " (error: " + resultFile.error + ")"
        );
    }

    var jsonString = JSON.stringify(data, null, 2);
    resultFile.write(jsonString);
    resultFile.close();
}
