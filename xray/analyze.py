from collections import Counter, defaultdict
from dataclasses import dataclass, field

from xray.classify import FAILED, UNKNOWN, Asker, error_kind
from xray.spans import Span

# Real traces span machines whose clocks disagree by a few milliseconds.
DEFAULT_TOLERANCE_NS = 5_000_000


def caller_gave_up(parent: Span, ask: Asker | None = None) -> bool:
    """Did the caller stop waiting? An error alone is not enough: the callee may have replied with an error.
    Error text nobody can classify counts as "gave up" (the old assumption) and is tallied in Report.unclassified."""
    if not parent.error:
        return False
    return error_kind(parent.error_text, parent.http_status, ask) != FAILED


def zombie_tail_ns(
    parent: Span, child: Span, tolerance_ns: int = DEFAULT_TOLERANCE_NS, ask: Asker | None = None
) -> int:
    """How long `child` (the callee's SERVER span) kept working after its caller gave up.

    `parent` is the caller's CLIENT span that triggered `child`; `parent.error` is True when
    the call ended in an error such as a timeout. Returns the tail in nanoseconds, or 0 when
    `child` is not zombie work. tests/test_analyze.py lists the cases that must hold.
    """
    if not caller_gave_up(parent, ask):
        # The caller got its answer, or the callee replied with an error. Whatever the callee does afterwards
        # (a follow-up job, cleanup) was not abandoned: nobody gave up, so it is not zombie work.
        return 0
    # Only time spent actually running counts: a job still waiting in a queue uses no capacity yet.
    tail = child.end_ns - max(parent.end_ns, child.run_start_ns)
    # A few ms of difference is two machines' clocks disagreeing, not zombie work.
    return tail if tail > tolerance_ns else 0


@dataclass
class EdgeStats:
    caller: str
    callee: str
    calls: int  # CLIENT -> SERVER pairs on this edge
    fanout: float  # calls per request the caller served
    reach: float  # calls per user request (the cumulative amplification)
    work_ns: int  # total time the callee spent running these calls (queue waiting excluded)
    used_ns: int  # the part whose result the caller used
    zombie_ns: int  # the part that outlived a caller who had already given up
    tail_ns: int  # time spent after the callers left (avoidable)

    @property
    def goodput(self) -> float:
        return self.used_ns / self.work_ns if self.work_ns else 1.0


@dataclass
class Report:
    user_requests: int
    edges: list[EdgeStats] = field(default_factory=list)
    unclassified: int = 0  # caller errors whose text nothing could classify (counted as "gave up")

    @property
    def dependency_calls(self) -> int:
        return sum(e.calls for e in self.edges)

    @property
    def total_work_ns(self) -> int:
        return sum(e.work_ns for e in self.edges)

    @property
    def used_work_ns(self) -> int:
        return sum(e.used_ns for e in self.edges)

    @property
    def zombie_work_ns(self) -> int:
        return sum(e.zombie_ns for e in self.edges)

    @property
    def tail_ns(self) -> int:
        return sum(e.tail_ns for e in self.edges)

    @property
    def amplification(self) -> float:
        return self.dependency_calls / max(1, self.user_requests)

    @property
    def goodput(self) -> float:
        return self.used_work_ns / self.total_work_ns if self.total_work_ns else 1.0

    def render(self) -> str:
        header = f"{'edge':<26}{'calls':>6}{'per user':>10}{'work':>8}{'goodput':>9}{'zombie':>8}{'tail':>8}"
        lines = [
            f"user requests: {self.user_requests}   dependency calls: {self.dependency_calls}"
            f"   amplification: {self.amplification:.1f}x",
            "",
            header,
        ]
        for e in self.edges:
            name = f"{e.caller} -> {e.callee}"
            lines.append(
                f"{name:<26}{e.calls:>6}{e.reach:>9.1f}x{_sec(e.work_ns):>8}"
                f"{e.goodput:>9.0%}{_sec(e.zombie_ns):>8}{_sec(e.tail_ns):>8}"
            )
        lines += [
            "",
            "goodput = share of the callee's work whose result the caller used",
            "zombie  = work by jobs that outlived a caller who had already given up",
            "tail    = the part of that done after the caller left (what cancelling would save)",
        ]
        if self.unclassified:
            lines.append(f"note    = {self.unclassified} caller errors could not be classified; counted as 'gave up'")
        return "\n".join(lines)


def _sec(ns: int) -> str:
    return f"{ns / 1e9:.1f}s"


def compare(before: Report, after: Report) -> str:
    rows = [
        ("user requests", str(before.user_requests), str(after.user_requests)),
        ("dependency calls", str(before.dependency_calls), str(after.dependency_calls)),
        ("dependency work", _sec(before.total_work_ns), _sec(after.total_work_ns)),
        ("goodput", f"{before.goodput:.0%}", f"{after.goodput:.0%}"),
        ("zombie tail (avoidable)", _sec(before.tail_ns), _sec(after.tail_ns)),
    ]
    lines = [f"{'':<26}{'before':>10}{'after':>10}"] + [f"{name:<26}{b:>10}{a:>10}" for name, b, a in rows]
    if before.total_work_ns:
        lines += ["", f"dependency work cut by {1 - after.total_work_ns / before.total_work_ns:.0%}"]
    return "\n".join(lines)


MAX_ANCESTOR_STEPS = 10_000  # a real call stack is nowhere near this deep


def _enclosing_server(span: Span, by_id: dict[str, Span]) -> Span | None:
    """The SERVER span this span was started inside, skipping INTERNAL spans in between.

    Uploaded traces are untrusted: parent ids can form a cycle. Without the visited set this loop never ended."""
    seen = {span.span_id}
    parent = by_id.get(span.parent_id) if span.parent_id else None
    while parent is not None and parent.kind != "SERVER":
        if parent.span_id in seen or len(seen) > MAX_ANCESTOR_STEPS:
            return None
        seen.add(parent.span_id)
        parent = by_id.get(parent.parent_id) if parent.parent_id else None
    return parent


def analyze(spans: list[Span], tolerance_ns: int = DEFAULT_TOLERANCE_NS, ask: Asker | None = None) -> Report:
    by_id = {s.span_id: s for s in spans}
    roots = [s for s in spans if s.kind == "SERVER" and s.parent_id is None]
    served = Counter(s.service for s in spans if s.kind == "SERVER")
    # One call = a CLIENT span in the caller plus the SERVER span it caused in the callee.
    caller_of = {
        s.span_id: by_id[s.parent_id]
        for s in spans
        if s.kind == "SERVER" and s.parent_id in by_id and by_id[s.parent_id].kind == "CLIENT"
    }
    verdicts: dict[str, tuple[bool, bool]] = {}

    def upstream_job(server: Span) -> Span | None:
        """The callee job that made this job's call, if that job is itself a called job."""
        upstream = _enclosing_server(caller_of[server.span_id], by_id)
        return upstream if upstream is not None and upstream.span_id in caller_of else None

    def verdict(server: Span) -> tuple[bool, bool]:
        """(used, zombie) for one callee job. Waste flows downstream: if the job's own caller was
        abandoned by ITS caller, this job is wasted too, even though its direct caller was happy.

        Iterative on purpose: the chain of callers comes from untrusted input and can be very long, or loop."""
        pending: list[Span] = []  # from this job up to the first job whose verdict is already known
        on_path = set()
        job = server
        while job is not None and job.span_id not in verdicts and job.span_id not in on_path:
            pending.append(job)
            on_path.add(job.span_id)
            job = upstream_job(job)
        for job in reversed(pending):  # resolve from the top of the chain down
            client = caller_of[job.span_id]
            used = not client.error
            zombie = zombie_tail_ns(client, job, tolerance_ns, ask) > 0
            upstream = upstream_job(job)
            if upstream is not None and upstream.span_id in verdicts:
                up_used, up_zombie = verdicts[upstream.span_id]
                used, zombie = used and up_used, zombie or up_zombie
            verdicts[job.span_id] = (used, zombie)
        return verdicts[server.span_id]

    totals: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for server_id, client in caller_of.items():
        server = by_id[server_id]
        used, zombie = verdict(server)
        edge = totals[(client.service, server.service)]
        edge["calls"] += 1
        edge["work"] += server.run_ns
        edge["used"] += server.run_ns if used else 0
        edge["zombie"] += server.run_ns if zombie else 0
        edge["tail"] += zombie_tail_ns(client, server, tolerance_ns, ask)

    unclassified = sum(
        1 for client in caller_of.values() if client.error and error_kind(client.error_text, client.http_status, ask) == UNKNOWN
    )
    edges = [
        EdgeStats(
            caller=caller,
            callee=callee,
            calls=t["calls"],
            fanout=t["calls"] / max(1, served[caller]),
            reach=t["calls"] / max(1, len(roots)),
            work_ns=t["work"],
            used_ns=t["used"],
            zombie_ns=t["zombie"],
            tail_ns=t["tail"],
        )
        for (caller, callee), t in sorted(totals.items())
    ]
    return Report(len(roots), edges, unclassified)
