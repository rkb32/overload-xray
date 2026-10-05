"""Build the GitHub Pages static demo into docs/site/.

The hosted page POSTs uploads to /api/analyze, which GitHub Pages cannot serve, so the static copy
ships with the three bundled samples and the load-test results already analyzed: the same JSON the
API would return, baked to files at build time. index.html and app.css are copied as-is; app.js gets
one patch (marked XRAY-STATIC in the output) that swaps the network calls for the baked data and
points "analyze your own" at running locally instead of an upload API.

Run from the repo root:  .venv\\Scripts\\python -m demo.build_pages
Serve the result:        .venv\\Scripts\\python -m http.server -d docs 8000

Output goes to docs/ itself, because GitHub Pages can publish only the repo root or /docs, not a
deeper folder. The build touches only its own known files (index.html, app.css, app.js, .nojekyll,
data/) so the markdown docs beside them are left alone.
"""
import csv
import json
import os
import re
import shutil

from xray import product

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs")

# data-sample names in index.html -> the file the server would have read for /samples/<name>.
SAMPLES = {
    "retry-storm": "retry-storm.json",
    "after-fix": "after-fix.json",
    "layered-retries": "layered-retries.json",
}

BANNER = "// XRAY-STATIC: baked demo for GitHub Pages. Built by demo/build_pages.py; do not edit the copy in docs/site/.\n"


def build_reports() -> dict:
    """Analyze each bundled sample with the real pipeline and return {name: report-dict}."""
    reports = {}
    for name, filename in SAMPLES.items():
        path = os.path.join(ROOT, "xray", "samples", filename)
        with open(path, encoding="utf-8") as handle:
            reports[name] = product.analyze_upload([(filename, handle.read())])
    return reports


def load_loadtest_rows() -> list:
    """The rows /api/loadtest would have returned from results/loadtest.csv ([] if the demo was never run)."""
    path = os.path.join(ROOT, "results", "loadtest.csv")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as handle:
        return [{key: float(value) for key, value in row.items()} for row in csv.DictReader(handle)]


# Every fetch the page makes, and what the static copy answers instead.
def patch_app_js(source: str, sample_names: list) -> str:
    static_samples = "const STATIC_SAMPLES = " + json.dumps(sample_names) + ";\n"

    static_try_sample = """async function trySample(name) {
  if (!STATIC_SAMPLES.includes(name)) return setStatus("This demo only ships the bundled samples.", true);
  try {
    renderReport(await getJSON("data/" + encodeURIComponent(name) + ".report.json"));
    setStatus("A pre-analyzed sample. To read your own traces, run xray locally (below).");
  } catch (error) {
    setStatus("Could not load the sample report.", true);
  }
}"""

    static_analyze = """async function analyze(files) {
  if (!files.length) return;
  setStatus("This static demo has no server behind it. Run the analyzer on your own traces in a terminal: "
    + "python -m xray diagnose your-traces.json (see the Export traces section for how to get the file).", true);
}"""

    patches = [
        (
            re.compile(r'async function trySample\(name\) \{.*?\n\}', re.DOTALL),
            lambda m: static_try_sample,
        ),
        (
            re.compile(r'async function analyze\(files\) \{.*?\n\}', re.DOTALL),
            lambda m: static_analyze,
        ),
        # setupCurl: there is no /api/analyze here, so show the local CLI instead.
        (
            re.compile(r'const command = `curl --data-binary @traces\.json \$\{location\.origin\}/api/analyze`;'),
            lambda m: 'const command = "python -m xray diagnose your-traces.json   # run locally, nothing uploaded";',
        ),
        # init(): baked rows instead of GET /api/loadtest.
        (
            re.compile(r'getJSON\("/api/loadtest"\)'),
            lambda m: 'getJSON("data/loadtest.json")',
        ),
        # init(): no server-side features or saved runs on a static host.
        (
            re.compile(r'getJSON\("/api/features"\)\.then\(async \(features\) => \{.*?\n  \}\)\.catch\(\(\) => \{\}\);', re.DOTALL),
            lambda m: '/* static demo: no /api/features, no saved runs */',
        ),
    ]

    for pattern, replacement in patches:
        source, count = pattern.subn(replacement, source, count=1)
        if count != 1:
            raise RuntimeError(f"app.js patch did not match exactly once: {pattern.pattern[:60]!r}")
    return BANNER + static_samples + source


def patch_index_html(source: str) -> str:
    """Root-relative URLs break under <user>.github.io/<repo>/; make them relative. The curl row shows
    a local CLI command in the static build, so its label has to stop saying "curl"."""
    patches = [
        ('href="/static/app.css"', 'href="app.css"'),
        ('src="/static/app.js"', 'src="app.js"'),
        ('href="/" aria-label="overload-xray, home"', 'href="index.html" aria-label="overload-xray, home"'),
        ('<span class="mono-label">or curl</span>', '<span class="mono-label">or run locally</span>'),
    ]
    for old, new in patches:
        if source.count(old) != 1:
            raise RuntimeError(f"index.html patch did not match exactly once: {old!r}")
        source = source.replace(old, new)
    return source


def main():
    # Clean only what this script owns: data/ is fully generated, the rest is overwritten file by file.
    data_dir = os.path.join(OUT, "data")
    if os.path.exists(data_dir):
        shutil.rmtree(data_dir)
    os.makedirs(data_dir)

    with open(os.path.join(ROOT, "xray", "static", "index.html"), encoding="utf-8") as handle:
        index = patch_index_html(handle.read())
    with open(os.path.join(ROOT, "xray", "static", "app.js"), encoding="utf-8") as handle:
        app_js = patch_app_js(handle.read(), list(SAMPLES))
    shutil.copyfile(os.path.join(ROOT, "xray", "static", "app.css"), os.path.join(OUT, "app.css"))

    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(index)
    with open(os.path.join(OUT, "app.js"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(app_js)
    open(os.path.join(OUT, ".nojekyll"), "w").close()  # serve files as-is; Jekyll would mangle nothing here anyway

    reports = build_reports()
    for name, report in reports.items():
        with open(os.path.join(OUT, "data", f"{name}.report.json"), "w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, separators=(",", ":"))
    with open(os.path.join(OUT, "data", "loadtest.json"), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(load_loadtest_rows(), handle, separators=(",", ":"))

    total = sum(os.path.getsize(os.path.join(dirpath, f)) for dirpath, _, files in os.walk(OUT) for f in files)
    print(f"built {OUT} ({total / 1024:.0f} KB, {len(reports)} sample reports)")


if __name__ == "__main__":
    main()
