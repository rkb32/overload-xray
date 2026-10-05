# CLAUDE.md — overload-xray

Session-start summary. The deep handoff doc is **HANDOFF.md** (git-ignored, local only, read it first); design rationale in `docs/DESIGN.md`. Update this file at the end of every session.

## What it is

Reads OpenTelemetry traces and measures wasted work: amplification, goodput, zombie tail, and the retry map (feature #1, live). Each finding names a fix. Flagship job-search project + real product for on-call engineers; finish line = users uploading their own traces, not features.

Surfaces: web page + JSON API (`xray/api.py`, `xray/product.py`, `xray/static/`), CLI (`xray/cli.py`: `report diagnose retries compare save serve mcp`), MCP server (`xray/mcp_server.py` + `skills/overload-xray/SKILL.md`), optional Postgres store (`xray/store.py`), demo services + load test (`demo/`), AWS infra (`infra/`).

## Current state

**Done:**
- Core maths, loader, retry map, deadline-middleware reference fix, page + API, MCP, Postgres store — all tested and hardened (cyclic / 5000-deep / hostile input).
- Public repo `github.com/rkb32/overload-xray` @ `927c45a` (branch `main`), Apache-2.0, CI green.
- **GitHub Pages static demo LIVE** (2026-10-04): <https://rkb32.github.io/overload-xray/>, served from `/docs` on `main`. Built by `demo/build_pages.py` (real analyzer over the 3 samples + `results/loadtest.csv`; index.html/app.css copied, app.js gets the marked XRAY-STATIC patch: baked sample reports, baked chart rows, upload row points at the local CLI). Rebuild + push to update. README links it.
- AWS live: CloudFront → API Gateway → Lambda container, Terraform (13 resources, us-east-1). Live image `20261004-114745`; rollback tags in HANDOFF. Reported 20/20 outside checks — **UNVERIFIED** by me (address is unlisted; confirm the checks still pass).

**In progress:** nothing mid-flight. Working tree is clean except `.gitignore` (adds `HANDOFF.md` — intentionally uncommitted) and HANDOFF.md itself (ignored).

**Broken:** nothing known. Test suite verified this session: **161 passed, 5 skipped** without `DATABASE_URL` (16s). Claim is 166 pass with Postgres — **UNVERIFIED** this session (needs Docker Desktop + `docker compose up -d db`).

**Blocked on the owner:** (1) say "next" (README fixes + bots commit); (2) name the first user (lean: SRE). ~~"pages"~~ — done 2026-10-04. ~~AWS Support case~~ — **owner decided 2026-10-04: deferred until the tool has real users**; both projects are low-traffic and the shared limit of 10 is accepted risk. Public announce uses the Pages link only; AWS address stays unlisted until the case is filed (~1 day approval lead time — file at first sign of real users, before the spike).

## How to run / test

Windows 11, PowerShell from project root (Git Bash: `.venv/Scripts/python.exe`).

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m pytest                  # 161 pass, 5 DB tests skip
.venv\Scripts\python -m xray serve              # page + API, http://127.0.0.1:8080
.venv\Scripts\python -m xray diagnose xray\samples\retry-storm.json
.venv\Scripts\python -m xray retries xray\samples\layered-retries.json
.venv\Scripts\python -m demo.run_demo           # writes spans\baseline + spans\fixed
```

With Postgres: `docker compose up -d --wait db`, set `DATABASE_URL=postgresql://postgres:xray@127.0.0.1:15432/xray`, then pytest → 166. Deploy/verify/rollback commands: HANDOFF.md §5. Terraform state is a **local git-ignored file** — back it up.

## Key decisions (full table: HANDOFF.md §4)

- Zombie = caller errored + gave up, callee ran >5 ms past that (D1); running time only, not queue wait (D2).
- Cancel-at-deadline alone does NOT rescue users; must also refuse jobs that can't finish (D4, measured: 97/65/67% success vs 35/9/6% baseline, synthetic).
- Retry = same job, same method+path, later start, previous attempt failed (D14); spans carry no retry flag, so unsure rows say "inferred".
- Nothing stored; JSON body; read-only non-root container — privacy enforced, not promised.
- Lambda + API Gateway + CloudFront (App Runner closed to new customers); custom cache policy because the managed one keys on `Host` → 403s.
- Live address stays unlisted + no flood-testing until the Lambda quota is raised (slots shared with Winnow).

## Next 3 tasks

1. **"next" commit** (waits for the word): README fixes (CI claim, Windows-only quickstart, `xray serve --help` DATABASE_URL error), commit `.gitignore`, add Dependabot + hardened CI + CodeQL + zizmor + PR template + CODEOWNERS; pin collector image in docker-compose. Then same bots as PRs on winnow + ckpt-chaos.
2. **Real evidence** (was #5): measure an open-source app (Online Boutique or the OTel demo) and put that number at the top of the page; get 3–5 engineers with real traces to try it. Feature #2 (browser-side minimizer + Jaeger/Zipkin import) after that — it would also let the Pages demo analyze uploads in-browser.
3. **AWS Support case** (DEFERRED by owner until real users exist): Lambda concurrent executions, us-east-1, ask 100–1000. Once raised: show `terraform plan -var image_tag=20261004-114745 -var lambda_reserved_concurrency=3`, apply only on yes, then link the live address in README/About.

## Rules that bind every session (owner's)

- Explicit yes in chat before any push, post, deploy, `terraform apply`, delete, repo-setting change, or publishing the live address. Show exact text of any public post first. Never burst-test AWS.
- Commits as `rkb32 <177729309+rkb32@users.noreply.github.com>`, **never** Co-Authored-By. No personal email in public files — grep diffs for `@gmail`.
- Run git **inside this folder**; parent `C:\Users\ranje` is also a repo (no commits, no remote) — never add a remote to it.
- Gotchas: use `127.0.0.1` not `localhost`; files on disk are CRLF; PS 5.1 traps in HANDOFF §5.

## UNVERIFIED items to confirm

- Live site still passing 20/20 outside checks (needs the address + `infra/verify_live.py`; I haven't run it).
- 166/166 with Postgres (ran 161+5 skip without DB only).
- Terraform state file present and current in `infra/` (files exist on disk; not validated against the real stack).
