import argparse

from xray.analyze import analyze, compare
from xray.diagnose import diagnose, diagnose_retries, render_findings
from xray.retries import retry_map
from xray.spans import load_spans

JEV_HELP = (
    "ask Jev about caller errors the rules cannot classify (sends scrubbed error text to api.typesafe.ai; "
    "needs TYPESAFE_API_KEY; off by default)"
)


def _ask(args):
    if not getattr(args, "jev", False):
        return None
    from xray.classify import jev_asker

    return jev_asker()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="xray", description="Find wasted work in OpenTelemetry traces")
    sub = parser.add_subparsers(dest="command", required=True)

    report = sub.add_parser("report", help="per-edge amplification, goodput and zombie work")
    report.add_argument("path", help="a span file, or a directory of them (SDK JSON lines, OTLP/JSON, or Zipkin v2)")
    report.add_argument("--jev", action="store_true", help=JEV_HELP)

    diag = sub.add_parser("diagnose", help="typed findings and the fix each one points to")
    diag.add_argument("path")
    diag.add_argument("--jev", action="store_true", help=JEV_HELP)

    retries = sub.add_parser("retries", help="which calls were retried, how many layers retried, and what that multiplied")
    retries.add_argument("path")

    versus = sub.add_parser("compare", help="side-by-side numbers for a before run and an after run")
    versus.add_argument("before")
    versus.add_argument("after")

    save = sub.add_parser("save", help="analyze a run and store it in Postgres (needs DATABASE_URL)")
    save.add_argument("path")
    save.add_argument("--name", required=True, help="how the run is listed in the dashboard")
    save.add_argument("--jev", action="store_true", help=JEV_HELP)

    serve = sub.add_parser("serve", help="start the dashboard and read-only API (no database needed)")
    serve.add_argument("--port", type=int, default=8080)

    sub.add_parser("mcp", help="run the MCP server for agents over stdio (folder limited by XRAY_ROOT)")

    args = parser.parse_args(argv)
    if args.command == "report":
        print(analyze(load_spans(args.path), ask=_ask(args)).render())
    elif args.command == "diagnose":
        spans = load_spans(args.path)
        print(render_findings(diagnose(analyze(spans, ask=_ask(args))) + diagnose_retries(retry_map(spans))))
    elif args.command == "retries":
        print(retry_map(load_spans(args.path)).render())
    elif args.command == "compare":
        print(compare(analyze(load_spans(args.before)), analyze(load_spans(args.after))))
    elif args.command == "save":
        from xray import store  # imported here so report/compare work without a database driver

        with store.connect() as conn:
            store.init_schema(conn)
            run_id = store.save_run(conn, args.name, analyze(load_spans(args.path), ask=_ask(args)))
        print(f"saved run {run_id} as {args.name!r}")
    elif args.command == "serve":
        import uvicorn

        uvicorn.run("xray.api:app", host="127.0.0.1", port=args.port)
    else:
        from xray.mcp_server import main as run_mcp

        run_mcp()
