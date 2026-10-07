"""The command line: the same findings the page shows, for people who would rather not upload anything."""
import json
import os

import pytest

from xray.cli import main

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "xray", "samples")
LAYERED = os.path.join(SAMPLES, "layered-retries.json")


def test_retries_prints_the_retry_map(capsys):
    main(["retries", LAYERED])

    out = capsys.readouterr().out
    assert "4 calls made 12 attempts" in out
    assert "2 layer(s) retried" in out and "received 9 call(s)" in out
    assert "retried write: POST" in out and "idempotent" in out


def test_diagnose_includes_the_findings_from_the_retry_map(capsys):
    main(["diagnose", LAYERED])

    out = capsys.readouterr().out
    assert "[retry_layers]" in out and "[retried_writes]" in out
    assert "[zombie_work]" in out  # the findings that were already there are still there


def test_retries_on_a_trace_with_no_calls_says_so(capsys, tmp_path):
    empty = tmp_path / "none.json"
    empty.write_text("", encoding="utf-8")

    main(["retries", str(empty)])

    assert "no client calls" in capsys.readouterr().out


def test_report_prints_the_wasted_token_estimate_with_the_price_flags(capsys, tmp_path):
    trace = json.dumps({
        "name": "generate", "context": {"trace_id": "0xt", "span_id": "0x11"}, "kind": "SpanKind.CLIENT",
        "parent_id": "0x10", "start_time": "2026-10-03T12:00:00.000Z", "end_time": "2026-10-03T12:00:01.000Z",
        "status": {"status_code": "ERROR", "description": "ReadTimeout: "},
        "attributes": {"gen_ai.usage.input_tokens": 400000, "gen_ai.usage.output_tokens": 100000},
        "resource": {"attributes": {"service.name": "agent"}},
    })
    file = tmp_path / "agent.jsonl"
    file.write_text(trace + "\n", encoding="utf-8")

    main(["report", str(file), "--input-token-price", "3", "--output-token-price", "15"])

    out = capsys.readouterr().out
    assert "LLM tokens: 400,000 in + 100,000 out" in out
    assert "about $2.70 wasted" in out  # 400000 / 1e6 * 3 + 100000 / 1e6 * 15


def test_report_without_a_price_never_prints_dollars(capsys, tmp_path):
    trace = json.dumps({
        "name": "generate", "context": {"trace_id": "0xt", "span_id": "0x11"}, "kind": "SpanKind.CLIENT",
        "parent_id": "0x10", "start_time": "2026-10-03T12:00:00.000Z", "end_time": "2026-10-03T12:00:01.000Z",
        "status": {"status_code": "ERROR", "description": "ReadTimeout: "},
        "attributes": {"gen_ai.usage.input_tokens": 5},
        "resource": {"attributes": {"service.name": "agent"}},
    })
    file = tmp_path / "agent.jsonl"
    file.write_text(trace + "\n", encoding="utf-8")

    main(["report", str(file)])

    out = capsys.readouterr().out
    assert "LLM tokens: 5 in + 0 out" in out
    assert "$" not in out  # token counts are free; dollar estimates need a price flag


# The build gate: thresholds and a non-zero exit code -------------------------------------------------------------

def test_report_exits_1_when_goodput_is_below_the_minimum(capsys):
    # The bundled sample wastes all of its work: goodput is 0%.
    with pytest.raises(SystemExit) as error:
        main(["report", LAYERED, "--min-goodput", "0.1"])

    assert error.value.code == 1
    assert "FAIL: goodput is 0%, below --min-goodput 10%" in capsys.readouterr().out


def test_report_exits_1_when_amplification_is_above_the_maximum(capsys):
    with pytest.raises(SystemExit) as error:
        main(["report", LAYERED, "--max-amplification", "2"])

    assert error.value.code == 1
    assert "FAIL: amplification is" in capsys.readouterr().out


def test_report_passes_when_the_trace_is_inside_the_limits(capsys):
    main(["report", LAYERED, "--min-goodput", "0", "--max-amplification", "20"])

    assert "FAIL:" not in capsys.readouterr().out


def test_report_still_prints_the_report_before_it_fails(capsys):
    with pytest.raises(SystemExit):
        main(["report", LAYERED, "--min-goodput", "0.5"])

    out = capsys.readouterr().out
    assert "user requests:" in out and "FAIL:" in out  # the full report, then the verdict
