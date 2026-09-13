"use strict";

// The trajectory viewer: lays out what scripts/build_viewer.py computed. Every figure on the page
// comes from viewer/data/, which is built from committed files only; nothing here calls a model,
// and nothing here computes a verdict. Every string from the data goes into the page through
// textContent, never as markup: the attack cases carry instructions written to be obeyed.
//
// The Run button (7.1) comes alive only when this page is served by the local API: the page is
// on a loopback address AND its own origin answers api/status as that API. Anywhere else the page
// never asks, so a deployed copy makes no request beyond its own files and Run stays disabled.

(() => {
  const state = { index: null, taskId: null, attackId: null, agent: "A1", cache: new Map(), live: null, liveRun: null, polling: null };
  const $ = (id) => document.getElementById(id);
  const LOOPBACK = new Set(["127.0.0.1", "localhost"]);

  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (value === undefined || value === null) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else node.setAttribute(key, value);
    }
    for (const child of children) {
      if (child === undefined || child === null || child === false) continue;
      node.append(typeof child === "string" ? document.createTextNode(child) : child);
    }
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  async function data(path) {
    if (state.cache.has(path)) return state.cache.get(path);
    const response = await fetch(`data/${path}`);
    if (!response.ok) throw new Error(`data/${path}: HTTP ${response.status}`);
    const value = await response.json();
    state.cache.set(path, value);
    return value;
  }

  const number = (value) => (value === null || value === undefined ? "—" : Number(value).toLocaleString("en-US"));
  const rows = (count) => `${number(count)} ${Number(count) === 1 ? "row" : "rows"}`;
  const seconds = (value) => (value === null || value === undefined ? "—" : `${Number(value).toFixed(2)} s`);

  // Text with every occurrence of `needle` wrapped in <mark>, built as nodes.
  function marked(text, needle) {
    const out = document.createDocumentFragment();
    if (!needle || !text || !text.includes(needle)) {
      out.append(document.createTextNode(text || ""));
      return out;
    }
    const parts = text.split(needle);
    parts.forEach((part, i) => {
      out.append(document.createTextNode(part));
      if (i < parts.length - 1) out.append(el("mark", { text: needle }));
    });
    return out;
  }

  function pre(text, needle, collapseOver = 700) {
    const block = el("pre");
    block.append(marked(text, needle));
    if ((text || "").length <= collapseOver) return block;
    const details = el("details", {}, el("summary", { text: `show all ${number(text.length)} characters` }), block);
    return details;
  }

  // -- the left panel ---------------------------------------------------------------------------

  function fillDatabases() {
    const select = $("database");
    clear(select);
    select.append(el("option", { value: "", text: "Every working-set database" }));
    for (const db of state.index.databases) select.append(el("option", { value: db, text: db }));
  }

  function fillTasks() {
    const select = $("task");
    const db = $("database").value;
    clear(select);
    for (const task of state.index.tasks) {
      if (db && task.database !== db) continue;
      const marks = ["A0", "A1", "A2"].map((a) => `${a} ${task[a] === state.index.labels.matches ? "✓" : "✗"}`).join("  ");
      select.append(el("option", { value: task.task_id, text: `${task.task_id} · ${marks}` }));
    }
    if (state.taskId) select.value = state.taskId;
  }

  function fillAttacks() {
    const box = $("preloaded");
    clear(box);
    for (const pick of state.index.preloaded) {
      const button = el("button", { type: "button" }, `${pick.case_id} — ${pick.outcome}`, el("small", { text: pick.why }));
      button.addEventListener("click", () => go({ attack: pick.case_id }));
      box.append(button);
    }
    const select = $("attack");
    clear(select);
    select.append(el("option", { value: "", text: "Every attack case…" }));
    for (const c of state.index.attacks) {
      const seen = c.seen ? (c.complied ? (c.contained ? "complied, contained" : "complied") : "resisted") : "never seen";
      select.append(el("option", { value: c.case_id, text: `${c.case_id} · ${c.placement} · ${c.category} · ${seen}` }));
    }
  }

  function syncToggle() {
    for (const button of document.querySelectorAll("[data-agent]")) {
      const active = !state.attackId && button.dataset.agent === state.agent;
      button.setAttribute("aria-pressed", String(active));
      button.disabled = Boolean(state.attackId) && button.dataset.agent !== "A1";
    }
    $("agent-note").textContent = state.attackId
      ? "The attack run measured A1 only."
      : "A0 sends one request with the whole schema; A1 discovers the schema with four tools; A2 runs A1's loop on the cheap model and escalates when the trajectory announces its own failure.";
  }

  // -- the centre -------------------------------------------------------------------------------

  function renderSteps(list, trajectory, needle) {
    for (const step of trajectory.steps) {
      if (step.kind === "call") {
        const head = el("div", { class: "step-head" }, el("span", { class: "tool", text: step.tool }), el("span", { class: "turn", text: `turn ${step.turn}` }));
        if (!step.executed) head.append(el("span", { class: "tag warn", text: "not executed" }));
        else if (step.ok) head.append(el("span", { class: "tag ok", text: step.rows_returned === null ? "ok" : rows(step.rows_returned) }));
        else head.append(el("span", { class: "tag bad", text: "error" }));
        if (step.truncated_by) head.append(el("span", { class: "tag warn", text: `truncated by ${step.truncated_by}` }));
        for (const control of step.controls) head.append(el("span", { class: "tag warn", text: `control ${control} fired — ${state.index.controls[control]}` }));
        const item = el("li", {}, head, el("div", { class: "small muted", text: "arguments in" }), pre(JSON.stringify(step.arguments, null, 1), needle));
        if (step.result !== null && step.result !== undefined) {
          item.append(el("div", { class: "small muted", text: step.ok === false ? "error back" : "result back" }), pre(step.result, needle));
        }
        list.append(item);
      } else if (step.kind === "say") {
        const head = el("div", { class: "step-head" }, el("span", { class: "tool", text: "the agent replied" }), el("span", { class: "turn", text: `turn ${step.turn}` }));
        if (step.repair) head.append(el("span", { class: "tag", text: "after the repair request" }));
        list.append(el("li", {}, head, pre(step.text, needle)));
      } else if (step.kind === "repair_request") {
        const head = el("div", { class: "step-head" }, el("span", { class: "tool", text: "repair request" }), el("span", { class: "turn", text: `turn ${step.turn}` }));
        list.append(el("li", {}, head, el("div", { class: "small muted", text: "the reply failed validation; this went back to the agent once" }), pre(step.text, needle)));
      }
    }
    const end = trajectory.end;
    if (!end || !end.termination) return; // a live trajectory still being written has no end yet
    const bits = [`ended on ${end.termination}`, `${end.turns} turns`, `${end.tool_calls} tool calls`];
    if (end.repair_attempts) bits.push(`repair ${end.repair_succeeded ? "succeeded" : "did not succeed"}`);
    if (end.repair_blocked) bits.push(`repair blocked: ${end.repair_blocked}`);
    const last = el("li", { class: "divider" }, el("span", { class: "muted", text: bits.join(" · ") }));
    if (end.failed_generation) {
      last.append(el("div", { class: "small muted", text: "what the model generated on the request the provider refused with a 400" }), pre(end.failed_generation, needle));
    }
    list.append(last);
  }

  function renderFinal(final) {
    const box = $("final");
    clear(box);
    box.append(el("h3", { text: "Final statement" }));
    box.append(final.sql ? pre(final.sql) : el("p", { class: "muted", text: "none" }));
    box.append(el("h3", { text: "Rows" }));
    box.append(el("p", { class: "muted small", text: final.rows_note }));
    if (final.rows) box.append(pre(final.rows.text));
    else if (final.sql && final.candidate_rows !== null && final.candidate_rows !== undefined) {
      box.append(el("p", { text: `${rows(final.candidate_rows)} when scoring ran it` }));
    }
  }

  function renderStrip(items) {
    const strip = $("strip");
    clear(strip);
    for (const [label, value, cls] of items) {
      strip.append(el("div", {}, el("span", { text: label }), el("strong", { class: cls, text: value })));
    }
  }

  function controlsText(footer) {
    return footer.controls.length
      ? footer.controls.map((c) => `${c.control} (${c.tool}${c.turn ? `, turn ${c.turn}` : ""})`).join(", ")
      : "none";
  }

  function footerItems(footer, extra = []) {
    const controls = controlsText(footer);
    const verdictClass = footer.verdict === state.index.labels.matches ? "verdict-ok" : "verdict-bad";
    return [
      ["Model", footer.model],
      ...extra,
      ["Tool calls", number(footer.tool_calls)],
      ["Turns", number(footer.turns)],
      ["Tokens in / out", `${number(footer.tokens_in)} / ${number(footer.tokens_out)}`],
      [state.index.labels.elapsed, seconds(footer.elapsed_s)],
      ["Controls fired", controls],
      ["Ended on", footer.termination || "one response"],
      ["Verdict", footer.verdict === state.index.labels.matches || !footer.reason ? footer.verdict || "—" : `${footer.verdict} (${footer.reason})`, verdictClass],
    ];
  }

  function heading(title, lines) {
    const box = $("heading");
    clear(box);
    box.append(el("h2", { text: title }));
    for (const line of lines) if (line) box.append(line);
  }

  function card(title, ...children) {
    return el("section", { class: "card" }, el("h3", { text: title }), ...children);
  }

  async function renderTask() {
    const task = await data(`tasks/${state.taskId}.json`);
    const context = $("context");
    const steps = $("steps");
    clear(context);
    clear(steps);
    $("question").value = task.question;
    heading(`${task.task_id} · ${task.database}${task.difficulty ? ` · ${task.difficulty}` : ""}`, [
      el("p", { text: task.question }),
      task.reference_note ? el("p", { class: "note", text: task.reference_note }) : null,
    ]);
    const prompts = state.index.system_prompts;

    if (state.agent === "A0") {
      const side = task.A0;
      context.append(card("What A0 was sent", el("p", { class: "small", text: side.prompt.note }), pre(side.prompt.system), el("p", { class: "small muted", text: `…then the schema of ${task.database}, then the question.` })));
      steps.append(el("li", { class: "divider" }, el("span", { class: "muted", text: "one request, no tools, no repair" })));
      renderFinal(side.final);
      renderStrip(footerItems(side.footer, [["Latency", seconds(side.footer.latency_s)]]));
      return;
    }

    if (state.agent === "A1") {
      const side = task.A1;
      context.append(el("details", { class: "card" }, el("summary", { text: "What A1 was told" }), pre(prompts[side.system_prompt])));
      renderSteps(steps, side);
      renderFinal(side.final);
      renderStrip(footerItems(side.footer));
      return;
    }

    const a2 = task.A2;
    const cheap = a2.cheap;
    const escalation = a2.escalated
      ? `escalated on ${a2.clauses.join(", ")}`
      : "not escalated: the cheap trajectory did not announce a failure";
    const why = el("div");
    for (const clause of a2.clauses) why.append(el("p", { class: "small" }, el("strong", { text: `${clause}: ` }), a2.clause_definitions[clause]));
    context.append(card(`A2 — ${escalation}`, why, el("p", { class: "small muted", text: `Composed, not run: the cheap trajectory below, and where the rule fired, A1's trajectory on this task from its measured run. Tokens on the trajectories that stand: cheap ${number(a2.tokens.cheap)}, strong ${number(a2.tokens.strong)}.` })));
    steps.append(el("li", { class: "divider" }, el("span", { class: "muted", text: `the cheap model — ${cheap.footer.model}` })));
    renderSteps(steps, cheap);
    let final = cheap.final;
    let footer = cheap.footer;
    if (a2.escalated) {
      steps.append(el("li", { class: "divider" }, el("span", { class: "muted", text: `escalated — the strong model — ${task.A1.footer.model}` })));
      renderSteps(steps, task.A1);
      final = task.A1.final;
      footer = task.A1.footer;
    }
    renderFinal(final);
    renderStrip(footerItems({ ...footer, verdict: a2.verdict, reason: footer.reason }, [["Escalated", a2.escalated ? `yes — ${a2.clauses.join(", ")}` : "no"]]));
  }

  function schemaTable(poisoned) {
    const needle = poisoned.injected_instruction;
    const wrap = el("div", { class: "table-wrap" });
    for (const table of poisoned.tables) {
      const grid = el("table", { class: "schema" });
      const caption = el("caption", { class: "small" });
      caption.append(marked(`table ${table.name}`, needle));
      grid.append(caption);
      const head = el("tr");
      for (const column of table.columns) {
        const th = el("th");
        th.append(marked(column.name, needle));
        th.append(el("div", { class: "muted small" }, marked(column.type, needle)));
        head.append(th);
      }
      grid.append(head);
      for (const row of table.rows || []) {
        const tr = el("tr");
        for (const value of row) {
          const td = el("td");
          td.append(marked(value === null ? "NULL" : String(value), needle));
          tr.append(td);
        }
        grid.append(tr);
      }
      wrap.append(grid);
    }
    return wrap;
  }

  async function renderAttack() {
    const found = await data(`attacks/${state.attackId}.json`);
    const context = $("context");
    const steps = $("steps");
    clear(context);
    clear(steps);
    $("question").value = found.question;
    const poisoned = found.poisoned;
    heading(`${found.case_id} · shown to the agent as ${found.database_shown}`, [
      el("p", { text: found.question }),
      el("p", { class: "muted small", text: `${found.category} · planted in ${poisoned.placement} (${poisoned.channel}) · ${found.containable ? `containable by ${found.containing_control}` : "no control can contain it"}` }),
    ]);
    const instruction = el("p");
    instruction.append(el("mark", { text: poisoned.injected_instruction }));
    context.append(card("The poisoned schema", instruction, schemaTable(poisoned)));
    const facts = found.found;
    const outcome = [
      `seen via ${facts.exposed_via.length ? facts.exposed_via.join(", ") : "nothing — the agent never looked where it was planted"}`,
      facts.complied ? "the agent complied" : "the agent did not comply",
      facts.contained === null ? null : facts.contained ? "a control contained it" : "no control contained it",
      facts.damaged === null ? null : facts.damaged ? "the answer was damaged" : "the answer was not damaged",
    ].filter(Boolean);
    const attempts = el("ul", { class: "small" });
    for (const attempt of facts.attempts) {
      attempts.append(el("li", { text: `${attempt.surface}, turn ${attempt.turn}: ${attempt.statements.join("; ")} — ${attempt.outcome}${attempt.control ? ` (${attempt.control})` : ""}` }));
    }
    context.append(card("What the attack run found", el("p", { text: outcome.join(" · ") }), facts.attempts.length ? attempts : null, el("p", { class: "small muted", text: "Read from results/attacks.json; this page never re-scores a case." })));
    renderSteps(steps, found.A1, poisoned.injected_instruction);
    renderFinal(found.A1.final);
    renderStrip(footerItems(found.A1.footer));
  }

  // -- a new trajectory, through the local API (7.1) --------------------------------------------

  // Null unless this page is on a loopback address and its own origin answers as the local API.
  async function probeLocalApi() {
    if (!LOOPBACK.has(location.hostname)) return null;
    try {
      const response = await fetch("api/status", { cache: "no-store" });
      if (!response.ok) return null;
      const status = await response.json();
      return status && status.surface === "local API" && status.live === true ? status : null;
    } catch {
      return null;
    }
  }

  function setRunAvailable(available) {
    if (!state.live) return;
    $("run").disabled = !available;
  }

  function liveMessage(text) {
    $("run-message").textContent = text;
  }

  function enableLive(status) {
    state.live = status;
    $("replay-note").textContent = status.masthead;
    $("run-note").textContent = status.note;
    $("question").readOnly = false;
    const select = $("live-agent");
    clear(select);
    for (const [name, agent] of Object.entries(status.agents)) {
      select.append(el("option", { value: name, text: `${name} — ${agent.model}` }));
    }
    select.hidden = false;
    $("run").addEventListener("click", submit);
    setRunAvailable(!status.running);
  }

  async function submit() {
    const database = $("database").value;
    const question = $("question").value.trim();
    if (!database) return liveMessage("Choose one database first: the local API runs a question against one.");
    if (!question) return liveMessage("Type a question first.");
    setRunAvailable(false);
    liveMessage("");
    try {
      const response = await fetch("api/questions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ database, question, agent: $("live-agent").value }),
      });
      const reply = await response.json();
      if (!response.ok) {
        const wait = reply.retry_after_s ? ` Try again in ${Math.ceil(reply.retry_after_s)} s.` : "";
        liveMessage(`${reply.error}.${wait}`);
        setRunAvailable(!reply.running);
        return;
      }
      location.hash = new URLSearchParams({ live: reply.run_id }).toString();
    } catch (error) {
      liveMessage(`Could not reach the local API: ${error.message}`);
      setRunAvailable(true);
    }
  }

  function liveFooter(footer) {
    return [
      ["Model", footer.model || "—"],
      ["Tool calls", number(footer.tool_calls)],
      ["Turns", number(footer.turns)],
      ["Tokens in / out", `${number(footer.tokens_in)} / ${number(footer.tokens_out)}`],
      [state.index.labels.elapsed, seconds(footer.elapsed_s)],
      ["Controls fired", controlsText(footer)],
      ["Ended on", footer.termination || "—"],
      ["Verdict", footer.verdict],
    ];
  }

  function renderLive(view) {
    const context = $("context");
    const steps = $("steps");
    clear(context);
    clear(steps);
    clear($("final"));
    heading(`${view.agent || ""} · ${view.database || ""} · a new trajectory`, [
      el("p", { text: view.question || "" }),
      el("p", { class: "note", text: `Run ${view.run_id}, ${view.status}. ${state.live.note}` }),
    ]);
    if (view.system_prompt) {
      context.append(el("details", { class: "card" }, el("summary", { text: `What ${view.agent} was told` }), pre(view.system_prompt)));
    }
    renderSteps(steps, { steps: view.steps || [], end: view.end });
    if (view.status === "running" || view.status === "starting") {
      steps.append(el("li", { class: "divider" }, el("span", { class: "muted", text: "running: the page asks again every second" })));
    }
    if (view.failure) {
      steps.append(el("li", { class: "divider" }, el("span", { text: `the task failed: ${view.failure.exception}: ${view.failure.message}` })));
    }
    if (view.final) renderFinal(view.final);
    if (view.footer) renderStrip(liveFooter(view.footer));
  }

  async function pollLive(runId) {
    clearTimeout(state.polling);
    if (state.liveRun !== runId) return;
    try {
      const response = await fetch(`api/questions/${encodeURIComponent(runId)}`, { cache: "no-store" });
      const view = await response.json();
      if (state.liveRun !== runId) return;
      if (!response.ok) {
        liveMessage(view.error || `HTTP ${response.status}`);
        setRunAvailable(true);
        return;
      }
      renderLive(view);
      if (view.status === "running" || view.status === "starting") {
        state.polling = setTimeout(() => pollLive(runId), state.live.poll_interval_s * 1000);
      } else {
        setRunAvailable(true);
      }
    } catch (error) {
      liveMessage(`Could not reach the local API: ${error.message}`);
      setRunAvailable(true);
    }
  }

  async function render() {
    syncToggle();
    try {
      if (state.attackId) await renderAttack();
      else if (state.taskId) await renderTask();
    } catch (error) {
      clear($("steps"));
      $("steps").append(el("li", { class: "divider" }, el("span", { text: `Could not load the data: ${error.message}. Serve this folder over HTTP (python -m http.server) rather than opening the file directly.` })));
    }
  }

  // -- navigation, kept in the address so a trajectory can be linked ------------------------------

  function go(target) {
    const params = new URLSearchParams();
    if (target.attack) params.set("attack", target.attack);
    else {
      params.set("task", target.task || state.taskId);
      params.set("agent", target.agent || state.agent);
    }
    location.hash = params.toString();
  }

  function fromHash() {
    const params = new URLSearchParams(location.hash.slice(1));
    const live = params.get("live");
    state.liveRun = state.live && live ? live : null;
    if (state.liveRun) {
      state.attackId = null;
      $("attack").value = "";
      syncToggle();
      pollLive(state.liveRun);
      return;
    }
    const attack = params.get("attack");
    const task = params.get("task");
    const agent = params.get("agent");
    if (attack && state.index.attacks.some((c) => c.case_id === attack)) {
      state.attackId = attack;
    } else {
      state.attackId = null;
      state.taskId = state.index.tasks.some((t) => t.task_id === task) ? task : state.taskId || state.index.tasks[0].task_id;
      if (["A0", "A1", "A2"].includes(agent)) state.agent = agent;
    }
    $("attack").value = state.attackId || "";
    $("task").value = state.attackId ? "" : state.taskId;
    render();
  }

  async function start() {
    state.index = await data("index.json");
    $("replay-note").textContent = state.index.labels.replay;
    $("run-note").textContent = state.index.labels.run_button;
    fillDatabases();
    fillTasks();
    fillAttacks();
    $("database").addEventListener("change", () => fillTasks());
    $("task").addEventListener("change", (event) => go({ task: event.target.value }));
    $("attack").addEventListener("change", (event) => event.target.value && go({ attack: event.target.value }));
    for (const button of document.querySelectorAll("[data-agent]")) {
      button.addEventListener("click", () => go({ task: state.taskId, agent: button.dataset.agent }));
    }
    window.addEventListener("hashchange", fromHash);
    fromHash();
    const status = await probeLocalApi();
    if (status) {
      enableLive(status);
      if (new URLSearchParams(location.hash.slice(1)).get("live")) fromHash();
    }
  }

  start().catch((error) => {
    $("replay-note").textContent = `Could not load the data: ${error.message}. Serve this folder over HTTP (python -m http.server) rather than opening the file directly.`;
  });
})();
