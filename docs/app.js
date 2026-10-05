// XRAY-STATIC: baked demo for GitHub Pages. Built by demo/build_pages.py; do not edit the copy in docs/site/.
const STATIC_SAMPLES = ["retry-storm", "after-fix", "layered-retries"];
"use strict";

// Defaults only: init() replaces them with what the server really enforces (GET /api/features).
let MAX_FILES = 10;
let MAX_BYTES = 15 * 1024 * 1024;
const $ = (selector) => document.querySelector(selector);
const pct = (x) => Math.round(x * 100) + "%";
const sec = (s) => (s >= 100 ? Math.round(s) : s.toFixed(1)) + "s";

// Builds elements without ever parsing HTML. Everything from a trace file (service names!) or from the API is
// untrusted text, so it only ever becomes text nodes.
function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (value === true) el.setAttribute(key, "");
    else if (value !== false && value != null) el.setAttribute(key, value);
  }
  el.append(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
  return el;
}

async function getJSON(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(url + " -> " + response.status);
  return response.json();
}

function setStatus(text, isError) {
  const el = $("#status");
  el.textContent = text;
  el.className = isError ? "error" : "";
  el.setAttribute("role", isError ? "alert" : "status");
  // On a phone this line sits below the fold, and an error nobody can see is no error message.
  if (isError) el.scrollIntoView({ block: "nearest" });
}

/* ---------- analyzing ---------- */

async function analyze(files) {
  if (!files.length) return;
  setStatus("This static demo has no server behind it. Run the analyzer on your own traces in a terminal: "
    + "python -m xray diagnose your-traces.json (see the Export traces section for how to get the file).", true);
}

async function handleFiles(fileList) {
  const picked = [...fileList];
  if (picked.reduce((total, file) => total + file.size, 0) > MAX_BYTES) return setStatus(`Too large: ${MAX_BYTES / (1024 * 1024)} MB in total at most.`, true);
  analyze(await Promise.all(picked.map(async (file) => ({ name: file.name, text: await file.text() }))));
}

async function trySample(name) {
  if (!STATIC_SAMPLES.includes(name)) return setStatus("This demo only ships the bundled samples.", true);
  try {
    renderReport(await getJSON("data/" + encodeURIComponent(name) + ".report.json"));
    setStatus("A pre-analyzed sample. To read your own traces, run xray locally (below).");
  } catch (error) {
    setStatus("Could not load the sample report.", true);
  }
}

/* ---------- the report ---------- */

const FINDING_TONE = {
  healthy: "good", degraded: "", retry_storm: "bad", zombie_work: "bad", doomed_work: "bad",
  retry_layers: "bad", retried_writes: "bad", retry_budget: "bad", retried_client_errors: "",
};

function card(number, what, tone) {
  return h("div", { class: "card" }, h("div", { class: "num " + (tone || "") }, number), h("div", { class: "what" }, what));
}

// Red when most of the work was wasted, amber when some was, green when little was.
const wasteTone = (goodput) => (goodput < 0.5 ? "bad" : goodput < 0.8 ? "mid" : "good");

function edgesTable(edges) {
  const head = h("tr", null, ...["edge", "calls", "per user request", "work", "goodput", "zombie work", "tail"].map((t) => h("th", null, t)));
  const rows = edges.map((e) =>
    h("tr", null,
      h("td", null, e.caller + " → " + e.callee), h("td", null, e.calls), h("td", null, e.reach.toFixed(1) + "×"),
      h("td", null, sec(e.work_s)), h("td", null, pct(e.goodput)), h("td", null, sec(e.zombie_s)), h("td", null, sec(e.tail_s))));
  return h("div", { class: "panel" }, h("table", null, h("thead", null, head), h("tbody", null, ...rows)));
}

/* ---------- the retry map: which calls were attempted again, how many layers did it, what that multiplied ---------- */

// A bar for the share of attempts that were retries, with a tick where a retry budget would usually stop it.
function shareBar(share, budget) {
  const fill = h("span", { class: "fill " + (share > budget ? "over" : "ok") });
  fill.style.width = Math.round(Math.min(1, share) * 100) + "%";
  const tick = h("span", { class: "tick", title: `a retry budget usually caps retries near ${pct(budget)}` });
  tick.style.left = Math.round(budget * 100) + "%";
  return h("span", { class: "share" }, h("span", { class: "track" }, fill, tick), h("span", { class: "pctlabel" }, pct(share)));
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function retryEdgeRow(e, budget) {
  const marks = [
    e.writes > 0 && h("span", { class: "mark-pill warn", title: "a POST, PUT, PATCH or DELETE was attempted more than once" }, "write"),
    e.client_errors > 0 && h("span", { class: "mark-pill warn", title: "retried after the server said the request itself was wrong" }, "4xx retried"),
    e.inferred && h("span", { class: "mark-pill", title: "recognised by a rule: the client library does not mark its retries" }, "inferred"),
  ];
  return h("div", { class: "rrow" },
    h("div", { class: "rname" }, e.caller + " → " + e.callee),
    h("div", { class: "rper" }, e.per_call.toFixed(1) + "× per call"),
    shareBar(e.retry_share, budget),
    h("div", { class: "routcome", title: "rescued: a retry worked. failed for good: every attempt failed." }, `${e.recovered} rescued · ${e.exhausted} failed for good`),
    h("div", { class: "rtags" }, ...marks));
}

// service-a ─×3→ service-b ─×3→ service-c, and what the last one received.
function retryStack(worst) {
  const names = [worst.steps[0].caller, ...worst.steps.map((step) => step.callee)];
  const flow = [];
  names.forEach((name, i) => {
    flow.push(h("span", { class: "node" }, name));
    if (i < worst.steps.length) flow.push(h("span", { class: "hop" }, "×" + worst.steps[i].attempts));
  });
  return h("div", { class: "stack" },
    h("div", { class: "flow" }, ...flow),
    h("p", { class: "sub" }, h("strong", null, `${worst.layers} layers retried. `),
      `${names[names.length - 1]} received ${worst.multiplication} calls for one call to ${worst.steps[0].callee}.`));
}

function retriedWrites(writes) {
  return h("div", { class: "writes" },
    h("strong", null, "Writes that were attempted more than once"),
    h("ul", null, ...writes.map((w) => h("li", null, h("code", null, `${w.method} ${w.target}`),
      ` (${w.edge}) was attempted up to ${w.max_attempts}× in ${w.calls} call(s). If the first attempt got through, it happened twice: is it idempotent?`))));
}

// Deadline propagation is the fix for work done after a caller left, so the saving is already measured.
function savings(summary) {
  if (!(summary.tail_s > 0 && summary.work_s > 0)) return null;
  return h("p", { class: "saves" }, h("strong", null, "What cancelling would save: "),
    `deadline propagation would have cancelled ${sec(summary.tail_s)} of the ${sec(summary.work_s)} of work (${pct(summary.tail_s / summary.work_s)}).`);
}

function retryMap(result) {
  const r = result.retries;
  if (!r || !r.totals.retries) return null;
  const retried = r.edges.filter((e) => e.retries > 0);
  const quiet = r.edges.length - retried.length;
  return h("div", { class: "retrymap" },
    h("h3", null, "Retry map"),
    h("p", { class: "sub" }, `${plural(r.totals.logical_calls, "call")} made ${plural(r.totals.attempts, "attempt")}: ${pct(r.totals.retry_share)} of attempts were retries. A retry budget usually caps this near ${pct(r.totals.budget_share)}.`),
    h("div", { class: "panel rows" }, ...retried.map((e) => retryEdgeRow(e, r.totals.budget_share))),
    quiet > 0 && h("p", { class: "note" }, `${quiet} other edge(s) had no retries.`),
    r.worst && r.worst.layers >= 2 && retryStack(r.worst),
    r.writes.length > 0 && retriedWrites(r.writes),
    savings(result.summary));
}

function markdown(result) {
  const s = result.summary;
  const lines = ["# overload-xray report", "", `**${s.headline}**`, "",
    `- user requests: ${s.user_requests}; calls between services: ${s.dependency_calls} (${s.amplification}× per user request)`,
    `- work: ${s.work_s}s; used by callers: ${s.used_s}s (goodput ${pct(s.goodput)}); done after callers left: ${s.tail_s}s`, ""];
  if (result.findings.length) {
    lines.push("## Findings", "");
    result.findings.forEach((f) => lines.push(`- **${f.label}** on ${f.edge}: ${f.evidence}. Fix: ${f.advice}`));
    lines.push("");
  }
  if (result.edges.length) {
    lines.push("| edge | calls | per user request | work | goodput | zombie work | tail |", "|---|---|---|---|---|---|---|");
    result.edges.forEach((e) => lines.push(`| ${e.caller} → ${e.callee} | ${e.calls} | ${e.reach}× | ${e.work_s}s | ${pct(e.goodput)} | ${e.zombie_s}s | ${e.tail_s}s |`));
    lines.push("");
  }
  const r = result.retries;
  if (r && r.totals.retries) {
    lines.push("## Retry map", "", `${plural(r.totals.logical_calls, "call")} made ${plural(r.totals.attempts, "attempt")}: ${pct(r.totals.retry_share)} were retries (budget guide: ${pct(r.totals.budget_share)}).`, "");
    r.edges.filter((e) => e.retries > 0).forEach((e) => lines.push(`- ${e.caller} → ${e.callee}: ${e.per_call}× per call, ${pct(e.retry_share)} retries, ${e.recovered} rescued, ${e.exhausted} failed for good${e.inferred ? " (inferred)" : ""}`));
    if (r.worst && r.worst.layers >= 2) {
      lines.push("", `Worst stack: ${r.worst.layers} layers (${r.worst.steps.map((x) => `${x.caller} → ${x.callee} ×${x.attempts}`).join(", then ")}); the last service received ${r.worst.multiplication} calls for one call to the first.`);
    }
    r.writes.forEach((w) => lines.push(`- retried write: ${w.method} ${w.target} (${w.edge}), up to ${w.max_attempts}×`));
    lines.push("");
  }
  if (result.notes.length) {
    lines.push("## What xray could not see", "");
    result.notes.forEach((n) => lines.push(`- ${n.text}`));
  }
  return lines.join("\n");
}

function download(result) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], { type: "application/json" }));
  const link = h("a", { href: url, download: "overload-xray-report.json" });
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function renderReport(result) {
  const s = result.summary;
  const hasEdges = result.edges.length > 0;
  const copy = h("button", { type: "button", class: "btn", onclick: async () => {
    try { await navigator.clipboard.writeText(markdown(result)); setStatus("Copied the report as Markdown."); }
    catch (error) { setStatus("Could not copy. Use “Download JSON” instead.", true); }
  } }, "Copy as Markdown");
  const save = h("button", { type: "button", class: "btn", onclick: () => download(result) }, "Download JSON");

  const section = $("#report");
  section.hidden = false;
  section.replaceChildren(
    // Only numbers go into this label: nothing from the trace file.
    h("p", { class: "eyebrow" }, `// report · ${result.input.spans.toLocaleString()} spans · ${result.input.files} file(s)`),
    h("h2", { id: "report-title", class: "headline", tabindex: "-1" }, s.headline),
    hasEdges && h("div", { class: "cards" },
      card(pct(1 - s.goodput), "of the work was never used", wasteTone(s.goodput)),
      card(s.amplification.toFixed(1) + "×", "calls per user request", s.amplification >= 2 ? "mid" : ""),
      card(sec(s.tail_s), "of work done after callers had left", s.tail_s > 0 ? "bad" : ""),
      card(sec(s.work_s), "of work in total")),
    hasEdges && retryMap(result),
    hasEdges && h("h3", null, "What to do"),
    ...result.findings.map((f) =>
      h("div", { class: "finding " + (FINDING_TONE[f.label] || "") },
        h("span", { class: "chip" }, f.label.replaceAll("_", " ")), " ", h("strong", null, f.edge),
        h("p", null, f.evidence + "."), h("p", null, h("strong", null, "Fix: "), f.advice))),
    hasEdges && h("h3", null, "Per service call"),
    hasEdges && edgesTable(result.edges),
    h("h3", null, "What xray could not see"),
    result.notes.length
      ? h("ul", { class: "notes" }, ...result.notes.map((n) => h("li", null, n.text)))
      : h("p", { class: "sub" }, "Nothing to flag for this input."),
    h("div", { class: "actions" }, copy, save));
  $("#report-title").focus();
}

/* ---------- evidence: the load test ---------- */

const CAPACITY = 8; // jobs per second the load test's backend can finish: 4 at once, 0.5 s each (demo/loadtest.py)

const MODES = [
  { key: "baseline", label: "baseline: no deadline sent", color: "var(--bad)", dash: "none", mark: "circle" },
  { key: "fixed", label: "cancel at the deadline", color: "var(--mid)", dash: "6 4", mark: "square" },
  { key: "shed", label: "cancel + refuse doomed jobs", color: "var(--good)", dash: "2 3", mark: "diamond" },
];

function drawChart(rows, metric) {
  const modes = MODES.filter((m) => rows.length && m.key + "_" + metric in rows[0]);
  if (!modes.length) return;
  const W = 640, H = 260, L = 44, R = 12, T = 12, B = 34;
  const xs = rows.map((r) => r.offered_per_s);
  const ymax = metric === "success_share" ? 1 : Math.max(...modes.flatMap((m) => rows.map((r) => r[m.key + "_" + metric]))) * 1.15;
  const x = (v) => L + ((v - Math.min(...xs)) / (Math.max(...xs) - Math.min(...xs))) * (W - L - R);
  const y = (v) => T + (1 - v / ymax) * (H - T - B);
  const fmt = (v) => (metric === "success_share" ? pct(v) : v.toFixed(0));
  // Only numbers and fixed labels go into this string; nothing from a trace or a user.
  let svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" aria-hidden="true">`;
  for (let i = 0; i <= 4; i++) {
    const v = (ymax * i) / 4;
    svg += `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${fmt(v)}</text>`;
  }
  xs.forEach((v) => (svg += `<text x="${x(v)}" y="${H - 14}" text-anchor="middle">${v}</text>`));
  svg += `<text x="${(L + W - R) / 2}" y="${H - 1}" text-anchor="middle">offered load (user requests per second)</text>`;
  modes.forEach((m) => {
    const pts = rows.map((r) => [x(r.offered_per_s), y(r[m.key + "_" + metric])]);
    svg += `<polyline fill="none" stroke="${m.color}" stroke-width="2.5" stroke-dasharray="${m.dash}" points="${pts.map((p) => p.join(",")).join(" ")}"/>`;
    pts.forEach(([px, py]) => {
      svg += m.mark === "circle" ? `<circle cx="${px}" cy="${py}" r="4" fill="${m.color}"/>`
        : m.mark === "square" ? `<rect x="${px - 4}" y="${py - 4}" width="8" height="8" fill="${m.color}"/>`
        : `<path d="M${px} ${py - 5}L${px + 5} ${py}L${px} ${py + 5}L${px - 5} ${py}Z" fill="${m.color}"/>`;
    });
  });
  $("#chart").innerHTML = svg + "</svg>";
  $("#legend").replaceChildren(...modes.map((m) => {
    const item = h("span", null, m.label);
    item.style.setProperty("--c", m.color);
    item.style.setProperty("--s", m.dash === "none" ? "solid" : "dashed");
    return item;
  }));
  $("#chart-note").textContent = `Synthetic services on one machine, one run per point. The backend can finish about ${CAPACITY} jobs per second; callers wait 1s per attempt and retry twice.`;
}

// The three headline numbers, read from the same rows as the chart, so the page cannot disagree with its own data.
function drawProof(rows) {
  const tiles = rows
    .filter((r) => r.offered_per_s >= CAPACITY && "baseline_success_share" in r && "shed_success_share" in r)
    .map((r) => h("div", { class: "tile" },
      h("div", { class: "num" }, h("span", { class: "was" }, pct(r.baseline_success_share)), h("span", { class: "arrow" }, " → "), h("span", { class: "now" }, pct(r.shed_success_share))),
      h("div", { class: "what" }, `of user requests succeed at ${r.offered_per_s} per second (${r.offered_per_s / CAPACITY}× what the backend can finish)`)));
  $("#proof").replaceChildren(...tiles);
}

/* ---------- saved runs (only when a database is configured) ---------- */

function renderRuns(runs) {
  const rows = runs.flatMap((r) => {
    const tone = r.goodput > 0.6 ? "var(--good)" : r.goodput > 0.25 ? "var(--mid)" : "var(--bad)";
    const bar = h("span", { class: "bar" });
    bar.style.width = Math.round(r.goodput * 60) + "px";
    bar.style.background = tone;
    return [
      h("tr", { class: "run", "data-id": r.id }, h("td", null, r.name), h("td", null, r.calls), h("td", null, sec(r.work_ns / 1e9)),
        h("td", null, bar, pct(r.goodput)), h("td", null, sec(r.tail_ns / 1e9))),
      h("tr", { id: "d" + r.id, hidden: true }, h("td", { colspan: 5 })),
    ];
  });
  const head = h("tr", null, ...["run", "calls", "work", "goodput", "zombie tail"].map((t) => h("th", null, t)));
  const box = $("#runs");
  box.replaceChildren(h("table", { class: "runs-table" }, h("thead", null, head), h("tbody", null, ...rows)));
  box.onclick = async (event) => {
    const row = event.target.closest("tr.run");
    if (!row) return;
    const detail = document.getElementById("d" + row.dataset.id);
    if (!detail.hidden) { detail.hidden = true; return; }
    const run = await getJSON("/api/runs/" + row.dataset.id);
    detail.firstElementChild.replaceChildren(edgesTable(run.edges.map((e) => ({ ...e, work_s: e.work_ns / 1e9, zombie_s: e.zombie_ns / 1e9, tail_s: e.tail_ns / 1e9 }))));
    detail.hidden = false;
  };
}

/* ---------- start ---------- */

// The same call the page makes, as a command: the address is this page's own origin (not user input).
function setupCurl() {
  const command = "python -m xray diagnose your-traces.json   # run locally, nothing uploaded";
  const button = $("#copy-curl");
  $("#curl-cmd").textContent = command;
  button.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(command); button.textContent = "Copied"; }
    catch (error) { button.textContent = "Copy failed"; }
    setTimeout(() => { button.textContent = "Copy"; }, 1800);
  });
}

function init() {
  const drop = $("#drop");
  ["dragenter", "dragover"].forEach((type) => drop.addEventListener(type, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((type) => drop.addEventListener(type, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => handleFiles(e.dataTransfer.files));
  $("#file").addEventListener("change", (e) => { handleFiles(e.target.files); e.target.value = ""; });
  document.querySelectorAll("[data-sample]").forEach((button) => button.addEventListener("click", () => trySample(button.dataset.sample)));

  setupCurl();

  // A link like /#sample=retry-storm opens the page with that sample already analyzed, so a result can be shared.
  // The server only serves the names it knows, so anything else in the link just fails to load. The in-page links
  // (#how, #evidence...) have no "sample=" in them, so they do nothing here.
  const runLinkedSample = () => {
    const linked = new URLSearchParams(location.hash.slice(1)).get("sample");
    if (linked) trySample(linked);
  };
  runLinkedSample();
  window.addEventListener("hashchange", runLinkedSample);

  getJSON("data/loadtest.json").then((rows) => {
    if (!rows.length) return;
    $("#evidence").hidden = false;
    drawProof(rows);
    drawChart(rows, $("#metric").value);
    $("#metric").addEventListener("change", () => drawChart(rows, $("#metric").value));
  }).catch(() => {});

  /* static demo: no /api/features, no saved runs */
}

init();
