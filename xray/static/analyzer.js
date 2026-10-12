"use strict";

/* analyzer.js — the overload-xray pipeline as a browser-side ES module.
 *
 * A line-for-line port of the Python pipeline (xray/spans.py, xray/analyze.py, xray/classify.py,
 * xray/retries.py, xray/diagnose.py, and the shaping in xray/product.py) so the static demo on
 * GitHub Pages can analyze uploaded traces in the browser: nothing is sent anywhere.
 *
 * THE CONTRACT: analyzeFiles(files) must return the same report product.analyze_upload(files)
 * returns (compared after a JSON round-trip). tests/test_js_parity.py runs both over the same
 * inputs and diffs them. Any change to the Python pipeline must be mirrored here, and vice versa.
 * Known differences that remain are listed in xray/static/PARITY_GAPS.md.
 *
 * Port notes:
 * - All nanosecond values are BigInt: OTLP nanosecond timestamps (e.g. 1791115200000000000) exceed 2^53.
 * - Python's round() and f-string formats do round-half-even on the exact binary value of the double;
 *   bigRound() below reproduces that bit-exactly by decomposing the IEEE-754 double and scaling with BigInt.
 * - Python dicts preserve insertion order; Maps are used here the same way.
 * - Python compares strings by code point; cmpStr does (Array.prototype.sort compares UTF-16 code units).
 */

// --- constants (mirror the Python modules exactly) ------------------------------------------------------------

const DEFAULT_TOLERANCE_NS = 5000000n; // real traces span machines whose clocks disagree by a few ms
const MAX_ANCESTOR_STEPS = 10000;      // a real call stack is nowhere near this deep
const MAX_FILES = 10;
const MAX_SPANS = 100000;
const MAX_NAME_CHARS = 80;    // product.py
const MAX_TARGET_CHARS = 200; // spans.py
const MAX_LABEL_CHARS = 80;   // retries.py
const MIN_REQUESTS_FOR_RATIOS = 3;

const HEALTHY_GOODPUT = 0.9;
const LOW_GOODPUT = 0.5;
const STORM_REACH = 2.0;    // calls per user request
const ZOMBIE_SHARE = 0.15;  // share of the callee's running time spent after the caller left
const SMALL_SHARE = 0.05;
const MIN_CALLS_FOR_BUDGET = 20;

const WRITE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const HTTP_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE", "GET", "HEAD", "OPTIONS"]);
const RETRYABLE_STATUSES = new Set([408, 429]);
const BUDGET_SHARE = 0.10;

const QUEUE_ATTRIBUTE = "app.queue_wait_ms";

const ABANDONED = "abandoned"; // the caller stopped waiting: timeout, deadline, cancellation
const FAILED = "failed";       // the call failed without the caller leaving: an error reply, a dropped connection
const UNKNOWN = "unknown";
const ABANDONED_WORDS = ["timeout", "timed out", "deadline", "cancel", "abort"];
const FAILED_WORDS = ["disconnect", "reset", "refused", "closed", "unreachable", "broken pipe"];

const HEALTHY = "healthy";
const RETRY_STORM = "retry_storm";
const ZOMBIE_WORK = "zombie_work";
const DOOMED_WORK = "doomed_work";
const DEGRADED = "degraded";
const RETRY_LAYERS = "retry_layers";
const RETRIED_WRITES = "retried_writes";
const RETRY_BUDGET = "retry_budget";
const RETRIED_CLIENT_ERRORS = "retried_client_errors";

class UploadError extends Error {
  constructor(status, message) {
    super(message);
    this.name = "UploadError";
    this.status = status;
  }
}

class TraceFormatError extends Error {
  constructor(message) {
    super(message);
    this.name = "TraceFormatError";
  }
}

class TooManySpans extends TraceFormatError {
  constructor(message) {
    super(message);
    this.name = "TooManySpans";
  }
}

// An error that Python would raise as KeyError / TypeError / ValueError / AttributeError. `pyName` is what
// Python's type(exc).__name__ would be: parse_text puts it in the "a span is malformed" message.
class PyError extends Error {
  constructor(pyName, message) {
    super(message);
    this.name = "PyError";
    this.pyName = pyName;
  }
}

// --- Python's round(): half-even on the exact binary value of the double ---------------------------------------

const _f64 = new DataView(new ArrayBuffer(8));
const _POW10 = [1, 10, 100, 1000, 10000, 100000];

// bigRound(x, n) = round(x * 10^n) as a BigInt, ties to even, computed on the exact value of the double x.
function bigRound(x, n) {
  if (!Number.isFinite(x)) throw new RangeError("round() of a non-finite value");
  const neg = x < 0 || Object.is(x, -0);
  _f64.setFloat64(0, Math.abs(x));
  const bits = _f64.getBigUint64(0);
  const E = Number((bits >> 52n) & 0x7ffn);
  let m = bits & ((1n << 52n) - 1n);
  let e;
  if (E === 0) { e = -1074; } else { m |= 1n << 52n; e = E - 1075; }
  // |x| = m * 2^e exactly, so |x| * 10^n = m * 5^n * 2^(e+n).
  const num = m * 5n ** BigInt(n);
  const p = e + n;
  let q;
  if (p >= 0) {
    q = num << BigInt(p);
  } else {
    const shift = BigInt(-p);
    const den = 1n << shift;
    q = num / den;
    const twice = (num % den) * 2n;
    if (twice > den || (twice === den && (q & 1n) === 1n)) q += 1n;
  }
  return neg ? -q : q;
}

// Python round(x, n) for the n this pipeline uses (0..4): the double nearest to bigRound(x, n) * 10^-n.
function pyRoundDouble(x, n) {
  return Number(bigRound(x, n)) / _POW10[n];
}

// Python f"{x:.{n}f}" — half-even on the exact binary value, always n fractional digits.
function fmtDecimal(x, n) {
  const R = bigRound(x, n);
  const neg = R < 0n;
  let s = (neg ? -R : R).toString();
  if (n > 0) {
    s = s.padStart(n + 1, "0");
    s = s.slice(0, -n) + "." + s.slice(-n);
  }
  return (neg ? "-" : "") + s;
}

// Python f"{x:.0%}": multiply by 100 as a double first (same as CPython), then round.
function fmtPct0(x) {
  return fmtDecimal(x * 100, 0) + "%";
}

// Python f"{n:,}" for an int (Number or BigInt).
function fmtInt(n) {
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

// Python int / int (true division, correctly rounded) for BigInts. Number(a) / Number(b) is already exact-then-rounded
// while both fit in 53 bits; beyond that, scale to a 64-bit quotient with a sticky bit so Number() rounds once.
function bigDiv(a, b) {
  if (b === 0n) throw new PyError("ZeroDivisionError", "division by zero");
  if (a === 0n) return 0;
  const neg = (a < 0n) !== (b < 0n);
  if (a < 0n) a = -a;
  if (b < 0n) b = -b;
  let result;
  if (a <= 9007199254740991n && b <= 9007199254740991n) {
    result = Number(a) / Number(b);
  } else {
    const shift = 64 - (a.toString(2).length - b.toString(2).length);
    let num = a;
    let den = b;
    if (shift >= 0) num <<= BigInt(shift); else den <<= BigInt(-shift);
    let q = num / den;
    if (num % den !== 0n) q |= 1n;
    result = Number(q) * 2 ** -shift;
  }
  return neg ? -result : result;
}

// A BigInt for the JSON report: a plain Number when it is exact, so the page can use it directly.
function toJsonInt(value) {
  return value <= 9007199254740991n && value >= -9007199254740991n ? Number(value) : value;
}

// --- string helpers ---------------------------------------------------------------------------------------------

// Python str comparison is by code point; JS default string compare is by UTF-16 code unit.
function cmpStr(a, b) {
  const aa = Array.from(a, (ch) => ch.codePointAt(0));
  const bb = Array.from(b, (ch) => ch.codePointAt(0));
  const n = Math.min(aa.length, bb.length);
  for (let i = 0; i < n; i++) {
    if (aa[i] !== bb[i]) return aa[i] - bb[i];
  }
  return aa.length - bb.length;
}

function cmpBigInt(a, b) {
  return a < b ? -1 : a > b ? 1 : 0;
}

// Python text[:n] slices by code point.
function pySlice(text, n) {
  return Array.from(String(text)).slice(0, n).join("");
}

// Python str.isspace() characters (str.strip() with no argument strips exactly these).
const PY_SPACE = "\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const PY_STRIP = new RegExp(`^[${PY_SPACE}]+|[${PY_SPACE}]+$`, "g");
const PY_LSTRIP = new RegExp(`^[${PY_SPACE}]+`);
const pyStrip = (text) => text.replace(PY_STRIP, "");
const pyLstrip = (text) => text.replace(PY_LSTRIP, "");

// Python str.splitlines() boundaries.
const LINE_BREAKS = /\r\n|[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]/;

// Python's name for a JSON-derived value's type, for error messages.
function pyTypeName(value) {
  if (value === null || value === undefined) return "NoneType";
  switch (typeof value) {
    case "boolean": return "bool";
    case "bigint": return "int";
    case "number": return Number.isInteger(value) ? "int" : "float";
    case "string": return "str";
    default: return Array.isArray(value) ? "list" : "dict";
  }
}

// Python str() for the values that come out of JSON.
function pyStr(value) {
  if (value === null || value === undefined) return "None";
  if (value === true) return "True";
  if (value === false) return "False";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : pyFloatRepr(value);
  if (typeof value === "object") return Array.isArray(value) ? `[${value.map(pyRepr).join(", ")}]` : `{${Object.entries(value).map(([k, v]) => `${pyRepr(k)}: ${pyRepr(v)}`).join(", ")}}`;
  return String(value);
}

function pyFloatRepr(x) {
  const s = String(x);
  return /^-?\d+$/.test(s) ? s + ".0" : s;
}

function pyRepr(value) {
  if (typeof value !== "string") return pyStr(value);
  const q = value.includes("'") && !value.includes('"') ? '"' : "'";
  let out = "";
  for (const ch of value) {
    if (ch === "\\") out += "\\\\";
    else if (ch === q) out += "\\" + q;
    else if (ch === "\n") out += "\\n";
    else if (ch === "\r") out += "\\r";
    else if (ch === "\t") out += "\\t";
    else out += ch;
  }
  return q + out + q;
}

// Python truthiness of a JSON-derived value.
function pyTruthy(value) {
  if (value === null || value === undefined || value === false) return false;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "bigint") return value !== 0n;
  if (typeof value === "string") return value.length > 0;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return true;
}

// --- Python int() and friends ----------------------------------------------------------------------------------------

// Python int(value) as a BigInt: exact for any size, like Python's own ints.
function pyInt(value) {
  switch (typeof value) {
    case "boolean": return value ? 1n : 0n;
    case "bigint": return value;
    case "number":
      if (Number.isNaN(value)) throw new PyError("ValueError", "cannot convert float NaN to integer");
      if (!Number.isFinite(value)) throw new PyError("OverflowError", "cannot convert float infinity to integer");
      return BigInt(Math.trunc(value));
    case "string": {
      const t = pyStrip(value);
      if (!/^[+-]?\d+(_\d+)*$/.test(t)) throw new PyError("ValueError", `invalid literal for int() with base 10: ${pyRepr(value)}`);
      return BigInt(t.replaceAll("_", ""));
    }
    default:
      throw new PyError("TypeError", `int() argument must be a string, a bytes-like object or a real number, not '${pyTypeName(value)}'`);
  }
}

// `except (TypeError, ValueError)` around int(): the two errors Python's int() raises for the wrong kind of value.
function intOrNull(value) {
  try {
    return pyInt(value);
  } catch (error) {
    if (error instanceof PyError && (error.pyName === "TypeError" || error.pyName === "ValueError")) return null;
    throw error;
  }
}

// --- Python mapping / list idioms ------------------------------------------------------------------------------------

const hasOwn = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);
const isMapping = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

// Python mapping[key]: a missing key raises KeyError, a non-mapping raises TypeError.
function req(obj, key) {
  if (!isMapping(obj)) throw new PyError("TypeError", `${pyTypeName(obj)} object is not subscriptable with a string`);
  if (!hasOwn(obj, key)) throw new PyError("KeyError", `'${key}'`);
  return obj[key];
}

// Python mapping.get(key, default): a non-mapping raises AttributeError.
function pyGet(obj, key, fallback = null) {
  if (!isMapping(obj)) throw new PyError("AttributeError", `'${pyTypeName(obj)}' object has no attribute 'get'`);
  return hasOwn(obj, key) ? obj[key] : fallback;
}

// Python (value or {}): falsy -> {}, a truthy non-mapping fails on .get.
function dictOr(value) {
  if (!pyTruthy(value)) return {};
  return value;
}

// Python (value or []) in a for loop: falsy -> [], a truthy non-list is not iterated like a list here.
function listOr(value) {
  if (!pyTruthy(value)) return [];
  if (!Array.isArray(value)) throw new PyError("TypeError", `'${pyTypeName(value)}' object is not iterable`);
  return value;
}

// A Python dict of attributes as a Map (so `key in attributes` and `.get(key)` read the same way, and a key such as
// "__proto__" is only a key). Values are kept as parsed.
function attributeMap(obj) {
  if (!isMapping(obj)) throw new PyError("AttributeError", `'${pyTypeName(obj)}' object has no attribute 'get'`);
  return new Map(Object.entries(obj));
}

// --- spans.py: reading spans -----------------------------------------------------------------------------------------

function newSpan(fields) {
  return Object.assign(
    {
      traceId: "", spanId: "", parentId: null, service: "", name: "", kind: "", startNs: 0n, endNs: 0n, error: false,
      queueNs: 0n, errorText: "", httpStatus: null, method: "", target: "", resendCount: null,
      inputTokens: 0n, outputTokens: 0n,
    },
    fields,
  );
}

const runStartNs = (span) => (span.endNs < span.startNs + span.queueNs ? span.endNs : span.startNs + span.queueNs);
const runNs = (span) => span.endNs - runStartNs(span);

function daysFromCivil(y, m, d) {
  // Howard Hinnant's algorithm: days since 1970-01-01 of a proleptic Gregorian date.
  y -= m <= 2 ? 1 : 0;
  const era = Math.floor(y / 400);
  const yoe = y - era * 400;
  const doy = Math.floor((153 * (m + (m > 2 ? -3 : 9)) + 2) / 5) + d - 1;
  const doe = yoe * 365 + Math.floor(yoe / 4) - Math.floor(yoe / 100) + doy;
  return era * 146097 + doe - 719468;
}

const isLeap = (y) => (y % 4 === 0 && y % 100 !== 0) || y % 400 === 0;
const daysInMonth = (y, m) => [31, isLeap(y) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1];

const ISO = /^(\d{4})-(\d{2})-(\d{2})(?:[\s\S](\d{2})(?::(\d{2})(?::(\d{2})(?:[.,](\d+))?)?)?(Z|[+-]\d{2}(?::?\d{2}(?::?\d{2}(?:\.\d{1,6})?)?)?)?)?$/;

// datetime.fromisoformat(iso) as nanoseconds since the epoch (microsecond resolution, like Python). A time without an
// offset is taken as UTC, like _iso_to_ns does. Only the extended ISO 8601 form is read (see PARITY_GAPS.md).
function isoToNs(iso) {
  if (typeof iso !== "string") throw new PyError("TypeError", "fromisoformat: argument must be str");
  const bad = () => new PyError("ValueError", `Invalid isoformat string: ${pyRepr(iso)}`);
  const match = ISO.exec(iso);
  if (!match) throw bad();
  const [year, month, day] = [Number(match[1]), Number(match[2]), Number(match[3])];
  const hour = match[4] === undefined ? 0 : Number(match[4]);
  const minute = match[5] === undefined ? 0 : Number(match[5]);
  const second = match[6] === undefined ? 0 : Number(match[6]);
  const micros = match[7] === undefined ? 0 : Number(match[7].slice(0, 6).padEnd(6, "0"));
  if (year < 1 || month < 1 || month > 12 || day < 1 || day > daysInMonth(year, month)) throw bad();
  if (hour > 23 || minute > 59 || second > 59) throw bad();
  let offsetSeconds = 0;
  const zone = match[8];
  if (zone && zone !== "Z") {
    const digits = zone.slice(1).replace(/[:.]/g, "");
    const [oh, om, os] = [Number(digits.slice(0, 2)), Number(digits.slice(2, 4) || 0), Number(digits.slice(4, 6) || 0)];
    if (oh > 23 || om > 59 || os > 59) throw bad();
    offsetSeconds = (zone[0] === "-" ? -1 : 1) * (oh * 3600 + om * 60 + os);
  }
  const seconds = daysFromCivil(year, month, day) * 86400 + hour * 3600 + minute * 60 + second - offsetSeconds;
  return (BigInt(seconds) * 1000000n + BigInt(micros)) * 1000n;
}

function queueNs(attributes) {
  return pyInt(attributes.has(QUEUE_ATTRIBUTE) ? attributes.get(QUEUE_ATTRIBUTE) : 0) * 1000000n;
}

function httpStatus(attributes) {
  for (const key of ["http.status_code", "http.response.status_code"]) { // the old and the current semantic convention
    if (attributes.has(key)) {
      const status = intOrNull(attributes.get(key));
      return status === null ? null : Number(status);
    }
  }
  return null;
}

function methodOf(attributes) {
  const first = attributes.get("http.request.method");
  const raw = pyTruthy(first) ? first : attributes.get("http.method");
  return pySlice((pyTruthy(raw) ? pyStr(raw) : "").toUpperCase(), 16);
}

// --- urllib.parse.urlsplit, as far as the target needs it (hostname, port, path) -------------------------------------

function isIPv6(text) {
  const [address, ...zone] = text.split("%");
  if (zone.length > 1 || (zone.length === 1 && zone[0] === "")) return false;
  const pieces = address.split("::");
  if (pieces.length > 2) return false;
  const groups = (part) => (part === "" ? [] : part.split(":"));
  const all = pieces.length === 2 ? [...groups(pieces[0]), ...groups(pieces[1])] : groups(pieces[0]);
  let count = 0;
  for (let i = 0; i < all.length; i++) {
    const group = all[i];
    if (i === all.length - 1 && group.includes(".")) {
      if (!/^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}$/.test(group)) return false;
      count += 2;
    } else {
      if (!/^[0-9a-fA-F]{1,4}$/.test(group)) return false;
      count += 1;
    }
  }
  return pieces.length === 2 ? count < 8 : count === 8;
}

function checkBracketedNetloc(netloc) {
  const hostPort = netloc.slice(netloc.lastIndexOf("@") + 1);
  const open = hostPort.indexOf("[");
  if (open < 0) return; // unreachable from urlsplit, which only calls this when both brackets are present
  if (hostPort.slice(0, open)) throw new PyError("ValueError", "Invalid IPv6 URL");
  const rest = hostPort.slice(open + 1);
  const close = rest.indexOf("]");
  const hostname = close >= 0 ? rest.slice(0, close) : rest;
  const port = close >= 0 ? rest.slice(close + 1) : "";
  if (port && !port.startsWith(":")) throw new PyError("ValueError", "Invalid IPv6 URL");
  if (hostname.startsWith("v")) {
    if (!/^v[a-fA-F0-9]+\..+$/s.test(hostname)) throw new PyError("ValueError", "IPvFuture address is invalid");
  } else if (!isIPv6(hostname)) {
    throw new PyError("ValueError", "Invalid IPv6 or IPv4 address");
  }
}

function checkNetloc(netloc) {
  if (!netloc || /^[\x00-\x7f]*$/.test(netloc)) return;
  const stripped = netloc.replace(/[@:#?]/g, "");
  const normalized = stripped.normalize("NFKC");
  if (stripped === normalized) return;
  for (const c of "/?#@:") {
    if (normalized.includes(c)) throw new PyError("ValueError", `netloc '${netloc}' contains invalid characters under NFKC normalization`);
  }
}

// attributes url.full / http.url -> host[:port]/path, or "" when there is nothing usable (spans.py _target).
function targetOf(attributes) {
  const first = attributes.get("url.full");
  const raw = pyTruthy(first) ? first : attributes.get("http.url");
  if (!pyTruthy(raw)) return "";
  try {
    let url = pyLstrip0(pyStr(raw));
    url = url.replace(/[\t\r\n]/g, "");
    const colon = url.indexOf(":");
    if (colon > 0 && /^[A-Za-z]$/.test(url[0]) && /^[A-Za-z0-9+\-.]+$/.test(url.slice(0, colon))) url = url.slice(colon + 1);
    let netloc = "";
    if (url.slice(0, 2) === "//") {
      let delim = url.length;
      for (const c of "/?#") {
        const at = url.indexOf(c, 2);
        if (at >= 0) delim = Math.min(delim, at);
      }
      netloc = url.slice(2, delim);
      url = url.slice(delim);
      if ((netloc.includes("[") && !netloc.includes("]")) || (netloc.includes("]") && !netloc.includes("["))) {
        throw new PyError("ValueError", "Invalid IPv6 URL");
      }
      if (netloc.includes("[") && netloc.includes("]")) checkBracketedNetloc(netloc);
    }
    const hash = url.indexOf("#");
    if (hash >= 0) url = url.slice(0, hash);
    const query = url.indexOf("?");
    if (query >= 0) url = url.slice(0, query);
    checkNetloc(netloc);

    const hostinfo = netloc.slice(netloc.lastIndexOf("@") + 1);
    let host;
    let port;
    const open = hostinfo.indexOf("[");
    if (open >= 0) {
      const bracketed = hostinfo.slice(open + 1);
      const close = bracketed.indexOf("]");
      host = close >= 0 ? bracketed.slice(0, close) : bracketed;
      const rest = close >= 0 ? bracketed.slice(close + 1) : "";
      const colonAt = rest.indexOf(":");
      port = colonAt >= 0 ? rest.slice(colonAt + 1) : "";
    } else {
      const colonAt = hostinfo.indexOf(":");
      host = colonAt >= 0 ? hostinfo.slice(0, colonAt) : hostinfo;
      port = colonAt >= 0 ? hostinfo.slice(colonAt + 1) : "";
    }
    let portNumber = null;
    if (port) {
      if (!/^[0-9]+$/.test(port)) throw new PyError("ValueError", `Port could not be cast to integer value as ${pyRepr(port)}`);
      portNumber = Number(BigInt(port));
      if (portNumber > 65535) throw new PyError("ValueError", "Port out of range 0-65535");
    }
    if (!host) return "";
    const percent = host.indexOf("%");
    const hostname = percent >= 0 ? host.slice(0, percent).toLowerCase() + host.slice(percent) : host.toLowerCase();
    return pySlice(`${hostname}${portNumber ? `:${portNumber}` : ""}${url}`, MAX_TARGET_CHARS);
  } catch (error) {
    if (error instanceof PyError && error.pyName === "ValueError") return ""; // e.g. "http://[broken"
    throw error;
  }
}

// url.lstrip(C0 controls and space)
const pyLstrip0 = (text) => text.replace(/^[\x00-\x20]+/, "");

function resendCount(attributes) {
  for (const key of ["http.request.resend_count", "http.resend_count"]) { // the current and the older name
    if (attributes.has(key)) {
      const count = intOrNull(attributes.get(key));
      if (count === null) return null;
      return count >= 0n ? Number(count) : null;
    }
  }
  return null;
}

// GenAI usage tokens as [input, output] BigInts: a value that is not a number is ignored (counts as 0).
function tokensOf(attributes) {
  const count = (...keys) => {
    for (const key of keys) {
      const value = attributes.get(key);
      if (value === undefined || value === null) continue;
      const parsed = intOrNull(value);
      if (parsed === null) return 0n;
      return parsed < 0n ? 0n : parsed;
    }
    return 0n;
  };
  return [
    count("gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens"),
    count("gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens"),
  ];
}

const withHexPrefix = (hexId) => {
  if (!pyTruthy(hexId)) return "";
  if (typeof hexId !== "string") throw new PyError("TypeError", `can only concatenate str (not "${pyTypeName(hexId)}") to str`);
  return "0x" + hexId;
};

// --- OpenTelemetry Python SDK JSON (one span per line) ---

function fromSdk(raw) {
  const resource = dictOr(pyGet(raw, "resource"));
  const resourceAttributes = pyGet(resource, "attributes", resource);
  const attributes = attributeMap(dictOr(pyGet(raw, "attributes")));
  const [inputTokens, outputTokens] = tokensOf(attributes);
  return newSpan({
    traceId: req(req(raw, "context"), "trace_id"),
    spanId: req(raw.context, "span_id"),
    parentId: pyGet(raw, "parent_id"),
    service: pyGet(resourceAttributes, "service.name", "unknown"),
    name: req(raw, "name"),
    kind: splitKind(req(raw, "kind")),
    startNs: isoToNs(req(raw, "start_time")),
    endNs: isoToNs(req(raw, "end_time")),
    error: pyGet(dictOr(pyGet(raw, "status")), "status_code") === "ERROR",
    queueNs: queueNs(attributes),
    errorText: pyTruthy(pyGet(dictOr(pyGet(raw, "status")), "description")) ? pyGet(dictOr(pyGet(raw, "status")), "description") : "",
    httpStatus: httpStatus(attributes),
    method: methodOf(attributes),
    target: targetOf(attributes),
    resendCount: resendCount(attributes),
    inputTokens,
    outputTokens,
  });
}

function splitKind(kind) {
  if (typeof kind !== "string") throw new PyError("AttributeError", `'${pyTypeName(kind)}' object has no attribute 'split'`);
  return kind.split(".").pop();
}

// --- OTLP/JSON (one export request per line) ---

const OTLP_KINDS = new Map([[0, "INTERNAL"], [1, "INTERNAL"], [2, "SERVER"], [3, "CLIENT"], [4, "PRODUCER"], [5, "CONSUMER"]]);

function otlpKind(raw) {
  if (typeof raw === "number" || typeof raw === "bigint" || typeof raw === "boolean") {
    const key = Number(raw);
    return OTLP_KINDS.has(key) ? OTLP_KINDS.get(key) : "INTERNAL";
  }
  const name = pyStr(raw).replace("SPAN_KIND_", ""); // some encoders write the enum name instead of the number
  return name === "UNSPECIFIED" || name === "" ? "INTERNAL" : name;
}

function otlpAttributes(items) {
  const found = new Map();
  for (const item of listOr(items)) {
    const value = pyGet(item, "value", {});
    if (value === null) throw new PyError("TypeError", "argument of type 'NoneType' is not iterable");
    for (const kind of ["stringValue", "intValue", "boolValue", "doubleValue"]) { // intValue is a string in OTLP/JSON
      if (isMapping(value) && hasOwn(value, kind)) {
        found.set(req(item, "key"), value[kind]);
        break;
      }
    }
  }
  return found;
}

function fromOtlp(raw) {
  const spans = [];
  for (const resourceSpans of listDefault(pyGet(raw, "resourceSpans", []))) {
    const resourceAttributes = otlpAttributes(pyGet(pyGet(resourceSpans, "resource", {}), "attributes"));
    const service = resourceAttributes.has("service.name") ? resourceAttributes.get("service.name") : "unknown";
    for (const scopeSpans of listDefault(pyGet(resourceSpans, "scopeSpans", []))) {
      for (const span of listDefault(pyGet(scopeSpans, "spans", []))) {
        const status = dictOr(pyGet(span, "status"));
        const attributes = otlpAttributes(pyGet(span, "attributes"));
        const [inputTokens, outputTokens] = tokensOf(attributes);
        const code = pyGet(status, "code");
        const message = pyGet(status, "message");
        spans.push(newSpan({
          traceId: withHexPrefix(req(span, "traceId")),
          spanId: withHexPrefix(req(span, "spanId")),
          parentId: withHexPrefix(pyGet(span, "parentSpanId", "")) || null,
          service,
          name: req(span, "name"),
          kind: otlpKind(pyGet(span, "kind", 0)),
          startNs: pyInt(req(span, "startTimeUnixNano")),
          endNs: pyInt(req(span, "endTimeUnixNano")),
          error: code === 2 || code === 2n || code === "STATUS_CODE_ERROR",
          queueNs: queueNs(attributes),
          errorText: pyTruthy(message) ? message : "",
          httpStatus: httpStatus(attributes),
          method: methodOf(attributes),
          target: targetOf(attributes),
          resendCount: resendCount(attributes),
          inputTokens,
          outputTokens,
        }));
      }
    }
  }
  return spans;
}

// Python mapping.get(key, []) then iterated: missing -> [], present-but-not-a-list fails on iteration.
function listDefault(value) {
  if (!Array.isArray(value)) throw new PyError("TypeError", `'${pyTypeName(value)}' object is not iterable`);
  return value;
}

// --- Zipkin v2 JSON (an array of spans per file) ---

const ZIPKIN_KINDS = new Set(["CLIENT", "SERVER", "PRODUCER", "CONSUMER"]);

function zipkinKind(raw) {
  if (raw !== null && typeof raw === "object") throw new PyError("TypeError", `unhashable type: '${pyTypeName(raw)}'`);
  return ZIPKIN_KINDS.has(raw) ? raw : "INTERNAL"; // a span without a kind is local work
}

// Zipkin marks failure with the `error` tag; the value is the message, or bare truth like "true".
function zipkinError(tags) {
  const raw = tags.get("error");
  const value = raw === undefined || raw === null ? "" : pyStr(raw);
  const lowered = value.toLowerCase();
  const error = !(lowered === "" || lowered === "false" || lowered === "0");
  return [error, lowered === "true" ? "" : value];
}

function zipkinService(raw) {
  for (const name of ["localEndpoint", "remoteEndpoint"]) { // the caller is the span's own service
    const endpoint = dictOr(pyGet(raw, name));
    if (pyTruthy(pyGet(endpoint, "serviceName"))) return endpoint.serviceName;
  }
  return "unknown";
}

function fromZipkin(raw) {
  const tags = attributeMap(dictOr(pyGet(raw, "tags")));
  const startNs = pyInt(orZero(pyGet(raw, "timestamp"))) * 1000n; // Zipkin timestamps are microseconds
  const durationNs = pyInt(orZero(pyGet(raw, "duration"))) * 1000n;
  const [error, errorText] = zipkinError(tags);
  const [inputTokens, outputTokens] = tokensOf(tags);
  return newSpan({
    traceId: withHexPrefix(req(raw, "traceId")),
    spanId: withHexPrefix(req(raw, "id")),
    parentId: withHexPrefix(orEmpty(pyGet(raw, "parentId"))) || null,
    service: zipkinService(raw),
    name: pyTruthy(pyGet(raw, "name")) ? raw.name : "",
    kind: zipkinKind(pyGet(raw, "kind")),
    startNs,
    endNs: startNs + durationNs,
    error,
    queueNs: queueNs(tags),
    errorText,
    httpStatus: httpStatus(tags),
    method: methodOf(tags),
    target: targetOf(tags),
    resendCount: resendCount(tags),
    inputTokens,
    outputTokens,
  });
}

const orZero = (value) => (pyTruthy(value) ? value : 0);
const orEmpty = (value) => (pyTruthy(value) ? value : "");

// --- Jaeger "Trace JSON" (one export document) ---

const JAEGER_KINDS = new Set(["CLIENT", "SERVER", "PRODUCER", "CONSUMER", "INTERNAL"]);

// Jaeger tags are a list of {key, type, value}; make the plain Map the OTLP/SDK paths use.
function jaegerTags(span) {
  const tags = new Map();
  for (const tag of listOr(pyGet(span, "tags"))) {
    const key = req(tag, "key");
    tags.set(key, pyGet(tag, "value"));
  }
  return tags;
}

function fromJaeger(raw) {
  const spans = [];
  for (const trace of listOr(pyGet(raw, "data"))) {
    const processes = dictOr(pyGet(trace, "processes"));
    for (const span of listOr(pyGet(trace, "spans"))) {
      const attributes = jaegerTags(span);
      const [inputTokens, outputTokens] = tokensOf(attributes);
      let parent = null;
      for (const ref of listOr(pyGet(span, "references"))) {
        if (pyGet(ref, "refType") === "CHILD_OF") { parent = ref; break; }
      }
      const spanKind = attributes.get("span.kind");
      const kind = pyStr(pyTruthy(spanKind) ? spanKind : "INTERNAL").toUpperCase();
      const startNs = pyInt(req(span, "startTime")) * 1000n;
      const processId = pyGet(span, "processID");
      const process = typeof processId === "string" && hasOwn(processes, processId) ? processes[processId] : null;
      const status = attributes.has("otel.status_code") ? attributes.get("otel.status_code") : "";
      const statusDescription = attributes.get("otel.status_description");
      spans.push(newSpan({
        traceId: withHexPrefix(pyStr(req(span, "traceID"))),
        spanId: withHexPrefix(pyStr(req(span, "spanID"))),
        parentId: parent ? withHexPrefix(pyStr(req(parent, "spanID"))) : null,
        service: pyGet(dictOr(process), "serviceName", "unknown"),
        name: req(span, "operationName"),
        kind: JAEGER_KINDS.has(kind) ? kind : "INTERNAL",
        startNs,
        endNs: startNs + pyInt(req(span, "duration")) * 1000n,
        error: attributes.get("error") === true || pyStr(status).toUpperCase() === "ERROR",
        queueNs: queueNs(attributes),
        errorText: pyTruthy(statusDescription) ? statusDescription : "",
        httpStatus: httpStatus(attributes),
        method: methodOf(attributes),
        target: targetOf(attributes),
        resendCount: resendCount(attributes),
        inputTokens,
        outputTokens,
      }));
    }
  }
  return spans;
}

// --- loading ---

function spansFrom(item, source) {
  if (isMapping(item) && hasOwn(item, "resourceSpans")) return fromOtlp(item);
  if (isMapping(item) && hasOwn(item, "context") && hasOwn(item, "start_time")) return [fromSdk(item)];
  if (isMapping(item) && hasOwn(item, "id") && hasOwn(item, "traceId")) return [fromZipkin(item)]; // a Zipkin v2 span
  if (isMapping(item) && hasOwn(item, "data") && jaegerData(item.data)) return fromJaeger(item);
  throw new TraceFormatError(`${source}: this is not an OpenTelemetry span, an OTLP/JSON export request, a Zipkin v2 span, or a Jaeger "Trace JSON" export`);
}

// any(isinstance(d, dict) and "traceID" in d for d in item.get("data") or [])
function jaegerData(data) {
  if (!pyTruthy(data)) return false;
  if (Array.isArray(data)) return data.some((d) => isMapping(d) && hasOwn(d, "traceID"));
  if (typeof data === "string" || isMapping(data)) return false; // iterating those yields characters / keys, never a dict
  throw new PyError("TypeError", `'${pyTypeName(data)}' object is not iterable`);
}

// JSON.parse, except that an integer too big for a double (an OTLP nanosecond timestamp written as a bare number)
// comes back as a BigInt, like Python's json.loads returns an exact int. Needs the reviver's `context.source`;
// where a browser lacks it, the number is read as a double (see PARITY_GAPS.md).
function parseJson(text) {
  if (!/\d{16}/.test(text)) return JSON.parse(text);
  return JSON.parse(text, (key, value, context) => {
    if (typeof value === "number" && context && typeof context.source === "string"
        && /^-?\d+$/.test(context.source) && !Number.isSafeInteger(value)) {
      return BigInt(context.source);
    }
    return value;
  });
}

// What Python's json.JSONDecodeError.msg would say, for the V8 error messages that have a clear counterpart.
function jsonErrorMessage(error) {
  const text = String(error && error.message);
  if (/after JSON|non-whitespace/.test(text)) return "Extra data";
  if (/Unterminated string/.test(text)) return "Unterminated string starting at";
  if (/Bad control character/.test(text)) return "Invalid control character at";
  if (/Bad escaped character|Bad Unicode escape/.test(text)) return "Invalid \\escape";
  if (/Expected property name/.test(text)) return "Expecting property name enclosed in double quotes";
  if (/Expected ':'/.test(text)) return "Expecting ':' delimiter";
  if (/Expected ','/.test(text)) return "Expecting ',' delimiter";
  return "Expecting value";
}

// One file's text: JSON lines, one JSON document (maybe pretty-printed), or a JSON array of either.
function parseText(text, source = "input", maxSpans = null) {
  let items = [];
  const head = pyLstrip(text).slice(0, 1);
  let whole = null;
  if (head === "{" || head === "[") {
    try {
      whole = parseJson(text); // a single document; JSON lines fails here at the second line and falls through
    } catch (error) {
      if (!(error instanceof SyntaxError)) throw error;
      whole = null;
    }
  }
  if (whole !== null) {
    items = Array.isArray(whole) ? whole : [whole];
  } else {
    const lines = text.split(LINE_BREAKS);
    if (lines.length && lines[lines.length - 1] === "") lines.pop(); // splitlines() has no trailing empty line
    for (let index = 0; index < lines.length; index++) {
      const line = lines[index];
      if (!pyStrip(line)) continue;
      try {
        items.push(parseJson(line));
      } catch (error) {
        if (!(error instanceof SyntaxError)) throw error;
        const reason = line.startsWith(String.fromCharCode(0xfeff)) ? "Unexpected UTF-8 BOM (decode using utf-8-sig)" : jsonErrorMessage(error);
        throw new TraceFormatError(`${source}: line ${index + 1} is not valid JSON (${reason})`);
      }
    }
  }

  const spans = [];
  for (const item of items) {
    try {
      for (const span of spansFrom(item, source)) spans.push(span);
    } catch (error) {
      if (error instanceof TraceFormatError) throw error;
      if (error instanceof PyError && ["KeyError", "TypeError", "ValueError", "AttributeError"].includes(error.pyName)) {
        throw new TraceFormatError(`${source}: a span is malformed (${error.pyName}: ${error.message})`);
      }
      if (error instanceof TypeError) throw new TraceFormatError(`${source}: a span is malformed (TypeError: ${error.message})`);
      throw error;
    }
    if (maxSpans !== null && spans.length > maxSpans) {
      throw new TooManySpans(`${source}: more than ${fmtInt(maxSpans)} spans; send a smaller time window`);
    }
  }
  return spans;
}

// OTLP delivery is at-least-once: a span is identified by its trace and span id, and the first copy wins.
function dedupe(spans) {
  const unique = new Map();
  for (const span of spans) {
    const key = JSON.stringify([span.traceId, span.spanId]);
    if (!unique.has(key)) unique.set(key, span);
  }
  return [...unique.values()];
}

// --- classify.py ---

// Rules decide the clear cases; there is no model to ask in the browser, so unplaceable text stays UNKNOWN.
function errorKind(errorText, status) {
  if (status !== null) return FAILED; // the callee replied, so the caller did not give up
  const text = String(errorText).toLowerCase();
  if (ABANDONED_WORDS.some((word) => text.includes(word))) return ABANDONED;
  if (FAILED_WORDS.some((word) => text.includes(word))) return FAILED;
  return UNKNOWN;
}

// --- analyze.py ---

function callerGaveUp(parent) {
  if (!parent.error) return false;
  return errorKind(parent.errorText, parent.httpStatus) !== FAILED;
}

function zombieTailNs(parent, child, toleranceNs = DEFAULT_TOLERANCE_NS) {
  if (!callerGaveUp(parent)) return 0n;
  const runStart = runStartNs(child);
  const tail = child.endNs - (parent.endNs > runStart ? parent.endNs : runStart);
  return tail > toleranceNs ? tail : 0n;
}

// The SERVER span this span was started inside, skipping INTERNAL spans in between. Parent ids can form a cycle.
function enclosingServer(span, byId) {
  const seen = new Set([span.spanId]);
  let parent = pyTruthy(span.parentId) ? byId.get(span.parentId) : undefined;
  while (parent !== undefined && parent.kind !== "SERVER") {
    if (seen.has(parent.spanId) || seen.size > MAX_ANCESTOR_STEPS) return null;
    seen.add(parent.spanId);
    parent = pyTruthy(parent.parentId) ? byId.get(parent.parentId) : undefined;
  }
  return parent === undefined ? null : parent;
}

const sumOf = (edges, field) => edges.reduce((total, edge) => total + edge[field], 0n);

function makeReport(userRequests, edges, unclassified, inputTokens, outputTokens, wastedIn, wastedOut) {
  const dependencyCalls = edges.reduce((total, edge) => total + edge.calls, 0);
  const totalWork = sumOf(edges, "workNs");
  const usedWork = sumOf(edges, "usedNs");
  return {
    userRequests, edges, unclassified, inputTokens, outputTokens,
    wastedInputTokens: wastedIn, wastedOutputTokens: wastedOut,
    dependencyCalls,
    totalWorkNs: totalWork,
    usedWorkNs: usedWork,
    zombieWorkNs: sumOf(edges, "zombieNs"),
    tailNs: sumOf(edges, "tailNs"),
    amplification: dependencyCalls / Math.max(1, userRequests),
    goodput: totalWork !== 0n ? bigDiv(usedWork, totalWork) : 1.0,
  };
}

function analyze(spans, toleranceNs = DEFAULT_TOLERANCE_NS) {
  const byId = new Map(spans.map((s) => [s.spanId, s]));
  const roots = spans.filter((s) => s.kind === "SERVER" && s.parentId === null);
  const served = new Map();
  for (const s of spans) if (s.kind === "SERVER") served.set(s.service, (served.get(s.service) ?? 0) + 1);
  // One call = a CLIENT span in the caller plus the SERVER span it caused in the callee.
  const callerOf = new Map();
  for (const s of spans) {
    if (s.kind === "SERVER" && byId.has(s.parentId) && byId.get(s.parentId).kind === "CLIENT") {
      callerOf.set(s.spanId, byId.get(s.parentId));
    }
  }
  const verdicts = new Map();

  // The callee job that made this job's call, if that job is itself a called job.
  const upstreamJob = (server) => {
    const upstream = enclosingServer(callerOf.get(server.spanId), byId);
    return upstream !== null && callerOf.has(upstream.spanId) ? upstream : null;
  };

  // (used, zombie) for one callee job. Waste flows downstream: if the job's own caller was abandoned by ITS caller,
  // this job is wasted too. Iterative on purpose: the chain of callers comes from untrusted input.
  const verdict = (server) => {
    const pending = [];
    const onPath = new Set();
    let job = server;
    while (job !== null && !verdicts.has(job.spanId) && !onPath.has(job.spanId)) {
      pending.push(job);
      onPath.add(job.spanId);
      job = upstreamJob(job);
    }
    for (const current of pending.reverse()) { // resolve from the top of the chain down
      const client = callerOf.get(current.spanId);
      let used = !client.error;
      let zombie = zombieTailNs(client, current, toleranceNs) > 0n;
      const upstream = upstreamJob(current);
      if (upstream !== null && verdicts.has(upstream.spanId)) {
        const [upUsed, upZombie] = verdicts.get(upstream.spanId);
        used = used && upUsed;
        zombie = zombie || upZombie;
      }
      verdicts.set(current.spanId, [used, zombie]);
    }
    return verdicts.get(server.spanId);
  };

  const totals = new Map();
  for (const [serverId, client] of callerOf) {
    const server = byId.get(serverId);
    const [used, zombie] = verdict(server);
    const key = JSON.stringify([client.service, server.service]);
    if (!totals.has(key)) {
      totals.set(key, { caller: client.service, callee: server.service, calls: 0, work: 0n, used: 0n, zombie: 0n, tail: 0n });
    }
    const edge = totals.get(key);
    edge.calls += 1;
    edge.work += runNs(server);
    edge.used += used ? runNs(server) : 0n;
    edge.zombie += zombie ? runNs(server) : 0n;
    edge.tail += zombieTailNs(client, server, toleranceNs);
  }

  let unclassified = 0;
  for (const client of callerOf.values()) {
    if (client.error && errorKind(client.errorText, client.httpStatus) === UNKNOWN) unclassified += 1;
  }

  // LLM tokens are an estimate, not an invoice: a token is "wasted" when the result nobody used paid for it.
  let inputTokens = 0n;
  let outputTokens = 0n;
  let wastedInput = 0n;
  let wastedOutput = 0n;
  for (const span of spans) {
    if (span.inputTokens === 0n && span.outputTokens === 0n) continue;
    const job = span.kind === "SERVER" ? span : enclosingServer(span, byId);
    const jobUsed = job === null || !verdicts.has(job.spanId) || verdicts.get(job.spanId)[0];
    const failedCall = span.kind === "CLIENT" && (
      span.error || (span.httpStatus !== null && (span.httpStatus >= 500 || RETRYABLE_STATUSES.has(span.httpStatus)))
    );
    inputTokens += span.inputTokens;
    outputTokens += span.outputTokens;
    if (!jobUsed || failedCall) {
      wastedInput += span.inputTokens;
      wastedOutput += span.outputTokens;
    }
  }

  const edges = [...totals.values()]
    .sort((a, b) => cmpStr(a.caller, b.caller) || cmpStr(a.callee, b.callee))
    .map((t) => ({
      caller: t.caller,
      callee: t.callee,
      calls: t.calls,
      fanout: t.calls / Math.max(1, served.get(t.caller) ?? 0),
      reach: t.calls / Math.max(1, roots.length),
      workNs: t.work,
      usedNs: t.used,
      zombieNs: t.zombie,
      tailNs: t.tail,
      goodput: t.work !== 0n ? bigDiv(t.used, t.work) : 1.0,
    }));
  return makeReport(roots.length, edges, unclassified, inputTokens, outputTokens, wastedInput, wastedOutput);
}

// --- retries.py ---

function failed(span) {
  const status = span.httpStatus;
  return span.error || (status !== null && (status >= 500 || RETRYABLE_STATUSES.has(status)));
}

// The server said the request itself is wrong (400, 404, 422...). Asking again cannot change that.
function answeredWithAClientError(span) {
  const status = span.httpStatus;
  return status !== null && status >= 400 && status < 500 && !RETRYABLE_STATUSES.has(status);
}

function methodOfSpan(span) {
  if (pyTruthy(span.method)) return span.method;
  const first = span.name.split(" ", 1)[0].toUpperCase();
  return HTTP_METHODS.has(first) ? first : "";
}

const IDENTIFIER = /^(\p{Nd}+|[0-9a-fA-F]{8,}|[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$/u;

// host/users/12345 -> host/users/{id}: the list of retried writes should read as routes, not as customers.
function route(target) {
  const slash = target.indexOf("/");
  const host = slash >= 0 ? target.slice(0, slash) : target;
  const path = slash >= 0 ? target.slice(slash + 1) : "";
  if (!path) return host;
  return host + "/" + path.split("/").map((part) => (IDENTIFIER.test(part) ? "{id}" : part)).join("/");
}

const chainRetries = (chain) => chain.attempts.length - 1;

function retryMap(spans, toleranceNs = DEFAULT_TOLERANCE_NS) {
  const byId = new Map(spans.map((s) => [s.spanId, s]));
  const calleeServers = new Map(); // CLIENT span id -> the SERVER spans it caused
  for (const s of spans) {
    if (s.kind === "SERVER" && byId.has(s.parentId) && byId.get(s.parentId).kind === "CLIENT") {
      if (!calleeServers.has(s.parentId)) calleeServers.set(s.parentId, []);
      calleeServers.get(s.parentId).push(s);
    }
  }
  const userRequests = spans.filter((s) => s.kind === "SERVER" && s.parentId === null).length;

  // 1. Group the client calls that could be the same call.
  const groups = new Map();
  let unattributed = 0;
  for (const s of spans) {
    if (s.kind !== "CLIENT") continue;
    const servers = calleeServers.get(s.spanId) ?? [];
    let operation;
    if (s.target) {
      operation = [methodOfSpan(s), s.target];
    } else if (servers.length) {
      operation = [servers[0].service, servers[0].name];
    } else {
      unattributed += 1;
      continue;
    }
    const enclosing = enclosingServer(s, byId);
    const scope = enclosing !== null ? enclosing.spanId : s.parentId;
    const key = JSON.stringify([s.traceId, scope, s.service, operation]);
    if (!groups.has(key)) groups.set(key, { scope, caller: s.service, spans: [] });
    groups.get(key).spans.push(s);
  }

  // 2. Inside a group, an attempt that starts after a FAILED one (or says it is a resend) continues the chain.
  const chains = [];
  for (const { scope, caller, spans: group } of groups.values()) {
    group.sort((a, b) => cmpBigInt(a.startNs, b.startNs) || cmpBigInt(a.endNs, b.endNs) || cmpStr(a.spanId, b.spanId));
    let current = null;
    for (const s of group) {
      if (current !== null) {
        const previous = current.attempts[current.attempts.length - 1];
        const saysSo = (s.resendCount ?? 0) >= 1;
        if (s.startNs + toleranceNs >= previous.endNs && (failed(previous) || saysSo)) {
          current.attempts.push(s);
          current.byAttribute = current.byAttribute && saysSo;
          continue;
        }
      }
      const firstServers = calleeServers.get(s.spanId) ?? [];
      current = { caller, callee: "", method: methodOfSpan(s), label: "", scope, attempts: [s], byAttribute: true };
      current.label = s.target ? route(s.target) : (firstServers.length ? firstServers[0].name : s.name);
      chains.push(current);
    }
  }
  for (const chain of chains) {
    let servedBy = null;
    for (const attempt of chain.attempts) {
      const servers = calleeServers.get(attempt.spanId);
      if (servers && servers.length) { servedBy = servers[0].service; break; }
    }
    const target = chain.attempts[0].target;
    const host = target.includes("/") ? target.slice(0, target.indexOf("/")) : target;
    chain.callee = (pyTruthy(servedBy) ? servedBy : null) || host || "unknown";
  }

  // 3. Per edge.
  const edges = new Map();
  const writes = new Map();
  for (const chain of chains) {
    const edgeKey = JSON.stringify([chain.caller, chain.callee]);
    if (!edges.has(edgeKey)) {
      edges.set(edgeKey, {
        caller: chain.caller, callee: chain.callee, logicalCalls: 0, attempts: 0, recovered: 0, exhausted: 0,
        writesRetried: 0, clientErrorsRetried: 0, inferred: false,
      });
    }
    const edge = edges.get(edgeKey);
    edge.logicalCalls += 1;
    edge.attempts += chain.attempts.length;
    if (!chainRetries(chain)) continue;
    if (failed(chain.attempts[chain.attempts.length - 1])) edge.exhausted += 1; else edge.recovered += 1;
    edge.inferred = edge.inferred || !chain.byAttribute;
    if (chain.attempts.slice(0, -1).some(answeredWithAClientError)) edge.clientErrorsRetried += 1;
    if (WRITE_METHODS.has(chain.method)) {
      edge.writesRetried += 1;
      const name = `${chain.caller} -> ${chain.callee}`;
      const label = pySlice(chain.label, MAX_LABEL_CHARS);
      const writeKey = JSON.stringify([chain.method, name, label]);
      if (!writes.has(writeKey)) writes.set(writeKey, { method: chain.method, edge: name, target: label, calls: 0, maxAttempts: 0 });
      const write = writes.get(writeKey);
      write.calls += 1;
      write.maxAttempts = Math.max(write.maxAttempts, chain.attempts.length);
    }
  }

  // 4. Layers: a chain made from inside a job that an earlier chain's attempt caused sits one layer below it.
  const serverToChain = new Map();
  chains.forEach((chain, index) => {
    for (const attempt of chain.attempts) {
      for (const server of calleeServers.get(attempt.spanId) ?? []) serverToChain.set(server.spanId, index);
    }
  });
  const parentOf = chains.map((chain) => (pyTruthy(chain.scope) && serverToChain.has(chain.scope) ? serverToChain.get(chain.scope) : null));
  const layers = new Array(chains.length).fill(null);
  const depth = new Array(chains.length).fill(0);
  const root = new Array(chains.length).fill(0);
  for (let start = 0; start < chains.length; start++) {
    if (layers[start] !== null) continue;
    const pending = [];
    const onPath = new Set();
    let node = start;
    while (node !== null && layers[node] === null && !onPath.has(node) && pending.length <= MAX_ANCESTOR_STEPS) {
      pending.push(node);
      onPath.add(node);
      node = parentOf[node];
    }
    const top = pending[pending.length - 1];
    const parent = parentOf[top];
    let runningLayers;
    let runningDepth;
    let runningRoot;
    if (parent !== null && layers[parent] !== null) {
      runningLayers = layers[parent];
      runningDepth = depth[parent] + 1;
      runningRoot = root[parent];
    } else { // a real top of the stack, or a loop / absurdly deep chain: start counting here
      runningLayers = 0;
      runningDepth = 0;
      runningRoot = top;
    }
    for (const n of pending.reverse()) {
      runningLayers += chainRetries(chains[n]) ? 1 : 0;
      layers[n] = runningLayers;
      depth[n] = runningDepth;
      root[n] = runningRoot;
      runningDepth += 1;
    }
  }

  const received = new Map(); // attempts that reached one edge at one depth under one top call
  const receivedKey = (index) => JSON.stringify([root[index], depth[index], chains[index].caller, chains[index].callee]);
  chains.forEach((chain, index) => {
    received.set(receivedKey(index), (received.get(receivedKey(index)) ?? 0) + chain.attempts.length);
  });
  let best = null;
  let bestScore = [0, 0, 0];
  chains.forEach((chain, index) => {
    if (layers[index]) {
      const score = [received.get(receivedKey(index)), layers[index], depth[index]];
      if (score[0] > bestScore[0] || (score[0] === bestScore[0] && (score[1] > bestScore[1] || (score[1] === bestScore[1] && score[2] > bestScore[2])))) {
        best = index;
        bestScore = score;
      }
    }
  });
  let worst = null;
  if (best !== null) {
    const path = [];
    const seen = new Set();
    let node = best;
    while (node !== null && !seen.has(node) && path.length <= MAX_ANCESTOR_STEPS) {
      path.push(node);
      seen.add(node);
      node = parentOf[node];
    }
    path.reverse();
    worst = {
      layers: layers[best],
      multiplication: bestScore[0],
      steps: path.map((i) => [chains[i].caller, chains[i].callee, chains[i].attempts.length]),
    };
  }

  const withDerived = [...edges.values()].map((e) => {
    const retries = e.attempts - e.logicalCalls;
    return {
      ...e,
      retries,
      attemptsPerCall: e.logicalCalls ? e.attempts / e.logicalCalls : 1.0,
      retryShare: e.attempts ? retries / e.attempts : 0.0,
    };
  });
  const ordered = withDerived.sort((a, b) => (b.retries - a.retries) || (b.attempts - a.attempts) || cmpStr(a.caller, b.caller) || cmpStr(a.callee, b.callee));
  const orderedWrites = [...writes.values()].sort((a, b) => (b.calls - a.calls) || cmpStr(a.method, b.method) || cmpStr(a.edge, b.edge) || cmpStr(a.target, b.target));
  const logicalCalls = ordered.reduce((total, e) => total + e.logicalCalls, 0);
  const attempts = ordered.reduce((total, e) => total + e.attempts, 0);
  return {
    userRequests,
    edges: ordered,
    worst,
    writes: orderedWrites,
    unattributed,
    logicalCalls,
    attempts,
    retries: attempts - logicalCalls,
    retryShare: attempts ? (attempts - logicalCalls) / attempts : 0.0,
  };
}

// --- diagnose.py ---

function finding(edge, label, evidence, advice) {
  return { edge, label, evidence, advice };
}

function diagnoseEdge(edge) {
  const name = `${edge.caller} -> ${edge.callee}`;
  if (edge.workNs === 0n || edge.goodput >= HEALTHY_GOODPUT) {
    return [finding(name, HEALTHY, `goodput ${fmtPct0(edge.goodput)}`, "nothing to fix on this edge")];
  }
  const tailShare = bigDiv(edge.tailNs, edge.workNs);
  const found = [];
  if (edge.reach >= STORM_REACH && edge.goodput < LOW_GOODPUT) {
    found.push(finding(
      name, RETRY_STORM,
      `${fmtDecimal(edge.reach, 1)} calls per user request and only ${fmtPct0(edge.goodput)} of the work was used`,
      "cap retries with a retry budget (about 10% of traffic), back off with jitter, and stop retrying once "
      + "the deadline has passed",
    ));
  }
  if (tailShare >= ZOMBIE_SHARE) {
    found.push(finding(
      name, ZOMBIE_WORK,
      `${fmtPct0(tailShare)} of the callee's running time happened after the caller had left`,
      "send the caller's remaining time to the callee and cancel the work when it passes (deadline propagation)",
    ));
  }
  if (edge.goodput < LOW_GOODPUT && tailShare < SMALL_SHARE) {
    found.push(finding(
      name, DOOMED_WORK,
      `goodput is ${fmtPct0(edge.goodput)} although almost nothing runs after the caller leaves`,
      "jobs start with too little time left to finish: refuse them up front (load shedding or admission "
      + "control) so the capacity goes to jobs that can still succeed",
    ));
  }
  return found.length ? found : [finding(
    name, DEGRADED, `goodput ${fmtPct0(edge.goodput)} with no single dominant pattern`,
    "look at the slowest calls and the error mix on this edge",
  )];
}

const diagnose = (report) => report.edges.flatMap(diagnoseEdge);

function diagnoseRetries(report) {
  const found = [];
  if (report.worst && report.worst.layers >= 2) {
    const stack = report.worst.steps.map(([caller, callee, attempts]) => `${caller} -> ${callee} x${attempts}`).join(", then ");
    const last = report.worst.steps[report.worst.steps.length - 1];
    found.push(finding(
      last[0] + " -> " + last[1], RETRY_LAYERS,
      `${report.worst.layers} layers retried (${stack}): the last service received ${report.worst.multiplication} `
      + "calls for one call to the first",
      "let one layer own the retry, the one closest to the failure, and make the others fail fast: a retry from a "
      + "layer that did not cause the failure only multiplies it",
    ));
  }
  for (const write of report.writes) {
    found.push(finding(
      write.edge, RETRIED_WRITES,
      `${write.method} ${write.target} was attempted up to ${write.maxAttempts} times in ${write.calls} call(s)`,
      "check that this write is idempotent (an idempotency key, or a duplicate check on the server) or stop "
      + "retrying it: a retried write that already took effect happens twice",
    ));
  }
  for (const edge of report.edges) {
    const name = `${edge.caller} -> ${edge.callee}`;
    if (edge.logicalCalls >= MIN_CALLS_FOR_BUDGET && edge.retryShare > BUDGET_SHARE) {
      found.push(finding(
        name, RETRY_BUDGET,
        `${fmtPct0(edge.retryShare)} of attempts were retries (${edge.retries} of ${edge.attempts}); `
        + `${edge.exhausted} retried call(s) never succeeded`,
        `cap retries with a budget (about ${fmtPct0(BUDGET_SHARE)} of traffic) so a failing dependency gets fewer `
        + "calls, not more",
      ));
    }
    if (edge.clientErrorsRetried) {
      found.push(finding(
        name, RETRIED_CLIENT_ERRORS,
        `${edge.clientErrorsRetried} call(s) were retried after the server said the request itself was wrong `
        + "(a 4xx other than 408 and 429)",
        "do not retry these: asking again cannot change the answer",
      ));
    }
  }
  return found;
}

// --- product.py ---

const seconds = (ns) => pyRoundDouble(Number(ns) / 1e9, 3);
const short = (text) => pySlice(pyStr(text), MAX_NAME_CHARS);

function headline(report) {
  if (!report.edges.length) return "No calls between services were found in these traces.";
  if (report.goodput >= HEALTHY_GOODPUT) {
    return `Nothing wasteful found: ${fmtPct0(report.goodput)} of the work your services did was used.`;
  }
  return `${fmtPct0(1 - report.goodput)} of the work your services did for these requests was never used.`;
}

// The retry map as plain data. Every name is cut short: some of them come straight out of a URL in the upload.
function retriesJson(retries) {
  const worst = retries.worst;
  return {
    totals: {
      logical_calls: retries.logicalCalls, attempts: retries.attempts, retries: retries.retries,
      retry_share: pyRoundDouble(retries.retryShare, 4), budget_share: BUDGET_SHARE,
    },
    edges: retries.edges.map((e) => ({
      caller: short(e.caller), callee: short(e.callee), calls: e.logicalCalls, attempts: e.attempts,
      retries: e.retries, per_call: pyRoundDouble(e.attemptsPerCall, 2), retry_share: pyRoundDouble(e.retryShare, 4),
      recovered: e.recovered, exhausted: e.exhausted, writes: e.writesRetried,
      client_errors: e.clientErrorsRetried, inferred: e.inferred,
    })),
    worst: worst === null ? null : {
      layers: worst.layers,
      multiplication: worst.multiplication,
      steps: worst.steps.map(([c, d, n]) => ({ caller: short(c), callee: short(d), attempts: n })),
    },
    writes: retries.writes.map((w) => ({
      method: w.method, edge: short(w.edge), target: short(w.target), calls: w.calls, max_attempts: w.maxAttempts,
    })),
    unattributed: retries.unattributed,
  };
}

// What xray could not see or could not be sure of. Showing this is part of being trusted.
function notesFor(report, spans, retries) {
  const notes = [];
  if (report.dependencyCalls === 0) {
    notes.push({
      kind: "no_calls",
      text: "xray needs a CLIENT span in the caller and a SERVER span in the callee, linked by trace context "
        + "(the traceparent header). Check that both services are instrumented and send to the same place.",
    });
    return notes;
  }
  if (report.userRequests < MIN_REQUESTS_FOR_RATIOS) {
    notes.push({
      kind: "small_sample",
      text: `Only ${report.userRequests} user request(s) in this sample. The numbers are exact for it, `
        + "but too few requests to say what is typical.",
    });
  }
  if (report.unclassified) {
    notes.push({
      kind: "unclassified",
      text: `${report.unclassified} caller errors could not be classified as 'caller gave up' or 'callee replied `
        + "with an error'. They were counted as 'gave up', so the zombie numbers may be overstated.",
    });
  }
  if (report.inputTokens !== 0n || report.outputTokens !== 0n) {
    notes.push({
      kind: "tokens_estimate",
      text: "LLM tokens are an estimate, not an invoice: they are counted for calls whose surrounding job was "
        + "abandoned or whose client call never delivered an answer "
        + `(${fmtInt(report.wastedInputTokens)} in + ${fmtInt(report.wastedOutputTokens)} out of `
        + `${fmtInt(report.inputTokens)} + ${fmtInt(report.outputTokens)}). Whether `
        + "a cancelled call stops billing depends on the provider.",
    });
  }
  if (retries.retries && retries.edges.some((e) => e.inferred)) {
    notes.push({
      kind: "retries_inferred",
      text: "These retries were recognised, not read: the same job called the same URL again after a failure. Your "
        + "client libraries do not mark retries (http.request.resend_count), so a loop that deliberately repeats "
        + "a failing call would look the same.",
    });
  }
  if (retries.unattributed) {
    notes.push({
      kind: "ungrouped_calls",
      text: `${retries.unattributed} client calls had neither a URL nor a callee span, so retries among them could `
        + "not be seen.",
    });
  }
  if (!spans.some((s) => s.kind === "SERVER" && s.queueNs !== 0n)) {
    notes.push({
      kind: "queue_wait",
      text: "No span reports queue wait (attribute app.queue_wait_ms). If your services queue requests before "
        + "running them, waiting is being counted as work.",
    });
  }
  const byId = new Map(spans.map((s) => [s.spanId, s]));
  let skewed = 0;
  for (const s of spans) {
    if (s.kind === "SERVER" && byId.has(s.parentId) && byId.get(s.parentId).kind === "CLIENT"
        && s.startNs + DEFAULT_TOLERANCE_NS < byId.get(s.parentId).startNs) {
      skewed += 1;
    }
  }
  if (skewed) {
    notes.push({
      kind: "clock_skew",
      text: `${skewed} calls have a callee that started before its caller did: the machines' clocks probably `
        + "disagree, which can hide or invent zombie work.",
    });
  }
  return notes;
}

// Python str(os.path.basename(name or "upload")) cleaned for error messages: short, no control characters.
function displayName(name) {
  const text = pyTruthy(name) ? pyStr(name) : "upload";
  const base = text.slice(text.lastIndexOf("/") + 1);
  const cleaned = pySlice(base.replace(/[^\p{L}\p{N}_.\- ]/gu, "_"), 60);
  return cleaned || "upload";
}

// UTF-8 length of the text, the way Python's text.encode("utf-8", errors="ignore") counts it: a lone surrogate is dropped.
function utf8Length(text) {
  if (!/[\ud800-\udfff]/.test(text)) return new TextEncoder().encode(text).length;
  let bytes = 0;
  for (let i = 0; i < text.length; i++) {
    const unit = text.charCodeAt(i);
    if (unit >= 0xd800 && unit <= 0xdbff && i + 1 < text.length && (text.charCodeAt(i + 1) & 0xfc00) === 0xdc00) {
      bytes += 4;
      i += 1;
    } else if (unit >= 0xd800 && unit <= 0xdfff) {
      continue;
    } else {
      bytes += unit < 0x80 ? 1 : unit < 0x800 ? 2 : 3;
    }
  }
  return bytes;
}

/* files: [name, text] pairs (or {name, text} objects). Returns the report as plain JSON-able data, or throws
 * UploadError (with .status) exactly where product.analyze_upload raises it. `options` can tighten maxFiles / maxSpans
 * the way XRAY_MAX_FILES / XRAY_MAX_SPANS do for a deployment. */
function analyzeFiles(files, options = {}) {
  const maxFiles = options.maxFiles ?? MAX_FILES;
  const maxSpans = options.maxSpans ?? MAX_SPANS;
  if (!files || !files.length) throw new UploadError(400, "No files were sent.");
  if (files.length > maxFiles) throw new UploadError(400, `At most ${maxFiles} files at a time.`);

  let spans = [];
  let totalBytes = 0;
  for (const file of files) {
    const [name, text] = Array.isArray(file) ? file : [file.name, file.text];
    totalBytes += utf8Length(text);
    try {
      for (const span of parseText(text, displayName(name), maxSpans - spans.length)) spans.push(span);
    } catch (error) {
      if (error instanceof TooManySpans) throw new UploadError(413, error.message); // "too large", not "malformed"
      if (error instanceof TraceFormatError) throw new UploadError(422, error.message);
      throw error;
    }
    if (spans.length > maxSpans) throw new UploadError(413, `More than ${fmtInt(maxSpans)} spans in total; send a smaller time window.`);
  }

  spans = dedupe(spans);
  const report = analyze(spans);
  const retries = retryMap(spans);
  return {
    summary: {
      headline: headline(report),
      user_requests: report.userRequests,
      dependency_calls: report.dependencyCalls,
      amplification: pyRoundDouble(report.amplification, 2),
      work_s: seconds(report.totalWorkNs),
      used_s: seconds(report.usedWorkNs),
      goodput: pyRoundDouble(report.goodput, 4),
      zombie_s: seconds(report.zombieWorkNs),
      tail_s: seconds(report.tailNs),
      tokens_in: toJsonInt(report.inputTokens),
      tokens_out: toJsonInt(report.outputTokens),
      wasted_tokens_in: toJsonInt(report.wastedInputTokens),
      wasted_tokens_out: toJsonInt(report.wastedOutputTokens),
    },
    edges: report.edges.map((e) => ({
      caller: e.caller, callee: e.callee, calls: e.calls,
      fanout: pyRoundDouble(e.fanout, 2), reach: pyRoundDouble(e.reach, 2),
      work_s: seconds(e.workNs), used_s: seconds(e.usedNs),
      zombie_s: seconds(e.zombieNs), tail_s: seconds(e.tailNs),
      goodput: pyRoundDouble(e.goodput, 4),
    })),
    findings: [...diagnose(report), ...diagnoseRetries(retries)],
    retries: retriesJson(retries),
    notes: notesFor(report, spans, retries),
    input: { files: files.length, spans: spans.length, bytes: totalBytes },
  };
}

export { analyzeFiles, parseText, dedupe, analyze, retryMap, diagnose, diagnoseRetries, UploadError, TraceFormatError, TooManySpans };
