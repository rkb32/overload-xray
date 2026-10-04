import json
import os
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from xray import api, product
from xray.limits import RateLimiter

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = os.path.join(os.path.dirname(HERE), "xray", "samples", "retry-storm.json")
RUN = {"id": 1, "name": "baseline", "calls": 3, "work_ns": 9_000_000_000, "goodput": 0.0, "tail_ns": 5_500_000_000}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api, "limiter", RateLimiter(max_events=1000, per_seconds=60))  # tests should not trip the limit
    monkeypatch.delenv("XRAY_TRUST_PROXY", raising=False)
    return TestClient(api.app)


def sample_text() -> str:
    with open(SAMPLE, encoding="utf-8") as handle:
        return handle.read()


def upload(client, files):
    return client.post("/api/analyze", json={"files": [{"name": n, "text": t} for n, t in files]})


# --- the page and its headers -------------------------------------------------------------------------------------

def test_the_home_page_is_served_with_strict_security_headers(client):
    response = client.get("/")

    assert response.status_code == 200 and "overload-xray" in response.text
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "'unsafe-inline'" not in response.headers["content-security-policy"].split("style-src")[0]  # scripts: never
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_the_page_has_no_inline_script_for_the_policy_to_block(client):
    page = client.get("/").text

    assert "<script>" not in page and "onclick=" not in page


def test_the_page_and_other_public_files_may_be_kept_by_a_cdn_but_browsers_recheck(client):
    for path in ("/", "/static/app.js", "/static/app.css", "/samples/retry-storm", "/api/features", "/api/loadtest"):
        control = client.get(path).headers["cache-control"]

        assert "s-maxage=300" in control and "max-age=0" in control, path


def test_an_analysis_result_is_never_cacheable_by_anything(client):
    response = upload(client, [("retry-storm.json", sample_text())])

    assert response.headers["cache-control"] == "no-store"
    assert upload(client, [("bad.txt", "not json")]).headers["cache-control"] == "no-store"  # errors too


def test_errors_are_not_cached(client):
    assert client.get("/samples/nope").headers["cache-control"] == "no-cache"
    assert client.get("/healthz").headers["cache-control"] == "no-cache"


def test_interactive_api_docs_are_not_exposed(client):
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404


def test_health_check(client):
    assert client.get("/healthz").json() == {"status": "ok"}


# --- analyzing ----------------------------------------------------------------------------------------------------

def test_a_sample_is_analyzed_into_headline_findings_and_notes(client):
    response = upload(client, [("retry-storm.json", sample_text())])

    body = response.json()
    assert response.status_code == 200
    assert body["summary"]["dependency_calls"] == 3 and body["summary"]["goodput"] == 0.0
    assert {f["label"] for f in body["findings"]} == {"retry_storm", "zombie_work"}
    assert "never used" in body["summary"]["headline"]
    assert {n["kind"] for n in body["notes"]} >= {"small_sample", "queue_wait"}


def test_curl_can_send_the_raw_file_as_the_body(client):
    response = client.post("/api/analyze", content=sample_text().encode("utf-8"), headers={"content-type": "text/plain"})

    assert response.status_code == 200 and response.json()["summary"]["dependency_calls"] == 3


def test_a_pretty_printed_document_and_a_json_array_are_understood(client):
    document = json.loads(sample_text().splitlines()[0])  # one OTLP export request
    pretty = json.dumps(document, indent=2)
    array = json.dumps([document])

    assert upload(client, [("pretty.json", pretty)]).status_code == 200
    assert upload(client, [("array.json", array)]).status_code == 200


def test_the_sample_endpoint_only_serves_known_names(client):
    assert client.get("/samples/retry-storm").status_code == 200
    assert client.get("/samples/..%2Fapp.py").status_code == 404
    assert client.get("/samples/nope").status_code == 404


# --- hostile and broken input -------------------------------------------------------------------------------------

def test_text_that_is_not_traces_gets_a_friendly_error_that_never_echoes_it_back(client):
    secret = "hunter2-super-secret-token-9d8f7a"

    response = upload(client, [("notes.txt", f"{secret} this is not json")])

    assert response.status_code == 422
    assert secret not in response.text and "notes.txt" in response.text


def test_json_that_is_not_a_span_is_refused_politely(client):
    response = upload(client, [("data.json", json.dumps({"hello": "world", "token": "abc-def-ghi-123456"}))])

    assert response.status_code == 422 and "abc-def-ghi-123456" not in response.text


def test_a_jaeger_export_is_recognised_and_explained(client):
    jaeger = json.dumps({"data": [{"traceID": "abc", "spans": []}]})

    response = upload(client, [("jaeger.json", jaeger)])

    assert response.status_code == 422 and "Jaeger" in response.json()["detail"]


def test_a_wrong_envelope_is_a_400(client):
    assert client.post("/api/analyze", json={"nope": 1}).status_code == 400
    assert client.post("/api/analyze", content=b"\xff\xfe\x00", headers={"content-type": "text/plain"}).status_code == 400


def test_an_empty_upload_is_a_400(client):
    assert client.post("/api/analyze", json={"files": []}).status_code == 400


def test_too_many_files_are_refused(client):
    files = [(f"f{i}.json", sample_text()) for i in range(product.MAX_FILES + 1)]

    assert upload(client, files).status_code == 400


def test_a_body_over_the_size_limit_is_refused(client, monkeypatch):
    monkeypatch.setattr(product, "MAX_REQUEST_BYTES", 2_000)

    response = upload(client, [("big.json", sample_text())])

    assert response.status_code == 413


def test_too_many_spans_are_refused(client, monkeypatch):
    monkeypatch.setattr(product, "MAX_SPANS", 5)

    response = upload(client, [("retry-storm.json", sample_text())])

    assert response.status_code == 413 and "smaller time window" in response.json()["detail"]


def test_a_cycle_of_ancestor_spans_does_not_hang_the_service(client):
    def span(span_id, parent, kind):
        return {"name": "n", "context": {"trace_id": "0xt", "span_id": span_id}, "kind": f"SpanKind.{kind}", "parent_id": parent,
                "start_time": "2026-10-03T12:00:00.000Z", "end_time": "2026-10-03T12:00:01.000Z", "status": {"status_code": "UNSET"},
                "attributes": {}, "resource": {"attributes": {"service.name": "svc"}}}

    lines = [span("0x1", "0x2", "INTERNAL"), span("0x2", "0x1", "INTERNAL"), span("0x3", "0x1", "CLIENT"), span("0x4", "0x3", "SERVER")]

    response = upload(client, [("evil.jsonl", "\n".join(json.dumps(s) for s in lines))])

    assert response.status_code == 200


# --- fair use -----------------------------------------------------------------------------------------------------

def test_the_rate_limit_answers_429_with_retry_after(monkeypatch):
    monkeypatch.setattr(api, "limiter", RateLimiter(max_events=2, per_seconds=60))
    client = TestClient(api.app)

    statuses = [upload(client, [("s.json", sample_text())]).status_code for _ in range(3)]

    assert statuses == [200, 200, 429]
    assert int(upload(client, [("s.json", sample_text())]).headers["retry-after"]) >= 1


def test_behind_a_trusted_proxy_the_limit_uses_the_address_the_proxy_appended(monkeypatch):
    monkeypatch.setattr(api, "limiter", RateLimiter(max_events=1, per_seconds=60))
    monkeypatch.setenv("XRAY_TRUST_PROXY", "1")
    client = TestClient(api.app)

    def post(forwarded):
        return client.post("/api/analyze", json={"files": [{"name": "s", "text": sample_text()}]}, headers={"x-forwarded-for": forwarded})

    assert post("1.1.1.1, 9.9.9.9").status_code == 200
    # the client rewrote the FIRST entry to dodge the limit; the proxy-appended last entry is still 9.9.9.9
    assert post("6.6.6.6, 9.9.9.9").status_code == 429
    assert post("1.1.1.1, 8.8.8.8").status_code == 200


def test_without_a_trusted_proxy_a_forged_header_is_ignored(monkeypatch):
    monkeypatch.setattr(api, "limiter", RateLimiter(max_events=1, per_seconds=60))
    client = TestClient(api.app)

    first = client.post("/api/analyze", json={"files": [{"name": "s", "text": sample_text()}]}, headers={"x-forwarded-for": "1.1.1.1"})
    second = client.post("/api/analyze", json={"files": [{"name": "s", "text": sample_text()}]}, headers={"x-forwarded-for": "2.2.2.2"})

    assert (first.status_code, second.status_code) == (200, 429)


def test_analyzing_writes_nothing_to_disk(client, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    upload(client, [("retry-storm.json", sample_text())])

    assert list(tmp_path.iterdir()) == []


# --- saved runs and the load-test chart (optional parts) ------------------------------------------------------------

class FakeStore:
    """Stands in for xray.store so the API can be tested without a database."""

    @staticmethod
    @contextmanager
    def connect():
        yield None

    @staticmethod
    def list_runs(conn):
        return [RUN]

    @staticmethod
    def get_run(conn, run_id):
        return {**RUN, "edges": []} if run_id == 1 else None


def test_runs_come_from_the_store(client, monkeypatch):
    monkeypatch.setattr(api, "store", FakeStore)

    assert client.get("/api/runs").json() == [RUN]
    assert client.get("/api/runs/1").json()["edges"] == []
    assert client.get("/api/runs/99").status_code == 404


def test_without_a_database_saved_runs_answer_503_and_features_say_so(client, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert client.get("/api/runs").status_code == 503
    assert client.get("/api/features").json()["saved_runs"] is False


def test_features_report_a_configured_database(client, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")

    assert client.get("/api/features").json()["saved_runs"] is True


def test_features_report_the_limits_the_server_really_enforces(client, monkeypatch):
    monkeypatch.setattr(product, "MAX_REQUEST_BYTES", 5 * 1024 * 1024)
    monkeypatch.setattr(product, "MAX_FILES", 4)

    limits = client.get("/api/features").json()["limits"]

    assert (limits["max_mb"], limits["max_files"]) == (5.0, 4)
    assert limits["max_spans"] == product.MAX_SPANS and limits["per_minute"] >= 1


def test_loadtest_endpoint_parses_the_csv(client, monkeypatch, tmp_path):
    csv_file = tmp_path / "loadtest.csv"
    csv_file.write_text("offered_per_s,baseline_ok_per_s\n8,2.6\n", encoding="utf-8")
    monkeypatch.setattr(api, "LOADTEST_CSV", str(csv_file))

    assert client.get("/api/loadtest").json() == [{"offered_per_s": 8.0, "baseline_ok_per_s": 2.6}]


def test_loadtest_endpoint_is_empty_before_the_first_run(client, monkeypatch, tmp_path):
    monkeypatch.setattr(api, "LOADTEST_CSV", str(tmp_path / "missing.csv"))

    assert client.get("/api/loadtest").json() == []


def test_the_layered_retries_sample_is_served_and_analyzes_into_a_retry_stack(client):
    sample = client.get("/samples/layered-retries")

    assert sample.status_code == 200
    result = upload(client, [("layered-retries.json", sample.text)]).json()
    assert result["retries"]["worst"]["layers"] == 2 and result["retries"]["worst"]["multiplication"] == 9
    assert "retry_layers" in {f["label"] for f in result["findings"]}


def test_sample_names_are_still_an_allow_list(client):
    assert client.get("/samples/layered-retries.json").status_code == 404  # only the exact names work
    assert client.get("/samples/..%2fapi.py").status_code in (400, 404)
