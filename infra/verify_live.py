"""Looks at a deployed overload-xray from the outside, the way a stranger's browser or script meets it.

    python infra/verify_live.py https://<your-address>        # about 20 requests, one at a time

Standard library only, so it runs anywhere Python does. Exit code 0 only if every check passed.

There is deliberately no flood test. This account allows 10 concurrent Lambda executions in total, shared with
winnow-api, and API Gateway's throttle is best effort: a burst of 20 requests at once was NOT turned away (0 of 20
got a 429), it used all 10 slots and made Lambda reject 5. Never run a burst against this stack.
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request

PAUSE_S = 0.7  # one request at a time, with room to breathe
MB = 1024 * 1024
JSON_HEADERS = {"Content-Type": "application/json"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand 3xx answers back as they are: the http -> https check has to see the redirect, not follow it."""

    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(NoRedirect)
results: list[tuple[bool, str]] = []


def call(url, method="GET", body=None, headers=None):
    """Returns (status, headers, body). Header lookups are case-insensitive."""
    time.sleep(PAUSE_S)
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with OPENER.open(request, timeout=45) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:  # a 4xx/5xx still has headers and a body worth checking
        return error.code, error.headers, error.read()


def check(name, ok, detail=""):
    results.append((bool(ok), name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail != "" else ""))


def as_json(body):
    try:
        return json.loads(body)
    except ValueError:
        return {}


def upload(files):
    return json.dumps({"files": [{"name": name, "text": text} for name, text in files]}).encode()


def main(base):
    base = base.rstrip("/")

    print("the page")
    status, headers, body = call(base + "/")
    # A deploy that ships the page without its files would pass every other check and look blank in a browser.
    assets = re.findall(r'(?:src|href)="(/static/[^"]+)"', body.decode("utf-8", "replace"))
    loaded = {path: call(base + path)[0] for path in assets}
    check("GET / is HTML, and the script and styles it names load",
          status == 200 and "text/html" in headers.get("content-type", "") and len(assets) >= 2
          and all(code == 200 for code in loaded.values()), f"{status}, {loaded}")
    csp = headers.get("content-security-policy", "")
    check("CSP allows only our own scripts and forbids framing",
          "script-src 'self'" in csp and "frame-ancestors 'none'" in csp)
    check("nosniff, no-frame and no-referrer headers",
          headers.get("x-content-type-options") == "nosniff" and headers.get("x-frame-options") == "DENY"
          and headers.get("referrer-policy") == "no-referrer")
    check("page may be kept by a shared cache for 5 minutes", "s-maxage=300" in headers.get("cache-control", ""),
          headers.get("cache-control", ""))
    _, again, _ = call(base + "/")
    served = again.get("x-cache")
    behind_cloudfront = served is not None
    if behind_cloudfront:
        check("second GET / is answered from CloudFront's cache", served.startswith(("Hit", "RefreshHit")), served)
        http_url = "http://" + base.split("://", 1)[1] + "/"
        status, headers, _ = call(http_url)
        check("plain http is redirected to https", status in (301, 302) and headers.get("location", "").startswith("https://"),
              f"{status} {headers.get('location', '')}")
    else:
        print("  skip  CloudFront cache and http redirect (no x-cache header: this address is not CloudFront)")

    print("the API")
    status, _, body = call(base + "/healthz")
    check("/healthz says ok", status == 200 and as_json(body) == {"status": "ok"}, status)
    status, _, body = call(base + "/api/features")
    features = as_json(body)
    check("limits are the hosted ones: 5 MB, no database",
          status == 200 and features.get("limits", {}).get("max_mb") == 5.0 and features.get("saved_runs") is False,
          json.dumps(features.get("limits", {})))

    print("analysing a trace")
    status, _, sample = call(base + "/samples/retry-storm")
    # The sample is one JSON document per line, so the whole file is not valid JSON: the analysis below is the real test.
    check("the sample downloads", status == 200 and len(sample) > 1000, f"{status}, {len(sample)} bytes")
    status, headers, body = call(base + "/api/analyze", "POST", upload([("retry-storm.json", sample.decode())]), JSON_HEADERS)
    report = as_json(body)
    summary = report.get("summary", {})
    labels = sorted({finding["label"] for finding in report.get("findings", [])})
    check("the sample gets the same answer as the local run (3x amplification, 9.018 s of zombie work)",
          status == 200 and summary.get("amplification") == 3.0 and summary.get("zombie_s") == 9.018
          and labels == ["retry_storm", "zombie_work"],
          f"{status}, amplification {summary.get('amplification')}, zombie {summary.get('zombie_s')} s, {labels}")
    check("the result is never cacheable", headers.get("cache-control") == "no-store", headers.get("cache-control", ""))
    # What `curl --data-binary @traces.json` sends: the raw file with curl's default content type.
    status, _, body = call(base + "/api/analyze", "POST", sample, {"Content-Type": "application/x-www-form-urlencoded"})
    check("a raw body (curl style) works too", status == 200 and as_json(body).get("summary", {}).get("amplification") == 3.0,
          status)

    status, _, layered = call(base + "/samples/layered-retries")
    analysed, _, body = call(base + "/api/analyze", "POST", upload([("layered-retries.json", layered.decode("utf-8", "replace"))]), JSON_HEADERS)
    worst = (as_json(body).get("retries") or {}).get("worst") or {}
    check("the retry map finds 2 layers and 9 calls at the bottom in the layered sample",
          status == 200 and analysed == 200 and worst.get("layers") == 2 and worst.get("multiplication") == 9,
          f"{status}/{analysed}, layers {worst.get('layers')}, multiplication {worst.get('multiplication')}")

    print("hostile input")
    marker = "SECRET-MARKER <script>alert(1)</script>"
    status, _, body = call(base + "/api/analyze", "POST", upload([("x.json", marker)]), JSON_HEADERS)
    check("garbage is a clean 422 that does not repeat what was sent",
          status == 422 and b"alert(1)" not in body and b"SECRET-MARKER" not in body, f"{status} {body[:70]!r}")
    status, _, body = call(base + "/api/analyze", "POST", upload([("big.json", "A" * int(5.5 * MB))]), JSON_HEADERS)
    check("5.5 MB is refused by the app with 413", status == 413 and b"5 MB" in body, f"{status} {body[:70]!r}")
    status, _, body = call(base + "/api/analyze", "POST", upload([("huge.json", "A" * (7 * MB))]), JSON_HEADERS)
    check("7 MB (above Lambda's 6 MB) is refused too, never answered 200", status >= 400, f"{status} {body[:70]!r}")
    hidden = {path: call(base + path)[0] for path in ("/docs", "/redoc", "/openapi.json")}
    check("API docs are not served", all(code == 404 for code in hidden.values()), hidden)
    escapes = {path: call(base + path) for path in ("/samples/..%2f..%2fetc%2fpasswd", "/static/..%2f..%2fetc%2fpasswd")}
    check("path tricks find nothing", all(code in (400, 404) and b"root:" not in body for code, _, body in escapes.values()),
          {path: code for path, (code, _, _) in escapes.items()})
    status, _, body = call(base + "/api/runs")
    check("saved runs say 'no database' and leak nothing", status == 503 and b"postgres" not in body.lower(), f"{status} {body[:60]!r}")
    status, headers, _ = call(base + "/api/analyze", "OPTIONS",
                              headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    check("another website cannot call the API from a browser (no CORS)", "access-control-allow-origin" not in headers, status)

    failed = [name for ok, name in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    for name in failed:
        print(f"  FAILED: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].startswith(("https://", "http://")):
        sys.exit("usage: python infra/verify_live.py https://<address>")
    sys.exit(main(sys.argv[1]))
