"""Jaeger "Trace JSON" import: one UI export document with a data list of traces.

The wire model is what the Jaeger UI's JSON download writes: {"data": [{"traceID", "spans",
"processes"}]}. Spans carry a list of {key, type, value} tags, microsecond times and parent
links through references (the first CHILD_OF); service names live in the processes map.
"""
import json

import pytest

from xray.spans import TraceFormatError, load_spans

TRACE = "078f4a15714fecfef7b2146e0b9f5f32"


def jaeger_export():
    """One trace, two spans: a CLIENT call that failed and the SERVER work it called."""
    return {
        "data": [
            {
                "traceID": TRACE,
                "spans": [
                    {
                        "traceID": TRACE,
                        "spanID": "449d99ea22da8017",
                        "operationName": "GET",
                        "references": [],
                        "startTime": 1790000000000000,  # microseconds
                        "duration": 1500000,  # 1.5 seconds, in microseconds
                        "processID": "p1",
                        "tags": [
                            {"key": "span.kind", "type": "string", "value": "client"},
                            {"key": "error", "type": "bool", "value": True},
                        ],
                    },
                    {
                        "traceID": TRACE,
                        "spanID": "71b56c323da87261",
                        "operationName": "GET /work",
                        "references": [{"refType": "CHILD_OF", "traceID": TRACE, "spanID": "449d99ea22da8017"}],
                        "startTime": 1790000000100000,
                        "duration": 500000,  # 0.5 seconds, in microseconds
                        "processID": "p2",
                        "tags": [{"key": "span.kind", "type": "string", "value": "server"}],
                    },
                ],
                "processes": {
                    "p1": {"serviceName": "service-a", "tags": []},
                    "p2": {"serviceName": "service-b", "tags": []},
                },
            }
        ]
    }


def write(tmp_path, raw) -> str:
    file = tmp_path / "jaeger.json"
    file.write_text(json.dumps(raw) + "\n", encoding="utf-8")
    return str(file)


def test_a_jaeger_export_is_read_with_services_and_the_parent_link(tmp_path):
    client, child = load_spans(write(tmp_path, jaeger_export()))

    assert (client.service, child.service) == ("service-a", "service-b")  # names come from the processes map
    assert client.trace_id == child.trace_id == "0x" + TRACE
    assert client.span_id == "0x449d99ea22da8017"
    assert child.span_id == "0x71b56c323da87261"
    assert client.parent_id is None  # the parent has no CHILD_OF reference
    assert child.parent_id == client.span_id  # the link comes from references, not a parentSpanId field
    assert client.kind == "CLIENT" and child.kind == "SERVER"  # the tag's case is normalised


def test_microsecond_times_become_nanosecond_durations(tmp_path):
    client, child = load_spans(write(tmp_path, jaeger_export()))

    assert client.start_ns == 1_790_000_000_000_000_000  # microseconds times 1000
    assert client.duration_ns == 1_500_000_000
    assert child.duration_ns == 500_000_000


def test_the_error_tag_marks_a_span_failed_but_not_the_child(tmp_path):
    client, child = load_spans(write(tmp_path, jaeger_export()))

    assert client.error is True  # tags carry error=true as a bool
    assert client.error_text == ""  # Jaeger has no status message
    assert child.error is False


def test_a_malformed_span_is_a_format_error_not_a_crash(tmp_path):
    broken = {"data": [{"traceID": TRACE, "spans": [{"traceID": TRACE, "spanID": "cc", "operationName": "GET"}]}]}

    with pytest.raises(TraceFormatError):
        load_spans(write(tmp_path, broken))
