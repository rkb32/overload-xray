from xray.analyze import analyze, zombie_tail_ns
from xray.spans import Span

S = 1_000_000_000  # one second in nanoseconds


def span(span_id, parent_id, service, kind, start_s, end_s, error=False, queue_s=0.0, **extra):
    return Span(
        "t1", span_id, parent_id, service, f"{service}-{kind}", kind,
        round(start_s * S), round(end_s * S), error, round(queue_s * S), **extra,
    )


def edges(report):
    return {(e.caller, e.callee): e for e in report.edges}


# The README example: the caller times out at 1s, the callee needs 5s, and the retry succeeds.
ROOT = span("a", None, "A", "SERVER", 0, 6)
CALL1 = span("c1", "a", "A", "CLIENT", 0, 1, error=True)  # attempt 1: the caller gave up at 1s
WORK1 = span("b1", "c1", "B", "SERVER", 0, 5)  # ...but B kept working until 5s
CALL2 = span("c2", "a", "A", "CLIENT", 1, 6)  # attempt 2: the caller waited and got an answer
WORK2 = span("b2", "c2", "B", "SERVER", 1, 6)
EXAMPLE = [ROOT, CALL1, WORK1, CALL2, WORK2]


def test_amplification_counts_calls_per_user_request():
    report = analyze(EXAMPLE)
    assert (report.user_requests, report.dependency_calls) == (1, 2)
    assert report.amplification == 2.0


def test_zombie_tail_is_the_work_after_the_caller_left():
    assert zombie_tail_ns(CALL1, WORK1) == 4 * S


def test_answered_call_has_no_zombie_tail():
    assert zombie_tail_ns(CALL2, WORK2) == 0


def test_background_work_after_a_successful_call_is_not_zombie():
    # The caller got its answer at 2s; the callee then kept running a follow-up job until 9s.
    call = span("c3", "a", "A", "CLIENT", 0, 2)
    work = span("b3", "c3", "B", "SERVER", 0, 9)
    assert zombie_tail_ns(call, work) == 0


def test_callee_that_finished_before_the_caller_gave_up_has_no_tail():
    call = span("c4", "a", "A", "CLIENT", 0, 3, error=True)  # the call failed for another reason
    work = span("b4", "c4", "B", "SERVER", 0, 1)
    assert zombie_tail_ns(call, work) == 0


def test_clock_skew_smaller_than_the_tolerance_is_ignored():
    call = span("c5", "a", "A", "CLIENT", 0, 1, error=True)
    work = span("b5", "c5", "B", "SERVER", 0, 1.002)  # 2 ms: two machines' clocks disagreeing
    assert zombie_tail_ns(call, work) == 0


def test_a_callee_that_replied_with_an_error_and_then_cleaned_up_is_not_zombie_work():
    # B answered 503 at 1s, so A did not give up. B kept tidying until 3s. Without the HTTP status this looked like a tail.
    call = span("c8", "a", "A", "CLIENT", 0, 1, error=True, http_status=503)
    work = span("b8", "c8", "B", "SERVER", 0, 3)
    assert zombie_tail_ns(call, work) == 0


def test_a_timeout_is_still_zombie_work():
    call = span("c9", "a", "A", "CLIENT", 0, 1, error=True, error_text="ReadTimeout: ")
    work = span("b9", "c9", "B", "SERVER", 0, 3)
    assert zombie_tail_ns(call, work) == 2 * S


def test_errors_nobody_can_classify_count_as_gave_up_and_are_tallied():
    root = span("a", None, "A", "SERVER", 0, 1)
    call = span("c10", "a", "A", "CLIENT", 0, 1, error=True, error_text="pricing engine says no (code 9031)")
    work = span("b10", "c10", "B", "SERVER", 0, 3)

    report = analyze([root, call, work])

    assert report.tail_ns == 2 * S  # the old assumption: an error means the caller left
    assert report.unclassified == 1
    assert "could not be classified" in report.render()


def test_a_model_can_settle_an_unclassifiable_error():
    root = span("a", None, "A", "SERVER", 0, 1)
    call = span("c11", "a", "A", "CLIENT", 0, 1, error=True, error_text="pricing engine says no (code 9031)")
    work = span("b11", "c11", "B", "SERVER", 0, 3)

    report = analyze([root, call, work], ask=lambda text: "failed")  # the model says the callee rejected it

    assert report.tail_ns == 0 and report.unclassified == 0


def test_time_spent_waiting_in_a_queue_is_not_zombie_tail():
    # The caller left at 1s. The job sat in B's queue until 4.5s and only ran for the last 0.5s.
    call = span("c6", "a", "A", "CLIENT", 0, 1, error=True)
    work = span("b6", "c6", "B", "SERVER", 0, 5, queue_s=4.5)
    assert zombie_tail_ns(call, work) == round(0.5 * S)


def test_work_counts_running_time_not_queue_time():
    root = span("a", None, "A", "SERVER", 0, 5)
    call = span("c7", "a", "A", "CLIENT", 0, 5)
    work = span("b7", "c7", "B", "SERVER", 0, 5, queue_s=4.0)  # waited 4s, ran 1s
    (edge,) = analyze([root, call, work]).edges
    assert edge.work_ns == 1 * S


def test_goodput_for_the_readme_example():
    report = analyze(EXAMPLE)
    assert report.total_work_ns == 10 * S
    assert report.used_work_ns == 5 * S
    assert report.zombie_work_ns == 5 * S
    assert report.tail_ns == 4 * S
    assert report.goodput == 0.5


def test_waste_flows_downstream_in_a_chain():
    # A gives up on B at 1s. B (unaware) keeps going; it had called C, which answered B just fine.
    root = span("a", None, "A", "SERVER", 0, 1)
    a_to_b = span("c1", "a", "A", "CLIENT", 0, 1, error=True)
    b = span("b1", "c1", "B", "SERVER", 0, 5)
    b_to_c = span("k1", "b1", "B", "CLIENT", 0.5, 4.5)
    c = span("s1", "k1", "C", "SERVER", 0.6, 4.4)
    found = edges(analyze([root, a_to_b, b, b_to_c, c]))
    assert found[("A", "B")].tail_ns == 4 * S
    assert found[("B", "C")].zombie_ns == round(3.8 * S)  # C's job is wasted although B was happy with it
    assert found[("B", "C")].goodput == 0.0


def finishes_within(seconds, function):
    """Run `function` in a thread; fail the test (instead of freezing the suite) if it does not return."""
    import threading

    outcome = {}
    thread = threading.Thread(target=lambda: outcome.update(value=function()), daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), f"did not finish within {seconds}s"
    return outcome["value"]


def test_ancestor_spans_that_point_at_each_other_do_not_hang_the_analyzer():
    # Uploaded traces are untrusted. Two INTERNAL spans that are each other's parent used to loop forever.
    spans = [
        span("i1", "i2", "A", "INTERNAL", 0, 1),
        span("i2", "i1", "A", "INTERNAL", 0, 1),
        span("c1", "i1", "A", "CLIENT", 0, 1),
        span("s1", "c1", "B", "SERVER", 0, 1),
    ]

    report = finishes_within(5, lambda: analyze(spans))

    assert report.dependency_calls == 1


def test_a_very_deep_call_chain_is_analyzed_without_hitting_the_recursion_limit():
    spans = []
    for i in range(20_000):
        spans.append(span(f"c{i}", f"s{i - 1}" if i else None, "A", "CLIENT", 0, 1))
        spans.append(span(f"s{i}", f"c{i}", "B", "SERVER", 0, 1))

    report = finishes_within(20, lambda: analyze(spans))

    assert report.dependency_calls == 20_000


def test_a_job_whose_callers_form_a_loop_gets_a_verdict():
    # s1 was called from inside s2, and s2 was called from inside s1: nonsense, but it must not crash.
    spans = [
        span("c1", "s2", "A", "CLIENT", 0, 1), span("s1", "c1", "B", "SERVER", 0, 1),
        span("c2", "s1", "B", "CLIENT", 0, 1), span("s2", "c2", "C", "SERVER", 0, 1),
    ]

    report = finishes_within(5, lambda: analyze(spans))

    assert report.dependency_calls == 2


def test_fanout_is_per_hop_and_reach_is_per_user_request():
    # Retry storm shape: A tries B three times; each time B calls C once.
    spans = [span("a", None, "A", "SERVER", 0, 3)]
    for i, (start, b_end, c_end) in enumerate([(0, 2.5, 2.3), (1, 3.5, 3.3), (2, 4.0, 3.8)], start=1):
        spans += [
            span(f"c{i}", "a", "A", "CLIENT", start, start + 1, error=True),
            span(f"b{i}", f"c{i}", "B", "SERVER", start, b_end),
            span(f"k{i}", f"b{i}", "B", "CLIENT", start + 0.1, b_end - 0.1),
            span(f"s{i}", f"k{i}", "C", "SERVER", start + 0.2, c_end),
        ]
    found = edges(analyze(spans))
    assert (found[("A", "B")].calls, found[("A", "B")].fanout, found[("A", "B")].reach) == (3, 3.0, 3.0)
    assert (found[("B", "C")].calls, found[("B", "C")].fanout, found[("B", "C")].reach) == (3, 1.0, 3.0)
