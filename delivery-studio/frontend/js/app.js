"use strict";
// Aetherion Delivery Studio — the page. Talks to studio/server.py only.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = {
  card: null,          // the loaded Jira card
  job: null,           // the run on screen
  polling: null,
  health: null,
  uploads: [],         // documents for the review: {key, filename}
  reviewPdf: "",       // the latest review PDF for this card
  tddPdf: "",          // the latest TDD PDF for this card (or an uploaded one)
  tddName: "",
};

const PAGES = {
  pipeline: ["Agent Workflow", "A Jira card, reviewed, broken down, designed and coded by the agents."],
  runs: ["Runs", "Every run started from this studio, with its Temporal workflows."],
  memory: ["Card memory", "What the agents remember about a card between runs."],
  system: ["System", "The local agents, their connections and their logs."],
  about: ["How it works", "The agents and how a card moves through them."],
};

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
  return body;
}

function toast(text, ms = 3800) {
  const t = $("#toast");
  t.textContent = text;
  t.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.add("hidden"), ms);
}

function fmtDur(sec) {
  if (sec == null || isNaN(sec)) return "";
  sec = Math.max(0, Math.round(sec));
  const m = Math.floor(sec / 60), s = sec % 60;
  return m ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}
const fmtTime = (ts) => new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

function confirmBox(title, html) {
  return new Promise((resolve) => {
    $("#confirm-title").textContent = title;
    $("#confirm-body").innerHTML = html;
    $("#confirm").classList.remove("hidden");
    const done = (v) => { $("#confirm").classList.add("hidden"); $("#confirm-yes").onclick = $("#confirm-no").onclick = null; resolve(v); };
    $("#confirm-yes").onclick = () => done(true);
    $("#confirm-no").onclick = () => done(false);
  });
}

function openDoc(doc) {
  $("#viewer-title").textContent = doc.name;
  $("#viewer-frame").src = doc.url;
  $("#viewer-open").href = doc.url;
  $("#viewer").classList.remove("hidden");
}
$("#viewer-close").onclick = () => { $("#viewer").classList.add("hidden"); $("#viewer-frame").src = "about:blank"; };
$("#viewer").onclick = (e) => { if (e.target.id === "viewer") $("#viewer-close").click(); };

const docHtml = (doc, sub = "PDF · click to open") => doc
  ? `<div class="doc" data-doc='${esc(JSON.stringify(doc))}'><div class="ic">PDF</div><div><div class="nm">${esc(doc.name)}</div><div class="sub">${esc(sub)}</div></div></div>`
  : "";
document.addEventListener("click", (e) => {
  const d = e.target.closest(".doc[data-doc]");
  if (d) openDoc(JSON.parse(d.dataset.doc));
});

// --- navigation -------------------------------------------------------------------
function show(page) {
  if (!PAGES[page]) page = "pipeline";
  $$("nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === page));
  $$(".page").forEach((p) => p.classList.toggle("hidden", p.id !== `page-${page}`));
  $("#page-title").textContent = PAGES[page][0];
  $("#page-sub").textContent = PAGES[page][1];
  if (page === "runs") loadRuns();
  if (page === "system") loadSystem();
  if (page === "memory" && $("#mem-key").value.trim() && !$("#memory-view").innerHTML.trim()) loadMemory();
}
window.addEventListener("hashchange", () => show(pageOf(location.hash.slice(1))));
const pageOf = (hash) => (hash === "workflow" ? "pipeline" : hash);

// --- health -----------------------------------------------------------------------
async function loadHealth() {
  try {
    const h = await api("/api/health");
    state.health = h;
    $("#link-temporal").href = h.links.temporal;
    $("#link-jira").href = h.links.jira + (h.project ? `/jira/software/projects/${h.project}/boards` : "");
    $("#link-github").href = h.links.github;
    const workersDown = h.workers.filter((w) => !w.running && w.agent !== "jira-task-creation");
    const svcDown = h.services.filter((s) => !s.ok);
    const dot = $("#health-dot");
    dot.className = "dot " + (workersDown.length ? "bad" : svcDown.length ? "warn" : "ok");
    $("#health-text").textContent = workersDown.length
      ? `${workersDown.length} agent(s) not running`
      : svcDown.length ? `${svcDown.map((s) => s.name).join(", ")} unavailable` : "All systems ready";
    if (!$("#page-system").classList.contains("hidden")) renderSystem(h);
  } catch (e) {
    $("#health-dot").className = "dot bad";
    $("#health-text").textContent = "Studio server not reachable";
  }
}

// --- cards ------------------------------------------------------------------------
async function loadCardList() {
  try {
    const cards = await api("/api/cards");
    $("#card-list").innerHTML = cards.map((c) => `<option value="${esc(c.key)}">${esc(c.key)} · ${esc(c.summary)}</option>`).join("");
  } catch { /* the list is a convenience */ }
}

async function loadCard(key) {
  key = (key || $("#card-key").value).trim().toUpperCase();
  if (!key) return toast("Type a card key, e.g. KS-6.");
  $("#load-card").disabled = true;
  try {
    const c = await api(`/api/card/${encodeURIComponent(key)}`);
    state.card = c;
    $("#card-key").value = c.key;
    localStorage.setItem("studio.card", c.key);
    state.reviewPdf = c.documents.reviews[0]?.path || "";
    state.tddPdf = c.documents.designs[0]?.path || "";
    state.tddName = c.documents.designs[0]?.name || "";
    renderCard();
    resetSteps();
    enableActions();
    // The card's latest run, so a reload (or a new visit) shows where it got to.
    if (!state.job || state.job.issue_key !== c.key || state.job.status !== "running") {
      // Each step's newest result for this card, from whichever run made it.
      const runs = (await api("/api/runs").catch(() => [])).filter((r) => r.issue_key === c.key && r.status !== "running");
      const steps = {}, workflows = [];
      for (const r of runs) {  // newest first
        for (const [name, step] of Object.entries(r.steps || {})) {
          if (!steps[name] && step.status && step.status !== "waiting") {
            steps[name] = step;
            const ids = step.workflow_ids || [];
            workflows.push(...(r.workflows || []).filter((w) => ids.includes(w.id)));
          }
        }
      }
      if (Object.keys(steps).length) {
        const merged = { id: "", kind: "history", issue_key: c.key, status: "done", steps, workflows, lines: [], error: "" };
        state.job = merged;
        renderSteps(merged);
        state.job = null;  // nothing is running
        enableActions();
      }
    }
  } catch (e) {
    toast(e.message);
  } finally {
    $("#load-card").disabled = false;
  }
}

function renderCard() {
  const c = state.card;
  const v = $("#card-view");
  v.classList.remove("hidden");
  const docs = [...c.documents.reviews.slice(0, 1).map((d) => ({ ...d, kind: "Latest review" })),
                ...c.documents.designs.slice(0, 1).map((d) => ({ ...d, kind: "Latest TDD" }))];
  v.innerHTML = `
    <div>
      <div class="meta"><span class="card-key">${esc(c.key)}</span><span class="tag strong">${esc(c.type)}</span><span class="tag">${esc(c.status)}</span>${c.priority ? `<span class="tag">${esc(c.priority)}</span>` : ""}<span class="tag">👤 ${esc(c.assignee)}</span>${c.parent ? `<span class="tag">parent ${esc(c.parent)}</span>` : ""}</div>
      <div class="card-title">${esc(c.summary)}</div>
    </div>
    <div class="link-row"><a class="btn ghost small" href="${esc(c.url)}" target="_blank" rel="noopener">Open in Jira ↗</a></div>
    ${c.description ? `<div class="card-desc" id="card-desc">${esc(c.description)}</div>` : ""}
    ${docs.length ? `<div style="grid-column:1/-1;display:flex;gap:10px;flex-wrap:wrap;margin-top:6px">${docs.map((d) => docHtml(d, d.kind)).join("")}</div>` : ""}`;
  const desc = $("#card-desc");
  if (desc) desc.onclick = () => desc.classList.toggle("open");
}

function enableActions() {
  const busy = state.job && state.job.status === "running";
  const has = !!state.card;
  $("#run-all").disabled = busy || !has;
  $$("[data-run]").forEach((b) => {
    const k = b.dataset.run;
    b.disabled = busy || (k === "code" ? !state.tddPdf : !has);
  });
  const codeBtn = $('[data-run="code"]');
  codeBtn.title = state.tddPdf ? `Code ${state.tddName || "the latest TDD"}` : "Run planning first, or use a TDD PDF";
}

// --- steps ------------------------------------------------------------------------
const STEP_IDS = ["review", "tasks", "plan", "code"];
const LABEL = { waiting: "Waiting", running: "Running", done: "Done", needs_answers: "Needs answers", failed: "Failed" };

function resetSteps() {
  for (const s of STEP_IDS) setStepStatus(s, "waiting");
  $("#step-review .step-body").innerHTML = state.reviewPdf
    ? `<div class="msg">Latest review for this card:</div>${docHtml({ name: state.reviewPdf.split("/").pop(), url: `/api/file?path=${encodeURIComponent(state.reviewPdf)}` })}${answersBox()}`
    : `<div class="empty">Not reviewed yet.</div>`;
  $("#step-tasks .step-body").innerHTML = `<div class="empty">Use it when the work should be divided into ${state.card?.type === "Epic" ? "Stories" : "Sub-tasks"} before it is designed and coded.</div>`;
  $("#step-plan .step-body").innerHTML = `${githubWarning()}<div class="empty">The design is written after a review that is ready for planning.</div>`;
  $("#step-code .step-body").innerHTML = state.tddPdf
    ? `<div class="msg">Will code the latest design:</div>${docHtml({ name: state.tddName, url: `/api/file?path=${encodeURIComponent(state.tddPdf)}` })}`
    : `<div class="empty">Needs a technical design (TDD).</div>`;
  updateStepper({});
}

function githubDown() {
  const gh = state.health?.services?.find((s) => s.name === "GitHub Enterprise");
  return gh && !gh.ok;
}
function githubWarning() {
  return githubDown() ? `<div class="warnbox">GitHub Enterprise is not reachable — connect the VPN. Planning and Coding read the code there.</div>` : "";
}

function setStepStatus(step, status) {
  const el = $(`#step-${step} .status`);
  el.dataset.s = status;
  el.innerHTML = status === "running" ? `<span class="spinner"></span> Running`
    : step === "tasks" && status === "waiting" ? "Optional" : LABEL[status] || status;
}

function updateStepper(steps, jobStatus) {
  const li = (k) => $(`#stepper li[data-step="${k}"]`);
  const set = (k, cls, text) => { const e = li(k); e.className = cls; e.querySelector(".state").textContent = text; };
  set("card", state.card ? "done" : "", state.card ? state.card.key : "Jira");
  for (const k of STEP_IDS) {
    const s = steps[k]?.status;
    const cls = { done: "done", running: "running", needs_answers: "needs", failed: "failed" }[s] || "";
    let text = s ? LABEL[s] : (k === "tasks" ? "optional" : "waiting");
    if (s && s !== "running" && steps[k].ended && steps[k].started) text += ` · ${fmtDur(steps[k].ended - steps[k].started)}`;
    set(k, cls, text);
  }
  const r = steps.review?.status;
  set("answers", r === "needs_answers" ? "needs" : r === "done" ? "done" : "",
      r === "needs_answers" ? "your turn" : r === "done" ? "not needed" : "if needed");
}

function answersBox(questions = []) {
  return `
    <div class="answer-box">
      <label class="msg" for="answers"><b>Your answers</b> — one per question: <code>Q1: …</code></label>
      <textarea id="answers" placeholder="${esc(questions.length ? questions.slice(0, 2).map((q, i) => `Q${i + 1}: …`).join("\n") : "Q1: …")}">${esc(localStorage.getItem(`studio.answers.${state.card?.key}`) || "")}</textarea>
      <div style="display:flex;justify-content:flex-end;margin-top:8px"><button class="btn small" id="rerun-review">Review again with these answers</button></div>
    </div>`;
}

function wfChips(step, job) {
  const ids = job.steps[step]?.workflow_ids || [];
  const wfs = job.workflows.filter((w) => ids.includes(w.id));
  if (!wfs.length) return "";
  return `<div class="wfs">${wfs.map((w) => `<a class="wf ${esc(w.status)}" href="${esc(w.url)}" target="_blank" rel="noopener" title="Open this run in Temporal">⏱ ${esc(w.workflow)} ↗</a>`).join("")}</div>`;
}

function renderSteps(job) {
  const st = job.steps;
  for (const k of STEP_IDS) if (st[k]) setStepStatus(k, st[k].status);
  updateStepper(st, job.status);

  const rv = st.review;
  if (rv) {
    const body = $("#step-review .step-body");
    if (rv.status === "running") {
      body.innerHTML = `<div class="msg"><span class="spinner"></span> Reading the card and everything around it… <span class="elapsed" data-since="${rv.started}"></span></div>${wfChips("review", job)}`;
    } else if (rv.status === "failed") {
      body.innerHTML = `<div class="msg" style="color:var(--bad)">${esc(rv.message || job.error)}</div>${wfChips("review", job)}`;
    } else {
      const score = rv.score ?? 0;
      const color = rv.ready ? "var(--ok)" : "var(--warn)";
      const qs = rv.open_questions || [];
      body.innerHTML = `
        <div class="score"><div class="gauge" style="--v:${score};--c:${color}"><span>${score}/5</span></div>
          <div><div class="verdict ${rv.ready ? "ok" : "warn"}">${rv.ready ? "Ready for planning" : "Not ready for planning yet"}</div>
          <div class="msg">${esc(rv.message || "")}</div></div></div>
        ${docHtml(rv.document, rv.note || "Review report")}
        ${qs.length ? `<div class="msg"><b>Open questions (${qs.length})</b></div><ul class="qlist">${qs.map((q) => { const m = q.match(/^(Q\d+)\s+(.*)$/s); return `<li>${m ? `<b>${esc(m[1])}</b>${esc(m[2])}` : esc(q)}</li>`; }).join("")}</ul>` : ""}
        ${wfChips("review", job)}
        ${qs.length ? answersBox(qs) : ""}`;
      if (rv.document) state.reviewPdf = decodeURIComponent(rv.document.url.split("path=")[1] || "");
    }
  }

  const tk = st.tasks;
  if (tk) {
    const body = $("#step-tasks .step-body");
    if (tk.status === "running") {
      body.innerHTML = `<div class="msg"><span class="spinner"></span> ${tk.create ? "Creating the issues in Jira" : "Working out the breakdown"}… <span class="elapsed" data-since="${tk.started}"></span></div>${wfChips("tasks", job)}`;
    } else if (tk.status === "failed") {
      body.innerHTML = `<div class="msg" style="color:var(--bad)">${esc(tk.message || job.error)}</div>${wfChips("tasks", job)}`;
    } else if (tk.status !== "waiting") {
      const created = tk.created || [];
      body.innerHTML = `
        <div class="verdict ${created.length ? "ok" : ""}">${created.length ? `${created.length} issue(s) created in Jira` : "Preview — nothing written to Jira"}</div>
        ${created.map((c) => `<div class="pr"><div class="top"><b>${esc(c.key)}</b><span class="draft">${esc(c.type)}</span></div><a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.summary || c.url)} ↗</a></div>`).join("")}
        ${(tk.proposed || []).map((p) => `<div class="msg">${esc(p)}</div>`).join("")}
        ${docHtml(tk.document, "The full breakdown")}
        <div class="msg">${esc(tk.message || "")}</div>
        ${wfChips("tasks", job)}`;
    }
  }

  const pl = st.plan;
  if (pl) {
    const body = $("#step-plan .step-body");
    if (pl.status === "waiting") {
      body.innerHTML = `<div class="empty">Starts when the review is ready for planning.</div>`;
    } else if (pl.status === "running") {
      body.innerHTML = `<div class="msg"><span class="spinner"></span> Ranking services, reading their code and writing the design… <span class="elapsed" data-since="${pl.started}"></span></div>${wfChips("plan", job)}`;
    } else if (pl.status === "failed") {
      body.innerHTML = `<div class="msg" style="color:var(--bad)">${esc(pl.message || job.error)}</div>${wfChips("plan", job)}`;
    } else {
      body.innerHTML = `
        <div class="msg">${esc(pl.message || "")}</div>
        ${(pl.services || []).length ? `<div class="svc">${pl.services.map((s) => { const m = s.match(/^(\d+)\.\s+(.*)$/); return `<div><span class="rank">${esc(m ? m[1] : "•")}</span>${esc(m ? m[2] : s)}</div>`; }).join("")}</div>` : ""}
        ${docHtml(pl.document, "Technical design document")}
        ${wfChips("plan", job)}`;
      if (pl.document) {
        state.tddPdf = decodeURIComponent(pl.document.url.split("path=")[1] || "");
        state.tddName = pl.document.name;
      }
    }
  }

  const cd = st.code;
  if (cd) {
    const body = $("#step-code .step-body");
    if (cd.status === "waiting") {
      body.innerHTML = `<div class="empty">Starts when the design is written.</div>`;
    } else if (cd.status === "running") {
      body.innerHTML = `<div class="msg"><span class="spinner"></span> Changing the code in each service and checking it… <span class="elapsed" data-since="${cd.started}"></span></div>${wfChips("code", job)}`;
    } else {
      const prs = cd.pull_requests || [], brs = cd.branches || [];
      body.innerHTML = `
        ${cd.branch ? `<div class="msg">Branch <code>${esc(cd.branch)}</code>${cd.source ? ` · cut from <code>${esc(cd.source)}</code> · pull request into <code>${esc(cd.source)}</code>` : ""}</div>` : ""}
        ${prs.map((p) => `<div class="pr"><div class="top"><b>${esc(p.service)}</b>${p.draft ? `<span class="draft">Draft</span>` : ""}</div><a href="${esc(p.pr_url)}" target="_blank" rel="noopener">${esc(p.pr_url)} ↗</a></div>`).join("")}
        ${brs.map((b) => `<div class="pr"><div class="top"><b>${esc(b.service)}</b><span class="draft">Branch only</span></div><a href="${esc(b.url)}" target="_blank" rel="noopener">${esc(b.url)} ↗</a></div>`).join("")}
        ${!prs.length && !brs.length ? `<div class="msg" style="color:var(--bad)">${esc(cd.message || job.error || "Nothing was pushed.")}</div>` : `<div class="msg">${esc(cd.message || "")}</div>`}
        ${wfChips("code", job)}`;
    }
  }

  // The live log and every workflow of this run.
  const con = $("#console");
  const atBottom = con.scrollHeight - con.scrollTop - con.clientHeight < 40;
  con.innerHTML = (job.lines || []).map((l) => `<span class="t">${String(l.t).padStart(6, " ")}s</span><span class="${/error|failed|Traceback/i.test(l.text) ? "err" : ""}">${esc(l.text)}</span>`).join("\n") || `<span class="muted">Starting…</span>`;
  if (atBottom) con.scrollTop = con.scrollHeight;
  $("#wf-list").innerHTML = job.workflows.map((w) => `<a class="wf ${esc(w.status)}" href="${esc(w.url)}" target="_blank" rel="noopener">${esc(w.agent)} · ${esc(w.status)}${w.ended ? " · " + fmtDur(w.ended - w.started) : ""} ↗</a>`).join("");
  wireAnswers();
  enableActions();
}

function wireAnswers() {
  const ta = $("#answers");
  if (ta) ta.oninput = () => localStorage.setItem(`studio.answers.${state.card?.key}`, ta.value);
  const btn = $("#rerun-review");
  if (btn) btn.onclick = () => start("review");
}

setInterval(() => {
  $$(".elapsed[data-since]").forEach((e) => { e.textContent = fmtDur(Date.now() / 1000 - Number(e.dataset.since)); });
}, 1000);

// --- coding: which branch to start from, and the new branch's name ------------------
// As "@Aetherion code" asks: the new branch is cut from the source branch, and the
// draft pull request goes back into it.
async function askBranches(kind) {
  const key = state.card?.key || "";
  let tdd = null;
  if (kind === "code" && state.tddPdf) {
    tdd = await api(`/api/tdd?path=${encodeURIComponent(state.tddPdf)}`).catch((e) => { toast(e.message); return null; });
    if (!tdd) return null;
  }
  const suggested = tdd?.suggested_branch || `fix/${key.toLowerCase()}-${Date.now() % 100000}`;
  const written = tdd ? [...new Set(tdd.services.map((s) => s.branch))].join(", ") : "";
  const html = `
    <p>${kind === "pipeline" ? "Review, then Planning, then Coding — stopping where a person is needed (open questions, a failed step)." : `Coding implements <b>${esc(state.tddName || "the design")}</b>.`}</p>
    ${tdd ? tdd.services.map((s) => `<div class="svc-line"><b>${esc(s.service)}</b> · ${esc(s.repo)} · written from <code>${esc(s.branch)}</code></div>`).join("") : ""}
    <label class="form-row">Start from branch <span class="hint">the new branch is cut from it, and the draft pull request goes into it${written ? ` · the design was written from ${esc(written)}` : " · empty: the branch the design is written from"}</span>
      ${tdd ? `<select id="dlg-source"><option>${esc(written.split(", ")[0])}</option></select>` : `<input id="dlg-source" placeholder="e.g. develop (empty: the design's branch)">`}
    </label>
    <label class="form-row">New branch name <span class="hint">must not exist yet</span>
      <input id="dlg-branch" value="${esc(suggested)}" spellcheck="false">
      <span class="field-note" id="dlg-branch-note"></span>
    </label>
    <p class="msg" style="margin-top:14px">Coding <b>pushes this new branch</b> to each service's repository and opens a <b>draft pull request</b>. Nothing is merged, and nothing is written to Jira.</p>`;
  const answer = confirmBox(kind === "code" ? "Run Coding" : "Run", html);
  if (tdd) {
    const repo = tdd.services[0].repo;
    api(`/api/branches?repo=${encodeURIComponent(repo)}`).then((r) => {
      const sel = $("#dlg-source");
      const keep = sel.value;
      sel.innerHTML = r.branches.map((b) => `<option ${b === keep ? "selected" : ""}>${esc(b)}</option>`).join("");
    }).catch((e) => toast(e.message));
    const check = async () => {
      const name = $("#dlg-branch").value.trim(), note = $("#dlg-branch-note");
      if (!name) { note.textContent = ""; return; }
      const r = await api(`/api/branch-exists?repo=${encodeURIComponent(repo)}&name=${encodeURIComponent(name)}`).catch((e) => { note.className = "field-note bad"; note.textContent = e.message; return null; });
      if (r) { note.className = "field-note " + (r.exists ? "bad" : "ok"); note.textContent = r.exists ? "A branch with this name already exists — choose another." : "Name is free."; }
    };
    $("#dlg-branch").onchange = check; check();
  }
  if (!(await answer)) return null;
  const branch = $("#dlg-branch").value.trim();
  if (!/^[A-Za-z0-9._\/-]+$/.test(branch) || branch.includes("..") || branch.endsWith("/")) {
    toast("That is not a valid branch name."); return null;
  }
  if ($("#dlg-branch-note")?.classList.contains("bad")) { toast("That branch already exists — choose another name."); return null; }
  return { branch, source: ($("#dlg-source").value || "").trim() };
}

// --- runs -------------------------------------------------------------------------
async function start(kind) {
  if (!state.card && kind !== "code") return toast("Load a card first.");
  let create = false;
  if (kind === "breakdown-create") {
    kind = "breakdown"; create = true;
    const ok = await confirmBox("Create in Jira", `<p>Task Creation will <b>create ${state.card.type === "Epic" ? "Stories" : "Sub-tasks"} under ${esc(state.card.key)}</b> in Jira. The card itself is not changed.</p><p>Run <b>Preview</b> first to see them.</p>`);
    if (!ok) return;
  }
  if (["plan", "code", "pipeline"].includes(kind) && githubDown()) {
    const ok = await confirmBox("GitHub Enterprise is not reachable", `<p>Planning and Coding read the code on github.asurint.com, which this laptop cannot reach right now — <b>connect the VPN</b>.</p><p>Run anyway?</p>`);
    if (!ok) return;
  }
  const answers = $("#answers")?.value || "";
  const body = {
    kind, issue_key: state.card?.key || "", answers,
    services: $("#opt-services").value.trim(),
    branch_name: "",
    files: state.uploads.map((u) => u.key),
    review_pdf: kind === "plan" ? state.reviewPdf : "",
    tdd_pdf: kind === "code" ? state.tddPdf : "",
    create_in_jira: create,
  };
  if ((kind === "code" || kind === "pipeline") && !create) {
    const chosen = await askBranches(kind);
    if (!chosen) return;
    body.branch_name = chosen.branch;
    body.source_branch = chosen.source;
  }
  try {
    const stepOf = { breakdown: "tasks" }[kind] || kind;
    for (const s of (kind === "pipeline" ? ["review", "plan", "code"] : [stepOf])) setStepStatus(s, s === (kind === "pipeline" ? "review" : stepOf) ? "running" : "waiting");
    const job = await api("/api/runs", { method: "POST", body: JSON.stringify(body) });
    state.job = job;
    localStorage.setItem("studio.job", job.id);
    $("#console").innerHTML = "";
    renderSteps(job);
    poll();
  } catch (e) {
    toast(e.message);
    resetSteps();
  }
}

function poll() {
  clearTimeout(state.polling);
  const tick = async () => {
    if (!state.job) return;
    try {
      const job = await api(`/api/runs/${state.job.id}`);
      state.job = job;
      renderSteps(job);
      if (job.status !== "running") {
        enableActions();
        const msg = { done: "Run finished.", needs_answers: "The review needs answers before planning.", failed: "The run failed — see the step for why." }[job.status];
        if (msg) toast(msg);
        if (state.card) loadCard(state.card.key).then(() => renderSteps(job));
        return;
      }
    } catch { /* the server restarting: keep trying */ }
    state.polling = setTimeout(tick, 1500);
  };
  tick();
}

async function loadRuns() {
  const runs = await api("/api/runs").catch(() => []);
  const tb = $("#runs-table tbody");
  tb.innerHTML = runs.length ? runs.map((r) => `
    <tr>
      <td>${fmtTime(r.started)}</td>
      <td><span class="card-key">${esc(r.issue_key || "—")}</span></td>
      <td>${esc({ pipeline: "Full run", review: "Review", breakdown: r.options?.create_in_jira ? "Break down · created" : "Break down · preview", plan: "Planning", code: "Coding" }[r.kind] || r.kind)}</td>
      <td><span class="status" data-s="${esc(r.status)}">${esc(LABEL[r.status] || r.status)}</span></td>
      <td>${r.ended ? fmtDur(r.ended - r.started) : `<span class="spinner"></span>`}</td>
      <td><div class="wfs">${(r.workflows || []).map((w) => `<a class="wf ${esc(w.status)}" href="${esc(w.url)}" target="_blank" rel="noopener">${esc(w.agent)} ↗</a>`).join("") || "—"}</div></td>
    </tr>`).join("") : `<tr><td colspan="6"><div class="empty">No runs yet. Start one from the Agent Workflow page.</div></td></tr>`;
}

// --- memory -----------------------------------------------------------------------
async function loadMemory() {
  const key = $("#mem-key").value.trim().toUpperCase();
  if (!key) return toast("Type a card key.");
  const v = $("#memory-view");
  v.innerHTML = `<div class="panel"><span class="spinner"></span> Reading the memory…</div>`;
  try {
    const m = await api(`/api/memory/${encodeURIComponent(key)}`);
    const open = m.questions.filter((q) => q.open).length;
    v.innerHTML = `
      <div class="mem-grid">
        <div class="panel"><h2>Questions · ${m.questions.length} (${open} open)</h2>
          ${m.questions.map((q) => `<div class="qa ${q.open ? "open" : "answered"}"><div class="q">${esc(q.id)} · ${esc(q.question)}</div><div class="a">${q.answers.length ? esc(q.answers.join("\n— ")) : "Not answered yet"}</div></div>`).join("") || `<div class="empty">No questions remembered for ${esc(key)}.</div>`}
        </div>
        <div class="panel"><h2>Services ranked</h2>
          <div class="svc" style="margin-top:12px">${m.services.map((s) => `<div title="${esc(s.reason || "")}"><span class="rank">${esc(s.rank)}</span><div><b>${esc(s.service)}</b><div class="msg">${esc(s.repo)}${s.branch ? " @ " + esc(s.branch) : ""}</div></div></div>`).join("") || `<div class="empty">Not ranked yet — the review or a plan ranks them.</div>`}</div>
          <p class="msg" style="margin-top:14px">Snapshot ${esc(m.snapshot || "none")} · encrypted at rest</p>
        </div>
      </div>`;
  } catch (e) {
    v.innerHTML = `<div class="panel"><div class="msg" style="color:var(--bad)">${esc(e.message)}</div></div>`;
  }
}

// --- system -----------------------------------------------------------------------
async function loadSystem() {
  await loadHealth();
  if (state.health) renderSystem(state.health);
  const sel = $("#log-agent");
  if (!sel.options.length && state.health) {
    sel.innerHTML = state.health.workers.map((w) => `<option value="${esc(w.agent)}">${esc(w.name)}</option>`).join("");
    sel.onchange = loadAgentLog;
  }
  loadAgentLog();
}

function renderSystem(h) {
  $("#sys-workers").innerHTML = h.workers.map((w) => `
    <div class="row"><span class="dot ${w.running ? "ok" : "bad"}"></span><div class="grow"><div class="nm">${esc(w.name)}</div><div class="sub">${esc(w.queue)}</div></div><span class="status" data-s="${w.running ? "done" : "failed"}">${w.running ? "Running" : "Stopped"}</span></div>`).join("");
  $("#sys-services").innerHTML = h.services.map((s) => `
    <div class="row"><span class="dot ${s.ok ? "ok" : "warn"}"></span><div class="grow"><div class="nm">${esc(s.name)}</div><div class="sub">${esc(s.detail)}</div></div>${s.url ? `<a class="btn ghost small" href="${esc(s.url)}" target="_blank" rel="noopener">Open ↗</a>` : ""}</div>`).join("");
  $("#sys-containers").innerHTML = h.containers.map((c) => `
    <div class="row"><span class="dot ${c.running ? "ok" : "bad"}"></span><div class="grow"><div class="nm">${esc(c.name)}</div><div class="sub">${esc(c.status)}</div></div></div>`).join("") || `<div class="empty">Docker is not available.</div>`;
}

async function loadAgentLog() {
  const agent = $("#log-agent").value;
  if (!agent) return;
  const r = await api(`/api/logs/${agent}?lines=300`).catch(() => ({ lines: [] }));
  const el = $("#agent-log");
  el.innerHTML = r.lines.map((l) => `<span class="${/error|Traceback|failed/i.test(l) ? "err" : ""}">${esc(l)}</span>`).join("\n") || `<span class="muted">No log yet.</span>`;
  el.scrollTop = el.scrollHeight;
}

// --- uploads ----------------------------------------------------------------------
async function upload(file) {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch("/api/upload", { method: "POST", body: fd });
  const body = await r.json();
  if (!r.ok) throw new Error(body.detail || "Upload failed");
  return body;
}

$("#opt-files").onchange = async (e) => {
  for (const f of e.target.files) {
    try { state.uploads.push(await upload(f)); } catch (err) { toast(err.message); }
  }
  $("#opt-uploaded").innerHTML = state.uploads.map((u) => `<span class="chip">${esc(u.filename)}</span>`).join("");
  e.target.value = "";
};
$("#tdd-upload").onchange = async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  try {
    const u = await upload(f);
    state.tddPdf = u.path; state.tddName = u.filename;
    $("#step-code .step-body").innerHTML = `<div class="msg">Will code:</div>${docHtml({ name: u.filename, url: `/api/file?path=${encodeURIComponent(u.path)}` })}`;
    enableActions();
  } catch (err) { toast(err.message); }
  e.target.value = "";
};

// --- start ------------------------------------------------------------------------
$("#load-card").onclick = () => loadCard();
$("#card-key").addEventListener("keydown", (e) => { if (e.key === "Enter") loadCard(); });
$("#run-all").onclick = () => start("pipeline");
$$("[data-run]").forEach((b) => (b.onclick = () => start(b.dataset.run)));
$("#load-memory").onclick = loadMemory;
$("#mem-key").addEventListener("keydown", (e) => { if (e.key === "Enter") loadMemory(); });

(async function init() {
  show(pageOf(location.hash.slice(1)) || "pipeline");
  loadHealth();
  setInterval(loadHealth, 15000);
  loadCardList();
  const asked = new URLSearchParams(location.search).get("card");
  const last = asked || localStorage.getItem("studio.card");
  if (last) { $("#card-key").value = last; $("#mem-key").value = last; await loadCard(last); }
  // Pick up a run still in progress (a page reload while it runs).
  const runs = await api("/api/runs").catch(() => []);
  const live = runs.find((r) => r.status === "running");
  if (live) { state.job = live; poll(); }
  if (!state.job || !state.job.lines?.length) $("#console").innerHTML = `<span class="muted">Runs print here as they happen.</span>`;
})();
