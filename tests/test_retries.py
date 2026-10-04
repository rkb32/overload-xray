"""What counts as a retry, and what a stack of retries multiplies.

A retry is not a span kind. It is a pattern, so every rule that decides "this was a retry" is pinned down here:
the same job repeated the same call after the previous attempt FAILED. Loops of successful calls, overlapping
duplicates and calls to different places are not retries."""
import threading

import pytest

from xray.retries import retry_map
from xray.spans import parse_text

S = 1_000_000_000


def span(span_id, parent_id, service, kind, start_s, end_s, error=False, name=None, trace="t1", **extra):
    from xray.spans import Span

    return Span(trace, span_id, parent_id, service, name or f"{service}-{kind}", kind,
                round(start_s * S), round(end_s * S), error, **extra)


def edge(report, caller, callee):
    return next(e for e in report.edges if (e.caller, e.callee) == (caller, callee))


def finishes_within(seconds, function):
    outcome = {}
    thread = threading.Thread(target=lambda: outcome.update(value=function()), daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), f"did not finish within {seconds}s"
    return outcome["value"]


# The README example: A calls B, gives up at 1s, B keeps working, and A's second attempt succeeds.
ROOT = span("a", None, "A", "SERVER", 0, 6)
EXAMPLE = [
    ROOT,
    span("c1", "a", "A", "CLIENT", 0, 1, error=True), span("b1", "c1", "B", "SERVER", 0, 5),
    span("c2", "a", "A", "CLIENT", 1, 6), span("b2", "c2", "B", "SERVER", 1, 6),
]


def test_a_failed_call_followed_by_the_same_call_is_a_retry():
    found = edge(retry_map(EXAMPLE), "A", "B")

    assert (found.logical_calls, found.attempts, found.retries) == (1, 2, 1)
    assert (found.recovered, found.exhausted) == (1, 0)  # the retry rescued the call
    assert found.attempts_per_call == 2.0 and found.retry_share == 0.5


def test_a_call_whose_every_attempt_failed_is_exhausted():
    spans = [ROOT]
    for i in range(3):
        spans += [span(f"c{i}", "a", "A", "CLIENT", i * 1.1, i * 1.1 + 1, error=True), span(f"b{i}", f"c{i}", "B", "SERVER", i * 1.1, i * 1.1 + 1)]

    found = edge(retry_map(spans), "A", "B")

    assert (found.logical_calls, found.attempts, found.retries) == (1, 3, 2)
    assert (found.recovered, found.exhausted) == (0, 1)


def test_repeating_a_call_that_keeps_succeeding_is_fan_out_not_retries():
    spans = [ROOT]
    for i in range(3):
        spans += [span(f"c{i}", "a", "A", "CLIENT", i, i + 1), span(f"b{i}", f"c{i}", "B", "SERVER", i, i + 1)]

    found = edge(retry_map(spans), "A", "B")

    assert (found.logical_calls, found.attempts, found.retries) == (3, 3, 0)


def test_overlapping_duplicates_are_hedging_not_retries():
    # The second request started while the first was still running, so it cannot be a reaction to its failure.
    spans = [ROOT, span("c1", "a", "A", "CLIENT", 0, 2, error=True), span("b1", "c1", "B", "SERVER", 0, 2),
             span("c2", "a", "A", "CLIENT", 1, 3), span("b2", "c2", "B", "SERVER", 1, 3)]

    found = edge(retry_map(spans), "A", "B")

    assert (found.logical_calls, found.retries) == (2, 0)


def test_calls_to_different_services_are_never_grouped_even_if_the_first_failed():
    spans = [ROOT,
             span("c1", "a", "A", "CLIENT", 0, 1, error=True, name="GET"), span("b1", "c1", "B", "SERVER", 0, 1),
             span("c2", "a", "A", "CLIENT", 1, 2, name="GET"), span("k1", "c2", "C", "SERVER", 1, 2)]

    report = retry_map(spans)

    assert edge(report, "A", "B").retries == 0 and edge(report, "A", "C").retries == 0


def test_the_same_host_but_a_different_path_is_a_different_call():
    first = span("c1", "a", "A", "CLIENT", 0, 1, error=True, name="GET", target="inv:80/items/2")
    second = span("c2", "a", "A", "CLIENT", 1, 2, name="GET", target="inv:80/items/3")

    found = edge(retry_map([ROOT, first, span("b1", "c1", "B", "SERVER", 0, 1), second,
                            span("b2", "c2", "B", "SERVER", 1, 2)]), "A", "B")

    assert (found.logical_calls, found.retries) == (2, 0)


def test_the_same_target_again_after_a_failure_is_a_retry():
    first = span("c1", "a", "A", "CLIENT", 0, 1, error=True, name="GET", target="inv:80/items/2")
    second = span("c2", "a", "A", "CLIENT", 1, 2, name="GET", target="inv:80/items/2")

    found = edge(retry_map([ROOT, first, span("b1", "c1", "B", "SERVER", 0, 1), second,
                            span("b2", "c2", "B", "SERVER", 1, 2)]), "A", "B")

    assert (found.logical_calls, found.retries) == (1, 1)


def test_without_a_target_the_callees_endpoint_tells_two_calls_apart():
    # Same service, no URL on the client span: "GET /a" then "GET /b" are different calls; "GET /a" twice is a retry.
    def calls(second_endpoint):
        return [ROOT, span("c1", "a", "A", "CLIENT", 0, 1, error=True, name="GET"),
                span("b1", "c1", "B", "SERVER", 0, 1, name="GET /a"),
                span("c2", "a", "A", "CLIENT", 1, 2, name="GET"),
                span("b2", "c2", "B", "SERVER", 1, 2, name=second_endpoint)]

    assert edge(retry_map(calls("GET /b")), "A", "B").retries == 0
    assert edge(retry_map(calls("GET /a")), "A", "B").retries == 1


@pytest.mark.parametrize("status", [500, 503, 408, 429])
def test_an_error_reply_counts_as_a_failed_attempt_even_without_an_error_status(status):
    spans = [ROOT, span("c1", "a", "A", "CLIENT", 0, 1, http_status=status), span("b1", "c1", "B", "SERVER", 0, 1),
             span("c2", "a", "A", "CLIENT", 1, 2, http_status=200), span("b2", "c2", "B", "SERVER", 1, 2)]

    assert edge(retry_map(spans), "A", "B").retries == 1


@pytest.mark.parametrize("status", [200, 301, 400, 404])
def test_a_reply_that_is_not_a_retryable_failure_does_not_start_a_retry(status):
    spans = [ROOT, span("c1", "a", "A", "CLIENT", 0, 1, http_status=status), span("b1", "c1", "B", "SERVER", 0, 1),
             span("c2", "a", "A", "CLIENT", 1, 2), span("b2", "c2", "B", "SERVER", 1, 2)]

    assert edge(retry_map(spans), "A", "B").retries == 0


def test_retrying_a_request_the_server_called_wrong_is_flagged():
    # A 404 cannot get better by asking again. The attempt has an error status, so the next one continues the chain.
    spans = [ROOT, span("c1", "a", "A", "CLIENT", 0, 1, error=True, http_status=404), span("b1", "c1", "B", "SERVER", 0, 1),
             span("c2", "a", "A", "CLIENT", 1, 2, error=True, http_status=404), span("b2", "c2", "B", "SERVER", 1, 2)]

    found = edge(retry_map(spans), "A", "B")

    assert found.retries == 1 and found.client_errors_retried == 1


def test_a_resend_count_from_the_client_library_marks_a_retry_and_removes_the_guesswork():
    spans = [ROOT, span("c1", "a", "A", "CLIENT", 0, 1), span("b1", "c1", "B", "SERVER", 0, 1),
             span("c2", "a", "A", "CLIENT", 1, 2, resend_count=1), span("b2", "c2", "B", "SERVER", 1, 2)]

    found = edge(retry_map(spans), "A", "B")

    assert found.retries == 1 and found.inferred is False
    assert edge(retry_map(EXAMPLE), "A", "B").inferred is True  # no resend count: xray had to infer it


def test_calls_with_no_target_and_no_callee_cannot_be_grouped_and_are_counted_as_such():
    spans = [ROOT, span("c1", "a", "A", "CLIENT", 0, 1, error=True, name="GET"),
             span("c2", "a", "A", "CLIENT", 1, 2, name="GET")]  # nobody answered either call: where did they go?

    report = retry_map(spans)

    assert report.edges == [] and report.unattributed == 2


def layered_trace(trace="t1", c_status=None):
    """A calls B three times (2s timeouts). Every B job calls C three times (0.7s timeouts). C is slow."""
    spans = [span("root", None, "A", "SERVER", 0, 7, trace=trace)]
    for k in range(3):
        start = 2.1 * k
        spans += [span(f"ab{k}", "root", "A", "CLIENT", start, start + 2, error=True, trace=trace),
                  span(f"job{k}", f"ab{k}", "B", "SERVER", start + 0.01, start + 2.2, trace=trace)]
        for j in range(3):
            begin = start + 0.01 + 0.7 * j
            spans += [span(f"bc{k}{j}", f"job{k}", "B", "CLIENT", begin, begin + 0.7, error=True, trace=trace),
                      span(f"c{k}{j}", f"bc{k}{j}", "C", "SERVER", begin, begin + 3, trace=trace)]
    return spans


def test_two_layers_of_retries_multiply():
    report = retry_map(layered_trace())

    assert (edge(report, "A", "B").logical_calls, edge(report, "A", "B").attempts) == (1, 3)
    below = edge(report, "B", "C")
    assert (below.logical_calls, below.attempts, below.retries, below.exhausted) == (3, 9, 6, 3)
    assert report.worst.layers == 2 and report.worst.multiplication == 9
    assert report.worst.steps == [("A", "B", 3), ("B", "C", 3)]


def test_one_layer_of_retries_above_a_single_call_still_multiplies_the_load_below():
    spans = [span("root", None, "A", "SERVER", 0, 7)]
    for k in range(3):  # A retries B three times; each B job calls C exactly once
        start = 2.1 * k
        spans += [span(f"ab{k}", "root", "A", "CLIENT", start, start + 2, error=True),
                  span(f"job{k}", f"ab{k}", "B", "SERVER", start, start + 2.2),
                  span(f"bc{k}", f"job{k}", "B", "CLIENT", start, start + 1), span(f"c{k}", f"bc{k}", "C", "SERVER", start, start + 1)]

    report = retry_map(spans)

    assert edge(report, "B", "C").retries == 0
    assert report.worst.layers == 1 and report.worst.multiplication == 3  # C was called 3 times for one call to B
    assert report.worst.steps == [("A", "B", 3), ("B", "C", 1)]


def test_there_is_no_worst_path_when_nothing_was_retried():
    spans = [ROOT, span("c1", "a", "A", "CLIENT", 0, 1), span("b1", "c1", "B", "SERVER", 0, 1)]

    report = retry_map(spans)

    assert report.worst is None and report.retries == 0 and report.retry_share == 0.0


def test_retried_writes_are_listed_and_retried_reads_are_not():
    def chain(method, name, target):
        return [ROOT, span("c1", "a", "A", "CLIENT", 0, 1, error=True, name=name, method=method, target=target),
                span("b1", "c1", "B", "SERVER", 0, 1),
                span("c2", "a", "A", "CLIENT", 1, 2, name=name, method=method, target=target), span("b2", "c2", "B", "SERVER", 1, 2)]

    writes = retry_map(chain("POST", "POST", "billing/users/12345/orders/9f1c2d3e-aaaa-bbbb-cccc-1234567890ab")).writes

    assert [(w.method, w.edge, w.calls, w.max_attempts) for w in writes] == [("POST", "A -> B", 1, 2)]
    assert writes[0].target == "billing/users/{id}/orders/{id}"  # ids are replaced so the list reads as a route
    assert retry_map(chain("GET", "GET", "billing/users/1")).writes == []


def test_the_method_can_come_from_the_span_name_when_no_attribute_has_it():
    spans = [ROOT, span("c1", "a", "A", "CLIENT", 0, 1, error=True, name="POST /charge"), span("b1", "c1", "B", "SERVER", 0, 1),
             span("c2", "a", "A", "CLIENT", 1, 2, name="POST /charge"), span("b2", "c2", "B", "SERVER", 1, 2)]

    assert [w.method for w in retry_map(spans).writes] == ["POST"]


def test_the_bundled_retry_storm_sample_is_three_attempts_at_one_layer():
    with open("xray/samples/retry-storm.json", encoding="utf-8") as handle:
        spans = parse_text(handle.read())

    report = retry_map(spans)
    found = edge(report, "service-a", "service-b")

    assert (found.logical_calls, found.attempts, found.retries, found.exhausted) == (1, 3, 2, 1)
    assert report.worst.layers == 1 and report.worst.steps == [("service-a", "service-b", 3)]
    assert report.writes == []  # GET


# --- uploads are untrusted ---------------------------------------------------------------------------------------

def test_spans_that_are_each_others_parents_do_not_hang_the_retry_map():
    spans = [
        span("i1", "i2", "A", "INTERNAL", 0, 1), span("i2", "i1", "A", "INTERNAL", 0, 1),
        span("c1", "i1", "A", "CLIENT", 0, 1, error=True), span("s1", "c1", "B", "SERVER", 0, 1),
        span("c2", "i1", "A", "CLIENT", 1, 2), span("s2", "c2", "B", "SERVER", 1, 2),
    ]

    report = finishes_within(5, lambda: retry_map(spans))

    assert edge(report, "A", "B").attempts == 2


def test_calls_that_were_made_from_inside_each_other_do_not_hang_the_layer_count():
    spans = [
        span("c1", "s2", "A", "CLIENT", 0, 1, error=True), span("c1b", "s2", "A", "CLIENT", 1, 2),
        span("s1", "c1", "B", "SERVER", 0, 1), span("s1b", "c1b", "B", "SERVER", 1, 2),
        span("c2", "s1", "B", "CLIENT", 0, 1, error=True), span("c2b", "s1", "B", "CLIENT", 1, 2),
        span("s2", "c2", "C", "SERVER", 0, 1), span("s2b", "c2b", "C", "SERVER", 1, 2),
    ]

    finishes_within(5, lambda: retry_map(spans))


def test_a_very_deep_stack_of_retries_does_not_hit_the_recursion_limit():
    spans, parent = [], None
    for i in range(5_000):  # every level retries once, and only the second attempt calls the next level down
        spans += [span(f"fail{i}", parent, "A", "CLIENT", 0, 1, error=True), span(f"sf{i}", f"fail{i}", "B", "SERVER", 0, 1),
                  span(f"ok{i}", parent, "A", "CLIENT", 1, 2), span(f"so{i}", f"ok{i}", "B", "SERVER", 1, 2)]
        parent = f"so{i}"

    report = finishes_within(30, lambda: retry_map(spans))

    assert report.worst.layers == 5_000
