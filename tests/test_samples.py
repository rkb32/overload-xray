"""The donated samples tell the stories they claim to tell. Each file is regenerated, not hand-edited:
`python -m demo.make_more_samples` (see demo/make_more_samples.py for the exact timings)."""
import os

from xray.product import analyze_upload

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(os.path.dirname(HERE), "xray", "samples")


def sample_text(name):
    with open(os.path.join(SAMPLES, name), encoding="utf-8") as handle:
        return handle.read()


def report(name):
    return analyze_upload([(name, sample_text(name))])


def edges(result):
    return {(e["caller"], e["callee"]): e for e in result["edges"]}


def test_deep_chain_trace_propagates_the_waste_downstream():
    # web gives up on billing at 1 s; billing works on to 2.02 s. Its own call to risk already answered at
    # 0.3 s, so on the billing -> risk edge the waste is inherited (zombie 0.2 s) with a tail of zero.
    result = report("deep-chain.json")
    by_edge = edges(result)

    assert by_edge[("web", "billing")]["calls"] == 1
    assert by_edge[("web", "billing")]["goodput"] == 0.0
    assert by_edge[("web", "billing")]["zombie_s"] == 2.0  # the whole abandoned job is waste
    assert by_edge[("web", "billing")]["tail_s"] == 1.02  # but only the part after web left can be cancelled
    assert by_edge[("billing", "risk")]["goodput"] == 0.0  # waste flows downstream even though risk answered
    assert by_edge[("billing", "risk")]["zombie_s"] == 0.18  # ...it inherits the job's doom
    assert by_edge[("billing", "risk")]["tail_s"] == 0.0  # ...while nothing ran after billing left


def test_fanout_trace_is_three_parallel_shard_reads_all_used():
    result = report("fanout.json")

    assert [e["callee"] for e in result["edges"]] == ["shard-a", "shard-b", "shard-c"]
    assert result["summary"]["amplification"] == 3.0
    assert result["summary"]["goodput"] == 1.0  # a deliberate fan-out, not a retry storm


def test_queued_trace_counts_only_the_time_after_the_queue_wait():
    result = report("queued.json")

    assert result["summary"]["work_s"] == 0.5  # 3.5 s of run time, but 3.0 s was waiting in a queue
    assert result["summary"]["dependency_calls"] == 1
    assert edges(result)[("web", "orders")]["goodput"] == 1.0


def test_hedged_trace_is_read_as_two_calls_with_zero_retries():
    # Two overlapping GETs at the same origin: hedging, not a retry (a chain continues only after a FAILED
    # attempt, DESIGN.md D14), so the retry map reports 2 calls in 2 attempts and no retry.
    result = report("hedged.json")

    totals = result["retries"]["totals"]
    assert (totals["logical_calls"], totals["attempts"], totals["retries"]) == (2, 2, 0)
    (edge,) = result["retries"]["edges"]
    assert (edge["caller"], edge["callee"], edge["per_call"]) == ("gateway", "origin", 1.0)
    assert result["summary"]["goodput"] == 1.0


def test_clock_skew_trace_fires_the_note_and_invents_no_zombie_work():
    result = report("clock-skew.json")

    assert "clock_skew" in {n["kind"] for n in result["notes"]}
    assert result["summary"]["goodput"] == 1.0  # the call succeeded; the earlier start is two clocks, not a zombie
    assert result["summary"]["zombie_s"] == 0.0


def test_every_sample_trace_parses_into_exactly_one_user_request():
    for filename in ("deep-chain.json", "fanout.json", "queued.json", "hedged.json", "clock-skew.json"):
        assert report(filename)["summary"]["user_requests"] == 1, filename