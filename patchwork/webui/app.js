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

  async function api(method, args = {}) {
    const res = await fetch(`/api/${method}`, { method: "POST", headers, body: JSON.stringify({ args }) });
    const data = await res.json().catch(() => ({ ok: false, error: `HTTP ${res.status}` }));
    if (!data.ok) throw new Error(data.error || "Something went wrong.");
    return data.result;
  }
  // Same as api() but shows the error to the user and returns undefined.
  async function act(method, args) {
    try { return await api(method, args); } catch (e) { await alertBox("That didn't work", e.message, "warn"); }
  }

  // ---------- state ----------
  let S = null;
  let seq = 0;
  let page = "swaps";
  let search = "";
  let selected = new Set();
  let renderedKeys = new Set();
  let renderedProfile = null;
  let hits = 0;
  let unseen = 0;
  let filter = "all";
  let editingIndex = null;
  const activity = [];

  const ICONS = {
    info: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 7.5v.5"/></svg>',
    warn: '<svg viewBox="0 0 24 24"><path d="M12 3 2.5 20h19L12 3z"/><path d="M12 10v4M12 17v.5"/></svg>',
    danger: '<svg viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>',
    shield: '<svg viewBox="0 0 24 24"><path d="M12 3 4.5 6v6c0 4.5 3.2 7.6 7.5 9 4.3-1.4 7.5-4.5 7.5-9V6L12 3z"/><path d="m9 12 2 2 4-4"/></svg>',
    edit: '<svg viewBox="0 0 24 24"><path d="M4 20h4L19 9l-4-4L4 16v4zM13.5 6.5l4 4"/></svg>',
    trash: '<svg viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>',
  };

  // ---------- modal / toast ----------
  let modalResolve = null;
  function modal({ title, html = "", icon = "info", buttons = [{ label: "OK", value: true, kind: "primary" }], input = null, dismissable = true }) {
    const wrap = $("#modal");
    $("#modal-title").textContent = title;
    $("#modal-body").innerHTML = html;
    const ic = $("#modal-icon");
    ic.className = "modal-icon " + (icon === "warn" ? "warn" : icon === "danger" ? "danger" : "");
    ic.innerHTML = ICONS[icon] || ICONS.info;
    const inp = $("#modal-input");
    inp.hidden = !input;
    if (input) { inp.value = input.value || ""; inp.placeholder = input.placeholder || ""; }
    const actions = $("#modal-actions");
    actions.innerHTML = "";
    buttons.forEach((b) => {
      const el = document.createElement("button");
      el.className = `btn ${b.kind || "ghost"}${b.kind === "primary" ? " burst" : ""}`;
      el.textContent = b.label;
      el.addEventListener("click", () => close(b.value === undefined ? b.label : b.value));
      actions.append(el);
    });
    wrap.hidden = false;
    wrap.dataset.dismissable = dismissable ? "1" : "";
    setTimeout(() => (input ? inp.focus() : actions.lastElementChild?.focus()), 60);
    return new Promise((resolve) => {
      modalResolve = (v) => resolve(input && v === true ? inp.value.trim() : v);
    });
    function close(v) { closeModal(v); }
  }
  function closeModal(value) {
    if ($("#modal").hidden) return;
    $("#modal").hidden = true;
    const r = modalResolve; modalResolve = null;
    r && r(value);
  }
  $("#modal").addEventListener("click", (e) => { if (e.target.id === "modal" && $("#modal").dataset.dismissable) closeModal(null); });
  $("#modal-input").addEventListener("keydown", (e) => { if (e.key === "Enter") closeModal(true); });
  const alertBox = (title, text, icon = "info") => modal({ title, html: `<p>${esc(text).replace(/\n/g, "<br>")}</p>`, icon });
  const confirmBox = (title, html, okLabel = "Continue", icon = "warn", okKind = "primary") =>
    modal({ title, html, icon, buttons: [{ label: "Cancel", value: false }, { label: okLabel, value: true, kind: okKind }] });

  function toast(text, { tone = "", action = null, ms = 3800 } = {}) {
    const t = document.createElement("div");
    t.className = `toast ${tone}`;
    t.innerHTML = `<i></i><span>${esc(text)}</span>`;
    if (action) {
      const b = document.createElement("button");
      b.className = "btn small ghost";
      b.textContent = action.label;
      b.addEventListener("click", () => { action.fn(); dismiss(); });
      t.append(b);
    }
    $("#toasts").append(t);
    while ($("#toasts").children.length > 3) $("#toasts").firstElementChild.remove();
    const timer = setTimeout(dismiss, ms);
    function dismiss() { clearTimeout(timer); t.classList.add("out"); setTimeout(() => t.remove(), 350); }
  }

  // ---------- stitch burst ----------
  const COLORS = ["var(--mint)", "var(--amber)", "var(--sky)", "var(--rose)"];
  function burst(x, y) {
    if (reduceMotion) return;
    for (let i = 0; i < 10; i++) {
      const s = document.createElement("span");
      s.className = "stitch";
      s.style.left = `${x}px`; s.style.top = `${y}px`;
      s.style.setProperty("--a", `${i * 36 + Math.random() * 18}deg`);
      s.style.setProperty("--c", COLORS[i % 4]);
      document.body.append(s);
      setTimeout(() => s.remove(), 650);
    }
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest(".burst, .power");
    if (b) burst(e.clientX, e.clientY);
  });

  // ---------- rendering ----------
  function render() {
    if (!S) return;
    const body = document.body;
    body.classList.toggle("running", S.running);
    body.classList.toggle("busy", S.busy);
    body.classList.toggle("in-rivals", S.running && S.game.in_rivals);

    // sidebar
    $("#power-label").textContent = S.busy ? "Working…" : S.running ? "Stop" : "Start";
    $("#power").setAttribute("aria-label", S.running ? "Stop Patchwork" : "Start Patchwork");
    const pill = $("#status-pill");
    pill.className = `pill ${S.status.tone}`;
    pill.querySelector("span").textContent = S.status.short;
    pill.title = S.status.text;
    $("#version").textContent = `v${S.version}`;
    $("#active-count").textContent = `${S.active_count} swap${S.active_count === 1 ? "" : "s"} on`;
    $("#undo-btn").disabled = !S.can_undo;

    renderProfiles();
    renderSwaps();

    // activity stats
    setStat("#stat-active", S.active_count);
    $("#stat-game").textContent = S.game.in_game ? (S.game.in_rivals ? S.rivals.name : "Another game") : "Not in a game";

    // settings
    $("#rivals-place").textContent = S.rivals.place_id;
    $("#home-path").textContent = S.paths.home;
    $$("[data-setting]").forEach((el) => { el.checked = !!S.settings[el.dataset.setting]; });
  }

  function renderProfiles() {
    const strip = $("#profile-strip");
    const sig = JSON.stringify([S.editing, S.profiles]);
    if (strip.dataset.sig === sig) return;
    const firstTime = !strip.dataset.sig;
    strip.dataset.sig = sig;
    strip.innerHTML = "";
    S.profiles.forEach((p, i) => {
      const chip = document.createElement("div");
      chip.className = `profile-chip${p.name === S.editing ? " editing" : ""}`;
      if (firstTime) chip.style.setProperty("--i", i); else chip.style.animation = "none";
      chip.title = p.enabled ? "On: these swaps apply in RIVALS" : "Off: switch on to use these swaps";
      chip.innerHTML = `<span class="pname">${esc(p.name)}</span><span class="pcount">${p.count}</span>
        <span class="switch" title="Switch this profile on or off"><input type="checkbox" ${p.enabled ? "checked" : ""}><i></i></span>`;
      chip.querySelector(".pname").addEventListener("click", () => p.name !== S.editing && selectProfile(p.name));
      chip.addEventListener("click", (e) => { if (e.target === chip) p.name !== S.editing && selectProfile(p.name); });
      chip.querySelector("input").addEventListener("change", async (e) => {
        const on = e.target.checked;
        await act("set_profile_enabled", { name: p.name, on });
        toast(on ? `${p.name} is on` : `${p.name} is off`);
      });
      strip.append(chip);
    });
  }

  async function selectProfile(name) {
    selected.clear();
    await act("select_profile", { name });
  }

  function swapKey(s) { return `${s.name}|${s.kind}|${s.value}|${s.targets.join(",")}`; }

  function renderSwaps() {
    const list = $("#swap-list");
    const profileChanged = renderedProfile !== S.editing;
    if (profileChanged) { selected.clear(); renderedKeys = new Set(); }
    renderedProfile = S.editing;
    const q = search.toLowerCase();
    const rows = S.swaps.filter((s) => !q || s.name.toLowerCase().includes(q) || s.display.toLowerCase().includes(q)
      || s.targets.join(" ").includes(q) || s.mode.toLowerCase() === q);
    selected = new Set([...selected].filter((i) => i < S.swaps.length));

    const onCount = S.swaps.filter((s) => s.enabled).length;
    const prof = S.profiles.find((p) => p.name === S.editing);
    $("#list-title").textContent = `${S.editing} · ${S.swaps.length} swap${S.swaps.length === 1 ? "" : "s"}${prof && !prof.enabled ? " · profile off" : ""}`;

    list.innerHTML = "";
    if (!S.swaps.length || !rows.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.innerHTML = S.swaps.length
        ? `<div class="mini-quilt"><i></i><i></i><i></i><i></i></div><h3>No matches</h3><p>Nothing in ${esc(S.editing)} matches “${esc(search)}”.</p>`
        : `<div class="mini-quilt"><i></i><i></i><i></i><i></i></div><h3>No swaps in ${esc(S.editing)} yet</h3>
           <p>Add your first swap, or import a Patchwork or Fleasion preset.</p>
           <div class="row-btns"><button class="btn primary burst" data-empty="new">New swap</button><button class="btn ghost" data-empty="import">Import preset</button></div>`;
      empty.querySelector('[data-empty="new"]')?.addEventListener("click", () => openEditor(null));
      empty.querySelector('[data-empty="import"]')?.addEventListener("click", importPreset);
      list.append(empty);
    }
    const nextKeys = new Set();
    let stagger = 0;
    rows.forEach((s) => {
      const key = swapKey(s);
      nextKeys.add(key);
      const row = document.createElement("div");
      row.className = `swap${s.enabled ? "" : " off"}${selected.has(s.index) ? " selected" : ""}`;
      row.dataset.kind = s.kind;
      row.dataset.index = s.index;
      if (!renderedKeys.has(key)) { row.classList.add("enter"); row.style.setProperty("--i", stagger++); }
      row.innerHTML = `
        <span class="switch" title="On / off"><input type="checkbox" ${s.enabled ? "checked" : ""}><i></i></span>
        <span class="sname" title="${esc(s.name)}">${esc(s.name)}</span>
        <span class="mode">${esc(s.mode)}</span>
        <span class="ids" title="${esc(s.targets.join(", "))}">${esc(s.targets.join(", "))}</span>
        <span class="repl" title="${esc(s.display)}">${esc(s.display)}</span>
        <span class="row-actions">
          <button class="icon-btn edit" title="Edit (Enter)">${ICONS.edit}</button>
          <button class="icon-btn del" title="Remove (Del)">${ICONS.trash}</button>
        </span>`;
      row.querySelector("input").addEventListener("change", (e) => act("set_swaps_enabled", { indexes: [s.index], on: e.target.checked }));
      row.querySelector(".edit").addEventListener("click", (e) => { e.stopPropagation(); openEditor(s.index); });
      row.querySelector(".del").addEventListener("click", (e) => { e.stopPropagation(); removeSwaps([s.index]); });
      row.addEventListener("click", (e) => {
        if (e.target.closest(".switch, .row-actions")) return;
        if (e.ctrlKey || e.metaKey) selected.has(s.index) ? selected.delete(s.index) : selected.add(s.index);
        else if (e.shiftKey && selected.size) {
          const anchor = Math.max(...selected);
          const [a, b] = [Math.min(anchor, s.index), Math.max(anchor, s.index)];
          for (let i = a; i <= b; i++) selected.add(i);
        } else selected = new Set([s.index]);
        updateSelection();
      });
      row.addEventListener("dblclick", (e) => { if (!e.target.closest(".switch, .row-actions")) openEditor(s.index); });
      list.append(row);
    });
    renderedKeys = nextKeys;
    updateSelection(onCount);
  }

  function updateSelection(onCount) {
    $$("#swap-list .swap").forEach((r) => r.classList.toggle("selected", selected.has(+r.dataset.index)));
    const on = onCount ?? S.swaps.filter((s) => s.enabled).length;
    $("#selection-info").textContent = selected.size
      ? `${selected.size} selected · Space turns on/off · Del removes · Enter edits`
      : `${S.swaps.length} swap${S.swaps.length === 1 ? "" : "s"} · ${on} on · double-click a swap to edit`;
  }

  function setStat(sel, value) {
    const el = $(sel);
    if (el.textContent === String(value)) return;
    el.textContent = value;
    el.classList.remove("bump"); void el.offsetWidth; el.classList.add("bump");
  }

  async function refresh() {
    S = await api("snapshot");
    render();
  }
  let refreshTimer = null;
  function scheduleRefresh() { clearTimeout(refreshTimer); refreshTimer = setTimeout(() => refresh().catch(() => {}), 40); }

  // ---------- pages ----------
  function go(name) {
    page = name;
    $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.page === name));
    $$(".page").forEach((p) => p.classList.toggle("active", p.id === `page-${name}`));
    moveIndicator();
    if (name === "activity") { unseen = 0; updateBadge(); const f = $("#feed"); f.scrollTop = 0; }
  }
  function moveIndicator() {
    const active = $(".nav-item.active");
    if (active) $(".nav-indicator").style.transform = `translateY(${active.offsetTop}px)`;
  }
  $$(".nav-item").forEach((b) => b.addEventListener("click", () => go(b.dataset.page)));

  // ---------- activity ----------
  function fmtTime(t) { return new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }
  function entryEl(e, fresh) {
    const el = document.createElement("div");
    el.className = `entry ${e.tag || "info"}${fresh ? " fresh" : ""}`;
    el.dataset.tag = e.tag || "info";
    el.innerHTML = `<time>${fmtTime(e.t)}</time><span class="dot"></span><span>${esc(e.text)}</span>`;
    if (filter !== "all" && el.dataset.tag !== filter) el.hidden = true;
    return el;
  }
  function addActivity(e, fresh = true) {
    activity.push(e);
    const feed = $("#feed");
    feed.prepend(entryEl(e, fresh));
    while (feed.children.length > 400) feed.lastElementChild.remove();
    if (e.tag === "hit") {
      hits++; setStat("#stat-hits", hits);
      const m = e.text.match(/\(([^()]*)\)\s*$/);
      if (m && S) {
        const s = S.swaps.find((x) => x.name === m[1]);
        const row = s && $(`#swap-list .swap[data-index="${s.index}"]`);
        if (row) { row.classList.remove("hit-glow"); void row.offsetWidth; row.classList.add("hit-glow"); }
      }
    }
    if (fresh && page !== "activity" && (e.tag === "hit" || e.tag === "warn")) { unseen++; updateBadge(); }
    if (fresh && e.tag === "warn" && page !== "activity") queueWarning(e.text);
  }
  // Several warnings at once (e.g. a few broken links) become one notification.
  let pendingWarnings = [];
  let warnTimer = null;
  function queueWarning(text) {
    pendingWarnings.push(text);
    clearTimeout(warnTimer);
    warnTimer = setTimeout(() => {
      const list = pendingWarnings; pendingWarnings = [];
      if (list.length === 1) toast(list[0], { tone: "warn", ms: 6000, action: { label: "View", fn: () => go("activity") } });
      else toast(`${list.length} new warnings`, { tone: "warn", ms: 6000, action: { label: "View", fn: () => go("activity") } });
    }, 500);
  }
  function updateBadge() {
    const b = $("#activity-badge");
    b.hidden = unseen === 0;
    b.textContent = unseen > 99 ? "99+" : unseen;
  }
  $$("#filters .chip").forEach((c) => c.addEventListener("click", () => {
    filter = c.dataset.filter;
    $$("#filters .chip").forEach((x) => x.classList.toggle("active", x === c));
    $$("#feed .entry").forEach((el) => { el.hidden = filter !== "all" && el.dataset.tag !== filter; });
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
          else if (ev.kind === "error") alertBox(ev.data.title, ev.data.text, "warn");
        }
      } catch {
        await sleep(1500);
      }
    }
  }

  // ---------- editor ----------
  const drawer = $("#drawer");
  function openEditor(index) {
    editingIndex = index;
    const s = index === null ? null : S.swaps.find((x) => x.index === index);
    $("#drawer-title").textContent = s ? "Edit swap" : "New swap";
    $("#drawer-save").textContent = s ? "Save changes" : "Add swap";
    $("#drawer-delete").hidden = !s;
    $("#f-name").value = s ? s.name : "";
    $("#f-ids").value = s ? s.targets.join(", ") : "";
    $("#f-with").value = s ? s.display : "";
    detect();
    $("#scrim").hidden = false;
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    setTimeout(() => (s ? $("#f-with") : $("#f-ids")).focus(), 250);
  }
  function closeEditor() {
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    $("#scrim").hidden = true;
  }
  $("#drawer-close").addEventListener("click", closeEditor);
  $("#drawer-cancel").addEventListener("click", closeEditor);
  $("#scrim").addEventListener("click", closeEditor);
  $("#drawer-delete").addEventListener("click", () => { const i = editingIndex; closeEditor(); removeSwaps([i]); });

  let detectTimer = null;
  function detect() {
    clearTimeout(detectTimer);
    detectTimer = setTimeout(async () => {
      const box = $("#detect");
      const text = $("#f-with").value;
      if (!text.trim()) {
        box.className = "detect"; box.removeAttribute("data-kind");
        box.querySelector("span").textContent = "Type an asset ID, paste a link, or browse for a file.";
        return;
      }
      try {
        const r = await api("detect", { replace_text: text });
        if (r.kind) {
          box.className = "detect ok"; box.dataset.kind = r.kind;
          const what = { asset: "Asset ID swap", url: "Web link: Patchwork downloads it once", file: "File on this PC" }[r.kind];
          box.querySelector("span").textContent = `${what} · ${r.describe}`;
        } else {
          box.className = "detect err"; box.removeAttribute("data-kind");
          box.querySelector("span").textContent = r.error || "Not recognised yet.";
        }
      } catch { /* ignore */ }
    }, 180);
  }
  $("#f-with").addEventListener("input", detect);

  $("#browse").addEventListener("click", async () => {
    const path = await act("pick_file", { kind: "replacement" });
    if (!path) return;
    $("#f-with").value = path;
    if (!$("#f-name").value.trim()) $("#f-name").value = path.split(/[\\/]/).pop().replace(/\.[^.]+$/, "");
    detect();
  });

  $("#swap-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const args = { name: $("#f-name").value, ids: $("#f-ids").value, replace: $("#f-with").value, index: editingIndex };
    let r;
    try { r = await api("save_swap", args); } catch (err) { return alertBox("Can't save this swap", err.message, "warn"); }
    if (r.confirm === "id_swap") {
      const ok = await confirmBox("Asset-ID swap",
        "<p>An asset-ID swap makes Roblox ask its servers for a different asset, which Roblox can see.</p>" +
        "<p>Link and file swaps only change things on your PC, so they're the lower-profile choice.</p>" +
        "<p>You can turn this reminder off in Settings.</p>", "Save anyway", "warn");
      if (!ok) return;
      try { r = await api("save_swap", { ...args, confirmed: true }); } catch (err) { return alertBox("Can't save this swap", err.message, "warn"); }
    }
    const wasNew = editingIndex === null;
    closeEditor();
    toast(wasNew ? "Swap added" : "Swap saved");
    await refresh();
    const row = $(`#swap-list .swap[data-index="${r.index}"]`);
    if (row) { row.scrollIntoView({ block: "nearest", behavior: "smooth" }); row.classList.add("flash"); }
  });

  // ---------- swap actions ----------
  async function removeSwaps(indexes) {
    if (!indexes.length) return;
    $$("#swap-list .swap").forEach((r) => { if (indexes.includes(+r.dataset.index)) r.classList.add("leave"); });
    await sleep(reduceMotion ? 0 : 260);
    const n = await act("remove_swaps", { indexes });
    selected.clear();
    if (n) toast(`Removed ${n} swap${n === 1 ? "" : "s"}`, { action: { label: "Undo", fn: () => act("undo") } });
  }
  async function toggleSelected() {
    const idx = [...selected];
    if (!idx.length) return;
    const allOn = idx.every((i) => S.swaps[i]?.enabled);
    await act("set_swaps_enabled", { indexes: idx, on: !allOn });
  }

  $("#new-swap").addEventListener("click", () => openEditor(null));
  $("#undo-btn").addEventListener("click", () => act("undo"));
  $("#clear-cache").addEventListener("click", async () => { if ((await act("clear_cache")) !== undefined) toast("Roblox's cache is clear"); });
  $("#search").addEventListener("input", (e) => { search = e.target.value; renderSwaps(); });

  // ---------- presets ----------
  async function importPreset() {
    const path = await act("pick_file", { kind: "preset" });
    if (!path) return;
    let r;
    try { r = await api("import_preset", { path }); } catch (e) { return alertBox("Can't import this file", e.message, "warn"); }
    if (r.confirm === "links") {
      const ok = await confirmBox("Import preset",
        `<p>This ${esc(r.format)} preset has <b>${r.count}</b> swaps. <b>${r.links}</b> of them download files from:</p>` +
        `<ul>${r.hosts.map((h) => `<li><code>${esc(h)}</code></li>`).join("")}</ul>` +
        "<p>Only import presets from people you trust.</p>", "Import");
      if (!ok) return;
      try { r = await api("import_preset", { path, confirmed: true }); } catch (e) { return alertBox("Can't import this file", e.message, "warn"); }
    }
    toast(`Imported ${r.count} swaps as “${r.name}”`);
    if (r.skipped?.length) {
      alertBox("Some rules were skipped", `Imported ${r.count} swaps. Skipped ${r.skipped.length}:\n\n${r.skipped.slice(0, 15).join("\n")}`, "warn");
    }
  }
  $("#import-btn").addEventListener("click", importPreset);

  function toggleMenu(menu, show) {
    $$(".menu").forEach((m) => { if (m !== menu) m.hidden = true; });
    menu.hidden = show === undefined ? !menu.hidden : !show;
  }
  $("#export-btn").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu($("#export-menu")); });
  $$("#export-menu button").forEach((b) => b.addEventListener("click", async () => {
    toggleMenu($("#export-menu"), false);
    const path = await act("pick_file", { kind: "preset", save: true, default_name: `${S.editing}.json` });
    if (!path) return;
    const r = await act("export_preset", { fmt: b.dataset.fmt, path });
    if (!r) return;
    toast(`Exported ${S.editing}`);
    if (r.skipped.length) alertBox("Some swaps were left out", "Fleasion presets can't point at files on your PC, so these swaps were left out:\n\n" + r.skipped.join("\n"));
  }));
  document.addEventListener("click", () => $$(".menu").forEach((m) => (m.hidden = true)));

  // ---------- profiles ----------
  $("#new-profile").addEventListener("click", async () => {
    const name = await modal({ title: "New profile", html: "<p>A profile is a named set of swaps you can switch on and off together.</p>",
      input: { placeholder: "e.g. Skins, Hit sounds" }, buttons: [{ label: "Cancel", value: null }, { label: "Create", value: true, kind: "primary" }] });
    if (!name) return;
    const created = await act("new_profile", { name });
    if (created) toast(`Created ${created}`);
  });
  $("#profile-menu-btn").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu($("#profile-menu")); });
  $$("#profile-menu button").forEach((b) => b.addEventListener("click", async () => {
    toggleMenu($("#profile-menu"), false);
    const name = S.editing;
    if (b.dataset.act === "rename") {
      const n = await modal({ title: "Rename profile", input: { value: name }, buttons: [{ label: "Cancel", value: null }, { label: "Rename", value: true, kind: "primary" }] });
      if (n && n !== name) { const r = await act("rename_profile", { new: n }); if (r) toast(`Renamed to ${r}`); }
    } else if (b.dataset.act === "duplicate") {
      const r = await act("duplicate_profile"); if (r) toast(`Created ${r}`);
    } else if (b.dataset.act === "configs") {
      act("open_folder", { which: "configs" });
    } else if (b.dataset.act === "delete") {
      const count = S.swaps.length;
      const ok = await confirmBox("Delete profile", `<p>Delete <b>${esc(name)}</b> and its ${count} swap${count === 1 ? "" : "s"}?</p><p>You can undo this with Ctrl+Z while Patchwork is open.</p>`, "Delete", "danger", "danger");
      if (ok && (await act("delete_profile")) !== undefined) toast(`Deleted ${name}`, { action: { label: "Undo", fn: () => act("undo") } });
    }
  }));

  // ---------- power ----------
  $("#power").addEventListener("click", async () => {
    if (!S || S.busy) return;
    if (S.running) await act("stop");
    else {
      if (!S.active_count) toast("No swaps are switched on yet. Turn on a profile.", { tone: "warn" });
      await act("start");
    }
  });

  // ---------- settings ----------
  $$("[data-setting]").forEach((el) => el.addEventListener("change", () => act("set_setting", { key: el.dataset.setting, value: el.checked })));
  $("#remove-cert").addEventListener("click", async () => {
    const ok = await confirmBox("Remove certificate",
      "<p>Stop Patchwork, remove its certificate and setting from every Roblox install, and delete this PC's certificate?</p><p>Your profiles are kept. Patchwork makes a new certificate next time you press Start.</p>",
      "Remove", "danger", "danger");
    if (ok && (await act("remove_certificate")) !== undefined) toast("Certificate removed");
  });
  $("#open-home").addEventListener("click", () => act("open_folder", { which: "home" }));
  $("#clear-downloads").addEventListener("click", async () => { if ((await act("clear_downloads")) !== undefined) toast("Downloaded files cleared"); });

  // ---------- keyboard ----------
  document.addEventListener("keydown", (e) => {
    const typing = e.target.closest("input, textarea");
    if (e.key === "Escape") {
      if (!$("#modal").hidden) { if ($("#modal").dataset.dismissable) closeModal(null); return; }
      if (drawer.classList.contains("open")) return closeEditor();
      $$(".menu").forEach((m) => (m.hidden = true));
      if (selected.size) { selected.clear(); updateSelection(); }
      return;
    }
    if (!$("#modal").hidden) return;
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === "z" && !typing) { e.preventDefault(); act("undo"); return; }
    if (mod && e.key.toLowerCase() === "f") { e.preventDefault(); go("swaps"); $("#search").focus(); return; }
    if (mod && e.key.toLowerCase() === "n") { e.preventDefault(); go("swaps"); openEditor(null); return; }
    if (mod && e.key.toLowerCase() === "a" && !typing && page === "swaps") { e.preventDefault(); selected = new Set(S.swaps.map((s) => s.index)); updateSelection(); return; }
    if (typing || drawer.classList.contains("open")) return;
    if (["1", "2", "3"].includes(e.key)) return go(["swaps", "activity", "settings"][+e.key - 1]);
    if (page !== "swaps") return;
    if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); removeSwaps([...selected]); }
    else if (e.key === " ") { e.preventDefault(); toggleSelected(); }
    else if (e.key === "Enter" && selected.size === 1) openEditor([...selected][0]);
  });

  // ---------- window focus ----------
  window.addEventListener("blur", () => document.body.classList.add("unfocused"));
  window.addEventListener("focus", () => document.body.classList.remove("unfocused"));
  window.addEventListener("resize", moveIndicator);

  // ---------- first run ----------
  async function firstRunNotice() {
    const ok = await modal({
      title: "Before you start",
      icon: "shield",
      dismissable: false,
      html: `<p>Patchwork changes how ${esc(S.rivals.name)} looks and sounds <b>on your PC only</b>.</p>
        <ul>
          <li>Roblox's rules don't allow third-party tools that change the game, so any asset swapper carries some risk of a ban. Patchwork keeps that risk low (it never touches the game's memory or your account), but nothing can make it zero.</li>
          <li>Keep swaps cosmetic. Swaps that give you an advantage, like see-through walls or easier-to-spot enemies, are what ${esc(S.rivals.name)} bans players for.</li>
          <li>Try it on an alt account first.</li>
        </ul>
        <p>Use Patchwork at your own risk.</p>`,
      buttons: [{ label: "Quit", value: false }, { label: "I understand", value: true, kind: "primary" }],
    });
    if (ok) await act("accept_notice");
    else {
      await fetch("/api/quit", { method: "POST", headers, body: "{}" }).catch(() => {});
      document.body.innerHTML = "";
      window.close();
    }
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
    const wait = reduceMotion ? 0 : Math.max(0, 1900 - (performance.now() - started));
    await Promise.race([sleep(wait), skipped]);
    document.body.classList.remove("booting");
    document.body.classList.add("ready");
    $("#intro").classList.add("leave");
    setTimeout(() => $("#intro").remove(), 1000);
    moveIndicator();
    pump();
    if (!S.notice_accepted) setTimeout(firstRunNotice, reduceMotion ? 0 : 700);
  }
  boot();
})();
