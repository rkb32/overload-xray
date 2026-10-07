"""Read spans from files. Three JSON formats are understood and can be mixed in one directory:

- the OpenTelemetry Python SDK's own span JSON (what the demo services write directly)
- OTLP/JSON, one export request per line (what the OpenTelemetry Collector's `file` exporter writes)
- Zipkin v2 JSON, an array of spans per file (timestamp and duration are microseconds)
"""
import glob
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
QUEUE_ATTRIBUTE = "app.queue_wait_ms"  # set by a service on a SERVER span that waited in a queue before running


@dataclass(frozen=True)
class Span:
    trace_id: str
    span_id: str
    parent_id: str | None
    service: str
    name: str
    kind: str  # "CLIENT", "SERVER" or "INTERNAL"
    start_ns: int
    end_ns: int
    error: bool  # the span ended with status ERROR, e.g. a client timeout
    queue_ns: int = 0  # time spent waiting in a queue first; a waiting job uses no capacity yet
    error_text: str = ""  # the status description, e.g. "ReadTimeout: "
    http_status: int | None = None  # set when the callee actually replied
    method: str = ""  # HTTP method of a CLIENT span, upper case ("" when the span says nothing)
    target: str = ""  # host[:port]/path the call went to: no credentials, query or fragment (those can hold secrets)
    resend_count: int | None = None  # http.request.resend_count: the client library says "this is attempt n+1"

    @property
    def duration_ns(self) -> int:
        return self.end_ns - self.start_ns

    @property
    def run_start_ns(self) -> int:
        return min(self.end_ns, self.start_ns + self.queue_ns)

    @property
    def run_ns(self) -> int:
        """Time spent actually running (what consumes capacity), not waiting."""
        return self.end_ns - self.run_start_ns


def _iso_to_ns(iso: str) -> int:
    moment = datetime.fromisoformat(iso)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return ((moment - _EPOCH) // timedelta(microseconds=1)) * 1000


def _queue_ns(attributes: dict) -> int:
    return int(attributes.get(QUEUE_ATTRIBUTE, 0)) * 1_000_000


def _http_status(attributes: dict) -> int | None:
    for key in ("http.status_code", "http.response.status_code"):  # the old and the current semantic convention
        if key in attributes:
            try:
                return int(attributes[key])
            except (TypeError, ValueError):
                return None
    return None


MAX_TARGET_CHARS = 200


def _method(attributes: dict) -> str:
    return str(attributes.get("http.request.method") or attributes.get("http.method") or "").upper()[:16]


def _target(attributes: dict) -> str:
    """Where a CLIENT span called, as host[:port]/path. Credentials, query string and fragment are dropped on purpose:
    URLs in traces can carry passwords and tokens, and finding retries only needs to know the call went to the same place."""
    raw = attributes.get("url.full") or attributes.get("http.url")
    if not raw:
        return ""
    try:
        parts = urlsplit(str(raw))
        host, port = parts.hostname, parts.port
    except ValueError:  # e.g. "http://[broken"
        return ""
    if not host:
        return ""
    return f"{host}{f':{port}' if port else ''}{parts.path}"[:MAX_TARGET_CHARS]


def _resend_count(attributes: dict) -> int | None:
    for key in ("http.request.resend_count", "http.resend_count"):  # the current and the older name
        if key in attributes:
            try:
                count = int(attributes[key])
            except (TypeError, ValueError):
                return None
            return count if count >= 0 else None
    return None


# --- OpenTelemetry Python SDK JSON (one span per line) ---------------------------------------------------------

def _from_sdk(raw: dict) -> Span:
    resource = raw.get("resource") or {}
    resource_attributes = resource.get("attributes", resource)
    return Span(
        trace_id=raw["context"]["trace_id"],
        span_id=raw["context"]["span_id"],
        parent_id=raw.get("parent_id"),
        service=resource_attributes.get("service.name", "unknown"),
        name=raw["name"],
        kind=raw["kind"].split(".")[-1],
        start_ns=_iso_to_ns(raw["start_time"]),
        end_ns=_iso_to_ns(raw["end_time"]),
        error=(raw.get("status") or {}).get("status_code") == "ERROR",
        queue_ns=_queue_ns(raw.get("attributes") or {}),
        error_text=(raw.get("status") or {}).get("description") or "",
        http_status=_http_status(raw.get("attributes") or {}),
        method=_method(raw.get("attributes") or {}),
        target=_target(raw.get("attributes") or {}),
        resend_count=_resend_count(raw.get("attributes") or {}),
    )


# --- OTLP/JSON (one export request per line) -------------------------------------------------------------------

_OTLP_KINDS = {0: "INTERNAL", 1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}


def _otlp_kind(raw) -> str:
    if isinstance(raw, int):
        return _OTLP_KINDS.get(raw, "INTERNAL")
    name = str(raw).replace("SPAN_KIND_", "")  # some encoders write the enum name instead of the number
    return "INTERNAL" if name in ("UNSPECIFIED", "") else name


def _otlp_attributes(items) -> dict:
    found = {}
    for item in items or []:
        value = item.get("value", {})
        for kind in ("stringValue", "intValue", "boolValue", "doubleValue"):  # intValue is a string in OTLP/JSON
            if kind in value:
                found[item["key"]] = value[kind]
                break
    return found


def _with_hex_prefix(hex_id: str) -> str:
    return "0x" + hex_id if hex_id else ""


def _from_otlp(raw: dict) -> list[Span]:
    spans = []
    for resource_spans in raw.get("resourceSpans", []):
        service = _otlp_attributes(resource_spans.get("resource", {}).get("attributes")).get("service.name", "unknown")
        for scope_spans in resource_spans.get("scopeSpans", []):
            for span in scope_spans.get("spans", []):
                status = span.get("status") or {}
                attributes = _otlp_attributes(span.get("attributes"))
                spans.append(
                    Span(
                        trace_id=_with_hex_prefix(span["traceId"]),
                        span_id=_with_hex_prefix(span["spanId"]),
                        parent_id=_with_hex_prefix(span.get("parentSpanId", "")) or None,
                        service=service,
                        name=span["name"],
                        kind=_otlp_kind(span.get("kind", 0)),
                        start_ns=int(span["startTimeUnixNano"]),
                        end_ns=int(span["endTimeUnixNano"]),
                        error=status.get("code") in (2, "STATUS_CODE_ERROR"),
                        queue_ns=_queue_ns(attributes),
                        error_text=status.get("message") or "",
                        http_status=_http_status(attributes),
                        method=_method(attributes),
                        target=_target(attributes),
                        resend_count=_resend_count(attributes),
                    )
                )
    return spans


# --- Zipkin v2 JSON (an array of spans per file) ------------------------------------------------------------------

_ZIPKIN_KINDS = {"CLIENT", "SERVER", "PRODUCER", "CONSUMER"}


def _zipkin_kind(raw: str | None) -> str:
    if raw in _ZIPKIN_KINDS:
        return raw
    return "INTERNAL"  # a span without a kind is local work


def _zipkin_error(tags: dict) -> tuple[bool, str]:
    """Zipkin marks failure with the `error` tag; the value is the message, or bare truth like "true"."""
    value = "" if tags.get("error") is None else str(tags.get("error"))
    error = value.lower() not in ("", "false", "0")
    return error, "" if value.lower() == "true" else value


def _zipkin_service(raw: dict) -> str:
    for name in ("localEndpoint", "remoteEndpoint"):  # the caller is the span's own service
        endpoint = raw.get(name) or {}
        if endpoint.get("serviceName"):
            return endpoint["serviceName"]
    return "unknown"


def _from_zipkin(raw: dict) -> Span:
    tags = raw.get("tags") or {}
    start_ns = int(raw.get("timestamp") or 0) * 1000  # Zipkin timestamps are microseconds
    duration_ns = int(raw.get("duration") or 0) * 1000
    error, error_text = _zipkin_error(tags)
    return Span(
        trace_id=_with_hex_prefix(raw["traceId"]),
        span_id=_with_hex_prefix(raw["id"]),
        parent_id=_with_hex_prefix(raw.get("parentId") or "") or None,
        service=_zipkin_service(raw),
        name=raw.get("name") or "",
        kind=_zipkin_kind(raw.get("kind")),
        start_ns=start_ns,
        end_ns=start_ns + duration_ns,
        error=error,
        queue_ns=_queue_ns(tags),
        error_text=error_text,
        http_status=_http_status(tags),
        method=_method(tags),
        target=_target(tags),
        resend_count=_resend_count(tags),
    )


# --- loading ---------------------------------------------------------------------------------------------------

class TraceFormatError(ValueError):
    """The input is not a trace file we understand. The message is safe to show to the person who uploaded it:
    it names the file and line and the kind of problem, never the contents."""


class TooManySpans(TraceFormatError):
    pass


def _spans_from(item, source: str) -> list[Span]:
    if isinstance(item, dict) and "resourceSpans" in item:
        return _from_otlp(item)
    if isinstance(item, dict) and "context" in item and "start_time" in item:
        return [_from_sdk(item)]
    if isinstance(item, dict) and "id" in item and "traceId" in item:
        return [_from_zipkin(item)]  # a Zipkin v2 span, on its own or one element of a file's array
    if isinstance(item, dict) and "data" in item and any(isinstance(d, dict) and "traceID" in d for d in item.get("data") or []):
        raise TraceFormatError(f"{source}: this looks like a Jaeger JSON export, which is not supported yet. Use OTLP/JSON.")
    raise TraceFormatError(f"{source}: this is not an OpenTelemetry span, an OTLP/JSON export request, or a Zipkin v2 span")


def parse_text(text: str, source: str = "input", max_spans: int | None = None) -> list[Span]:
    """Parse one file's text: JSON lines, one JSON document (maybe pretty-printed), or a JSON array of either."""
    items: list = []
    head = text.lstrip()[:1]
    whole = None
    if head in ("{", "["):
        try:
            whole = json.loads(text)  # a single document; JSON lines fails here at the second line and falls through
        except json.JSONDecodeError:
            whole = None
    if whole is not None:
        items = whole if isinstance(whole, list) else [whole]
    else:
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise TraceFormatError(f"{source}: line {number} is not valid JSON ({exc.msg})") from None

    spans: list[Span] = []
    for item in items:
        try:
            spans.extend(_spans_from(item, source))
        except TraceFormatError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise TraceFormatError(f"{source}: a span is malformed ({type(exc).__name__}: {exc})") from None
        if max_spans is not None and len(spans) > max_spans:
            raise TooManySpans(f"{source}: more than {max_spans:,} spans; send a smaller time window")
    return spans


def dedupe(spans: list[Span]) -> list[Span]:
    """OTLP delivery is at-least-once: retries and collectors can hand over the same span twice, and counting it
    twice would inflate every number. A span is identified by its trace and span id."""
    unique: dict[tuple[str, str], Span] = {}
    for span in spans:
        unique.setdefault((span.trace_id, span.span_id), span)
    return list(unique.values())


def load_spans(path: str) -> list[Span]:
    """Read every *.jsonl / *.json under `path` (or the single file `path`) into Span objects."""
    if os.path.isfile(path):
        files = [path]
    else:
        files = sorted(glob.glob(os.path.join(path, "*.jsonl")) + glob.glob(os.path.join(path, "*.json")))
    spans: list[Span] = []
    for file in files:
        with open(file, encoding="utf-8") as handle:
            spans.extend(parse_text(handle.read(), source=os.path.basename(file)))
    return dedupe(spans)
