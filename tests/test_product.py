import json

import pytest

from xray.product import MAX_FILES, UploadError, _display_name, analyze_upload
from xray.spans import TooManySpans, TraceFormatError, parse_text


def sdk_line(span_id, parent=None, kind="SERVER", status="UNSET", attributes=None):
    return json.dumps({
        "name": "n", "context": {"trace_id": "0xt", "span_id": span_id}, "kind": f"SpanKind.{kind}", "parent_id": parent,
        "start_time": "2026-10-03T12:00:00.000Z", "end_time": "2026-10-03T12:00:01.000Z",
        "status": {"status_code": status}, "attributes": attributes or {},
        "resource": {"attributes": {"service.name": "svc"}},
    })


def test_json_lines_a_single_document_and_an_array_all_parse():
    lines = "\n".join([sdk_line("0x1"), sdk_line("0x2")])

    assert len(parse_text(lines)) == 2
    assert len(parse_text(sdk_line("0x1"))) == 1  # one document
    assert len(parse_text("[" + sdk_line("0x1") + "," + sdk_line("0x2") + "]")) == 2  # an array


def test_the_error_for_a_broken_line_names_the_line_but_not_its_contents():
    text = sdk_line("0x1") + "\n" + "password=hunter2 {broken"

    with pytest.raises(TraceFormatError) as error:
        parse_text(text, source="traces.jsonl")

    assert "traces.jsonl" in str(error.value) and "line 2" in str(error.value) and "hunter2" not in str(error.value)


def test_a_span_with_missing_fields_is_a_format_error_not_a_crash():
    broken = json.dumps({"context": {"trace_id": "0xt"}, "start_time": "x"})  # no span id, no kind, no times

    with pytest.raises(TraceFormatError):
        parse_text(broken)


def test_the_span_limit_stops_parsing_early():
    text = "\n".join(sdk_line(f"0x{i}") for i in range(10))

    with pytest.raises(TooManySpans):
        parse_text(text, max_spans=5)


def test_file_names_are_cleaned_before_they_reach_an_error_message():
    assert _display_name("../../etc/passwd") == "passwd"
    assert _display_name("evil\x00name\n<script>.json") == "evil_name__script_.json"
    assert len(_display_name("a" * 500)) == 60
    assert _display_name("") == "upload" and _display_name(None) == "upload"


def test_no_calls_between_services_is_explained_not_an_error():
    result = analyze_upload([("one-service.jsonl", sdk_line("0x1"))])

    assert result["edges"] == [] and result["findings"] == []
    assert [n["kind"] for n in result["notes"]] == ["no_calls"]
    assert "No calls" in result["summary"]["headline"]


def test_upload_limits_are_enforced_with_clear_status_codes():
    with pytest.raises(UploadError) as no_files:
        analyze_upload([])
    with pytest.raises(UploadError) as too_many:
        analyze_upload([(f"f{i}", sdk_line("0x1")) for i in range(MAX_FILES + 1)])

    assert (no_files.value.status, too_many.value.status) == (400, 400)


# --- the retry map in the report --------------------------------------------------------------------------------------

def sample_text(name):
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(os.path.dirname(here), "xray", "samples", name), encoding="utf-8") as handle:
        return handle.read()


def test_the_report_carries_the_retry_map_for_the_bundled_sample():
    retries = analyze_upload([("retry-storm.json", sample_text("retry-storm.json"))])["retries"]

    assert retries["totals"] == {"logical_calls": 1, "attempts": 3, "retries": 2, "retry_share": 0.6667, "budget_share": 0.1}
    (edge,) = retries["edges"][:1]
    assert (edge["caller"], edge["callee"], edge["attempts"], edge["per_call"], edge["exhausted"]) == ("service-a", "service-b", 3, 3.0, 1)
    assert edge["inferred"] is True
    assert retries["worst"] == {"layers": 1, "multiplication": 3,
                                "steps": [{"caller": "service-a", "callee": "service-b", "attempts": 3}]}
    assert retries["writes"] == [] and retries["unattributed"] == 0


def test_the_layered_sample_shows_two_layers_nine_calls_and_two_retried_writes():
    result = analyze_upload([("layered-retries.json", sample_text("layered-retries.json"))])

    assert result["retries"]["worst"]["layers"] == 2 and result["retries"]["worst"]["multiplication"] == 9
    assert {w["method"] for w in result["retries"]["writes"]} == {"POST"} and len(result["retries"]["writes"]) == 2
    assert {"retry_layers", "retried_writes"} <= {f["label"] for f in result["findings"]}


def test_a_note_says_the_retries_were_inferred_not_read_from_the_spans():
    notes = analyze_upload([("retry-storm.json", sample_text("retry-storm.json"))])["notes"]

    assert "retries_inferred" in {n["kind"] for n in notes}


def test_a_note_says_how_many_calls_could_not_be_grouped():
    spans = [sdk_line("0x1"), sdk_line("0x2", "0x1", "CLIENT"), sdk_line("0x3", "0x2"),  # one call that can be followed
             sdk_line("0x4", "0x1", "CLIENT"), sdk_line("0x5", "0x1", "CLIENT")]  # two with no URL and no callee

    notes = analyze_upload([("mixed.jsonl", "\n".join(spans))])["notes"]

    (note,) = [n for n in notes if n["kind"] == "ungrouped_calls"]
    assert "2 client calls" in note["text"]


def client_line(span_id, parent, start, end, error, url, method="POST"):
    return json.dumps({
        "name": "n", "context": {"trace_id": "0xt", "span_id": span_id}, "kind": "SpanKind.CLIENT", "parent_id": parent,
        "start_time": f"2026-10-03T12:00:{start:02d}.000Z", "end_time": f"2026-10-03T12:00:{end:02d}.000Z",
        "status": {"status_code": "ERROR" if error else "UNSET"}, "attributes": {"http.url": url, "http.method": method},
        "resource": {"attributes": {"service.name": "svc"}},
    })


def test_names_in_the_retry_map_are_cut_to_a_sane_length():
    url = "http://" + "x" * 500 + "/" + "p" * 500  # a hostile upload can put anything in a URL
    lines = [sdk_line("0x1"), client_line("0x2", "0x1", 0, 1, True, url), client_line("0x3", "0x1", 1, 2, False, url)]

    result = analyze_upload([("long.jsonl", "\n".join(lines))])

    assert result["retries"]["totals"]["retries"] == 1
    assert all(len(e["callee"]) <= 80 for e in result["retries"]["edges"])
    assert all(len(w["target"]) <= 80 for w in result["retries"]["writes"])


def test_the_summary_counts_wasted_llm_tokens_and_swears_they_are_an_estimate():
    # The callee job of a timed-out client call runs to completion with LLM tokens on it: the result was never used.
    lines = [
        sdk_line("0x1"),
        sdk_line("0x2", "0x1", "CLIENT", status="ERROR"),
        sdk_line("0x3", "0x2", attributes={"gen_ai.usage.input_tokens": 120, "gen_ai.usage.output_tokens": 40}),
    ]
    result = analyze_upload([("agent.jsonl", "\n".join(lines))])

    assert result["summary"]["tokens_in"] == 120 and result["summary"]["tokens_out"] == 40
    assert result["summary"]["wasted_tokens_in"] == 120 and result["summary"]["wasted_tokens_out"] == 40
    (note,) = [n for n in result["notes"] if n["kind"] == "tokens_estimate"]
    assert "estimate, not an invoice" in note["text"] and "depends on the provider" in note["text"]
