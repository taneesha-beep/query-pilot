"use strict";

// The results page: lays out what scripts/build_viewer.py computed from the committed summaries
// in results/. Every figure arrives as text already formatted in Python, under test; nothing here
// computes one. Every string goes into the page through textContent, never as markup.

(() => {
  const $ = (id) => document.getElementById(id);

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

  function table(columns, rows) {
    const head = el("tr", {}, ...columns.map((column) => el("th", { scope: "col", text: column })));
    const body = rows.map((row) => el("tr", {}, ...row.map((cell, i) => el(i === 0 ? "th" : "td", i === 0 ? { scope: "row", text: cell } : { text: cell }))));
    return el("div", { class: "table-wrap" }, el("table", { class: "figures" }, el("thead", {}, head), el("tbody", {}, ...body)));
  }

  function section(part) {
    const head = el("header", { class: "section-head" }, el("p", { class: "eyebrow", text: part.eyebrow }), el("h2", { id: `${part.id}-title`, text: part.title }), el("p", { class: "hint", text: part.lead }));
    const notes = part.notes.length ? el("ul", { class: "report-notes" }, ...part.notes.map((note) => el("li", { text: note }))) : null;
    const sources = part.sources.length ? el("div", { class: "sources" }, el("span", { class: "io-label", text: "Sources" }), ...part.sources.map((source) => el("p", { text: source }))) : null;
    return el("section", { class: "report-section", id: part.id, "aria-labelledby": `${part.id}-title` }, head, table(part.columns, part.rows), notes, sources);
  }

  async function start() {
    const response = await fetch("data/results.json");
    if (!response.ok) throw new Error(`data/results.json: HTTP ${response.status}`);
    const data = await response.json();
    $("definition").textContent = data.definition;
    for (const part of data.sections) $("report").append(section(part));
  }

  start().catch((error) => {
    $("report").append(el("p", { class: "note", text: `Could not load the data: ${error.message}. Serve this folder over HTTP (python -m http.server) rather than opening the file directly.` }));
  });
})();
