"""The command line: the same findings the page shows, for people who would rather not upload anything."""
import os

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
