# Real-app run notes: the OpenTelemetry demo

Notes on two runs of xray against a real multi-service app, the OpenTelemetry demo (source tag 3.1.0 at commit dedc0178918e260823323b8d95005a8cb924b007; the images pulled were not pinned to that tag — see section 1). The first run has no faults. In the second, one dependency was forced to fail on every call. Every number below is copied from output files saved at the time of the runs. Nothing was run again to write this page. `<scratch>` stands for the folder that holds those files. Section 5 lists each figure with the file and line it came from.

Marks used: **Checked** means confirmed against a saved file or a source line while writing this page. **Not checked** means it rests on the run setup as reported, or on a guess that no file backs.

## 1. What was run

- **App.** The OpenTelemetry demo, source tag 3.1.0 at commit dedc0178918e260823323b8d95005a8cb924b007 (checked: the clone used for these notes has its HEAD at that tag and commit), started with `compose.yaml` plus `compose.observability.yaml`. The images were not pinned to that tag: the clone's `.env` sets `DEMO_VERSION=latest` (checked in the clone's `.env`), and the images pulled were `ghcr.io/open-telemetry/demo:latest-frontend`, `ghcr.io/open-telemetry/demo:latest-product-catalog`, `ghcr.io/open-telemetry/demo:latest-opamp-server`, `ghcr.io/open-telemetry/demo:latest-recommendation`, `ghcr.io/open-telemetry/demo:latest-load-generator`, `ghcr.io/open-telemetry/demo:latest-quote`, `ghcr.io/open-telemetry/demo:latest-flagd-ui`, `ghcr.io/open-telemetry/demo:latest-shipping`, `ghcr.io/open-telemetry/demo:latest-cart`, `ghcr.io/open-telemetry/demo:latest-email`, `ghcr.io/open-telemetry/demo:latest-payment`, `ghcr.io/open-telemetry/demo:latest-ad`, `ghcr.io/open-telemetry/demo:latest-frontend-proxy`, `ghcr.io/open-telemetry/demo:latest-checkout`, `ghcr.io/open-telemetry/demo:latest-telemetry-docs`, `ghcr.io/open-telemetry/demo:latest-opensearch`, `ghcr.io/open-telemetry/demo:latest-currency` and `ghcr.io/open-telemetry/demo:latest-image-provider` (checked in `compose-up.log`). What ran is therefore whatever those `latest-*` images contained, not the tagged source. The observability file adds Jaeger, which keeps traces in memory.
- **Load.** The demo's own load generator (Locust). It sends synthetic traffic. These are not real users. The clone's default is 5 simulated users with autostart on (checked in the clone's `.env`); what the run actually used was not checked.
- **Length.** Measured from the saved trace files, first span start to last span end: a 6.18-minute window (370.7 s) for the baseline and a 14.99-minute window (899.7 s) for the fault run (checked: computed from the span timestamps; Jaeger's times are microseconds). The load itself was reported as about 6 minutes for the baseline and about 8 minutes for the fault run (not checked: reported setup). The fault window is nearly twice the reported load, so the fetch caught spans from outside the load window as well.
- **Trace fetch.** Jaeger's query API, service `frontend`, a 30-minute lookback, with a limit of 2000 traces for the baseline and 5000 for the fault run. Each saved file holds exactly that many traces (checked by counting), so the limit capped both fetches. Which traces were left out was not looked at.
- **Fetch route for the fault run.** The fault run read Jaeger on its own port, not through the demo's proxy, because the proxy returned 504 for the large query (not checked: reported setup, no saved output).
- **Files.** The baseline trace file is 23762749 bytes with 2000 traces and 12831 spans. The fault trace file is 65077769 bytes with 5000 traces and 32671 spans (checked by counting). Both are larger than the 15 MB upload limit of the web page and API (README), so the page could not take them. The outputs here come from the command line.
- **The fault.** In `src/flagd/demo.flagd.json` the flag `productCatalogFailure` was changed from `defaultVariant` "off" to "on", and its `targeting` was replaced with `{}` so the default applies to every call. Nothing else in the file changed (checked: `fault-flag-change.txt` holds the before and after).
- **What the flag does** (checked in the product-catalog source). Only `GetProduct` reads this flag. With it on, every `GetProduct` returns an internal error straight away, with the message "Error: Product Catalog Fail Feature Flag Enabled". `ListProducts` and `SearchProducts` do not read the flag and kept working.
- **Not a realistic outage.** The failure was on for every product lookup, not for a share of them, and it failed fast. A real partial outage usually hits a share of requests and often fails slowly (timeouts). This run therefore shows what xray reports when a dependency is completely broken. It does not show what xray reports in a partial outage.

## 2. Results

xray was run three ways on each trace file: `report`, `retries`, `diagnose`. In `report`, "user requests" counts server spans that have no parent (`xray/analyze.py`). It is not the number of requests the load generator sent. "Dependency calls" counts client-to-server pairs between services. In `retries`, "calls" and "attempts" count client spans xray could group. The two counts measure different things and are not expected to match (4023 against 4012 in the baseline, 11269 against 11221 in the fault run). The gap was not investigated.

### Baseline (no faults)

| Measure | Value |
|---|---|
| user requests | 1497 |
| dependency calls | 4023 |
| amplification | 2.7x |
| error spans | not counted for this run (no saved count) |
| calls grouped by `retries` | 4012 |
| attempts | 4012 |
| retries | 0 (0%) |
| rescued / failed | 0 / 0 |
| client calls with no URL and no callee (retries among them cannot be seen) | 1312 |
| edges in the report | 20 |
| edges labelled healthy by `diagnose` | 20 |
| lowest goodput on any edge | 91% (load-generator -> frontend-proxy), then 96% (frontend-proxy -> frontend) |

### Fault run (product-catalog `GetProduct` fails on every call)

| Measure | Value |
|---|---|
| user requests | 3482 |
| dependency calls | 11269 |
| amplification | 3.2x |
| error spans (`error=true` or `otel.status_code=ERROR`) | 10193 of 32671 spans |
| calls grouped by `retries` | 11198 |
| attempts | 11221 |
| retries | 23, all on frontend -> product-catalog, marked (inferred) |
| rescued / failed | 0 / 23 |
| share of retries on that edge | 1% (the whole-run header line rounds the same retries to 0%) |
| worst stack | 1 layer retried: frontend -> product-catalog x2; the last service received 2 calls for one call to the first |
| client calls with no URL and no callee | 1690 |
| caller errors that could not be classified (counted as "gave up") | 2340 |
| edges in the report | 13 |
| edges labelled healthy | 6 (all at 100%) |
| edges labelled `doomed_work` | 7 |
| retry findings from `diagnose` | none |

Edges labelled `doomed_work` in the fault run:

| Edge | calls | work | goodput | zombie | tail |
|---|---|---|---|---|---|
| checkout -> cart | 33 | 0.1s | 0% | 0.0s | 0.0s |
| checkout -> product-catalog | 32 | 1.2s | 0% | 0.0s | 0.0s |
| frontend -> checkout | 33 | 36.1s | 0% | 0.0s | 0.0s |
| frontend -> product-catalog | 2400 | 482.6s | 35% | 5.6s | 1.1s |
| frontend-proxy -> frontend | 5205 | 852.4s | 38% | 0.0s | 0.0s |
| frontend-web -> frontend-proxy | 1095 | 157.1s | 26% | 0.0s | 0.0s |
| load-generator -> frontend-proxy | 590 | 421.7s | 3% | 0.0s | 0.0s |

The six healthy edges were ad -> flagd, frontend -> ad, frontend -> cart, frontend -> currency, frontend -> recommendation and recommendation -> product-catalog.

The fault run's error spans by service and operation (top rows of `fault-errors.txt`; this list was made by a separate small script using the same error rule, not by xray):

| service | operation | count |
|---|---|---|
| product-catalog | oteldemo.ProductCatalogService/GetProduct | 2307 |
| frontend | oteldemo.ProductCatalogService/GetProduct | 2286 |
| frontend-proxy | router frontend egress | 936 |
| frontend | GET /api/recommendations | 909 |
| frontend-proxy | GET | 902 |

The full list has 19 rows. It has no row for the cart service.

## 3. What the results show and do not show

### What they show

- **Baseline: no retries.** Of 4012 grouped calls, none was attempted twice, and `diagnose` found all 20 edges healthy. Healthy means goodput of at least 90% (`xray/diagnose.py`), which every baseline edge met (the lowest is 91%). Checked against the files.
- **Fault run: no real retries.** xray's `retries` output reports 23 inferred retries on frontend -> product-catalog, all failed (none rescued). They are xray's output, likely false positives, not observed resends: all 23 pair two different `demo.product.id` values (checked in `evidence-numbers.json`, a saved analysis of the fault trace file, not an xray output), and the rule groups calls by method and URL, so gRPC calls to the same method for different products look alike. The fault run shows no real retries. The worst stack is one layer deep, so no retry-layers or retry-budget finding was raised. Checked against the files.
- **xray reads a real Jaeger export of a multi-service app** and produces all three outputs on 12831 and 32671 spans without error. Checked: the outputs exist.
- **The loss of goodput lines up with the fault.** Goodput fell below 50% on the two edges that call the failing operation (frontend -> product-catalog 35%, checkout -> product-catalog 0%) and on the edges that inherited the error: frontend -> checkout 0% and checkout -> cart 0% (the call into checkout failed, so everything beneath it counts as unused; cart itself produced no error spans), plus the proxy chain above frontend (frontend-proxy -> frontend 38%, frontend-web -> frontend-proxy 26%, load-generator -> frontend-proxy 3%). The six edges still at 100% are ad -> flagd, frontend -> ad, frontend -> cart, frontend -> currency, frontend -> recommendation and recommendation -> product-catalog. Two of those need the setup to read: recommendation -> product-catalog calls the failing service and stayed healthy because only `GetProduct` fails, and frontend -> cart stayed at 100% while checkout -> cart fell to 0%. Checked against the files.

### What they do not show

- **One run each.** There is one baseline and one fault run, no repeats and no confidence intervals. Do not read the rise from 2.7x to 3.2x as an effect of the fault. The runs differ in length and trace limit, and nothing here measures how much amplification moves between two identical runs.
- **Synthetic load.** The traffic comes from a load generator running scripted user paths. It says nothing about real user behaviour, real traffic shape or production capacity.
- **xray does not measure real users.** It reads spans. "Goodput" is the share of a callee's running time whose result the caller used, judged from span error flags. It does not know what a person saw, waited for, or gave up on. "User requests" are root server spans in the fetched traces, not people.
- **Neither of xray's main targets occurred.** There was no retry storm and almost no zombie work (5.6s on one edge, 1.1s of it after the caller left, 0.0s everywhere else). Because the failure is an instant error and not a timeout, this run could not exercise the timeout and abandoned-caller paths. It is not evidence that xray finds retry storms or zombie work in a real app.
- **The retry rule can count two different calls as one retried call.** The rule says: a later call by the same job to the same callee method, started after the earlier one failed, is a retry, recognised by the rule in `xray/retries.py` rather than by a resend-count attribute. For calls without a URL, such as gRPC, the rule groups by method and URL and cannot see which product was asked for, so gRPC calls to the same method for different products look alike. In the fault run this explains all 23 inferred pairs: every pair matches two different `demo.product.id` values (checked in `evidence-numbers.json`), so none of them is a real retry.
- **Blind spots in both runs.** 1312 (baseline) and 1690 (fault run) client calls had no URL and no callee, so retries among them could not be seen. The baseline's "0 retries" holds for the grouped calls only.
- **The fetch was capped.** Each file holds exactly as many traces as the limit, so traces beyond the cap, if any, were never analysed.
- **Healthy does not mean fast.** The label uses goodput only. The baseline shows 2306.2s of callee work for 742 frontend -> product-catalog calls, against 482.6s for 2400 calls in the fault run. The cause was not investigated.
- **Unclassified errors.** xray ignores `otel.status_description` (`xray/spans.py` near line 311), which the fault export sets on product-catalog and checkout spans. Counts not re-verified. Its Jaeger reader therefore has no error message text to work with, so when the failed call also has no HTTP status, as with gRPC, xray cannot tell "the callee answered with an error" from "the caller gave up". It counted 2340 caller errors as "gave up". This affects only the zombie and tail columns, not goodput (goodput treats any errored call as unused), and those columns stayed near zero.

### What `doomed_work` means in the fault run

The label is a two-number rule (`xray/diagnose.py`): goodput below 50% and a zombie tail below 5% of the edge's work. It says the callers did not use most of what the callee did, and the callee was not still running after the caller left. It does not say why.

Here the reason is known from the setup: product-catalog returned an error on every `GetProduct`, the caller's span was marked as an error, and xray counts the callee's work under an errored call as unused. That status is also passed down to calls beneath a failed one. This is why checkout -> cart shows 0% goodput although the cart service produced no error spans: the call into checkout failed, so everything checkout did beneath it counts as unused.

The fix text printed with the label ("jobs start with too little time left to finish: refuse them up front") is the same for every `doomed_work` edge. It does not describe this fault. Load shedding would not repair a service that returns an error on every `GetProduct`. In this run the label means "work was thrown away", not "here is the cause". xray also gave the same label to the failing edges (frontend -> product-catalog, checkout -> product-catalog) and to the edges above them that only inherited the errors (frontend-proxy -> frontend, frontend-web -> frontend-proxy, load-generator -> frontend-proxy), and it does not rank which edge is the origin. Reading the error list next to the report is what points at product-catalog.

## 4. Reproduce

Needs Docker with enough memory for the whole demo, and this repository for xray.

```
git clone --depth 1 --branch 3.1.0 https://github.com/open-telemetry/opentelemetry-demo
cd opentelemetry-demo
docker compose -f compose.yaml -f compose.observability.yaml up -d
```

The clone's `.env` sets `DEMO_VERSION=latest`, so this pulls `latest-*` images rather than images built from the tag you cloned; the runs on this page used `latest-*` images too (section 1).

Let the load generator run for several minutes, then fetch traces from Jaeger. Through the demo's proxy (port 8080 in the clone's `.env`):

```
curl -o traces.json "http://localhost:8080/jaeger/ui/api/traces?service=frontend&limit=2000&lookback=30m"
```

If the proxy returns 504 for a large query, use Jaeger's own port. The compose file publishes container port 16686 on a host port Docker picks, so look it up first:

```
docker compose -f compose.yaml -f compose.observability.yaml port jaeger 16686
curl -o traces.json "http://localhost:<host-port>/jaeger/ui/api/traces?service=frontend&limit=5000&lookback=30m"
```

Then, from this repository (see the README for the virtual environment):

```
python -m xray diagnose <traces.json>
python -m xray report <traces.json>
python -m xray retries <traces.json>
```

The raw `.txt` outputs behind the numbers on this page (`otel-report.txt`, `otel-retries.txt`, `otel-diagnose.txt`, `fault-report.txt`, `fault-retries.txt`, `fault-diagnose.txt`, `fault-errors.txt`, `fault-flag-change.txt`) were kept locally in `<scratch>` and are not committed to this repository.

For the fault run, edit `src/flagd/demo.flagd.json` before bringing the stack up. In the `productCatalogFailure` flag set `"defaultVariant": "on"` and `"targeting": {}`, leaving the rest of the file as it is. To go back, restore the file (`git checkout src/flagd/demo.flagd.json` in the clone) and run `docker compose -f compose.yaml -f compose.observability.yaml down`.

Expect different numbers on your machine. The ones above come from a single run each.

## 5. Number check

Every figure in sections 1 to 4 appears below with the file and the line it was read from. Files in `<scratch>` are named without a folder. `otel-demo/` is the demo clone in `<scratch>`. `xray/` and `README.md` are in this repository. "n/a" means the figure has no output-file line: it was counted from a trace file, comes from the reported run setup, or is a value given in a command.

| File | Line | Figures on that line |
|---|---|---|
| otel-report.txt | 1 | 1497, 4023, 2.7x |
| otel-report.txt | 4-23 | 20 rows, one per edge |
| otel-report.txt | 15 | 742, 2306.2s |
| otel-report.txt | 18 | 96% |
| otel-report.txt | 20 | 91% |
| otel-retries.txt | 1 | 4012, 0% |
| otel-retries.txt | 4 | 0 (retries), 0 (rescued), 0 (failed) |
| otel-retries.txt | 25 | 1312 |
| otel-diagnose.txt | 1-79 | 20 blocks labelled healthy |
| fault-report.txt | 1 | 3482, 11269, 3.2x |
| fault-report.txt | 4-16 | 13 rows, one per edge |
| fault-report.txt | 5 | 33, 0.1s, 0%, 0.0s, 0.0s |
| fault-report.txt | 6 | 32, 1.2s, 0%, 0.0s, 0.0s |
| fault-report.txt | 9 | 33, 36.1s, 0%, 0.0s, 0.0s |
| fault-report.txt | 11 | 2400, 482.6s, 35%, 5.6s, 1.1s |
| fault-report.txt | 13 | 5205, 852.4s, 38%, 0.0s, 0.0s |
| fault-report.txt | 14 | 1095, 157.1s, 26%, 0.0s, 0.0s |
| fault-report.txt | 15 | 590, 421.7s, 3%, 0.0s, 0.0s |
| fault-report.txt | 21 | 2340 |
| fault-retries.txt | 1 | 11198, 11221, 0% |
| fault-retries.txt | 4 | 2377, 2400, 23, 1%, 0, 23 |
| fault-retries.txt | 20 | 1, x2 |
| fault-retries.txt | 21 | 2 |
| fault-retries.txt | 22 | 1690 |
| fault-diagnose.txt | 5, 9, 21, 29, 37, 41, 45 | 7 blocks labelled doomed_work |
| fault-diagnose.txt | 1, 13, 17, 25, 33, 49 | 6 blocks labelled healthy, goodput 100% |
| fault-errors.txt | 1 | 10193, 32671 |
| fault-errors.txt | 4 | 2307 |
| fault-errors.txt | 5 | 2286 |
| fault-errors.txt | 6 | 936 |
| fault-errors.txt | 7 | 909 |
| fault-errors.txt | 8 | 902 |
| fault-errors.txt | 4-22 | 19 rows in the full list |
| fault-flag-change.txt | 3, 7, 32, 35 | defaultVariant "off" to "on"; targeting `{}` |
| otel-traces.json | n/a | 23762749 bytes, 2000 traces, 12831 spans (counted) |
| fault-traces.json | n/a | 65077769 bytes, 5000 traces, 32671 spans (counted) |
| evidence-numbers.json | 5, 16 | 6.18 and 14.99 minutes, the measured trace windows |
| evidence-numbers.json | 43-72 | all 23 retry pairs pair two different demo.product.id values |
| otel-demo clone | n/a | tag 3.1.0, commit dedc0178918e260823323b8d95005a8cb924b007 (the clone's HEAD) |
| otel-demo/.env | 4 | DEMO_VERSION=latest |
| compose-up.log | 1, 3-8, 10-12, 14-17, 21-23, 25 | the 18 ghcr.io/open-telemetry/demo:latest-* image names (Pulling lines) |
| run setup | n/a | about 6 minutes, about 8 minutes, limit 2000, limit 5000, 504 (reported by the person who ran it) |
| commands | n/a | 3.1.0, depth 1, 30m, 8080, 16686 (values in the commands; ports checked in the rows below) |
| otel-demo/.env | 87 | 8080 |
| otel-demo/.env | 105 | 5 |
| otel-demo/.env | 180 | 16686 |
| otel-demo/src/product-catalog/main.go | 396, 442 | the flag is read only through checkProductFailure, called only in GetProduct |
| xray/diagnose.py | 23 | 0.9 (HEALTHY_GOODPUT; written as 90% in section 3) |
| xray/diagnose.py | 24 | 0.5 (LOW_GOODPUT; written as 50% in section 3) |
| xray/diagnose.py | 27 | 0.05 (SMALL_SHARE; written as 5% in section 3) |
| xray/analyze.py | 171 | user requests are server spans with no parent |
| xray/spans.py | 311 | error_text stays "" — the Jaeger reader ignores otel.status_description |
| README.md | 29 | 15 MB |
