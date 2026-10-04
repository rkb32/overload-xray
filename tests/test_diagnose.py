from xray.analyze import EdgeStats, Report
from xray.diagnose import DEGRADED, DOOMED_WORK, HEALTHY, RETRY_STORM, ZOMBIE_WORK, diagnose, render_findings

S = 1_000_000_000


def edge(**overrides) -> EdgeStats:
    values = dict(caller="A", callee="B", calls=3, fanout=3.0, reach=3.0,
                  work_ns=9 * S, used_ns=0, zombie_ns=9 * S, tail_ns=6 * S)
    values.update(overrides)
    return EdgeStats(**values)


def labels(*edges) -> set[str]:
    return {f.label for f in diagnose(Report(user_requests=1, edges=list(edges)))}


def test_the_baseline_run_is_a_retry_storm_full_of_zombie_work():
    assert labels(edge()) == {RETRY_STORM, ZOMBIE_WORK}


def test_cancelling_at_the_deadline_leaves_doomed_work_behind():
    # zombie tail is gone but goodput is still 0%: the run from the load test's middle step
    cancel_only = edge(work_ns=3 * S, used_ns=0, zombie_ns=0, tail_ns=0)

    assert labels(cancel_only) == {RETRY_STORM, DOOMED_WORK}


def test_a_healthy_edge_gets_a_single_healthy_finding():
    assert labels(edge(reach=1.0, fanout=1.0, calls=1, work_ns=S, used_ns=S, zombie_ns=0, tail_ns=0)) == {HEALTHY}


def test_an_edge_without_work_is_healthy_not_a_division_by_zero():
    assert labels(edge(work_ns=0, used_ns=0, zombie_ns=0, tail_ns=0)) == {HEALTHY}


def test_middling_goodput_without_a_pattern_is_degraded():
    assert labels(edge(reach=1.0, fanout=1.0, calls=1, work_ns=10 * S, used_ns=7 * S, zombie_ns=S, tail_ns=0)) == {DEGRADED}


def test_findings_render_with_their_fix():
    text = diagnose_text = render_findings(diagnose(Report(user_requests=1, edges=[edge()])))

    assert "[retry_storm] A -> B" in text and "fix:" in diagnose_text


def test_no_edges_renders_a_clear_message():
    assert "no calls" in render_findings([])


# --- findings from the retry map -------------------------------------------------------------------------------------

from xray.diagnose import (  # noqa: E402
    RETRIED_CLIENT_ERRORS, RETRIED_WRITES, RETRY_BUDGET, RETRY_LAYERS, diagnose_retries,
)
from xray.retries import EdgeRetries, RetryPath, RetryReport, WriteRetry  # noqa: E402


def retry_labels(report) -> set[str]:
    return {f.label for f in diagnose_retries(report)}


def retried_edge(**overrides) -> EdgeRetries:
    values = dict(caller="A", callee="B", logical_calls=100, attempts=100)
    values.update(overrides)
    return EdgeRetries(**values)


def test_two_layers_of_retries_get_one_finding_that_names_the_stack_and_what_it_multiplied():
    stack = RetryPath(layers=2, multiplication=9, steps=[("A", "B", 3), ("B", "C", 3)])

    (finding,) = diagnose_retries(RetryReport(1, [retried_edge(logical_calls=1, attempts=3)], worst=stack))

    assert finding.label == RETRY_LAYERS
    assert "9 call" in finding.evidence and "A -> B x3" in finding.evidence and "B -> C x3" in finding.evidence
    assert "one layer" in finding.advice


def test_a_single_retried_layer_is_not_a_layering_finding():
    one = RetryPath(layers=1, multiplication=3, steps=[("A", "B", 3)])

    assert RETRY_LAYERS not in retry_labels(RetryReport(1, [retried_edge()], worst=one))


def test_a_retried_write_asks_whether_it_is_idempotent():
    report = RetryReport(1, [retried_edge()], writes=[WriteRetry("POST", "A -> B", "billing/charge", calls=2, max_attempts=3)])

    (finding,) = diagnose_retries(report)

    assert finding.label == RETRIED_WRITES and finding.edge == "A -> B"
    assert "POST billing/charge" in finding.evidence and "3 times" in finding.evidence
    assert "idempotent" in finding.advice


def test_a_retry_share_over_the_budget_is_flagged_only_when_there_is_enough_data():
    busy = retried_edge(logical_calls=100, attempts=140)  # 40 retries in 140 attempts: 29%
    tiny = retried_edge(logical_calls=3, attempts=5)

    assert RETRY_BUDGET in retry_labels(RetryReport(10, [busy]))
    assert RETRY_BUDGET not in retry_labels(RetryReport(1, [tiny]))  # three calls say nothing about a budget


def test_a_retry_share_within_the_budget_is_not_a_finding():
    calm = retried_edge(logical_calls=100, attempts=108)  # 7%

    assert retry_labels(RetryReport(10, [calm])) == set()


def test_retrying_what_the_server_called_wrong_is_flagged():
    report = RetryReport(1, [retried_edge(logical_calls=4, attempts=8, client_errors_retried=4)])

    (finding,) = diagnose_retries(report)

    assert finding.label == RETRIED_CLIENT_ERRORS
    assert "cannot change the answer" in finding.advice


def test_nothing_retried_means_no_retry_findings():
    assert diagnose_retries(RetryReport(1, [retried_edge()])) == []
