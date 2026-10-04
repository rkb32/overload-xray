"""Turn the numbers into typed findings and the fix each one points to.

The rules are plain thresholds on purpose: they are cheap, explainable and testable. Each label maps to a fix
the load test measured (see README): the labels are not a guess, they say which step of the ladder is missing.
"""
from dataclasses import dataclass

from xray.analyze import EdgeStats, Report
from xray.retries import BUDGET_SHARE, RetryReport

HEALTHY = "healthy"
RETRY_STORM = "retry_storm"
ZOMBIE_WORK = "zombie_work"
DOOMED_WORK = "doomed_work"
DEGRADED = "degraded"
RETRY_LAYERS = "retry_layers"
RETRIED_WRITES = "retried_writes"
RETRY_BUDGET = "retry_budget"
RETRIED_CLIENT_ERRORS = "retried_client_errors"

MIN_CALLS_FOR_BUDGET = 20  # a handful of calls says nothing about a share of traffic

HEALTHY_GOODPUT = 0.9
LOW_GOODPUT = 0.5
STORM_REACH = 2.0  # calls per user request
ZOMBIE_SHARE = 0.15  # share of the callee's running time spent after the caller left
SMALL_SHARE = 0.05


@dataclass(frozen=True)
class Finding:
    edge: str
    label: str
    evidence: str
    advice: str


def diagnose_edge(edge: EdgeStats) -> list[Finding]:
    name = f"{edge.caller} -> {edge.callee}"
    if edge.work_ns == 0 or edge.goodput >= HEALTHY_GOODPUT:
        return [Finding(name, HEALTHY, f"goodput {edge.goodput:.0%}", "nothing to fix on this edge")]

    tail_share = edge.tail_ns / edge.work_ns
    found = []
    if edge.reach >= STORM_REACH and edge.goodput < LOW_GOODPUT:
        found.append(Finding(
            name, RETRY_STORM,
            f"{edge.reach:.1f} calls per user request and only {edge.goodput:.0%} of the work was used",
            "cap retries with a retry budget (about 10% of traffic), back off with jitter, and stop retrying once "
            "the deadline has passed",
        ))
    if tail_share >= ZOMBIE_SHARE:
        found.append(Finding(
            name, ZOMBIE_WORK,
            f"{tail_share:.0%} of the callee's running time happened after the caller had left",
            "send the caller's remaining time to the callee and cancel the work when it passes (deadline propagation)",
        ))
    if edge.goodput < LOW_GOODPUT and tail_share < SMALL_SHARE:
        found.append(Finding(
            name, DOOMED_WORK,
            f"goodput is {edge.goodput:.0%} although almost nothing runs after the caller leaves",
            "jobs start with too little time left to finish: refuse them up front (load shedding or admission "
            "control) so the capacity goes to jobs that can still succeed",
        ))
    return found or [Finding(name, DEGRADED, f"goodput {edge.goodput:.0%} with no single dominant pattern",
                             "look at the slowest calls and the error mix on this edge")]


def diagnose(report: Report) -> list[Finding]:
    return [finding for edge in report.edges for finding in diagnose_edge(edge)]


def diagnose_retries(report: RetryReport) -> list[Finding]:
    """Findings from the retry map. Each one says what was seen and how it was decided, never more than that."""
    found = []
    if report.worst and report.worst.layers >= 2:
        stack = ", then ".join(f"{caller} -> {callee} x{attempts}" for caller, callee, attempts in report.worst.steps)
        found.append(Finding(
            report.worst.steps[-1][0] + " -> " + report.worst.steps[-1][1], RETRY_LAYERS,
            f"{report.worst.layers} layers retried ({stack}): the last service received {report.worst.multiplication} "
            "calls for one call to the first",
            "let one layer own the retry, the one closest to the failure, and make the others fail fast: a retry from a "
            "layer that did not cause the failure only multiplies it",
        ))
    for write in report.writes:
        found.append(Finding(
            write.edge, RETRIED_WRITES,
            f"{write.method} {write.target} was attempted up to {write.max_attempts} times in {write.calls} call(s)",
            "check that this write is idempotent (an idempotency key, or a duplicate check on the server) or stop "
            "retrying it: a retried write that already took effect happens twice",
        ))
    for edge in report.edges:
        name = f"{edge.caller} -> {edge.callee}"
        if edge.logical_calls >= MIN_CALLS_FOR_BUDGET and edge.retry_share > BUDGET_SHARE:
            found.append(Finding(
                name, RETRY_BUDGET,
                f"{edge.retry_share:.0%} of attempts were retries ({edge.retries} of {edge.attempts}); "
                f"{edge.exhausted} retried call(s) never succeeded",
                f"cap retries with a budget (about {BUDGET_SHARE:.0%} of traffic) so a failing dependency gets fewer "
                "calls, not more",
            ))
        if edge.client_errors_retried:
            found.append(Finding(
                name, RETRIED_CLIENT_ERRORS,
                f"{edge.client_errors_retried} call(s) were retried after the server said the request itself was wrong "
                "(a 4xx other than 408 and 429)",
                "do not retry these: asking again cannot change the answer",
            ))
    return found


def render_findings(findings: list[Finding]) -> str:
    if not findings:
        return "no calls between services were found in these spans"
    return "\n\n".join(f"[{f.label}] {f.edge}\n  evidence: {f.evidence}\n  fix:      {f.advice}" for f in findings)
