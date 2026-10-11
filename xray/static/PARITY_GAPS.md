# analyzer.js against the Python pipeline

`analyzer.js` must return what `xray.product.analyze_upload` returns. `tests/test_js_parity.py` checks that for every
file in `xray/samples` and for generated traces in all four formats. This file lists what was missing from the first
version of the port, and what still differs.

## Gaps the first version had (all closed)

1. It was only a scaffold: constants and number helpers. No span parsing, no `analyze`, no retry map, no `diagnose`,
   no `analyzeFiles`. Nothing in the header's contract could run.
2. No Zipkin v2 import (`spans._from_zipkin`: microsecond times, string tags, the `error` tag as the message).
3. No Jaeger Trace JSON import (`spans._from_jaeger`: typed tag list, `CHILD_OF` references, `processes` for services).
4. No GenAI token fields (`gen_ai.usage.*`, both the stable and the older names), so no `tokens_in`/`tokens_out`/
   `wasted_tokens_*` in the summary and no `tokens_estimate` note.
5. No `export`: the header says ES module, the file exported nothing.
6. `pyInt` returned a double, which loses nanosecond timestamps and token counts above 2^53 (Python ints are exact).
   It now returns a BigInt, and a bare JSON integer above 2^53 is read exactly where the browser allows it.
7. `req` read inherited properties (`obj["constructor"]`), so a missing key could look present.

## Differences that remain

- **JSON error text.** Which input is rejected and the status (422) match. The reason in "line N is not valid JSON
  (...)" is mapped from V8's message to Python's closest `JSONDecodeError.msg`, and some cases fall back to
  "Expecting value". Other "a span is malformed (...)" details match Python for a missing key, a bad integer and a
  bad timestamp, not for every exception.
- **JSON numbers.** Python accepts `NaN` and `Infinity`; `JSON.parse` rejects them. A bare integer above 2^53 is exact
  only where `JSON.parse` gives the reviver `context.source` (Node 22 does; check older browsers before relying on it);
  elsewhere it is read as a double. OTLP writes nanoseconds as strings, so real exports are not affected.
- **Timestamps.** Only extended ISO 8601 (`2026-10-04T12:00:00.123456Z`, any offset) is read. Python 3.11+ also reads
  the basic form (`20261004T120000`).
- **URLs.** `urlsplit` is ported from CPython 3.13. Bracketed IPv6 hosts are checked with a small validator, not
  `ipaddress`. Another Python version can differ on odd URLs, and then the target is `""` on one side only.
- **Whitespace and unicode.** Lone surrogates and non-ASCII digits in `int()` strings follow JS rules. A leading BOM is
  rejected by both with the same message.
- **File names.** Only used in error messages. Python's `os.path.basename` also splits on `\` on Windows; the port
  splits on `/` only.
- **Non-string ids or services** (a numeric `traceId`, say) are not coerced the way Python would.
- **No model asker.** `analyze(..., ask=jev_asker())` exists in Python for classifying error text. The upload path
  never passes it, and the browser has no model, so unplaceable text stays `unknown` in both.
- **Limits.** `XRAY_MAX_FILES` and `XRAY_MAX_SPANS` are environment variables in Python. The port takes
  `analyzeFiles(files, {maxFiles, maxSpans})`.
