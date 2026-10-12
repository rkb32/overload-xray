"""The browser-side port (xray/static/analyzer.js) must give the same report as the Python pipeline.

"Private by construction" means the page analyzes a trace in the browser and never sends it anywhere. That only holds
up if the JavaScript pipeline agrees with the Python one, so this test runs both over the same inputs (every file in
xray/samples, plus generated traces in each of the four formats that exercise zombies, retries, queue wait, clock
skew and LLM tokens) and compares the reports after a JSON round-trip.

It needs `node` (21 or newer) and is skipped without it. tests/js_parity_driver.mjs does the node side: it reads the
cases from stdin and analyzer.js from the repo, and nothing else.
"""
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from xray import product
from xray.product import UploadError

ROOT = Path(__file__).resolve().parent.parent
DRIVER = ROOT / "tests" / "js_parity_driver.mjs"
SAMPLES = sorted((ROOT / "xray" / "samples").glob("*.json"))

# --- the generated trace -----------------------------------------------------------------------------------------

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
T0_NS = 1_791_374_400 * 1_000_000_000
URL = "http://User:pw@API.local:8080/orders/12345?token=secret#frag"


def _s(n, parent, service, name, kind, start_ms, end_ms, error="", **attrs):
    return {"id": f"{n:016x}", "parent": None if parent is None else f"{parent:016x}", "service": service, "name": name,
            "kind": kind, "start": start_ms, "end": end_ms, "error": error, "attrs": attrs}


# One checkout whose first order call times out (the callee keeps working: zombie work, and the retry that follows is a
# write attempted twice), a billed LLM call that fails with a 503, a callee whose own dependency drops the connection,
# and a second request whose GET is retried after an error nothing can classify.
SCENARIO = [
    _s(1, None, "web", "POST /checkout", "SERVER", 0, 10_000),
    _s(2, 1, "web", "handler", "INTERNAL", 5, 9_990),
    _s(3, 2, "web", "POST", "CLIENT", 10, 2_010, error="ReadTimeout: timed out", **{"http.request.method": "post", "url.full": URL}),
    _s(4, 3, "api", "POST /orders/{id}", "SERVER", 12, 6_000, **{"app.queue_wait_ms": 100, "gen_ai.usage.input_tokens": 800,
                                                                  "gen_ai.usage.output_tokens": 200}),
    _s(5, 2, "web", "POST", "CLIENT", 2_100, 3_000, **{"http.method": "POST", "http.url": URL, "http.request.resend_count": 1,
                                                       "http.response.status_code": 200}),
    _s(6, 5, "api", "POST /orders/{id}", "SERVER", 2_105, 2_950, **{"gen_ai.usage.input_tokens": 50, "gen_ai.usage.output_tokens": 10}),
    _s(7, 2, "web", "chat", "CLIENT", 3_100, 3_500, **{"url.full": "http://llm:9000/v1/chat", "gen_ai.usage.prompt_tokens": 500,
                                                       "gen_ai.usage.completion_tokens": 120, "http.status_code": 503}),
    _s(8, 7, "llm", "chat", "SERVER", 3_105, 3_400),
    _s(9, 4, "api", "GET", "CLIENT", 20, 5_000, error="ECONNRESET connection reset", **{"url.full": "http://db:5432/q?x=1"}),
    _s(10, 9, "db", "query", "SERVER", 30, 5_900),
    _s(11, None, "web", "GET /health", "SERVER", 20_000, 20_100),
    _s(12, 11, "web", "GET", "CLIENT", 20_010, 20_050, error="boom", **{"url.full": "http://cache:6379/ping"}),
    _s(13, 11, "web", "GET", "CLIENT", 20_048, 20_090, **{"url.full": "http://cache:6379/ping"}),
    _s(14, 11, "web", "internal-call", "CLIENT", 20_091, 20_095),
    _s(15, 11, "web", "PUT", "CLIENT", 20_100, 20_150, error="Timeout", **{"url.full": "http://db:5432/put"}),
    _s(16, 15, "db", "put", "SERVER", 20_105, 20_153),  # 3 ms after the caller left: clock noise, not zombie work
]
SERVER_CLIENT_KINDS = {"INTERNAL": 1, "SERVER": 2, "CLIENT": 3}


def _ns(ms):
    return T0_NS + ms * 1_000_000


def _otlp_value(value):
    return {"intValue": str(value)} if isinstance(value, int) else {"stringValue": value}


def render_otlp(spans, bare_numbers=False):
    """One export request per service, one per line (what the Collector's file exporter writes)."""
    lines = []
    for service in dict.fromkeys(s["service"] for s in spans):
        rendered = []
        for s in (s for s in spans if s["service"] == service):
            span = {"traceId": TRACE, "spanId": s["id"], "parentSpanId": s["parent"] or "", "name": s["name"],
                    "kind": SERVER_CLIENT_KINDS[s["kind"]],
                    "startTimeUnixNano": _ns(s["start"]) if bare_numbers else str(_ns(s["start"])),
                    "endTimeUnixNano": _ns(s["end"]) if bare_numbers else str(_ns(s["end"])),
                    "attributes": [{"key": k, "value": _otlp_value(v)} for k, v in s["attrs"].items()]}
            if s["error"]:
                span["status"] = {"code": 2, "message": s["error"]}
            rendered.append(span)
        lines.append(json.dumps({"resourceSpans": [{
            "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
            "scopeSpans": [{"spans": rendered}]}]}))
    return "\n".join(lines) + "\n"


def _iso(ns, zulu):
    moment = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ns // 1000)
    text = moment.isoformat()
    return text.replace("+00:00", "Z") if zulu else text


def render_sdk(spans):
    """The OpenTelemetry Python SDK's span JSON, one span per line."""
    lines = []
    for index, s in enumerate(spans):
        lines.append(json.dumps({
            "name": s["name"], "context": {"trace_id": "0x" + TRACE, "span_id": "0x" + s["id"]},
            "kind": f"SpanKind.{s['kind']}", "parent_id": None if s["parent"] is None else "0x" + s["parent"],
            "start_time": _iso(_ns(s["start"]), index % 2 == 0), "end_time": _iso(_ns(s["end"]), index % 2 == 0),
            "status": {"status_code": "ERROR", "description": s["error"]} if s["error"] else {"status_code": "UNSET"},
            "attributes": dict(s["attrs"]), "resource": {"attributes": {"service.name": s["service"]}}}))
    return "\n".join(lines) + "\n"


def render_zipkin(spans):
    """Zipkin v2: one array, microsecond times, string tags."""
    out = []
    for s in spans:
        span = {"traceId": TRACE, "id": s["id"], "name": s["name"], "timestamp": _ns(s["start"]) // 1000,
                "duration": (s["end"] - s["start"]) * 1000, "localEndpoint": {"serviceName": s["service"]},
                "tags": {k: str(v) for k, v in s["attrs"].items()}}
        if s["parent"]:
            span["parentId"] = s["parent"]
        if s["kind"] != "INTERNAL":
            span["kind"] = s["kind"]
        if s["error"]:
            span["tags"]["error"] = s["error"]
        out.append(span)
    return json.dumps(out)


def _jaeger_tag(key, value):
    kind = "bool" if isinstance(value, bool) else "int64" if isinstance(value, int) else "string"
    return {"key": key, "type": kind, "value": value}


def render_jaeger(spans):
    """A Jaeger UI export: tags are typed lists, parents are CHILD_OF references, services live in `processes`."""
    services = list(dict.fromkeys(s["service"] for s in spans))
    out = []
    for s in spans:
        tags = [_jaeger_tag(k, v) for k, v in s["attrs"].items()] + [_jaeger_tag("span.kind", s["kind"].lower())]
        if s["error"]:
            tags.append(_jaeger_tag("error", True))
        out.append({"traceID": TRACE, "spanID": s["id"], "operationName": s["name"], "startTime": _ns(s["start"]) // 1000,
                    "duration": (s["end"] - s["start"]) * 1000, "tags": tags,
                    "processID": f"p{services.index(s['service'])}",
                    "references": [{"refType": "CHILD_OF", "traceID": TRACE, "spanID": s["parent"]}] if s["parent"] else []})
    return json.dumps({"data": [{"traceID": TRACE, "spans": out,
                                 "processes": {f"p{i}": {"serviceName": name} for i, name in enumerate(services)}}]})


# --- the cases -----------------------------------------------------------------------------------------------------

# Odd URLs: the report shows the target of a retried write, so a difference in how a URL is cut apart shows up in it.
URLS = [
    "http://[::1]:8080/a/b?x#y", "http://[broken/x", "http://host:99999/x", "http://host:abc/x", "//host/path",
    "HTTP://Host.EXAMPLE:0080/x/550e8400-e29b-41d4-a716-446655440000", "host/path", "http://éxample.com/ü/12345678",
    "  http://sp ace/x", "http://u:p@h:1/\tx", "http://[fe80::1%25eth0]/x", "http://[v1.x]/z", "http://[1.2.3.4]/z",
    "http://a／b/c", "mailto:someone@example.com", "http://host:/x", "http://h/" + "p" * 300,
]


def _retried_post(url):
    """A write that failed and was sent again (the client says so), to the given URL."""
    return [
        _s(1, None, "web", "POST /go", "SERVER", 0, 500),
        _s(2, 1, "web", "POST", "CLIENT", 10, 50, error="boom", **{"url.full": url}),
        _s(3, 1, "web", "POST", "CLIENT", 60, 100, **{"url.full": url, "http.request.resend_count": 1}),
    ]


def _zipkin_variants(text):
    spans = json.loads(text)
    spans[2]["tags"]["error"] = "true"
    spans[3]["tags"]["error"] = "false"
    spans[4]["tags"]["error"] = "0"
    spans[5]["kind"] = "bogus"
    spans[6].pop("localEndpoint")
    spans[6]["remoteEndpoint"] = {"serviceName": "remote"}
    spans[7]["tags"]["gen_ai.usage.input_tokens"] = "not-a-number"
    spans[8]["tags"]["gen_ai.usage.input_tokens"] = "-5"
    spans[9]["timestamp"] = spans[9]["duration"] = None
    return spans


def _jaeger_variants(text):
    document = json.loads(text)
    spans = document["data"][0]["spans"]
    spans[0]["tags"].append({"key": "otel.status_code", "type": "string", "value": "error"})
    spans[1]["processID"] = "nope"
    spans[2]["references"] = [{"refType": "FOLLOWS_FROM", "spanID": "x"}]
    spans[3]["tags"].append({"key": "span.kind", "type": "string", "value": "weird"})
    return document


def _sdk_variants(text):
    spans = [json.loads(line) for line in text.splitlines()]
    spans[0]["start_time"] = "2026-10-04 12:00:00.123456789+05:30"
    spans[1]["start_time"] = "2026-10-04T12:00:00"
    spans[2]["resource"] = {"service.name": "flat"}
    spans[3]["attributes"] = None
    spans[4]["status"] = None
    # Every form datetime.fromisoformat accepts for the same moment, basic and extended (Python 3.11+ reads them, so
    # the port must too). One start_time per span, with the end after them all so every duration stays positive.
    basic_forms = ["20261010", "20261010T120000", "20261010T120000Z", "20261010T1200", "20261010T12",
                   "2026-10-10T12:00:00Z", "20261010T120000.5Z", "20261010T120000+0530", "20261010T120000+05:30",
                   "2026-10-10T120000Z", "2026-10-10T1200Z"]
    for span, form in zip(spans[5:], basic_forms):
        span["start_time"] = form
        span["end_time"] = "2026-10-11T00:00:00Z"
    return "\n".join(json.dumps(span) for span in spans)


OTLP_NO_NAME = json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [
    {"traceId": "ab", "spanId": "cd", "kind": 2, "startTimeUnixNano": "1", "endTimeUnixNano": "2"}]}]}]})
OTLP_BAD_INT = OTLP_NO_NAME.replace('"startTimeUnixNano": "1"', '"name": "x", "startTimeUnixNano": "abc"')
SDK_BAD_TIME = json.dumps({"name": "x", "context": {"trace_id": "0x1", "span_id": "0x2"}, "kind": "SpanKind.SERVER",
                           "start_time": "nope", "end_time": "nope"})


def build_cases():
    """id -> (files, options). Every one is run through Python and through the port."""
    out = {f"sample:{path.name}": ([(path.name, path.read_text(encoding="utf-8"))], {}) for path in SAMPLES}
    out["samples:all-in-one-upload"] = ([(p.name, p.read_text(encoding="utf-8")) for p in SAMPLES], {})
    otlp, sdk, zipkin, jaeger = render_otlp(SCENARIO), render_sdk(SCENARIO), render_zipkin(SCENARIO), render_jaeger(SCENARIO)
    out["format:otlp"] = ([("t.jsonl", otlp)], {})
    out["format:otlp-bare-number-nanoseconds"] = ([("t.json", render_otlp(SCENARIO, bare_numbers=True))], {})
    out["format:sdk"] = ([("t.jsonl", sdk)], {})
    out["format:zipkin"] = ([("t.json", zipkin)], {})
    out["format:jaeger"] = ([("t.json", jaeger)], {})
    out["format:pretty-printed-jaeger"] = ([("t.json", json.dumps(json.loads(jaeger), indent=2))], {})
    out["mixed:all-four-formats-in-one-upload"] = ([("a.jsonl", otlp), ("b.jsonl", sdk), ("c.json", zipkin), ("d.json", jaeger)], {})
    out["dedupe:same-file-twice"] = ([("a.jsonl", otlp), ("b.jsonl", otlp)], {})
    out["empty:no-spans"] = ([("empty.json", "[]")], {})
    out["odd:leading-bom"] = ([("a.json", "﻿" + otlp)], {})
    out["odd:crlf-line-endings"] = ([("a.jsonl", otlp.replace("\n", "\r\n"))], {})
    out["odd:blank-lines"] = ([("a.jsonl", "\n\n" + otlp.replace("\n", "\n\n"))], {})
    out["odd:otlp-requests-in-one-array"] = ([("a.json", json.dumps([json.loads(line) for line in otlp.splitlines()]))], {})
    out["odd:one-zipkin-span-alone"] = ([("a.json", json.dumps(json.loads(zipkin)[0]))], {})
    out["odd:zipkin-tag-variants"] = ([("a.json", json.dumps(_zipkin_variants(zipkin)))], {})
    out["odd:jaeger-tag-variants"] = ([("a.json", json.dumps(_jaeger_variants(jaeger)))], {})
    out["odd:sdk-time-and-resource-variants"] = ([("a.jsonl", _sdk_variants(sdk))], {})
    for number, url in enumerate(URLS):
        out[f"url:{number}:{url}"] = ([("a.json", render_otlp(_retried_post(url)))], {})
    out["error:no-files"] = ([], {})
    out["error:too-many-files"] = ([("a.json", "[]")] * 11, {})
    out["error:not-a-span"] = ([("x.json", '{"hello": 1}')], {})
    out["error:bad-json-line"] = ([("x.json", '{"a": 1}\nnot json\n')], {})
    out["error:missing-key"] = ([("x.json", OTLP_NO_NAME)], {})
    out["error:not-an-integer"] = ([("x.json", OTLP_BAD_INT)], {})
    out["error:bad-timestamp"] = ([("x.json", SDK_BAD_TIME)], {})
    out["error:too-many-spans"] = ([("big.json", SAMPLES[0].read_text(encoding="utf-8"))], {"maxSpans": 3})
    return out


CASES = build_cases()


def python_result(files, options):
    """What the Python pipeline says, after the same JSON round-trip the web page gets."""
    saved = product.MAX_SPANS, product.MAX_FILES
    product.MAX_SPANS = options.get("maxSpans", saved[0])
    product.MAX_FILES = options.get("maxFiles", saved[1])
    try:
        return {"ok": json.loads(json.dumps(product.analyze_upload(files)))}
    except UploadError as exc:
        return {"error": {"status": exc.status, "message": exc.message}}
    finally:
        product.MAX_SPANS, product.MAX_FILES = saved


def differences(js, py, path="report"):
    """Every place two JSON values disagree, as readable lines (capped, so a total mismatch does not flood the output)."""
    found = []
    if isinstance(js, dict) and isinstance(py, dict):
        for key in sorted(set(js) | set(py)):
            if key not in js:
                found.append(f"{path}.{key}: missing from the port (python: {py[key]!r})")
            elif key not in py:
                found.append(f"{path}.{key}: only in the port ({js[key]!r})")
            else:
                found += differences(js[key], py[key], f"{path}.{key}")
    elif isinstance(js, list) and isinstance(py, list):
        if len(js) != len(py):
            found.append(f"{path}: {len(js)} items in the port, {len(py)} in python")
        for index, (a, b) in enumerate(zip(js, py)):
            found += differences(a, b, f"{path}[{index}]")
    elif js != py or isinstance(js, bool) != isinstance(py, bool):
        found.append(f"{path}: port {js!r} != python {py!r}")
    return found[:20]


@pytest.fixture(scope="module")
def port_results():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH: the browser-side port (xray/static/analyzer.js) cannot be checked against Python")
    version = subprocess.run([node, "--version"], capture_output=True, text=True, check=True).stdout.strip()
    if int(re.match(r"v(\d+)", version).group(1)) < 21:
        pytest.skip(f"node {version} is too old: the driver needs JSON.rawJSON (node 21 or newer)")
    request = {"cases": [{"id": name, "files": [list(f) for f in files], "options": options}
                         for name, (files, options) in CASES.items()]}
    run = subprocess.run([node, str(DRIVER)], input=json.dumps(request), capture_output=True, text=True,
                         encoding="utf-8", timeout=120, cwd=ROOT)
    assert run.returncode == 0, f"the node driver crashed:\n{run.stderr[-3000:]}"
    return json.loads(run.stdout)


@pytest.mark.parametrize("name", list(CASES))
def test_port_matches_python(name, port_results):
    files, options = CASES[name]
    expected = python_result(files, options)
    got = port_results[name]
    assert ("ok" in got) == ("ok" in expected), f"{name}: the port said {got}, python said {expected}"
    found = differences(got, expected, name)
    assert not found, "the port and python disagree:\n  " + "\n  ".join(found)


def test_every_sample_in_the_repo_is_covered():
    assert SAMPLES, "no samples found"
    assert {f"sample:{p.name}" for p in SAMPLES} <= set(CASES)
