"""The product layer: turn uploaded trace text into the JSON report the web page shows.

Everything happens in memory. Nothing is written to disk and nothing from the upload is logged.
Uploads come from strangers, so every limit here exists to keep one bad request from hurting the next.
"""
import os
import re

from xray.analyze import DEFAULT_TOLERANCE_NS, Report, analyze
from xray.diagnose import HEALTHY_GOODPUT, diagnose, diagnose_retries
from xray.retries import BUDGET_SHARE, RetryReport, retry_map
from xray.spans import Span, TooManySpans, TraceFormatError, dedupe, parse_text

MAX_NAME_CHARS = 80  # names in the retry map come from the upload (a host in a URL, a service name): keep them short

# Limits can be tightened per deployment: AWS Lambda, for example, cannot take a request body above 6 MB.
MAX_REQUEST_BYTES = int(float(os.environ.get("XRAY_MAX_REQUEST_MB", "15")) * 1024 * 1024)  # the whole request body
MAX_FILES = int(os.environ.get("XRAY_MAX_FILES", "10"))
MAX_SPANS = int(os.environ.get("XRAY_MAX_SPANS", "100000"))
MIN_REQUESTS_FOR_RATIOS = 3


class UploadError(Exception):
    """A problem with what was uploaded. `message` is safe to show: it never contains the uploaded text."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _display_name(name: object) -> str:
    """A file name goes into error messages, so keep it short and free of control characters."""
    text = os.path.basename(str(name or "upload"))
    text = re.sub(r"[^\w.\- ]", "_", text)[:60]
    return text or "upload"


def _seconds(ns: int) -> float:
    return round(ns / 1e9, 3)


def _headline(report: Report) -> str:
    if not report.edges:
        return "No calls between services were found in these traces."
    if report.goodput >= HEALTHY_GOODPUT:
        return f"Nothing wasteful found: {report.goodput:.0%} of the work your services did was used."
    return f"{1 - report.goodput:.0%} of the work your services did for these requests was never used."


def _short(text: object) -> str:
    return str(text)[:MAX_NAME_CHARS]


def _retries_json(retries: RetryReport) -> dict:
    """The retry map as plain data. Every name is cut short: some of them come straight out of a URL in the upload."""
    worst = retries.worst
    return {
        "totals": {"logical_calls": retries.logical_calls, "attempts": retries.attempts, "retries": retries.retries,
                   "retry_share": round(retries.retry_share, 4), "budget_share": BUDGET_SHARE},
        "edges": [
            {"caller": _short(e.caller), "callee": _short(e.callee), "calls": e.logical_calls, "attempts": e.attempts,
             "retries": e.retries, "per_call": round(e.attempts_per_call, 2), "retry_share": round(e.retry_share, 4),
             "recovered": e.recovered, "exhausted": e.exhausted, "writes": e.writes_retried,
             "client_errors": e.client_errors_retried, "inferred": e.inferred}
            for e in retries.edges
        ],
        "worst": None if worst is None else {
            "layers": worst.layers, "multiplication": worst.multiplication,
            "steps": [{"caller": _short(c), "callee": _short(d), "attempts": n} for c, d, n in worst.steps],
        },
        "writes": [{"method": w.method, "edge": _short(w.edge), "target": _short(w.target), "calls": w.calls,
                    "max_attempts": w.max_attempts} for w in retries.writes],
        "unattributed": retries.unattributed,
    }


def _notes(report: Report, spans: list[Span], retries: RetryReport) -> list[dict]:
    """What xray could not see or could not be sure of. Showing this is part of being trusted."""
    notes = []
    if report.dependency_calls == 0:
        notes.append({
            "kind": "no_calls",
            "text": "xray needs a CLIENT span in the caller and a SERVER span in the callee, linked by trace context "
                    "(the traceparent header). Check that both services are instrumented and send to the same place.",
        })
        return notes
    if report.user_requests < MIN_REQUESTS_FOR_RATIOS:
        notes.append({
            "kind": "small_sample",
            "text": f"Only {report.user_requests} user request(s) in this sample. The numbers are exact for it, "
                    "but too few requests to say what is typical.",
        })
    if report.unclassified:
        notes.append({
            "kind": "unclassified",
            "text": f"{report.unclassified} caller errors could not be classified as 'caller gave up' or 'callee replied "
                    "with an error'. They were counted as 'gave up', so the zombie numbers may be overstated.",
        })
    if retries.retries and any(e.inferred for e in retries.edges):
        notes.append({
            "kind": "retries_inferred",
            "text": "These retries were recognised, not read: the same job called the same URL again after a failure. Your "
                    "client libraries do not mark retries (http.request.resend_count), so a loop that deliberately repeats "
                    "a failing call would look the same.",
        })
    if retries.unattributed:
        notes.append({
            "kind": "ungrouped_calls",
            "text": f"{retries.unattributed} client calls had neither a URL nor a callee span, so retries among them could "
                    "not be seen.",
        })
    if not any(s.queue_ns for s in spans if s.kind == "SERVER"):
        notes.append({
            "kind": "queue_wait",
            "text": "No span reports queue wait (attribute app.queue_wait_ms). If your services queue requests before "
                    "running them, waiting is being counted as work.",
        })
    by_id = {s.span_id: s for s in spans}
    skewed = sum(
        1 for s in spans
        if s.kind == "SERVER" and s.parent_id in by_id and by_id[s.parent_id].kind == "CLIENT"
        and s.start_ns + DEFAULT_TOLERANCE_NS < by_id[s.parent_id].start_ns
    )
    if skewed:
        notes.append({
            "kind": "clock_skew",
            "text": f"{skewed} calls have a callee that started before its caller did: the machines' clocks probably "
                    "disagree, which can hide or invent zombie work.",
        })
    return notes


def analyze_upload(files: list[tuple[str, str]]) -> dict:
    """files: (name, text) pairs. Returns the report as plain JSON-able data, or raises UploadError."""
    if not files:
        raise UploadError(400, "No files were sent.")
    if len(files) > MAX_FILES:
        raise UploadError(400, f"At most {MAX_FILES} files at a time.")

    spans: list[Span] = []
    total_bytes = 0
    for name, text in files:
        total_bytes += len(text.encode("utf-8", errors="ignore"))
        try:
            spans.extend(parse_text(text, source=_display_name(name), max_spans=MAX_SPANS - len(spans)))
        except TooManySpans as exc:
            raise UploadError(413, str(exc)) from None  # "too large", not "malformed"
        except TraceFormatError as exc:
            raise UploadError(422, str(exc)) from None
        if len(spans) > MAX_SPANS:
            raise UploadError(413, f"More than {MAX_SPANS:,} spans in total; send a smaller time window.")

    spans = dedupe(spans)
    report = analyze(spans)
    retries = retry_map(spans)
    return {
        "summary": {
            "headline": _headline(report),
            "user_requests": report.user_requests,
            "dependency_calls": report.dependency_calls,
            "amplification": round(report.amplification, 2),
            "work_s": _seconds(report.total_work_ns),
            "used_s": _seconds(report.used_work_ns),
            "goodput": round(report.goodput, 4),
            "zombie_s": _seconds(report.zombie_work_ns),
            "tail_s": _seconds(report.tail_ns),
        },
        "edges": [
            {
                "caller": e.caller, "callee": e.callee, "calls": e.calls,
                "fanout": round(e.fanout, 2), "reach": round(e.reach, 2),
                "work_s": _seconds(e.work_ns), "used_s": _seconds(e.used_ns),
                "zombie_s": _seconds(e.zombie_ns), "tail_s": _seconds(e.tail_ns),
                "goodput": round(e.goodput, 4),
            }
            for e in report.edges
        ],
        "findings": [
            {"edge": f.edge, "label": f.label, "evidence": f.evidence, "advice": f.advice}
            for f in diagnose(report) + diagnose_retries(retries)
        ],
        "retries": _retries_json(retries),
        "notes": _notes(report, spans, retries),
        "input": {"files": len(files), "spans": len(spans), "bytes": total_bytes},
    }
