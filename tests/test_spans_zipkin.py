"""Zipkin v2 JSON import: an array of spans with microsecond timestamps and string tags.

The wire model is https://zipkin.io/zipkin-api/ (v2): {traceId, id, parentId, name, kind,
timestamp (µs), duration (µs), localEndpoint.serviceName, tags}. `parse_text` splits a file's
array into per-span items, so `_spans_from` receives one span object at a time.
"""
import json

from xray.spans import load_spans

TRACE = "078f4a15714fecfef7b2146e0b9f5f32"


def zipkin_span(span_id, **overrides):
    span = {
        "traceId": TRACE,
        "id": span_id,
        "parentId": "3d863283a9249b63",
        "name": "GET",
        "kind": "CLIENT",
        "timestamp": 1790000000000000,  # microseconds (1.79e15 µs)
        "duration": 1000000,  # 1 second, in microseconds
        "localEndpoint": {"serviceName": "service-a"},
        "tags": {"http.method": "GET", "http.url": "http://127.0.0.1:8001/work"},
    }
    span.update(overrides)
    return span


def write(tmp_path, raw) -> str:
    file = tmp_path / "zipkin.json"
    file.write_text(json.dumps(raw) + "\n", encoding="utf-8")
    return str(file)


def test_an_array_of_zipkin_spans_is_read(tmp_path):
    (span,) = load_spans(write(tmp_path, [zipkin_span("449d99ea22da8017")]))

    assert span.trace_id == "0x" + TRACE
    assert span.span_id == "0x449d99ea22da8017"
    assert span.parent_id == "0x3d863283a9249b63"
    assert span.service == "service-a"
    assert span.kind == "CLIENT"
    assert span.error is False
    assert span.duration_ns == 1_000_000_000  # microseconds converted to nanoseconds


def test_a_server_span_with_the_error_tag_is_an_error(tmp_path):
    server = zipkin_span(
        "71b56c323da87261",
        name="GET /work",
        kind="SERVER",
        localEndpoint={"serviceName": "service-b"},
        tags={"http.status_code": "503", "error": "upstream timed out"},
    )

    (span,) = load_spans(write(tmp_path, [server]))

    assert (span.kind, span.service) == ("SERVER", "service-b")
    assert span.error is True and span.error_text == "upstream timed out"
    assert span.http_status == 503


def test_a_bare_true_error_tag_sets_error_but_not_the_text(tmp_path):
    (span,) = load_spans(write(tmp_path, [zipkin_span("aa", tags={"error": "true"})]))

    assert span.error is True and span.error_text == ""


def test_a_missing_timestamp_duration_and_kind_is_zero_length_and_internal(tmp_path):
    (span,) = load_spans(write(tmp_path, [zipkin_span("bb", kind=None, timestamp=None, duration=None)]))

    assert (span.start_ns, span.end_ns, span.duration_ns) == (0, 0, 0)
    assert span.kind == "INTERNAL"


def test_method_and_target_come_from_tags_and_secrets_are_dropped(tmp_path):
    span = zipkin_span(
        "cc",
        tags={
            "http.method": "post",
            "http.url": "https://user:hunter2@payments.example.com:8443/v1/charge?token=SECRET#frag",
        },
    )

    (parsed,) = load_spans(write(tmp_path, [span]))

    assert parsed.method == "POST"
    assert parsed.target == "payments.example.com:8443/v1/charge"
    assert "hunter2" not in parsed.target and "SECRET" not in parsed.target and "frag" not in parsed.target


def test_a_remote_only_endpoint_supplies_the_service(tmp_path):
    span = zipkin_span("dd", localEndpoint=None, remoteEndpoint={"serviceName": "service-a"})

    (parsed,) = load_spans(write(tmp_path, [span]))

    assert parsed.service == "service-a"


def test_zipkin_can_mix_with_sdk_json_in_one_directory(tmp_path):
    (tmp_path / "zipkin.json").write_text(json.dumps([zipkin_span("449d99ea22da8017")]) + "\n", encoding="utf-8")
    (tmp_path / "service-b.jsonl").write_text(json.dumps({
        "name": "GET /work",
        "context": {"trace_id": "0x" + TRACE, "span_id": "0x71b56c323da87261"},
        "kind": "SpanKind.SERVER",
        "start_time": "2026-10-03T20:53:04.966700Z",
        "end_time": "2026-10-03T20:53:05.966700Z",
        "resource": {"attributes": {"service.name": "service-b"}},
    }) + "\n", encoding="utf-8")

    spans = load_spans(str(tmp_path))
    by_service = {span.service: span for span in spans}

    assert by_service["service-a"].kind == "CLIENT"
    assert by_service["service-b"].kind == "SERVER"