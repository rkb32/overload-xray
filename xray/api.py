"""The web app: the public upload product, plus a read-only API over saved runs (optional, needs Postgres).

    python -m xray serve        # http://127.0.0.1:8080

The product part (/ and /api/analyze) needs no database and keeps nothing: traces are analyzed in memory and dropped.
"""
import csv
import json
import os

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from xray import product, store
from xray.limits import RateLimiter

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
SAMPLES_DIR = os.path.join(HERE, "samples")
SAMPLES = {  # an allow-list: names never become paths
    "retry-storm": "retry-storm.json", "after-fix": "after-fix.json", "layered-retries": "layered-retries.json",
}
LOADTEST_CSV = os.path.join(os.path.dirname(HERE), "results", "loadtest.csv")

# Scripts only from our own origin. Styles allow inline because the charts set widths and colors per element.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

# docs_url etc. off: a public product does not need to advertise its internals.
app = FastAPI(title="overload-xray", docs_url=None, redoc_url=None, openapi_url=None)
limiter = RateLimiter(max_events=int(os.environ.get("XRAY_ANALYSES_PER_MINUTE", "20")), per_seconds=60)


# Public, unchanging-per-deployment pages may be kept by a shared cache (a CDN) for 5 minutes, which keeps visitors
# from using up the service's capacity. max-age=0 makes a browser re-check every time, so edits show at once.
SHARED_CACHE = "public, max-age=0, s-maxage=300"
CACHEABLE_PATHS = ("/", "/api/features", "/api/loadtest")


def cache_policy(request: Request, status: int) -> str:
    if request.url.path == "/api/analyze":
        return "no-store"  # someone's traces went in and a report about them came out: nothing may keep a copy
    cacheable = request.method == "GET" and status == 200 and (
        request.url.path in CACHEABLE_PATHS or request.url.path.startswith(("/static/", "/samples/"))
    )
    return SHARED_CACHE if cacheable else "no-cache"


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = cache_policy(request, response.status_code)
    return response


def client_key(request: Request) -> str:
    """Who is asking, for rate limiting. Behind a trusted proxy (XRAY_TRUST_PROXY=1) that is the address the proxy
    appended to X-Forwarded-For (the LAST entry; earlier ones are written by the client and can be faked)."""
    if os.environ.get("XRAY_TRUST_PROXY") == "1":
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()[:64]
    return request.client.host if request.client else "unknown"


async def read_limited(request: Request, limit: int) -> bytes:
    """Read the body, refusing as soon as it is too big (not after buffering all of it)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, f"Too large: the limit is {limit // (1024 * 1024)} MB per request.")
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(413, f"Too large: the limit is {limit // (1024 * 1024)} MB per request.")
        chunks.append(chunk)
    return b"".join(chunks)


def files_from_body(content_type: str, body: bytes) -> list[tuple[str, str]]:
    """Two ways in. The web page sends JSON: {"files": [{"name": ..., "text": ...}]}.
    curl can send the raw trace file as the body: curl --data-binary @traces.json <url>/api/analyze"""
    try:
        if content_type.split(";")[0].strip() == "application/json":
            sent = json.loads(body)
            entries = sent["files"]
            if not isinstance(entries, list):
                raise TypeError
            return [(str(entry.get("name", "upload")), str(entry["text"])) for entry in entries]
        return [("upload", body.decode("utf-8"))]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, AttributeError):
        raise HTTPException(400, 'Send JSON like {"files": [{"name": "traces.json", "text": "..."}]}, '
                                 "or the raw trace file as the request body (UTF-8 text).") from None


@app.post("/api/analyze")
async def analyze_traces(request: Request):
    key = client_key(request)
    if not limiter.allow(key):
        raise HTTPException(429, "Too many analyses from this address. Try again in a minute.",
                            headers={"Retry-After": str(limiter.retry_after(key))})
    body = await read_limited(request, product.MAX_REQUEST_BYTES)
    files = files_from_body(request.headers.get("content-type", ""), body)
    try:
        return await run_in_threadpool(product.analyze_upload, files)
    except product.UploadError as error:
        raise HTTPException(error.status, error.message) from None


@app.get("/samples/{name}")
def sample(name: str):
    if name not in SAMPLES:
        raise HTTPException(404, "no such sample")
    return FileResponse(os.path.join(SAMPLES_DIR, SAMPLES[name]), media_type="application/json")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/features")
def features():
    """Lets the page decide what to show without probing endpoints that would answer with errors."""
    return {
        "saved_runs": bool(os.environ.get("DATABASE_URL")),
        "limits": {  # the page shows these, so what it says always matches what the server enforces
            "max_files": product.MAX_FILES,
            "max_mb": round(product.MAX_REQUEST_BYTES / (1024 * 1024), 1),
            "max_spans": product.MAX_SPANS,
            "per_minute": limiter.max_events,
        },
    }


@app.get("/")
def home():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/loadtest")
def loadtest():
    """The goodput-vs-load results written by `python -m demo.loadtest`."""
    if not os.path.exists(LOADTEST_CSV):
        return []
    with open(LOADTEST_CSV, encoding="utf-8", newline="") as handle:
        return [{key: float(value) for key, value in row.items()} for row in csv.DictReader(handle)]


def from_database(read):
    """Saved runs are optional: without a database these answer 503 and the page simply hides that section."""
    try:
        with store.connect() as conn:
            return read(conn)
    except KeyError:
        raise HTTPException(503, "no database configured") from None
    except psycopg.OperationalError:
        raise HTTPException(503, "database unavailable") from None


@app.get("/api/runs")
def runs():
    return from_database(store.list_runs)


@app.get("/api/runs/{run_id}")
def run(run_id: int):
    found = from_database(lambda conn: store.get_run(conn, run_id))
    if found is None:
        raise HTTPException(404, "no such run")
    return found


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
