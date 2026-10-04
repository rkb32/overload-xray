"""Retries: which calls were attempted more than once, how many layers retried, and what that multiplied.

A retry is not a span kind. It is a pattern, so the rule is written down here and pinned by tests/test_retries.py:

1. Attempts are CLIENT spans started by the same job (the same enclosing SERVER span) for the same call. "The same
   call" is the same method and target (host, port and path); when the client span has no URL, it is the same endpoint
   of the same callee, as seen on the callee's SERVER span.
2. A later attempt continues the chain when it started after the previous one ended AND the previous one failed (an
   error status, a 5xx reply, 408 or 429), or when the client library says so itself (http.request.resend_count >= 1).
3. Anything else starts a new logical call. Ten successful calls in a loop are ten calls with one attempt each, and
   two overlapping requests are hedging, not a retry.

Calls that name neither a target nor a callee cannot be grouped safely. They are counted, not guessed.
Everything that walks parent links is iterative with cycle guards: uploaded traces are untrusted.
"""
import re
from collections import defaultdict
from dataclasses import dataclass, field

from xray.analyze import DEFAULT_TOLERANCE_NS, MAX_ANCESTOR_STEPS, _enclosing_server
from xray.spans import Span

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
HTTP_METHODS = WRITE_METHODS | {"GET", "HEAD", "OPTIONS"}
RETRYABLE_STATUSES = {408, 429}  # the 4xx answers that mean "try again later"
BUDGET_SHARE = 0.10  # a retry budget usually caps retries near a tenth of all attempts
MAX_LABEL_CHARS = 80


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def failed(span: Span) -> bool:
    status = span.http_status
    return span.error or (status is not None and (status >= 500 or status in RETRYABLE_STATUSES))


def _answered_with_a_client_error(span: Span) -> bool:
    """The server said the request itself is wrong (400, 404, 422...). Asking again cannot change that."""
    status = span.http_status
    return status is not None and 400 <= status < 500 and status not in RETRYABLE_STATUSES


def _method(span: Span) -> str:
    if span.method:
        return span.method
    first = span.name.split(" ", 1)[0].upper()
    return first if first in HTTP_METHODS else ""


_IDENTIFIER = re.compile(r"^(\d+|[0-9a-fA-F]{8,}|[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$")


def _route(target: str) -> str:
    """host/users/12345 -> host/users/{id}: the list of retried writes should read as routes, not as customers."""
    host, _, path = target.partition("/")
    if not path:
        return host
    return host + "/" + "/".join("{id}" if _IDENTIFIER.match(part) else part for part in path.split("/"))


@dataclass
class EdgeRetries:
    caller: str
    callee: str
    logical_calls: int = 0  # calls, however many attempts each took
    attempts: int = 0
    recovered: int = 0  # retried calls whose last attempt worked: the retry did its job
    exhausted: int = 0  # retried calls that never worked: every extra attempt was load for nothing
    writes_retried: int = 0  # POST / PUT / PATCH / DELETE calls that were attempted more than once
    client_errors_retried: int = 0  # calls retried after the server said the request itself was wrong
    inferred: bool = False  # at least one retried call was recognised by the rule, not by a resend count

    @property
    def retries(self) -> int:
        return self.attempts - self.logical_calls

    @property
    def attempts_per_call(self) -> float:
        return self.attempts / self.logical_calls if self.logical_calls else 1.0

    @property
    def retry_share(self) -> float:
        return self.retries / self.attempts if self.attempts else 0.0


@dataclass
class RetryPath:
    """The worst stack of retries: from the first retried layer down to where the extra calls land."""
    layers: int  # retried calls on the way down
    multiplication: int  # calls the deepest service received for ONE call to the top service
    steps: list[tuple[str, str, int]]  # (caller, callee, attempts) from the top down


@dataclass
class WriteRetry:
    method: str
    edge: str
    target: str  # a route such as billing/users/{id}/orders, never a raw URL
    calls: int
    max_attempts: int


@dataclass
class RetryReport:
    user_requests: int
    edges: list[EdgeRetries] = field(default_factory=list)
    worst: RetryPath | None = None
    writes: list[WriteRetry] = field(default_factory=list)
    unattributed: int = 0  # client calls with no URL and no callee: not grouped, so retries among them stay invisible

    @property
    def logical_calls(self) -> int:
        return sum(e.logical_calls for e in self.edges)

    @property
    def attempts(self) -> int:
        return sum(e.attempts for e in self.edges)

    @property
    def retries(self) -> int:
        return self.attempts - self.logical_calls

    @property
    def retry_share(self) -> float:
        return self.retries / self.attempts if self.attempts else 0.0

    def render(self) -> str:
        if not self.edges:
            return "no client calls that could be grouped were found in these spans"
        header = f"{'edge':<28}{'calls':>6}{'attempts':>9}{'per call':>9}{'retries':>8}{'share':>7}{'rescued':>8}{'failed':>8}"
        lines = [f"{_plural(self.logical_calls, 'call')} made {_plural(self.attempts, 'attempt')}: {self.retry_share:.0%} were retries "
                 f"(a retry budget usually caps this near {BUDGET_SHARE:.0%})", "", header]
        for e in self.edges:
            lines.append(f"{e.caller + ' -> ' + e.callee:<28.28}{e.logical_calls:>6}{e.attempts:>9}{e.attempts_per_call:>8.1f}x"
                         f"{e.retries:>8}{e.retry_share:>7.0%}{e.recovered:>8}{e.exhausted:>8}"
                         + ("  (inferred)" if e.inferred else ""))
        if self.worst:
            path = ", then ".join(f"{caller} -> {callee} x{attempts}" for caller, callee, attempts in self.worst.steps)
            lines += ["", f"worst stack: {self.worst.layers} layer(s) retried: {path}",
                      f"             the last service received {self.worst.multiplication} call(s) for one call to the first"]
        for w in self.writes:
            lines.append(f"retried write: {w.method} {w.target} ({w.edge}) was attempted up to {w.max_attempts}x: is it idempotent?")
        if self.unattributed:
            lines.append(f"note: {self.unattributed} client calls had no URL and no callee, so retries among them could not be seen")
        lines += ["", "rescued = a retry worked; failed = every attempt failed", "(inferred) = recognised by the rule in xray/retries.py, "
                  "not by an http.request.resend_count attribute"]
        return "\n".join(lines)


@dataclass
class _Chain:
    caller: str
    callee: str
    method: str
    label: str  # what the call was, for the list of retried writes
    scope: str | None  # the SERVER span (in the caller) whose job made the call
    attempts: list[Span]
    by_attribute: bool = True  # every extra attempt carried a resend count

    @property
    def retries(self) -> int:
        return len(self.attempts) - 1


def retry_map(spans: list[Span], tolerance_ns: int = DEFAULT_TOLERANCE_NS) -> RetryReport:
    by_id = {s.span_id: s for s in spans}
    callee_servers: dict[str, list[Span]] = defaultdict(list)  # CLIENT span id -> the SERVER spans it caused
    for s in spans:
        if s.kind == "SERVER" and s.parent_id in by_id and by_id[s.parent_id].kind == "CLIENT":
            callee_servers[s.parent_id].append(s)
    user_requests = sum(1 for s in spans if s.kind == "SERVER" and s.parent_id is None)

    # 1. Group the client calls that could be the same call.
    groups: dict[tuple, list[Span]] = defaultdict(list)
    unattributed = 0
    for s in spans:
        if s.kind != "CLIENT":
            continue
        servers = callee_servers.get(s.span_id, [])
        if s.target:
            operation = (_method(s), s.target)
        elif servers:
            operation = (servers[0].service, servers[0].name)
        else:
            unattributed += 1
            continue
        enclosing = _enclosing_server(s, by_id)
        scope = enclosing.span_id if enclosing is not None else s.parent_id
        groups[(s.trace_id, scope, s.service, operation)].append(s)

    # 2. Inside a group, an attempt that starts after a FAILED one (or says it is a resend) continues the chain.
    chains: list[_Chain] = []
    for (_trace, scope, caller, _operation), group in groups.items():
        group.sort(key=lambda span: (span.start_ns, span.end_ns, span.span_id))
        current: _Chain | None = None
        for s in group:
            if current is not None:
                previous = current.attempts[-1]
                says_so = (s.resend_count or 0) >= 1
                if s.start_ns + tolerance_ns >= previous.end_ns and (failed(previous) or says_so):
                    current.attempts.append(s)
                    current.by_attribute = current.by_attribute and says_so
                    continue
            first_servers = callee_servers.get(s.span_id, [])
            current = _Chain(caller=caller, callee="", method=_method(s), label="", scope=scope, attempts=[s])
            current.label = _route(s.target) if s.target else (first_servers[0].name if first_servers else s.name)
            chains.append(current)
    for chain in chains:
        served_by = next((callee_servers[a.span_id][0].service for a in chain.attempts if callee_servers.get(a.span_id)), None)
        host = chain.attempts[0].target.partition("/")[0]
        chain.callee = served_by or host or "unknown"

    # 3. Per edge.
    edges: dict[tuple[str, str], EdgeRetries] = {}
    writes: dict[tuple[str, str, str], WriteRetry] = {}
    for chain in chains:
        edge = edges.setdefault((chain.caller, chain.callee), EdgeRetries(chain.caller, chain.callee))
        edge.logical_calls += 1
        edge.attempts += len(chain.attempts)
        if not chain.retries:
            continue
        if failed(chain.attempts[-1]):
            edge.exhausted += 1
        else:
            edge.recovered += 1
        edge.inferred = edge.inferred or not chain.by_attribute
        if any(_answered_with_a_client_error(a) for a in chain.attempts[:-1]):
            edge.client_errors_retried += 1
        if chain.method in WRITE_METHODS:
            edge.writes_retried += 1
            name = f"{chain.caller} -> {chain.callee}"
            write = writes.setdefault((chain.method, name, chain.label[:MAX_LABEL_CHARS]),
                                      WriteRetry(chain.method, name, chain.label[:MAX_LABEL_CHARS], 0, 0))
            write.calls += 1
            write.max_attempts = max(write.max_attempts, len(chain.attempts))

    # 4. Layers: a chain made from inside a job that an earlier chain's attempt caused sits one layer below it.
    server_to_chain: dict[str, int] = {}
    for index, chain in enumerate(chains):
        for attempt in chain.attempts:
            for server in callee_servers.get(attempt.span_id, []):
                server_to_chain[server.span_id] = index
    parent_of = [server_to_chain.get(chain.scope) if chain.scope else None for chain in chains]
    layers: list[int | None] = [None] * len(chains)
    depth, root = [0] * len(chains), [0] * len(chains)
    for start in range(len(chains)):
        if layers[start] is not None:
            continue
        pending, on_path, node = [], set(), start
        while node is not None and layers[node] is None and node not in on_path and len(pending) <= MAX_ANCESTOR_STEPS:
            pending.append(node)
            on_path.add(node)
            node = parent_of[node]
        top = pending[-1]
        parent = parent_of[top]
        if parent is not None and layers[parent] is not None:
            running_layers, running_depth, running_root = layers[parent], depth[parent] + 1, root[parent]
        else:  # a real top of the stack, or a loop / absurdly deep chain: start counting here
            running_layers, running_depth, running_root = 0, 0, top
        for node in reversed(pending):
            running_layers += 1 if chains[node].retries else 0
            layers[node], depth[node], root[node] = running_layers, running_depth, running_root
            running_depth += 1

    received: dict[tuple, int] = defaultdict(int)  # attempts that reached one edge at one depth under one top call
    for index, chain in enumerate(chains):
        received[(root[index], depth[index], chain.caller, chain.callee)] += len(chain.attempts)
    best, best_score = None, (0, 0, 0)
    for index, chain in enumerate(chains):
        if layers[index]:
            score = (received[(root[index], depth[index], chain.caller, chain.callee)], layers[index], depth[index])
            if score > best_score:
                best, best_score = index, score
    worst = None
    if best is not None:
        path, seen, node = [], set(), best
        while node is not None and node not in seen and len(path) <= MAX_ANCESTOR_STEPS:
            path.append(node)
            seen.add(node)
            node = parent_of[node]
        path.reverse()
        worst = RetryPath(layers=layers[best], multiplication=best_score[0],
                          steps=[(chains[i].caller, chains[i].callee, len(chains[i].attempts)) for i in path])

    ordered = sorted(edges.values(), key=lambda e: (-e.retries, -e.attempts, e.caller, e.callee))
    return RetryReport(user_requests, ordered, worst, sorted(writes.values(), key=lambda w: (-w.calls, w.method, w.edge, w.target)),
                       unattributed)
