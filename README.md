# overload-xray

**Is your backend doing work nobody is waiting for?** Drop in your OpenTelemetry traces and see how much of the work was wasted on retries and on callers that had already given up, and which fix is missing. No install, no account, and your traces are never stored.

```
A calls B and waits 1s, retrying twice. B needs 3s and nothing tells it A left.

without a fix:  3 calls per user request, 9.0s of work, 0% of it used, 6.0s done after A left
with a fix:     3 calls per user request, 2.9s of work (-68%), nothing done after A left
```

## Try it

**Just looking? [Try the bundled samples in your browser](https://rkb32.github.io/overload-xray/)** — a static copy with the samples already analyzed. To read your own traces, run it locally:

```
# Windows
.venv\Scripts\python -m xray serve        # http://127.0.0.1:8080 , no database needed
# macOS / Linux
.venv/bin/python -m xray serve
```

Open the page, drop in trace files (OTLP/JSON or OpenTelemetry SDK span JSON) or click a sample. You get a headline number, the findings with the fix each one points to, a per-service table, and a plain list of what xray could not see.

From a terminal: `curl --data-binary @traces.json http://127.0.0.1:8080/api/analyze`

**Private by design.** Traces are analyzed in memory and dropped. Nothing is written to disk or logged, and error messages never repeat what you sent. The `web` container runs with a read-only filesystem, as an unprivileged user, with every Linux capability dropped, so the claim is enforced, not just stated (`docker diff` shows no changes after an upload).

**Limits.** 10 files, 15 MB, 100,000 spans per request; 20 analyses per minute per address. Anything bigger is refused with a clear message.

## What the load test showed

B can finish about 8 jobs per second. Share of user requests that succeeded, by offered load:

| offered / s | baseline | cancel at the deadline | cancel + refuse doomed jobs |
|---|---|---|---|
| 2 | 100% | 100% | 100% |
| 5 | 100% | 100% | 100% |
| 8 | 35% | 38% | **97%** |
| 12 | 9% | 11% | **65%** |
| 20 | 6% | 9% | **67%** |

What the traces say about the same runs (all steps combined):

| | amplification | B work | goodput | zombie tail |
|---|---|---|---|---|
| baseline | 2.5x | 496s | 9% | 442s |
| cancel at the deadline | 2.5x | 185s | 28% | 0s |
| cancel + refuse doomed jobs | 1.8x | 149s | 99% | 0s |

The surprise: cancelling at the deadline makes the zombie tail disappear, but users still fail. Under overload the job at the front of the queue has already used most of its time, starts, cannot finish, and gets cancelled midway. Only refusing jobs that cannot finish in the time left rescues the users. The tool shows this too: after the middle fix the zombie tail is 0 but goodput is still 28%, and it flags that as `doomed_work`.

Limits, plainly: synthetic services, one random arrival sequence per rate, no confidence intervals, one laptop. No real application has been measured yet. See [docs/DESIGN.md](docs/DESIGN.md).

## Run the rest

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

.venv\Scripts\python -m demo.run_demo                          # one request, without and with the fix
.venv\Scripts\python -m xray report spans/baseline             # per-edge numbers
.venv\Scripts\python -m xray diagnose spans/baseline           # findings and the fix each points to
.venv\Scripts\python -m xray retries spans/baseline            # which calls were retried, how many layers, what it multiplied
.venv\Scripts\python -m xray compare spans/baseline spans/fixed
.venv\Scripts\python -m demo.loadtest                          # the table above (about 10 minutes)
.venv\Scripts\python -m pytest
```

### Through Docker (needs Docker Desktop running)

```
docker compose up -d --build web                               # the product, on http://127.0.0.1:18080
python -m demo.docker_demo                                     # demo services in containers, spans via an OpenTelemetry Collector
docker compose up -d db                                        # Postgres on 127.0.0.1:15432
set DATABASE_URL=postgresql://postgres:xray@127.0.0.1:15432/xray
python -m xray save spans/baseline --name baseline             # keep a run; the page then lists saved runs
```

Use `127.0.0.1`, not `localhost`: the ports are published on IPv4 only, and `localhost` may try `::1` first and wait a long time.
The database tests run when `DATABASE_URL` is set and are skipped otherwise.

## For agents (Claude Code, Paperclip, other MCP-capable coworkers)

```
set XRAY_ROOT=C:\path\to\traces
python -m xray mcp                                             # MCP server over stdio: report, retries, diagnose, compare
```

[skills/overload-xray/SKILL.md](skills/overload-xray/SKILL.md) teaches an agent the procedure. The server reads files only inside `XRAY_ROOT`, never writes, and opens no network connection unless `use_jev` is passed.

**Jev (optional):** `--jev` asks TypeSafe's Jev model to classify caller errors the rules cannot place. It sends scrubbed error text to a third party, needs `TYPESAFE_API_KEY`, and is off by default. Tested against the SDK's real response types; not run against the live API.

## Definitions
- **amplification**: calls made to a dependency per user request (per edge: *fan-out* per hop, *reach* per user request)
- **goodput**: share of the callee's running time whose result the caller used
- **zombie work**: work by jobs that outlived a caller who had already given up
- **zombie tail**: the part of that done *after* the caller left (what cancelling would save)
- **retry**: the same job calling the same URL again after the previous attempt *failed* (see [docs/DESIGN.md](docs/DESIGN.md), D14, for the exact rule)
- **retry layers / multiplication**: how many stacked calls were retried, and how many calls the deepest service received for one call to the top one

## Where things are
- `xray/api.py`, `xray/product.py`, `xray/limits.py`, `xray/static/`, `xray/samples/`: the product (upload API, report, limits, page, samples)
- `xray/spans.py` reads span files (two formats, de-duplicated); `xray/analyze.py` is the maths, with `zombie_tail_ns` as the core rule
- `xray/retries.py` the retry map (attempts per call, layers, retried writes); `demo/make_layered_sample.py` writes the synthetic two-layer sample
- `xray/classify.py` caller-error rules plus the optional Jev asker; `xray/diagnose.py` typed findings
- `xray/store.py`, `xray/schema.sql` Postgres for saved runs; `xray/mcp_server.py` and `skills/` the agent interfaces
- `demo/` two small services, the deadline middleware, the demo runners and the load test
- `Dockerfile`, `docker-compose.yml`, `deploy/otel-collector.yaml`, `.github/workflows/ci.yml`
- [docs/DESIGN.md](docs/DESIGN.md), [docs/DEPLOY.md](docs/DEPLOY.md), [docs/LAUNCH.md](docs/LAUNCH.md), [docs/FEATURES.md](docs/FEATURES.md) (what to build next, with sources)

## Status
Built and tested locally and in Docker: everything above. **Deployed to AWS on 2026-10-03** with Terraform (Lambda behind API Gateway and CloudFront), redesigned page and the retry map shipped 2026-10-04, and checked from outside with `infra/verify_live.py` (20 of 20 pass), but **not announced yet**: the account's Lambda slots are shared with another product and cannot be capped until AWS raises the quota, so the address stays unlisted. Details and numbers in [docs/DEPLOY.md](docs/DEPLOY.md). Not built: Kubernetes, the Go rewrite of the analyzer, GraphQL, and a run on a real open-source microservice app. CI (tests against Postgres 16, plus a Docker build) runs on every push and pull request and is green.

## License
Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
