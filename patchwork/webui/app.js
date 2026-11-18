/* Patchwork's interface. Talks to the local Python server (server.py). */
(() => {
  "use strict";

  // ---------- plumbing ----------
  const token = location.hash.slice(1);
  history.replaceState(null, "", location.pathname);
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const headers = { "Content-Type": "application/json", "X-Patchwork-Token": token };
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

  async function api(method, args = {}) {
    const res = await fetch(`/api/${method}`, { method: "POST", headers, body: JSON.stringify({ args }) });
    const data = await res.json().catch(() => ({ ok: false, error: `HTTP ${res.status}` }));
    if (!data.ok) throw new Error(data.error || "Something went wrong.");
    return data.result;
  }
  // Like api(), but shows the error to the user and returns undefined.
  async function act(method, args) {
    try { return await api(method, args); } catch (e) { await alertBox("That didn't work", e.message); }
  }

  // ---------- state ----------
  let S = null;
  let seq = 0;
  let page = "replacer";
  let filterText = "";
  let sort = { key: null, asc: true };
  let selected = new Set();
  let renderedKeys = new Set();
  let renderedProfile = null;
  let hits = 0;
  let unseen = 0;
  let feedFilter = "all";

  // ---------- modal / toast ----------
  let modalResolve = null;
  function modal({ title, html = "", buttons = [{ label: "OK", value: true, kind: "primary" }], input = null, dismissable = true }) {
    const wrap = $("#modal");
    $("#modal-title").textContent = title;
    $("#modal-body").innerHTML = html;
    const inp = $("#modal-input");
    inp.hidden = !input;
    if (input) { inp.value = input.value || ""; inp.placeholder = input.placeholder || ""; }
    const actions = $("#modal-actions");
    actions.innerHTML = "";
    for (const b of buttons) {
      const el = document.createElement("button");
      el.className = `btn ${b.kind || ""}`;
      el.textContent = b.label;
      el.addEventListener("click", () => closeModal(b.value === undefined ? b.label : b.value));
      actions.append(el);
    }
    wrap.hidden = false;
    wrap.dataset.dismissable = dismissable ? "1" : "";
    setTimeout(() => (input ? inp.focus() : actions.lastElementChild?.focus()), 60);
    return new Promise((resolve) => { modalResolve = (v) => resolve(input && v === true ? inp.value.trim() : v); });
  }
  function closeModal(value) {
    if ($("#modal").hidden) return;
    $("#modal").hidden = true;
    const r = modalResolve; modalResolve = null;
    r && r(value);
  }
  $("#modal").addEventListener("click", (e) => { if (e.target.id === "modal" && $("#modal").dataset.dismissable) closeModal(null); });
  $("#modal-input").addEventListener("keydown", (e) => { if (e.key === "Enter") closeModal(true); });
  const alertBox = (title, text) => modal({ title, html: `<p>${esc(text).replace(/\n/g, "<br>")}</p>` });
  const confirmBox = (title, html, okLabel = "Continue", okKind = "primary") =>
    modal({ title, html, buttons: [{ label: "Cancel", value: false }, { label: okLabel, value: true, kind: okKind }] });
  const promptBox = (title, html, value = "", okLabel = "OK") =>
    modal({ title, html, input: { value }, buttons: [{ label: "Cancel", value: null }, { label: okLabel, value: true, kind: "primary" }] });

  function toast(text, { tone = "", action = null, ms = 3800 } = {}) {
    const t = document.createElement("div");
    t.className = `toast ${tone}`;
    t.innerHTML = `<i></i><span>${esc(text)}</span>`;
    if (action) {
      const b = document.createElement("button");
      b.className = "btn small";
      b.textContent = action.label;
      b.addEventListener("click", () => { action.fn(); dismiss(); });
      t.append(b);
    }
    const box = $("#toasts");
    box.append(t);
    while (box.children.length > 3) box.firstElementChild.remove();
    const timer = setTimeout(dismiss, ms);
    function dismiss() { clearTimeout(timer); t.classList.add("out"); setTimeout(() => t.remove(), 300); }
  }

  // ---------- sparks: lines burst out of buttons on hover and click ----------
  const SPARK_COLORS = ["#c9a7ff", "#a77bff", "#8f6bff", "#e3d4ff"];
  function sparks(x, y, count, spread = 360, start = 0, dist = 26) {
    if (reduceMotion) return;
    for (let i = 0; i < count; i++) {
      const s = document.createElement("span");
      s.className = "spark";
      s.style.left = `${x}px`; s.style.top = `${y}px`;
      s.style.setProperty("--a", `${start + (spread / count) * i + Math.random() * 12}deg`);
      s.style.setProperty("--c", SPARK_COLORS[i % SPARK_COLORS.length]);
      s.style.setProperty("--d", `${dist + Math.random() * 10}px`);
      document.body.append(s);
      setTimeout(() => s.remove(), 520);
    }
  }
  const lastSpark = new WeakMap();
  document.addEventListener("mouseover", (e) => {
    const b = e.target.closest(".btn");
    if (!b || b.contains(e.relatedTarget)) return;
    const now = performance.now();
    if (now - (lastSpark.get(b) || 0) < 600) return;
    lastSpark.set(b, now);
    const r = b.getBoundingClientRect();
    // a few lines from each end of the button
    sparks(r.left + 4, r.top + r.height / 2, 3, 90, 135, 16);
    sparks(r.right - 4, r.top + r.height / 2, 3, 90, -45, 16);
  });
  document.addEventListener("click", (e) => {
    const b = e.target.closest(".burst");
    if (b) sparks(e.clientX, e.clientY, 12);
  });

  // ---------- dropdowns ----------
  function closeMenus(except) {
    $$(".dd").forEach((d) => { if (d !== except) { d.classList.remove("open"); $(".dd-menu", d).hidden = true; } });
  }
  $$(".dd").forEach((dd) => {
    const btn = dd.querySelector(".dd-btn, #export-btn");
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = !dd.classList.contains("open");
      closeMenus(dd);
      dd.classList.toggle("open", open);
      $(".dd-menu", dd).hidden = !open;
    });
    $(".dd-menu", dd).addEventListener("click", (e) => e.stopPropagation());
  });
  document.addEventListener("click", () => closeMenus());

  // ---------- rendering ----------
  function render() {
    if (!S) return;
    document.body.classList.toggle("running", S.running);
    document.body.classList.toggle("busy", S.busy);
    document.body.classList.toggle("in-rivals", S.running && S.game.in_rivals);

    $("#power-label").textContent = S.busy ? "Working…" : S.running ? "Stop" : "Start";
    $("#power").setAttribute("aria-label", S.running ? "Stop Patchwork" : "Start Patchwork");
    const pill = $("#status-pill");
    pill.className = `pill ${S.status.tone}`;
    pill.title = S.status.text;
    pill.querySelector("span").textContent = S.status.text;

    $("#version").textContent = `Patchwork v${S.version}`;
    $("#active-count").textContent = `${plural(S.active_count, "swap")} on`;
    $("#configs-path").textContent = `Configs: ${S.paths.configs}`;
    $("#undo-btn").disabled = !S.can_undo;

    renderProfiles();
    renderRows();

    setStat("#stat-active", S.active_count);
    $("#stat-game").textContent = S.game.in_game ? (S.game.in_rivals ? S.rivals.name : "Another game") : "Not in a game";
    $("#rivals-place").textContent = S.rivals.place_id;
    $("#home-path").textContent = S.paths.home;
    $$("[data-setting]").forEach((el) => { el.checked = !!S.settings[el.dataset.setting]; });
  }

  function renderProfiles() {
    const current = S.profiles.find((p) => p.name === S.editing);
    const ed = $("#dd-editing");
    $(".dd-text", ed).textContent = S.editing;
    $(".dot", ed).className = `dot ${current?.enabled ? "on" : "off"}`;
    $(".dd-btn", ed).title = current?.enabled ? `${S.editing} is on` : `${S.editing} is off`;
    const em = $(".dd-menu", ed);
    em.innerHTML = "";
    for (const p of S.profiles) {
      const b = document.createElement("button");
      b.className = p.name === S.editing ? "current" : "";
      b.innerHTML = `<i class="dot ${p.enabled ? "on" : "off"}"></i><span>${esc(p.name)}</span><span class="count">${p.count}</span>`;
      b.addEventListener("click", () => { closeMenus(); if (p.name !== S.editing) { selected.clear(); act("select_profile", { name: p.name }); } });
      em.append(b);
    }

    const on = S.profiles.filter((p) => p.enabled);
    const en = $("#dd-enabled");
    $(".dd-text", en).textContent = !on.length ? "No Configs Enabled" : on.length === 1 ? on[0].name : `${on.length} Configs Enabled`;
    const nm = $(".dd-menu", en);
    nm.innerHTML = "";
    for (const p of S.profiles) {
      const l = document.createElement("label");
      l.innerHTML = `<input type="checkbox" ${p.enabled ? "checked" : ""}><span>${esc(p.name)}</span><span class="count">${p.on}/${p.count}</span>`;
      l.querySelector("input").addEventListener("change", async (e) => {
        await act("set_profile_enabled", { name: p.name, on: e.target.checked });
        toast(`${p.name} is ${e.target.checked ? "on" : "off"}`);
      });
      nm.append(l);
    }
  }

  const key = (s) => `${s.name}|${s.kind}|${s.value}|${s.targets.join(",")}`;
  function visibleRows() {
    const q = filterText.toLowerCase();
    let rows = S.swaps.filter((s) => !q || s.name.toLowerCase().includes(q) || s.display.toLowerCase().includes(q)
      || s.targets.join(" ").includes(q) || s.mode.toLowerCase() === q);
    if (sort.key) {
      const val = (s) => sort.key === "ids" ? s.targets.join(",") : sort.key === "enabled" ? (s.enabled ? 0 : 1) : String(s[sort.key]).toLowerCase();
      rows = [...rows].sort((a, b) => (val(a) < val(b) ? -1 : val(a) > val(b) ? 1 : 0) * (sort.asc ? 1 : -1));
    }
    return rows;
  }

  function renderRows() {
    const body = $("#rows");
    if (renderedProfile !== S.editing) { selected.clear(); renderedKeys = new Set(); }
    renderedProfile = S.editing;
    selected = new Set([...selected].filter((i) => i < S.swaps.length));
    const prof = S.profiles.find((p) => p.name === S.editing);
    $("#table-title").textContent = `Replacement Profiles · ${S.editing} (${plural(S.swaps.length, "swap")}${prof && !prof.enabled ? ", off" : ""})`;

    const rows = visibleRows();
    body.innerHTML = "";
    if (!rows.length) {
      const e = document.createElement("div");
      e.className = "empty-row";
      e.innerHTML = S.swaps.length
        ? `<b>No matches</b><span>Nothing in ${esc(S.editing)} matches “${esc(filterText)}”.</span>`
        : `<b>No swaps in ${esc(S.editing)} yet</b><span>Fill in the form below and press Add New, or Import a Patchwork or Fleasion preset.</span>`;
      body.append(e);
    }
    const next = new Set();
    let stagger = 0;
    for (const s of rows) {
      const k = key(s);
      next.add(k);
      const row = document.createElement("div");
      row.className = `row${s.enabled ? "" : " off"}${selected.has(s.index) ? " selected" : ""}`;
      row.dataset.kind = s.kind;
      row.dataset.index = s.index;
      if (!renderedKeys.has(k)) { row.classList.add("enter"); row.style.setProperty("--i", Math.min(stagger++, 20)); }
      row.innerHTML = `
        <span class="status"><span class="switch"><input type="checkbox" ${s.enabled ? "checked" : ""} title="On / off"><i></i></span></span>
        <span class="name" title="${esc(s.name)}">${esc(s.name)}</span>
        <span class="mode">${esc(s.mode)}</span>
        <span class="ids" title="${esc(s.targets.join(", "))}">${esc(s.targets.join(", "))}</span>
        <span class="repl" title="${esc(s.display)}">${esc(s.display)}</span>`;
      row.querySelector("input").addEventListener("change", (e) => act("set_swaps_enabled", { indexes: [s.index], on: e.target.checked }));
      row.addEventListener("click", (e) => {
        if (e.target.closest(".switch")) return;
        if (e.ctrlKey || e.metaKey) selected.has(s.index) ? selected.delete(s.index) : selected.add(s.index);
        else if (e.shiftKey && selected.size) {
          const order = visibleRows().map((x) => x.index);
          const a = order.indexOf([...selected].pop()), b = order.indexOf(s.index);
          order.slice(Math.min(a, b), Math.max(a, b) + 1).forEach((i) => selected.add(i));
        } else selected = new Set([s.index]);
        markSelection();
      });
      row.addEventListener("dblclick", (e) => { if (!e.target.closest(".switch")) { selected = new Set([s.index]); markSelection(); loadSelected(); } });
      body.append(row);
    }
    renderedKeys = next;
    markSelection();
  }

  function markSelection() {
    $$("#rows .row").forEach((r) => r.classList.toggle("selected", selected.has(+r.dataset.index)));
    const one = selected.size === 1;
    $("#load-sel").disabled = !one;
    $("#update-sel").disabled = !one;
    $("#toggle-sel").disabled = !selected.size;
    $("#remove-sel").disabled = !selected.size;
  }

  function setStat(sel, value) {
    const el = $(sel);
    if (el.textContent === String(value)) return;
    el.textContent = value;
    el.classList.remove("bump"); void el.offsetWidth; el.classList.add("bump");
  }

  $$(".thead button").forEach((b) => b.addEventListener("click", () => {
    const k = b.dataset.sort;
    if (sort.key === k && !sort.asc) sort = { key: null, asc: true };
    else sort = { key: k, asc: sort.key === k ? false : true };
    $$(".thead button").forEach((x) => { x.classList.toggle("sorted", x.dataset.sort === sort.key); x.classList.toggle("asc", x.dataset.sort === sort.key && sort.asc); });
    renderRows();
  }));
  $("#search").addEventListener("input", (e) => { filterText = e.target.value; renderRows(); });

  async function refresh() { S = await api("snapshot"); render(); }
  let refreshTimer = null;
  const scheduleRefresh = () => { clearTimeout(refreshTimer); refreshTimer = setTimeout(() => refresh().catch(() => {}), 40); };

  // ---------- tabs ----------
  function go(name) {
    page = name;
    $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.page === name));
    $$(".page").forEach((p) => p.classList.toggle("active", p.id === `page-${name}`));
    moveTabLine();
    if (name === "activity") { unseen = 0; updateBadge(); $("#feed").scrollTop = 0; }
  }
  function moveTabLine() {
    const t = $(".tab.active");
    if (!t) return;
    const line = $(".tab-line");
    line.style.width = `${t.offsetWidth - 20}px`;
    line.style.transform = `translateX(${t.offsetLeft + 10}px)`;
  }
  $$(".tab").forEach((t) => t.addEventListener("click", () => go(t.dataset.page)));
  window.addEventListener("resize", moveTabLine);

  // ---------- activity ----------
  const fmtTime = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  function addActivity(e, fresh = true) {
    const el = document.createElement("div");
    el.className = `entry ${e.tag || "info"}${fresh ? " fresh" : ""}`;
    el.dataset.tag = e.tag || "info";
    el.innerHTML = `<time>${fmtTime(e.t)}</time><span class="edot"></span><span>${esc(e.text)}</span>`;
    el.hidden = feedFilter !== "all" && el.dataset.tag !== feedFilter;
    const feed = $("#feed");
    feed.prepend(el);
    while (feed.children.length > 400) feed.lastElementChild.remove();
    if (e.tag === "hit") {
      hits++; setStat("#stat-hits", hits);
      const m = e.text.match(/\(([^()]*)\)\s*$/);
      const s = m && S && S.swaps.find((x) => x.name === m[1]);
      const row = s && $(`#rows .row[data-index="${s.index}"]`);
      if (row) { row.classList.remove("hit"); void row.offsetWidth; row.classList.add("hit"); }
    }
    if (fresh && page !== "activity" && (e.tag === "hit" || e.tag === "warn")) { unseen++; updateBadge(); }
    if (fresh && e.tag === "warn" && page !== "activity") queueWarning(e.text);
  }
  function updateBadge() { const b = $("#activity-badge"); b.hidden = !unseen; b.textContent = unseen > 99 ? "99+" : unseen; }
  let pendingWarnings = [], warnTimer = null;
  function queueWarning(text) {
    pendingWarnings.push(text);
    clearTimeout(warnTimer);
    warnTimer = setTimeout(() => {
      const list = pendingWarnings; pendingWarnings = [];
      toast(list.length === 1 ? list[0] : `${list.length} new warnings`, { tone: "warn", ms: 6000, action: { label: "View", fn: () => go("activity") } });
    }, 500);
  }
  $$("#filters .chip").forEach((c) => c.addEventListener("click", () => {
    feedFilter = c.dataset.filter;
    $$("#filters .chip").forEach((x) => x.classList.toggle("active", x === c));
    $$("#feed .entry").forEach((el) => { el.hidden = feedFilter !== "all" && el.dataset.tag !== feedFilter; });
  }));

  // ---------- events from Python ----------
  async function pump() {
    for (;;) {
      try {
        const res = await fetch(`/api/events?after=${seq}`, { headers });
        const data = await res.json();
        for (const ev of data.events || []) {
          seq = Math.max(seq, ev.seq);
          if (ev.kind === "state") scheduleRefresh();
          else if (ev.kind === "log") addActivity(ev.data, true);
          else if (ev.kind === "error") alertBox(ev.data.title, ev.data.text);
        }
      } catch { await sleep(1500); }
    }
  }

  // ---------- editor ----------
  const fName = $("#f-name"), fIds = $("#f-ids"), fWith = $("#f-with");
  let detectTimer = null;
  function detect() {
    clearTimeout(detectTimer);
    detectTimer = setTimeout(async () => {
      const box = $("#detect");
      const text = fWith.value;
      const label = box.querySelector("span");
      if (!text.trim()) {
        box.className = "detect"; box.removeAttribute("data-kind");
        label.textContent = "Type an asset ID, paste a link, or browse for a file.";
        return;
      }
      try {
        const r = await api("detect", { replace_text: text });
        if (r.kind) {
          box.className = "detect ok"; box.dataset.kind = r.kind;
          label.textContent = `${{ asset: "Asset ID", url: "Web link (downloaded once)", file: "File on this PC" }[r.kind]} · ${r.describe}`;
        } else {
          box.className = "detect err"; box.removeAttribute("data-kind");
          label.textContent = r.error || "Not recognised yet.";
        }
      } catch { /* ignore */ }
    }, 160);
  }
  fWith.addEventListener("input", detect);

  async function saveSwap(index) {
    const args = { name: fName.value, ids: fIds.value, replace: fWith.value, index };
    let r;
    try { r = await api("save_swap", args); } catch (e) { return alertBox("Can't save this swap", e.message); }
    if (r.confirm === "id_swap") {
      const ok = await confirmBox("Asset-ID swap",
        "<p>An asset-ID swap makes Roblox ask its servers for a different asset, which Roblox can see.</p>" +
        "<p>Link and file swaps only change things on your PC, so they're the lower-profile choice.</p>" +
        "<p>You can turn this reminder off in Settings.</p>", "Save anyway");
      if (!ok) return;
      try { r = await api("save_swap", { ...args, confirmed: true }); } catch (e) { return alertBox("Can't save this swap", e.message); }
    }
    return r;
  }

  $("#editor").addEventListener("submit", async (e) => {
    e.preventDefault();
    const r = await saveSwap(null);
    if (!r) return;
    fName.value = fIds.value = fWith.value = ""; detect();
    toast("Swap added");
    await refresh();
    selected = new Set([r.index]); markSelection();
    flashRow(r.index);
  });
  function loadSelected() {
    if (selected.size !== 1) return;
    const s = S.swaps.find((x) => x.index === [...selected][0]);
    if (!s) return;
    fName.value = s.name; fIds.value = s.targets.join(", "); fWith.value = s.display;
    detect();
    $("#editor").classList.remove("loaded"); void $("#editor").offsetWidth; $("#editor").classList.add("loaded");
    fWith.focus();
  }
  $("#load-sel").addEventListener("click", loadSelected);
  $("#update-sel").addEventListener("click", async () => {
    if (selected.size !== 1) return;
    const index = [...selected][0];
    const r = await saveSwap(index);
    if (!r) return;
    toast("Swap updated");
    await refresh();
    flashRow(index);
  });
  $("#toggle-sel").addEventListener("click", toggleSelected);
  $("#remove-sel").addEventListener("click", () => removeSwaps([...selected]));
  $("#browse").addEventListener("click", async () => {
    const path = await act("pick_file", { kind: "replacement" });
    if (!path) return;
    fWith.value = path;
    if (!fName.value.trim()) fName.value = path.split(/[\\/]/).pop().replace(/\.[^.]+$/, "");
    detect();
  });

  function flashRow(index) {
    const row = $(`#rows .row[data-index="${index}"]`);
    if (!row) return;
    row.scrollIntoView({ block: "nearest", behavior: "smooth" });
    row.classList.remove("flash"); void row.offsetWidth; row.classList.add("flash");
  }
  async function removeSwaps(indexes) {
    if (!indexes.length) return;
    $$("#rows .row").forEach((r) => { if (indexes.includes(+r.dataset.index)) r.classList.add("leave"); });
    await sleep(reduceMotion ? 0 : 240);
    const n = await act("remove_swaps", { indexes });
    selected.clear();
    if (n) toast(`Removed ${plural(n, "swap")}`, { action: { label: "Undo", fn: () => act("undo") } });
  }
  async function toggleSelected() {
    const idx = [...selected];
    if (!idx.length) return;
    const allOn = idx.every((i) => S.swaps[i]?.enabled);
    await act("set_swaps_enabled", { indexes: idx, on: !allOn });
  }

  // ---------- configuration buttons ----------
  $("#p-new").addEventListener("click", async () => {
    const name = await promptBox("New profile", "<p>A profile is a named set of swaps you can switch on and off together.</p>", "", "Create");
    if (!name) return;
    const r = await act("new_profile", { name });
    if (r) toast(`Created ${r}`);
  });
  $("#p-dup").addEventListener("click", async () => { const r = await act("duplicate_profile"); if (r) toast(`Created ${r}`); });
  $("#p-rename").addEventListener("click", async () => {
    const n = await promptBox("Rename profile", "", S.editing, "Rename");
    if (n && n !== S.editing) { const r = await act("rename_profile", { new: n }); if (r) toast(`Renamed to ${r}`); }
  });
  $("#p-delete").addEventListener("click", async () => {
    const name = S.editing, count = S.swaps.length;
    const ok = await confirmBox("Delete profile", `<p>Delete <b>${esc(name)}</b> and its ${plural(count, "swap")}?</p><p>You can undo this with Ctrl+Z while Patchwork is open.</p>`, "Delete", "danger");
    if (ok && (await act("delete_profile")) !== undefined) toast(`Deleted ${name}`, { action: { label: "Undo", fn: () => act("undo") } });
  });

  async function importPreset() {
    const path = await act("pick_file", { kind: "preset" });
    if (!path) return;
    let r;
    try { r = await api("import_preset", { path }); } catch (e) { return alertBox("Can't import this file", e.message); }
    if (r.confirm === "links") {
      const ok = await confirmBox("Import preset",
        `<p>This ${esc(r.format)} preset has <b>${r.count}</b> swaps. <b>${r.links}</b> of them download files from:</p>` +
        `<ul>${r.hosts.map((h) => `<li><code>${esc(h)}</code></li>`).join("")}</ul><p>Only import presets from people you trust.</p>`, "Import");
      if (!ok) return;
      try { r = await api("import_preset", { path, confirmed: true }); } catch (e) { return alertBox("Can't import this file", e.message); }
    }
    toast(`Imported ${plural(r.count, "swap")} as “${r.name}”`);
    if (r.skipped?.length) alertBox("Some rules were skipped", `Imported ${plural(r.count, "swap")}. Skipped ${r.skipped.length}:\n\n${r.skipped.slice(0, 15).join("\n")}`);
  }
  $("#import-btn").addEventListener("click", importPreset);
  $$("#dd-export .dd-menu button").forEach((b) => b.addEventListener("click", async () => {
    closeMenus();
    const path = await act("pick_file", { kind: "preset", save: true, default_name: `${S.editing}.json` });
    if (!path) return;
    const r = await act("export_preset", { fmt: b.dataset.fmt, path });
    if (!r) return;
    toast(`Exported ${S.editing}`);
    if (r.skipped.length) alertBox("Some swaps were left out", "Fleasion presets can't point at files on your PC, so these swaps were left out:\n\n" + r.skipped.join("\n"));
  }));

  // ---------- footer, power, settings ----------
  $("#clear-cache").addEventListener("click", async () => { if ((await act("clear_cache")) !== undefined) toast("Roblox's cache is clear"); });
  $("#open-configs").addEventListener("click", () => act("open_folder", { which: "configs" }));
  $("#undo-btn").addEventListener("click", () => act("undo"));
  $("#power").addEventListener("click", async () => {
    if (!S || S.busy) return;
    if (S.running) return act("stop");
    if (!S.active_count) toast("No swaps are switched on yet. Tick a profile under Enabled.", { tone: "warn" });
    await act("start");
  });
  $$("[data-setting]").forEach((el) => el.addEventListener("change", () => act("set_setting", { key: el.dataset.setting, value: el.checked })));
  $("#remove-cert").addEventListener("click", async () => {
    const ok = await confirmBox("Remove certificate",
      "<p>Stop Patchwork, remove its certificate and setting from every Roblox install, and delete this PC's certificate?</p><p>Your profiles are kept. Patchwork makes a new certificate next time you press Start.</p>", "Remove", "danger");
    if (ok && (await act("remove_certificate")) !== undefined) toast("Certificate removed");
  });
  $("#open-home").addEventListener("click", () => act("open_folder", { which: "home" }));
  $("#clear-downloads").addEventListener("click", async () => { if ((await act("clear_downloads")) !== undefined) toast("Downloaded files cleared"); });

  // ---------- keyboard ----------
  document.addEventListener("keydown", (e) => {
    const typing = e.target.closest("input");
    if (e.key === "Escape") {
      if (!$("#modal").hidden) { if ($("#modal").dataset.dismissable) closeModal(null); return; }
      closeMenus();
      if (selected.size) { selected.clear(); markSelection(); }
      return;
    }
    if (!$("#modal").hidden) return;
    const mod = e.ctrlKey || e.metaKey;
    const k = e.key.toLowerCase();
    if (mod && k === "z" && !typing) { e.preventDefault(); act("undo"); return; }
    if (mod && k === "f") { e.preventDefault(); go("replacer"); $("#search").focus(); return; }
    if (mod && k === "n") { e.preventDefault(); go("replacer"); fIds.focus(); return; }
    if (mod && k === "a" && !typing && page === "replacer") { e.preventDefault(); selected = new Set(S.swaps.map((s) => s.index)); markSelection(); return; }
    if (typing || page !== "replacer") return;
    if (e.key === "Delete") { e.preventDefault(); removeSwaps([...selected]); }
    else if (e.key === " ") { e.preventDefault(); toggleSelected(); }
    else if (e.key === "Enter") loadSelected();
  });

  window.addEventListener("blur", () => document.body.classList.add("unfocused"));
  window.addEventListener("focus", () => document.body.classList.remove("unfocused"));

  // ---------- first run ----------
  async function firstRunNotice() {
    const name = esc(S.rivals.name);
    const ok = await modal({
      title: "Before you start",
      dismissable: false,
      html: `<p>Patchwork changes how ${name} looks and sounds <b>on your PC only</b>.</p>
        <ul>
          <li>Roblox's rules don't allow third-party tools that change the game, so any asset swapper carries some risk of a ban. Patchwork keeps that risk low (it never touches the game's memory or your account), but nothing can make it zero.</li>
          <li>Keep swaps cosmetic. Swaps that give you an advantage, like see-through walls or easier-to-spot enemies, are what ${name} bans players for.</li>
          <li>Try it on an alt account first.</li>
        </ul>
        <p>Use Patchwork at your own risk.</p>`,
      buttons: [{ label: "Quit", value: false }, { label: "I understand", value: true, kind: "primary" }],
    });
    if (ok) return act("accept_notice");
    await fetch("/api/quit", { method: "POST", headers, body: "{}" }).catch(() => {});
    document.body.innerHTML = "";
    window.close();
  }

  // ---------- boot ----------
  async function boot() {
    const started = performance.now();
    const skipped = new Promise((r) => $("#intro").addEventListener("click", r, { once: true }));
    for (let tries = 0; ; tries++) {
      try { S = await api("snapshot"); break; } catch { await sleep(Math.min(2000, 200 * (tries + 1))); }
    }
    seq = S.seq || 0;
    try { (await api("activity_log")).forEach((e) => addActivity(e, false)); } catch { /* ignore */ }
    render();
    await Promise.race([sleep(reduceMotion ? 0 : Math.max(0, 1900 - (performance.now() - started))), skipped]);
    $("#intro").classList.add("leave");
    document.body.classList.remove("booting");
    document.body.classList.add("ready");
    setTimeout(() => $("#intro").remove(), 900);
    requestAnimationFrame(moveTabLine);
    pump();
    if (!S.notice_accepted) setTimeout(firstRunNotice, reduceMotion ? 0 : 800);
  }
  boot();
})();
