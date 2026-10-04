"""Expose xray to agents as MCP tools, so Claude Code, Codex, Paperclip agents or any MCP-capable coworker can
analyze traces themselves.

    XRAY_ROOT=/path/to/spans python -m xray mcp

The tools only read files, and only inside XRAY_ROOT (default: the current directory): a path that leaves the
root, by `..`, an absolute path or a symlink, is refused. They never write anything and never open a network
connection unless the caller sets use_jev (which needs TYPESAFE_API_KEY and sends scrubbed error text).
"""
import os

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from typesafe_sdk import TypeSafeError

from xray.analyze import analyze, compare as compare_reports
from xray.classify import jev_asker
from xray.diagnose import diagnose as diagnose_report, diagnose_retries, render_findings
from xray.retries import retry_map
from xray.spans import Span, TraceFormatError, load_spans

MAX_BYTES = 200_000_000  # refuse absurdly large inputs instead of exhausting memory

server = MCPServer(
    "overload-xray",
    instructions=(
        "Analyze OpenTelemetry trace files for wasted work: retry amplification, zombie work (work a callee kept "
        "doing after its caller gave up) and goodput. Call `report` for the numbers, `retries` for which calls were "
        "retried and how many layers multiplied them, `diagnose` for findings and the fix each one points to, "
        "`compare` to check that a fix helped. Paths are relative to the configured root."
    ),
)


def _inside_root(path: str) -> str:
    """ToolError (not ValueError) so the agent sees why it was refused; an unexpected crash stays a generic error."""
    root = os.path.realpath(os.environ.get("XRAY_ROOT", os.getcwd()))
    full = os.path.realpath(os.path.join(root, path))  # an absolute `path` replaces root here, and is caught below
    try:
        outside = os.path.commonpath([root, full]) != root
    except ValueError:  # a different drive on Windows
        outside = True
    if outside:
        raise ToolError(f"{path!r} is outside the allowed folder")
    if not os.path.exists(full):
        raise ToolError(f"{path!r} does not exist")
    size = os.path.getsize(full) if os.path.isfile(full) else sum(
        os.path.getsize(os.path.join(folder, name)) for folder, _, names in os.walk(full) for name in names
    )
    if size > MAX_BYTES:
        raise ToolError(f"{path!r} is larger than {MAX_BYTES // 1_000_000} MB")
    return full


def _asker(use_jev: bool):
    try:
        return jev_asker() if use_jev else None
    except TypeSafeError as exc:
        raise ToolError("use_jev needs an API key: set TYPESAFE_API_KEY in the server's environment") from exc


def _load(path: str) -> list[Span]:
    full = _inside_root(path)
    try:
        return load_spans(full)
    except TraceFormatError as exc:  # the message names the file and line, never the contents
        raise ToolError(str(exc)) from exc
    except (OSError, ValueError, KeyError) as exc:
        raise ToolError(f"could not read spans from {path!r} ({type(exc).__name__})") from exc


def _analyze(path: str, use_jev: bool):
    ask = _asker(use_jev)  # a missing key is reported before any file is touched, as before
    return analyze(_load(path), ask=ask)


@server.tool()
def report(path: str, use_jev: bool = False) -> str:
    """Per-edge amplification, goodput, zombie work and zombie tail for the spans at `path`."""
    return _analyze(path, use_jev).render()


@server.tool()
def diagnose(path: str, use_jev: bool = False) -> str:
    """Typed findings (retry_storm, zombie_work, retry_layers, retried_writes, ...) with the fix each one points to."""
    ask = _asker(use_jev)
    spans = _load(path)
    return render_findings(diagnose_report(analyze(spans, ask=ask)) + diagnose_retries(retry_map(spans)))


@server.tool()
def retries(path: str) -> str:
    """Which calls were retried, how many layers retried, what that multiplied, and which writes were attempted twice."""
    return retry_map(_load(path)).render()


@server.tool()
def compare(before: str, after: str) -> str:
    """Side-by-side numbers for two runs, to check that a fix helped."""
    return compare_reports(_analyze(before, False), _analyze(after, False))


def main() -> None:
    server.run("stdio")
