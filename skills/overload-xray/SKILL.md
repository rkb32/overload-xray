---
name: overload-xray
description: Find wasted work in a service's OpenTelemetry traces (retry amplification, zombie work, goodput) and say which fix is missing. Use after a retry storm or incident, when a service gets slower under load, or to check whether deadline propagation or load shedding actually helped.
---

# overload-xray

Answers one question from trace files: of the work a callee did, how much did a caller actually use?

## Connect it
The tools come from an MCP server, launched over stdio. Point it at the folder that holds the trace files:

```json
{
  "mcpServers": {
    "overload-xray": {
      "command": "python",
      "args": ["-m", "xray", "mcp"],
      "cwd": "/path/to/overload-xray",
      "env": { "XRAY_ROOT": "/path/to/folder-with-traces" }
    }
  }
}
```

Paths you pass to the tools are relative to `XRAY_ROOT`. Anything outside it (`..`, an absolute path, a symlink) is refused.

## Procedure
1. Locate the traces: OTLP/JSON written by the OpenTelemetry Collector's `file` exporter, the OpenTelemetry Python SDK's span JSON lines, or Zipkin v2 JSON (an array of spans). A folder of files is fine.
2. Call `report`. For each caller -> callee edge read:
   - **per user** = calls per user request (above ~2 with low goodput means a retry storm)
   - **goodput** = share of the callee's work whose result the caller used
   - **tail** = work done after the caller had already left (avoidable by cancelling)
3. Call `diagnose`. Each finding names the missing fix:
   - `retry_storm` -> retry budget (about 10% of traffic), backoff with jitter, stop retrying once the deadline has passed
   - `zombie_work` -> send the caller's remaining time to the callee and cancel when it passes (deadline propagation)
   - `doomed_work` -> refuse jobs that cannot finish in the time left (load shedding / admission control)
   - `retry_layers` -> let one layer own the retry (the one closest to the failure); the others should fail fast
   - `retried_writes` -> check the write is idempotent (idempotency key or a duplicate check) or stop retrying it
   - `retry_budget` -> cap retries with a budget; `retried_client_errors` -> do not retry a 4xx (except 408 and 429)
   Call `retries` for the evidence behind these: attempts per call, how many layers retried, what that multiplied. A row marked "inferred" was recognised from repeated calls after a failure, not read from a resend count; say so.
4. If the report's LLM-token line is non-zero, read it as an estimate of the tokens billed for work nobody used (a job whose caller had already gone, or a client call that never delivered an answer). The dollar figure only appears when a price was given, and whether a cancelled call stops billing depends on the provider.
5. After a fix is deployed, call `compare` with the before and after folders. Expect the tail to reach 0 with cancellation. Goodput only recovers once doomed work is refused.
6. Tell the user the numbers, the finding and the fix, and what you could not know (see below).

## Limits to state, not hide
- Waiting in a queue is not work. Services should set the span attribute `app.queue_wait_ms`; without it, waiting counts as working.
- Sampled traces give partial counts. Ratios hold only if sampling keeps whole traces.
- The report may say some caller errors "could not be classified". They are counted as "caller gave up", so the tail can be overstated.
- `use_jev` sends scrubbed error text to a third party (api.typesafe.ai) and needs `TYPESAFE_API_KEY`. Use it only if the user agrees.
- Retries inside one span (a library that loops internally), sidecar retries that leave no spans, and attempts the sampler dropped are invisible to `retries`.
- This tool measures; it never changes a service.
