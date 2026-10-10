// Runs the browser-side port (xray/static/analyzer.js) over the cases tests/test_js_parity.py sends on stdin and
// prints the results as JSON on stdout. It reads one file only: analyzer.js, found next to this script inside the repo.
//
// stdin:  {"cases": [{"id": "...", "files": [["name.json", "<text>"], ...], "options": {"maxSpans": 3}}]}
// stdout: {"<id>": {"ok": <report>} | {"error": {"status": 422, "message": "..."}}}
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "..", "xray", "static", "analyzer.js"), "utf8");
// Import through a data: URL so this works whatever the Node version does with a .js file that has no package.json.
const { analyzeFiles, UploadError } = await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));

const request = JSON.parse(readFileSync(0, "utf8"));
const results = {};
for (const { id, files, options } of request.cases) {
  try {
    results[id] = { ok: analyzeFiles(files, options || {}) };
  } catch (error) {
    if (!(error instanceof UploadError)) throw error; // a crash is a test failure, not a result
    results[id] = { error: { status: error.status, message: error.message } };
  }
}
// Integers beyond 2^53 are BigInt in the port: write them as exact digits, like Python's json.dumps does.
process.stdout.write(JSON.stringify(results, (key, value) => (typeof value === "bigint" ? JSON.rawJSON(value.toString()) : value)));
