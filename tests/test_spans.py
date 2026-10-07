import json

from xray.spans import load_spans

# One real span copied from demo output: service A's call to B that ended in a read timeout.
REAL_CLIENT_ERROR_SPAN = {
    "name": "GET",
    "context": {"trace_id": "0x078f4a15714fecfef7b2146e0b9f5f32", "span_id": "0x449d99ea22da8017", "trace_state": "[]"},
    "kind": "SpanKind.CLIENT",
    "parent_id": "0x3d863283a9249b63",
    "start_time": "2026-10-03T20:53:04.966700Z",
    "end_time": "2026-10-03T20:53:06.959974Z",
    "status": {"status_code": "ERROR", "description": "ReadTimeout: "},
    "attributes": {"http.method": "GET", "http.url": "http://127.0.0.1:8001/work"},
    "events": [{"name": "exception", "timestamp": "2026-10-03T20:53:06.959924Z"}],
    "links": [],
    "resource": {"attributes": {"service.name": "service-a"}, "schema_url": ""},
}


def sdk_span(span_id, **overrides):
    """The real span above under a different span id (spans with the same trace and span id are one span)."""
    return dict(REAL_CLIENT_ERROR_SPAN, context={**REAL_CLIENT_ERROR_SPAN["context"], "span_id": span_id}, **overrides)


def test_load_spans_reads_a_real_otel_json_line(tmp_path):
    file = tmp_path / "service-a.jsonl"
    file.write_text(json.dumps(REAL_CLIENT_ERROR_SPAN) + "\n\n", encoding="utf-8")

    (span,) = load_spans(str(file))

    assert span.service == "service-a"
    assert span.kind == "CLIENT"
    assert span.error is True
    assert span.parent_id == "0x3d863283a9249b63"
    assert abs(span.duration_ns - 1_993_274_000) < 1_000  # 1.993274 s


def test_load_spans_reads_the_error_text_and_the_http_status(tmp_path):
    reply = sdk_span("0x1111111111111111", status={"status_code": "ERROR"}, attributes={"http.status_code": 503})
    file = tmp_path / "service-a.jsonl"
    file.write_text(json.dumps(REAL_CLIENT_ERROR_SPAN) + "\n" + json.dumps(reply) + "\n", encoding="utf-8")

    timeout, answered = load_spans(str(file))

    assert (timeout.error_text, timeout.http_status) == ("ReadTimeout: ", None)
    assert (answered.error_text, answered.http_status) == ("", 503)


def test_load_spans_reads_the_queue_wait_attribute(tmp_path):
    raw = dict(REAL_CLIENT_ERROR_SPAN, attributes={"app.queue_wait_ms": 4500})
    file = tmp_path / "service-b.jsonl"
    file.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    (span,) = load_spans(str(file))

    assert span.queue_ns == 4_500_000_000
    assert span.run_ns == 0  # the span lasted 1.99s, so a 4.5s queue wait clamps running time to zero


def otlp_line(**overrides):
    """One OTLP/JSON export request as the Collector's file exporter writes it: a service A client span that
    timed out, and a service B server span that waited 4.5s in a queue."""
    client = {
        "traceId": "078f4a15714fecfef7b2146e0b9f5f32",
        "spanId": "449d99ea22da8017",
        "parentSpanId": "3d863283a9249b63",
        "name": "GET",
        "kind": 3,
        "startTimeUnixNano": "1790000000000000000",
        "endTimeUnixNano": "1790000001000000000",
        "status": {"code": 2, "message": "ReadTimeout: "},
        "attributes": [{"key": "http.method", "value": {"stringValue": "GET"}}],
    }
    server = {
        "traceId": "078f4a15714fecfef7b2146e0b9f5f32",
        "spanId": "71b56c323da87261",
        "parentSpanId": "449d99ea22da8017",
        "name": "GET /work",
        "kind": 2,
        "startTimeUnixNano": "1790000000100000000",
        "endTimeUnixNano": "1790000005100000000",
        "status": {},
        "attributes": [{"key": "app.queue_wait_ms", "value": {"intValue": "4500"}}],
    }
    server.update(overrides)
    resource = lambda name: {"attributes": [{"key": "service.name", "value": {"stringValue": name}}]}  # noqa: E731
    return json.dumps(
        {
            "resourceSpans": [
                {"resource": resource("service-a"), "scopeSpans": [{"spans": [client]}]},
                {"resource": resource("service-b"), "scopeSpans": [{"spans": [server]}]},
            ]
        }
    )


def test_load_spans_reads_otlp_json(tmp_path):
    file = tmp_path / "otlp-traces.json"
    file.write_text(otlp_line() + "\n", encoding="utf-8")

    client, server = load_spans(str(file))

    assert (client.service, client.kind, client.error) == ("service-a", "CLIENT", True)
    assert (server.service, server.kind, server.error) == ("service-b", "SERVER", False)
    assert server.parent_id == client.span_id  # the link between the two services survives the format change
    assert client.parent_id == "0x3d863283a9249b63"
    assert client.duration_ns == 1_000_000_000
    assert server.queue_ns == 4_500_000_000 and server.run_ns == 500_000_000


def test_otlp_json_accepts_enum_names_and_a_missing_parent(tmp_path):
    file = tmp_path / "otlp-traces.json"
    file.write_text(otlp_line(kind="SPAN_KIND_SERVER", parentSpanId="") + "\n", encoding="utf-8")

    _, server = load_spans(str(file))

    assert server.kind == "SERVER"
    assert server.parent_id is None


def test_a_span_delivered_twice_is_counted_once(tmp_path):
    file = tmp_path / "otlp-traces.json"
    file.write_text(otlp_line() + "\n" + otlp_line() + "\n", encoding="utf-8")  # at-least-once delivery

    assert len(load_spans(str(file))) == 2  # one client span and one server span, not four


def test_both_formats_can_live_in_one_directory(tmp_path):
    (tmp_path / "sdk.jsonl").write_text(json.dumps(sdk_span("0x2222222222222222")) + "\n", encoding="utf-8")
    (tmp_path / "otlp.json").write_text(otlp_line() + "\n", encoding="utf-8")

    assert len(load_spans(str(tmp_path))) == 3


def test_load_spans_reads_every_jsonl_in_a_directory(tmp_path):
    for name, span_id in (("service-a.jsonl", "0xaaaa"), ("service-b.jsonl", "0xbbbb")):
        (tmp_path / name).write_text(json.dumps(sdk_span(span_id)) + "\n", encoding="utf-8")

    assert len(load_spans(str(tmp_path))) == 2


def test_load_spans_keeps_the_method_and_the_target_but_not_credentials_query_or_fragment(tmp_path):
    # URLs in traces can carry passwords and tokens. xray needs only where the call went, so only that is kept.
    raw = dict(REAL_CLIENT_ERROR_SPAN, attributes={
        "http.method": "post",
        "http.url": "https://user:hunter2@payments.example.com:8443/v1/charge?token=SECRET#frag",
    })
    file = tmp_path / "service-a.jsonl"
    file.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    (span,) = load_spans(str(file))

    assert span.method == "POST"
    assert span.target == "payments.example.com:8443/v1/charge"
    assert "hunter2" not in span.target and "SECRET" not in span.target and "frag" not in span.target


def test_load_spans_reads_the_current_semantic_convention_names(tmp_path):
    raw = dict(REAL_CLIENT_ERROR_SPAN, attributes={
        "http.request.method": "PUT", "url.full": "http://inventory/api/items/7?x=1", "http.request.resend_count": 2,
    })
    file = tmp_path / "service-a.jsonl"
    file.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    (span,) = load_spans(str(file))

    assert (span.method, span.target, span.resend_count) == ("PUT", "inventory/api/items/7", 2)


def test_an_unreadable_url_or_resend_count_is_ignored_not_a_crash(tmp_path):
    raw = dict(REAL_CLIENT_ERROR_SPAN, attributes={"http.url": "http://[broken", "http.request.resend_count": "many"})
    file = tmp_path / "service-a.jsonl"
    file.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    (span,) = load_spans(str(file))

    assert span.target == "" and span.resend_count is None and span.method == ""


def test_otlp_json_reads_method_target_and_resend_count(tmp_path):
    client = {
        "traceId": "078f4a15714fecfef7b2146e0b9f5f32", "spanId": "449d99ea22da8017", "parentSpanId": "3d863283a9249b63",
        "name": "POST", "kind": 3, "startTimeUnixNano": "1790000000000000000", "endTimeUnixNano": "1790000001000000000",
        "status": {},
        "attributes": [
            {"key": "http.request.method", "value": {"stringValue": "POST"}},
            {"key": "url.full", "value": {"stringValue": "http://billing:8080/charge?card=4111"}},
            {"key": "http.request.resend_count", "value": {"intValue": "1"}},  # intValue is a string in OTLP/JSON
        ],
    }
    document = {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "a"}}]},
                                   "scopeSpans": [{"spans": [client]}]}]}
    file = tmp_path / "otlp.json"
    file.write_text(json.dumps(document) + "\n", encoding="utf-8")

    (span,) = load_spans(str(file))

    assert (span.method, span.target, span.resend_count) == ("POST", "billing:8080/charge", 1)


# --- gen_ai usage tokens ---------------------------------------------------------------------------------------

def test_sdk_json_reads_gen_ai_usage_tokens(tmp_path):
    raw = dict(REAL_CLIENT_ERROR_SPAN, attributes={"gen_ai.usage.input_tokens": 120, "gen_ai.usage.output_tokens": 40})
    file = tmp_path / "agent.jsonl"
    file.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    (span,) = load_spans(str(file))

    assert (span.input_tokens, span.output_tokens) == (120, 40)


def test_otlp_json_reads_gen_ai_usage_tokens_as_strings(tmp_path):
    client = {
        "traceId": "078f4a15714fecfef7b2146e0b9f5f32", "spanId": "449d99ea22da8017", "parentSpanId": "3d863283a9249b63",
        "name": "generate", "kind": 3, "startTimeUnixNano": "1790000000000000000", "endTimeUnixNano": "1790000001000000000",
        "status": {},
        "attributes": [
            {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "1200"}},
            {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "300"}},
        ],
    }
    document = {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "a"}}]},
                                   "scopeSpans": [{"spans": [client]}]}]}
    file = tmp_path / "otlp.json"
    file.write_text(json.dumps(document) + "\n", encoding="utf-8")

    (span,) = load_spans(str(file))

    assert (span.input_tokens, span.output_tokens) == (1200, 300)


def test_the_older_gen_ai_convention_names_are_read_too(tmp_path):
    raw = dict(REAL_CLIENT_ERROR_SPAN, attributes={"gen_ai.usage.prompt_tokens": 7, "gen_ai.usage.completion_tokens": 9})
    file = tmp_path / "agent.jsonl"
    file.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    (span,) = load_spans(str(file))

    assert (span.input_tokens, span.output_tokens) == (7, 9)


def test_zipkin_tags_have_no_tokens(tmp_path):
    file = tmp_path / "zipkin.json"
    file.write_text(json.dumps([{
        "traceId": "e05063c5f98a7a8d", "id": "24771e40391986be", "name": "generate",
        "timestamp": 1791067277117856, "duration": 1000,
        "tags": {"gen_ai.usage.input_tokens": "50", "gen_ai.usage.output_tokens": "25"},
    }]) + "\n", encoding="utf-8")

    (span,) = load_spans(str(file))

    assert (span.input_tokens, span.output_tokens) == (50, 25)


def test_a_token_value_that_is_not_a_number_is_ignored_not_a_crash(tmp_path):
    raw = dict(REAL_CLIENT_ERROR_SPAN, attributes={"gen_ai.usage.input_tokens": "many", "gen_ai.usage.output_tokens": -3})
    file = tmp_path / "agent.jsonl"
    file.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    (span,) = load_spans(str(file))

    assert span.input_tokens == 0 and span.output_tokens == 0  # negative is read as zero: an estimate does not go negative
