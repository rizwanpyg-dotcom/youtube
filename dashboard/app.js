"use strict";

const TABS = ["Overview", "Pipeline", "Calendar", "Research", "Performance", "Operations"];
const STAGES = ["planned", "researched", "fact_checked", "scripted", "script_approval", "voiced", "visuals_ready",
  "rendered", "thumbnailed", "seo_ready", "qc_passed", "final_approval", "package_ready", "scheduled", "published"];

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n, d = 0) => (n === null || n === undefined || n === "") ? "–" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d });
const when = (t) => t ? new Date(t).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "–";

let repo = null;
const issueUrl = (n) => repo && n ? `https://github.com/${repo}/issues/${n}` : null;
const link = (url, text) => url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(text)}</a>` : esc(text);

function table(rows, cols) {
  if (!rows || !rows.length) return `<p class="empty">Nothing here yet.</p>`;
  const head = cols.map((c) => `<th>${esc(c.label)}</th>`).join("");
  const body = rows.map((r) => "<tr>" + cols.map((c) => {
    const raw = c.render ? c.render(r) : esc(r[c.key]);
    return `<td class="${c.num ? "num" : ""}">${raw ?? ""}</td>`;
  }).join("") + "</tr>").join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function lineChart(el, points, label) {
  if (!points.length) { el.innerHTML = `<p class="empty">No analytics yet. These appear after the first published video.</p>`; return; }
  const W = 800, H = 220, P = 30;
  const xs = points.map((_, i) => i), ys = points.map((p) => p.v);
  const minY = Math.min(...ys), maxY = Math.max(...ys) || 1;
  const sx = (i) => P + (i / Math.max(1, xs.length - 1)) * (W - 2 * P);
  const sy = (v) => H - P - ((v - minY) / Math.max(1, maxY - minY)) * (H - 2 * P);
  const d = points.map((p, i) => `${i ? "L" : "M"}${sx(i).toFixed(1)},${sy(p.v).toFixed(1)}`).join(" ");
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}">
    <line x1="${P}" y1="${H - P}" x2="${W - P}" y2="${H - P}" stroke="var(--line)"/>
    <path d="${d}" fill="none" stroke="var(--accent)" stroke-width="2.5"/>
    <text x="${P}" y="16" fill="var(--muted)" font-size="12">${fmt(maxY)}</text>
    <text x="${P}" y="${H - 8}" fill="var(--muted)" font-size="12">${esc(points[0].t)}</text>
    <text x="${W - P}" y="${H - 8}" fill="var(--muted)" font-size="12" text-anchor="end">${esc(points[points.length - 1].t)}</text>
  </svg>`;
}

function bar(label, frac, detail) {
  const pct = Math.max(0, Math.min(1, frac || 0)) * 100;
  return `<div>${esc(label)} <span class="note">${esc(detail)}</span></div><div class="bar"><span style="width:${pct.toFixed(1)}%"></span></div>`;
}

function setupTabs() {
  const nav = $("tabs");
  let current = TABS[0];
  try { current = localStorage.getItem("tab") || current; } catch (e) { /* storage blocked */ }
  if (!TABS.includes(current)) current = TABS[0];
  const show = (name) => {
    document.querySelectorAll(".tab").forEach((s) => s.classList.toggle("active", s.dataset.tab === name));
    nav.querySelectorAll("button").forEach((b) => b.setAttribute("aria-selected", String(b.textContent === name)));
    try { localStorage.setItem("tab", name); } catch (e) { /* ignore */ }
  };
  nav.innerHTML = TABS.map((t) => `<button role="tab">${t}</button>`).join("");
  nav.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => show(b.textContent)));
  show(current);
}

function render(d) {
  repo = d.repo;
  $("generated").textContent = `Updated ${when(d.generated_at)} · publish mode: ${d.settings.publish_mode || "package"}` +
    (d.settings.pipeline_paused === "true" ? " · PAUSED" : "");

  const ctl = issueUrl(d.settings.control_issue);
  $("actions").innerHTML = [
    repo && `<a class="btn primary" href="https://github.com/${esc(repo)}/actions/workflows/pipeline.yml" target="_blank" rel="noopener">Run pipeline now</a>`,
    ctl && `<a class="btn" href="${esc(ctl)}" target="_blank" rel="noopener">Pause / resume (comment /pause)</a>`,
    repo && `<a class="btn" href="https://github.com/${esc(repo)}/issues?q=is%3Aopen+label%3Aapproval" target="_blank" rel="noopener">Approvals</a>`,
    d.run_url && `<a class="btn" href="${esc(d.run_url)}" target="_blank" rel="noopener">Last run</a>`,
  ].filter(Boolean).join("");

  const published = d.pipeline.filter((p) => p.stage === "published").length;
  const waiting = d.pipeline.filter((p) => p.status === "waiting").length;
  const failing = d.health.filter((h) => !h.ok).length;
  const cost = d.costs.filter((c) => c.month === d.generated_at.slice(0, 7)).reduce((s, c) => s + Number(c.amount_usd), 0);
  const y = d.ypp || {};
  $("tiles").innerHTML = [
    ["Subscribers", fmt(y.subscribers)], ["Watch hours (365d)", fmt(y.watch_hours_365d, 1)],
    ["Published", published], ["Waiting for you", waiting], ["Health issues", failing],
    ["Cost this month", `$${cost.toFixed(2)}`], ["YouTube quota left", fmt(d.youtube_quota_left_today)],
  ].map(([k, v]) => `<div class="tile"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`).join("");

  $("ypp").innerHTML = d.ypp ? bar("Subscribers", y.subs_progress, `${fmt(y.subscribers)} / 1,000`) +
    bar("Watch hours", y.watch_progress, `${fmt(y.watch_hours_365d, 1)} / 4,000`) +
    bar("Shorts views (90 days)", y.shorts_progress, `${fmt(y.shorts_views_90d)} / 10,000,000`)
    : `<p class="empty">No channel data yet. This starts after the YouTube connection is set up.</p>`;

  $("health").innerHTML = table(d.health, [
    { label: "Check", key: "check_name" },
    { label: "", render: (r) => r.ok ? `<span class="ok">OK</span>` : `<span class="bad">FAIL</span>` },
    { label: "Detail", key: "detail" }, { label: "Checked", render: (r) => when(r.ts) }]);

  const pend = d.approvals.filter((a) => a.decision === "pending");
  const ready = d.pipeline.filter((p) => p.stage === "package_ready");
  $("waiting").innerHTML = (pend.length || ready.length) ? table(
    [...pend.map((a) => ({ what: `${a.gate} approval`, title: a.working_title || a.subject, n: a.issue_number })),
     ...ready.map((p) => ({ what: "upload package ready", title: p.topic, n: null }))],
    [{ label: "Action", key: "what" }, { label: "Video", key: "title" },
     { label: "Where", render: (r) => r.n ? link(issueUrl(r.n), `issue #${r.n}`) : "see Ready-to-upload issue" }])
    : `<p class="empty">Nothing needs you right now.</p>`;

  const cols = STAGES.filter((s) => d.pipeline.some((p) => p.stage === s));
  $("kanban").innerHTML = cols.length ? cols.map((s) => `<div class="col"><h3>${esc(s.replace(/_/g, " "))}</h3>` +
    d.pipeline.filter((p) => p.stage === s).map((p) => `<div class="item"><strong>#${p.id}</strong> ${esc(p.topic)}
      <div><span class="chip">${esc(p.format)}</span> <span class="chip ${p.status === "failed" ? "bad" : p.status === "waiting" ? "warn" : ""}">${esc(p.status)}</span></div>
      ${p.last_error ? `<div class="note bad">${esc(p.last_error)}</div>` : ""}</div>`).join("") + "</div>").join("")
    : `<p class="empty">No videos yet.</p>`;

  $("scripts").innerHTML = table(d.scripts, [
    { label: "Video", key: "video_id" }, { label: "Version", key: "version", num: true },
    { label: "Words", key: "word_count", num: true }, { label: "Est. min", key: "est_min", num: true },
    { label: "Status", key: "status" }, { label: "Written by", key: "llm_provider" }]);
  $("qc").innerHTML = table(d.qc, [
    { label: "Video", key: "video_id" }, { label: "Gate", key: "gate" },
    { label: "Result", render: (r) => r.passed ? `<span class="ok">pass</span>` : `<span class="bad">fail</span>` },
    { label: "Score", key: "score", num: true }]);

  $("calendar").innerHTML = table(d.calendar, [
    { label: "Slot (your time)", render: (r) => when(r.slot_at) }, { label: "Format", key: "format" },
    { label: "Status", key: "status" }, { label: "Video", render: (r) => r.video_id ? `#${r.video_id} ${esc(r.working_title || "")}` : "–" }]);

  $("research").innerHTML = table(d.research_queue, [
    { label: "Topic", render: (r) => `<strong>${esc(r.title)}</strong><div class="note">${esc(r.angle)}</div>` },
    { label: "Category", key: "category" }, { label: "Score", render: (r) => fmt(r.final_score, 2), num: true },
    { label: "Wiki views (12m)", render: (r) => fmt(r.pageviews_12m), num: true },
    { label: "Quality refs", key: "tier12_refs", num: true }, { label: "Status", key: "status" }]);
  let niche = d.niche ? JSON.parse(d.niche.evidence_json || "{}") : {};
  $("niche").innerHTML = d.niche ? `<p><strong>${esc(d.niche.name)}</strong> · method: ${esc(niche.method || niche.basis || "pending Agent 1 run")}</p>` +
    table(Object.entries(niche.categories || {}).map(([k, v]) => ({ cat: k, views: v.wikipedia_views_12m,
      gap: (niche.category_gap || {})[k], yt: v.youtube ? fmt(v.youtube.median_views_per_day) : "–" })),
    [{ label: "Category", key: "cat" }, { label: "Wiki views (12m)", render: (r) => fmt(r.views), num: true },
     { label: "YT median views/day", key: "yt", num: true }, { label: "Gap score", render: (r) => fmt(r.gap, 2), num: true }])
    : `<p class="empty">No niche data.</p>`;

  const ch = [...d.channel].reverse();
  lineChart($("chart-subs"), ch.map((c) => ({ t: c.day, v: c.subscribers || 0 })), "Subscribers over time");
  $("performance").innerHTML = table(d.video_performance, [
    { label: "Video", render: (r) => link(r.youtube_video_id && `https://youtu.be/${r.youtube_video_id}`, r.title || `#${r.id}`) },
    { label: "Format", key: "format" }, { label: "Views", render: (r) => fmt(r.views), num: true },
    { label: "Watch h", render: (r) => fmt(r.watch_hours, 1), num: true },
    { label: "Avg viewed %", render: (r) => fmt(r.avg_view_pct, 1), num: true },
    { label: "Subs", render: (r) => fmt(r.subs), num: true }]);
  $("insights").innerHTML = table(d.insights, [{ label: "When", render: (r) => when(r.created_at) },
    { label: "Finding", key: "finding" }]);
  $("strategy").innerHTML = table(d.strategy, [{ label: "Dimension", key: "dimension" }, { label: "Option", key: "value" },
    { label: "Mean reward", render: (r) => fmt(r.mean, 2), num: true }, { label: "Videos", key: "n_videos", num: true }]);

  $("failed").innerHTML = table(d.failed_tasks, [{ label: "Task", key: "kind" }, { label: "Video", key: "video_id" },
    { label: "Attempts", key: "attempts", num: true }, { label: "Error", render: (r) => `<span class="note">${esc(r.last_error)}</span>` },
    { label: "When", render: (r) => when(r.finished_at) }]);
  $("api").innerHTML = table(d.api_usage, [{ label: "Provider", key: "provider" }, { label: "Day", key: "day" },
    { label: "Units", key: "units", num: true }, { label: "Cap", key: "daily_cap", num: true }, { label: "Errors", key: "errors", num: true }]);
  $("costs").innerHTML = table(d.costs, [{ label: "Month", key: "month" }, { label: "Item", key: "item" },
    { label: "USD", render: (r) => `$${Number(r.amount_usd).toFixed(2)}`, num: true }, { label: "Note", key: "note" }]);
  $("runs").innerHTML = table(d.runs, [{ label: "Workflow", key: "workflow" }, { label: "Started", render: (r) => when(r.started_at) },
    { label: "Status", render: (r) => `<span class="${r.status === "success" ? "ok" : r.status === "running" ? "warn" : "bad"}">${esc(r.status)}</span>` },
    { label: "CPU s", key: "cpu_s", num: true }, { label: "Peak MB", key: "peak_mb", num: true }]);
  $("render").innerHTML = table(d.render_stats, [{ label: "Video", key: "id" }, { label: "Format", key: "format" },
    { label: "Length s", key: "duration_s", num: true }, { label: "Media work s", key: "media_s", num: true }]);
  $("approvals").innerHTML = table(d.approvals, [{ label: "Video", render: (r) => r.video_id ? `#${r.video_id}` : esc(r.subject) },
    { label: "Gate", key: "gate" }, { label: "Decision", key: "decision" }, { label: "By", key: "decided_by" },
    { label: "Issue", render: (r) => r.issue_number ? link(issueUrl(r.issue_number), `#${r.issue_number}`) : "–" }]);
}

setupTabs();
fetch("data/dashboard.json", { cache: "no-store" })
  .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
  .then(render)
  .catch((e) => { $("generated").textContent = `Could not load data (${e.message}). The dashboard appears after the first pipeline run.`; });
