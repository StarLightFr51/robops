"use strict";
/* robops dashboard — talks only to the local robops server (/api/*). Every value
   coming from Roblox or GitHub is inserted with textContent, never as HTML. */

const TOKEN_KEY = "robops-token";
const VIEWS = ["overview", "deployments", "players", "data", "setup"];
const state = {
  token: null, summary: null, env: null, view: "overview",
  live: {}, deployments: null, backups: null, player: null,
  ds: { store: null, key: null, scope: "", prefix: "", next: null, ids: [] },
};

/* ----------------------------------------------------------------- utils */

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key.startsWith("--")) el.style.setProperty(key, value);
    else if (key in el && typeof value !== "string") el[key] = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const $ = (id) => document.getElementById(id);
const put = (el, ...kids) => el.replaceChildren(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false));
const nf = new Intl.NumberFormat();
const fmt = (n) => (typeof n === "number" ? nf.format(n) : "—");
const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

function ago(iso) {
  if (!iso) return "—";
  const seconds = (new Date(iso).getTime() - Date.now()) / 1000;
  const steps = [[60, "second"], [3600, "minute"], [86400, "hour"], [2592000, "day"], [31536000, "month"]];
  let unit = "year", div = 31536000;
  for (let i = 0; i < steps.length; i++) {
    if (Math.abs(seconds) < steps[i][0]) { unit = steps[i][1]; div = i ? steps[i - 1][0] : 1; break; }
  }
  return rtf.format(Math.round(seconds / div), unit);
}
const when = (iso) => (iso ? new Date(iso).toLocaleString() : "");
function size(bytes) {
  if (typeof bytes !== "number") return "—";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0, v = bytes;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${i ? v.toFixed(1) : v} ${units[i]}`;
}
function duration(a, b) {
  if (!a || !b) return null;
  const s = Math.max(0, Math.round((new Date(b) - new Date(a)) / 1000));
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, "0")} s`;
}

/* One colour per environment: the band, the buttons and every confirmation use it. */
const ENV_RULES = [[/prod|live/i, "#DA8541"], [/stag|test|qa|dev/i, "#008F9C"]];
const ENV_EXTRA = ["#6B327C", "#74869D", "#694028", "#27462D"];
function envColor(name, index) {
  for (const [re, color] of ENV_RULES) if (re.test(name)) return color;
  return ENV_EXTRA[index % ENV_EXTRA.length];
}
function luminance(hex) {
  const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
}
const inkOn = (hex) => ((1.05) / (luminance(hex) + 0.05) >= (luminance(hex) + 0.05) / 0.06 ? "#FFFFFF" : "#10161D");

function envTag(name, index, text) {
  const color = envColor(name, index);
  return h("span", { class: "tag env", "--chip": color, "--chip-ink": inkOn(color), text });
}

function currentEnv() {
  return state.summary?.environments.find((e) => e.name === state.env) || null;
}

/* ------------------------------------------------------------------- API */

async function api(path, body) {
  const options = { headers: { "X-Robops-Token": state.token || "" } };
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let resp;
  try {
    resp = await fetch(path, options);
  } catch {
    throw new Error("The robops dashboard is not running anymore: start it again with robops dashboard.");
  }
  let data = {};
  try { data = await resp.json(); } catch { /* empty body */ }
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}
const envPath = (suffix) => `/api/env/${encodeURIComponent(state.env)}/${suffix}`;

function toast(message, bad = false) {
  const el = h("div", { class: `toast${bad ? " bad" : ""}`, text: message });
  $("toasts").append(el);
  setTimeout(() => el.remove(), bad ? 9000 : 5000);
}

/* ----------------------------------------------------------------- modal */

/* Opens a confirmation. For protected environments (production…) the name must be typed. */
function openModal({ title, build, okLabel = "Confirm", danger = false, typed = null, onSubmit }) {
  const dialog = $("modal"), body = $("modal-body"), ok = $("modal-ok");
  $("modal-title").textContent = title;
  put(body);
  const errorLine = h("p", { class: "error-text", hidden: true });
  const extra = build ? build(body) : null;
  let typedInput = null;
  if (typed) {
    typedInput = h("input", { type: "text", autocomplete: "off", spellcheck: "false" });
    body.append(h("label", {}, h("span", {}, "Type ", h("strong", { text: typed }), " to confirm"), typedInput));
  }
  body.append(errorLine);
  ok.textContent = okLabel;
  ok.className = danger ? "btn danger solid" : "btn env";
  const sync = () => { ok.disabled = typedInput ? typedInput.value.trim() !== typed : false; };
  if (typedInput) typedInput.addEventListener("input", sync);
  sync();
  $("modal-form").onsubmit = async (event) => {
    event.preventDefault();
    if (ok.disabled) return;
    ok.disabled = true;
    errorLine.hidden = true;
    const label = ok.textContent;
    ok.textContent = "Working…";
    try {
      const result = await onSubmit(extra);
      dialog.close();
      if (result?.message) toast(result.message);
    } catch (err) {
      errorLine.textContent = err.message;
      errorLine.hidden = false;
      ok.textContent = label;
      ok.disabled = false;
    }
  };
  $("modal-cancel").onclick = () => dialog.close();
  dialog.showModal();
  (typedInput || body.querySelector("input, select, textarea") || ok).focus();
}

function changesBlock(changes) {
  if (!changes.length) return h("p", { class: "muted", text: "No difference with the current data." });
  return h("pre", { class: "changes", text: changes.slice(0, 200).join("\n") + (changes.length > 200 ? `\n… ${changes.length - 200} more` : "") });
}

/* ------------------------------------------------------------- the shell */

function renderRail() {
  const s = state.summary;
  $("version").textContent = `v${s.version}`;
  const list = $("env-list");
  put(list, ...s.environments.map((env, i) => {
    const color = envColor(env.name, i);
    return h("button", {
      type: "button", role: "radio", class: "env-option", "aria-checked": String(env.name === state.env),
      "--chip": color, onclick: () => selectEnv(env.name),
    }, h("span", { class: "env-chip", "aria-hidden": "true" }), env.name,
    h("span", { class: "env-meta", text: env.universe_id ? `#${env.universe_id}` : "invalid" }));
  }));
  for (const btn of $("nav").querySelectorAll("button")) {
    if (btn.dataset.view === state.view) btn.setAttribute("aria-current", "page");
    else btn.removeAttribute("aria-current");
  }
  $("rail-foot").textContent = s.github.repo ? `GitHub: ${s.github.repo}` : "GitHub repository not detected";
}

function renderBand() {
  const env = currentEnv();
  const index = state.summary.environments.findIndex((e) => e.name === state.env);
  const color = envColor(state.env, Math.max(index, 0));
  document.documentElement.style.setProperty("--env", color);
  document.documentElement.style.setProperty("--env-ink", inkOn(color));
  put($("band-env"), h("span", { class: "env-word", text: state.env }));
  const live = state.live[state.env];
  const name = live?.universe?.ok?.displayName || live?.game?.ok?.name;
  const sub = $("band-sub");
  put(sub);
  if (name) sub.append(h("span", { text: name }));
  if (env?.universe_id) {
    sub.append(h("span", { class: "num", text: `Universe ${env.universe_id}` }));
    sub.append(h("a", { href: `https://create.roblox.com/dashboard/creations/experiences/${env.universe_id}/overview`,
      target: "_blank", rel: "noreferrer", text: "Creator Dashboard" }));
    const root = live?.game?.ok?.root_place_id;
    if (root) sub.append(h("a", { href: `https://www.roblox.com/games/${root}`, target: "_blank", rel: "noreferrer", text: "Roblox page" }));
  }
  const actions = $("band-actions");
  put(actions);
  if (env?.universe_id) {
    actions.append(
      h("button", { type: "button", class: "btn ghost", onclick: restartServers }, "Restart servers"),
      h("button", { type: "button", class: "btn env", onclick: deployDialog }, `Deploy ${state.env}`),
    );
  }
}

function selectEnv(name) {
  state.env = name;
  state.player = null;
  state.ds = { store: null, key: null, scope: "", prefix: "", next: null, ids: [] };
  sessionStorage.setItem("robops-env", name);
  render();
}

function selectView(view) {
  state.view = VIEWS.includes(view) ? view : "overview";
  history.replaceState(null, "", `#${state.view}`);
  render();
  $("main").focus({ preventScroll: true });
}

function render() {
  renderRail();
  renderBand();
  const view = $("view");
  put(view);
  ({ overview: viewOverview, deployments: viewDeployments, players: viewPlayers, data: viewData, setup: viewSetup })[state.view](view);
}

function panel(title, note, ...content) {
  return h("section", { class: "panel" },
    h("div", { class: "panel-head" }, h("h2", { text: title }), note ? (note instanceof Node ? note : h("p", { text: note })) : null),
    ...content);
}

function notice(text, bad = false) {
  return h("p", { class: `notice${bad ? " bad" : ""}`, text });
}

/* --------------------------------------------------------------- actions */

function restartServers() {
  const env = currentEnv();
  openModal({
    title: `Restart the servers of ${env.name}?`,
    build: (body) => body.append(h("p", { text: "Servers running an older version than the latest published one are shut down and replaced. Players in them are moved to new servers." })),
    okLabel: "Restart servers", typed: env.protected ? env.name : null,
    onSubmit: () => api(envPath("restart"), { confirm: env.name }),
  });
}

function deployDialog() {
  const env = currentEnv();
  openModal({
    title: `Deploy ${env.name}`,
    build: (body) => {
      const fields = {
        ref: h("input", { type: "text", placeholder: "default branch", autocomplete: "off" }),
        version: h("select", {}, h("option", { value: "Published", text: "Published (players get it)" }),
          h("option", { value: "Saved", text: "Saved (not played)" })),
        restart: h("select", {}, h("option", { value: "per config", text: `As configured (${env.restart_servers ? "restart" : "no restart"})` }),
          h("option", { value: "yes", text: "Restart the servers" }), h("option", { value: "no", text: "Keep the servers running" })),
        datastores: h("input", { type: "checkbox" }),
      };
      body.append(
        h("p", { text: "Launches the Roblox Deploy workflow on GitHub: build, checks, then publish. Follow it in Deployments." }),
        h("label", {}, "Branch", fields.ref),
        h("label", {}, "Version type", fields.version),
        h("label", {}, "Servers", fields.restart),
        h("label", { class: "check" }, fields.datastores, h("span", { text: "Also overwrite the configured DataStores" })),
      );
      return fields;
    },
    okLabel: `Deploy ${env.name}`, typed: env.protected ? env.name : null,
    onSubmit: async (f) => {
      const result = await api(envPath("deploy"), {
        confirm: env.name, ref: f.ref.value.trim() || null, version_type: f.version.value,
        restart: f.restart.value === "per config" ? null : f.restart.value, with_datastores: f.datastores.checked,
      });
      state.deployments = null;
      setTimeout(() => { if (state.view === "deployments" || state.view === "overview") refresh(); }, 6000);
      return result;
    },
  });
}

/* -------------------------------------------------------------- overview */

async function loadLive(force = false) {
  if (!force && state.live[state.env]) return state.live[state.env];
  const env = state.env;
  const data = await api(envPath("live"));
  state.live[env] = data;
  return data;
}

function viewOverview(view) {
  const env = currentEnv();
  if (!env?.universe_id) {
    view.append(notice("This environment has a configuration problem: see Setup.", true));
    return;
  }
  const draw = (live) => {
    renderBand();
    put(view, statsPanel(live), serversPanel(live), placesPanel(live), lastDeployPanel(), universePanel(live));
  };
  if (state.live[state.env] && state.deployments) { draw(state.live[state.env]); return; }
  view.append(h("p", { class: "loading", text: "Loading the live game…" }));
  Promise.all([loadLive(), loadDeployments()]).then(([live]) => {
    if (state.view === "overview" && live.environment === state.env) draw(live);
  }).catch((err) => put(view, notice(err.message, true)));
}

function statsPanel(live) {
  if (live.game.error) return panel("Right now", null, notice(live.game.error, true));
  const g = live.game.ok;
  const votes = (g.up_votes ?? 0) + (g.down_votes ?? 0);
  const ratio = votes ? Math.round((100 * g.up_votes) / votes) : null;
  const item = (label, value, small) => h("div", {}, h("dt", { text: label }), h("dd", {}, value, small ? h("small", { text: small }) : null));
  const note = "Public stats from roblox.com, refreshed every 30 seconds."
    + (live.places.length > 1 ? " Roblox only counts the players of the start place here." : "");
  return panel("Right now", note,
    h("dl", { class: "stats" },
      item("Playing", fmt(g.playing)),
      item("Visits", fmt(g.visits)),
      item("Favorites", fmt(g.favorites)),
      item("Likes", ratio === null ? "—" : `${ratio}%`, votes ? `of ${fmt(votes)}` : null),
      item("Players per server", fmt(g.max_players)),
    ));
}

function serversPanel(live) {
  const blocks = [];
  let servers = 0, players = 0;
  for (const place of live.places) {
    if (place.servers.error) { blocks.push(notice(`${place.name}: ${place.servers.error}`, true)); continue; }
    const list = place.servers.ok;
    servers += list.length;
    players += list.reduce((n, s) => n + (s.playing || 0), 0);
    const bricks = list.map((s) => {
      const share = s.max_players ? Math.min(100, Math.round((100 * (s.playing || 0)) / s.max_players)) : 0;
      return h("span", {
        class: `brick${share >= 100 ? " full" : ""}`, "--fill": `${share}%`, role: "img",
        "aria-label": `${s.playing ?? 0} of ${s.max_players ?? "?"} players`,
        title: `${s.playing ?? 0}/${s.max_players ?? "?"} players, ${s.fps ? Math.round(s.fps) : "?"} fps, ping ${s.ping ?? "?"} ms`,
      });
    });
    blocks.push(h("div", {}, live.places.length > 1 ? h("h3", { class: "servers-legend", text: place.name }) : null,
      bricks.length ? h("div", { class: "servers" }, bricks) : h("p", { class: "empty", text: "No public server running." })));
  }
  return panel("Public servers", `${fmt(servers)} server${servers === 1 ? "" : "s"}, ${fmt(players)} player${players === 1 ? "" : "s"}. Each brick fills up with its players; private servers are not listed.`, ...blocks);
}

function placesPanel(live) {
  const rows = live.places.map((p) => {
    const info = p.info.ok;
    return h("tr", {},
      h("td", {}, h("strong", { text: p.name })),
      h("td", { class: "num", text: p.place_id }),
      h("td", { text: info ? info.displayName || "—" : "" }, p.info.error ? h("span", { class: "error-text", text: p.info.error.split("\n")[0] }) : null),
      h("td", { class: "num right", text: info?.serverSize ?? "—" }),
      h("td", { title: when(info?.updateTime), text: info ? ago(info.updateTime) : "—" }));
  });
  return panel("Places", "Name and settings as Roblox has them now. A place's update time changes on every publish.",
    h("table", {}, h("thead", {}, h("tr", {}, h("th", { text: "Place" }), h("th", { text: "Place ID" }), h("th", { text: "Name on Roblox" }),
      h("th", { class: "right", text: "Server size" }), h("th", { text: "Last updated" }))), h("tbody", {}, rows)));
}

function lastDeployPanel() {
  if (!state.deployments || state.deployments.error) {
    return panel("Last deployment", null, h("p", { class: "empty", text: state.deployments?.error || "Deployments are not available: see Setup." }));
  }
  const run = state.deployments.runs.find((r) => r.environment === state.env);
  if (!run) return panel("Last deployment", null, h("p", { class: "empty", text: `No deployment of ${state.env} in the latest runs.` }));
  return panel("Last deployment", null, h("ul", { class: "timeline" }, runItem(run)));
}

function universePanel(live) {
  if (live.universe.error) return panel("Experience settings", null, notice(live.universe.error, true));
  const u = live.universe.ok;
  const devices = ["desktop", "mobile", "tablet", "console", "vr"].filter((d) => u[`${d}Enabled`]);
  const pretty = (v) => (v ? String(v).replace(/^(VISIBILITY|AGE_RATING)_/, "").replaceAll("_", " ").toLowerCase() : "—");
  return panel("Experience settings", "From Open Cloud, with your API key.",
    h("div", { class: "panel-body" }, h("dl", { class: "kv" },
      h("dt", { text: "Visibility" }), h("dd", { text: pretty(u.visibility) }),
      h("dt", { text: "Age rating" }), h("dd", { text: pretty(u.ageRating) }),
      h("dt", { text: "Devices" }), h("dd", { text: devices.join(", ") || "—" }),
      h("dt", { text: "Created" }), h("dd", { text: when(u.createTime) || "—" }),
      h("dt", { text: "Last change" }), h("dd", { text: when(u.updateTime) || "—" }))));
}

/* ----------------------------------------------------------- deployments */

async function loadDeployments(force = false) {
  if (state.deployments && !force) return state.deployments;
  try {
    state.deployments = await api("/api/deployments");
  } catch (err) {
    state.deployments = { error: err.message };
  }
  state.deploymentsAt = Date.now();
  return state.deployments;
}

function runStatus(run) {
  if (run.status !== "completed") return h("span", { class: "status busy", text: run.status === "queued" ? "Queued" : "Running" });
  const map = { success: ["ok", "Succeeded"], failure: ["bad", "Failed"], cancelled: ["idle", "Cancelled"], skipped: ["idle", "Skipped"] };
  const [cls, label] = map[run.conclusion] || ["idle", run.conclusion || "Done"];
  return h("span", { class: `status ${cls}`, text: label });
}

function runItem(run) {
  const index = state.summary.environments.findIndex((e) => e.name === run.environment);
  const what = run.kind === "publish" ? `Publish ${run.environment}` : run.kind === "verify" ? "Build and checks" : "Workflow run";
  return h("li", {},
    h("span", { class: "when", title: when(run.created_at), text: ago(run.created_at) }),
    h("div", { class: "what" },
      h("div", { class: "row" }, runStatus(run), h("strong", { text: what }),
        run.environment ? envTag(run.environment, Math.max(index, 0), run.environment) : null),
      h("div", { class: "detail" },
        h("span", { text: run.title || "" }),
        h("span", { class: "num", text: `${run.branch || "?"} @ ${run.sha || "?"}` }),
        h("span", { text: run.actor ? `by ${run.actor}` : "" }),
        h("span", { text: run.event === "workflow_dispatch" ? "launched by hand" : run.event === "push" ? "on push" : run.event || "" }),
        run.status === "completed" ? h("span", { text: duration(run.created_at, run.updated_at) || "" }) : null)),
    h("a", { href: run.url, target: "_blank", rel: "noreferrer", text: `Run #${run.number}` }));
}

function viewDeployments(view) {
  const fresh = state.deployments && Date.now() - (state.deploymentsAt || 0) < 15000;
  if (!fresh) view.append(h("p", { class: "loading", text: "Loading the runs from GitHub…" }));
  (fresh ? Promise.resolve(state.deployments) : loadDeployments(true)).then((data) => {
    if (state.view !== "deployments") return;
    if (data.error) {
      put(view, panel("Deployments", null, h("div", { class: "panel-body stack" }, notice(data.error, true),
        h("p", {}, "The Setup view checks the GitHub connection step by step. ",
          h("button", { type: "button", class: "btn ghost small", onclick: () => selectView("setup") }, "Open Setup")))));
      return;
    }
    const busy = data.runs.some((r) => r.status !== "completed");
    const source = data.workflow ? `${data.workflow.name} (${data.workflow.file})` : "the deploy workflow";
    put(view, panel("Deployment runs", `Latest runs of ${source} on ${data.repo}${busy ? ", refreshing while a run is in progress" : ""}.`,
      data.runs.length ? h("ul", { class: "timeline" }, data.runs.map(runItem)) : h("p", { class: "empty", text: "No run yet. Use Deploy to launch the first one." })));
  });
}

/* --------------------------------------------------------------- players */

function viewPlayers(view) {
  const input = h("input", { type: "search", placeholder: "UserId or username", "aria-label": "UserId or username",
    value: state.player?.query || "", autocomplete: "off", spellcheck: "false" });
  const result = h("div", { class: "stack" });
  const search = async (event) => {
    event?.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    put(result, h("p", { class: "loading", text: "Looking up the player…" }));
    try {
      const data = await api(`${envPath("player")}?q=${encodeURIComponent(q)}`);
      state.player = { query: q, data };
      put(result, playerResult(data));
    } catch (err) {
      put(result, notice(err.message, true));
    }
  };
  const env = currentEnv();
  view.append(panel("Find a player", env?.players ? `Profiles in DataStore ${env.players.datastore}, keys ${env.players.key_prefix}<UserId>.` : null,
    h("div", { class: "panel-body" }, h("form", { class: "search", onsubmit: search }, input, h("button", { class: "btn", type: "submit" }, "Search")))),
  result, backupsPanel());
  if (state.player) result.append(playerResult(state.player.data));
}

function playerResult(p) {
  const user = p.user.ok;
  const head = h("div", { class: "person" },
    p.headshot ? h("img", { src: p.headshot, alt: "" }) : h("span", { class: "avatar", "aria-hidden": "true" }),
    h("div", {}, h("h3", { text: user?.display_name || user?.name || `User ${p.user_id}` }),
      h("p", { class: "muted", text: user ? `@${user.name}, UserId ${p.user_id}` : `UserId ${p.user_id}` }),
      user?.banned ? h("p", { class: "error-text", text: "Banned from Roblox" }) : null));
  if (!p.exists) {
    return panel("Profile", null, h("div", { class: "panel-body stack" }, head,
      h("p", { text: `No saved data under ${p.key} in ${p.datastore}. The player never joined, or their profile was deleted.` }),
      restoreBox(p)));
  }
  if (!p.is_profile) {
    return panel("Profile", null, h("div", { class: "panel-body stack" }, head,
      notice(`${p.key} exists but is not a ProfileStore profile (no Data / MetaData): edit it from DataStores.`, true),
      h("pre", { class: "changes", text: JSON.stringify(p.raw, null, 2) })));
  }
  const status = { offline: ["ok", "Offline"], online: ["bad", "Online"], stale: ["busy", "Stale lock"] }[p.state.status];
  const editor = h("textarea", { rows: 22, spellcheck: "false", "aria-label": "Player data (JSON)" });
  editor.value = JSON.stringify(p.data, null, 2);
  const parseError = h("p", { class: "error-text", hidden: true });
  const parse = () => {
    try { const v = JSON.parse(editor.value); editor.classList.remove("invalid"); parseError.hidden = true; return v; }
    catch (err) { editor.classList.add("invalid"); parseError.textContent = `Invalid JSON: ${err.message}`; parseError.hidden = false; return undefined; }
  };
  editor.addEventListener("input", parse);
  const force = h("input", { type: "checkbox" });
  let saveButton = null;
  force.addEventListener("change", () => { if (saveButton) saveButton.disabled = !p.state.writable && !force.checked; });
  const save = async () => {
    const data = parse();
    if (data === undefined) return;
    try {
      const preview = await api(envPath("player/edit"), { user_id: p.user_id, data, preview: true, force: force.checked });
      if (!preview.changes.length) { toast("No difference with the saved data."); return; }
      const env = currentEnv();
      openModal({
        title: `Save player ${p.user_id} in ${env.name}?`,
        build: (body) => body.append(h("p", { text: "These fields of Data change. A copy of the current profile is saved in .robops/safety first." }), changesBlock(preview.changes)),
        okLabel: "Save player", typed: env.protected ? env.name : null,
        onSubmit: async () => {
          const r = await api(envPath("player/edit"), { user_id: p.user_id, data, force: force.checked, confirm: env.name });
          refreshPlayer();
          return r;
        },
      });
    } catch (err) { toast(err.message, true); }
  };
  const meta = p.meta || {};
  return h("div", { class: "split" },
    panel("Data", "Edit the JSON, then save: you see the changed fields before anything is written.",
      h("div", { class: "editor" }, editor, parseError,
        h("div", { class: "row" },
          h("button", { type: "button", class: "btn ghost", onclick: () => { const v = parse(); if (v !== undefined) editor.value = JSON.stringify(v, null, 2); } }, "Format"),
          h("button", { type: "button", class: "btn ghost", onclick: () => { editor.value = JSON.stringify(p.data, null, 2); parse(); } }, "Undo changes"),
          (saveButton = h("button", { type: "button", class: "btn env", onclick: save, disabled: !p.state.writable }, "Save player")),
        ),
        !p.state.writable ? h("label", { class: "check" }, force, h("span", { text: "Write even though the player looks online (their server may overwrite it)" })) : null)),
    h("div", { class: "stack" },
      panel("Profile", null, h("div", { class: "panel-body stack" }, head,
        h("p", {}, h("span", { class: `status ${status[0]}`, text: status[1] })),
        h("p", { class: "muted", text: p.state.detail }),
        h("dl", { class: "kv" },
          h("dt", { text: "Key" }), h("dd", { class: "code", text: p.key }),
          h("dt", { text: "Sessions" }), h("dd", { class: "num", text: fmt(meta.SessionLoadCount) }),
          h("dt", { text: "Profile created" }), h("dd", { text: meta.ProfileCreateTime ? when(new Date(meta.ProfileCreateTime * 1000).toISOString()) : "—" }),
          h("dt", { text: "Account created" }), h("dd", { text: when(user?.created) || "—" })))),
      panel("Restore from a backup", null, h("div", { class: "panel-body" }, restoreBox(p))),
      panel("Delete the profile", "For a data erasure request. Roblox purges it within 30 days.",
        h("div", { class: "panel-body" }, h("button", { type: "button", class: "btn danger", onclick: () => deletePlayer(p) }, "Delete profile")))));
}

function refreshPlayer() {
  if (!state.player) return;
  api(`${envPath("player")}?q=${encodeURIComponent(state.player.data.user_id)}`)
    .then((data) => { state.player.data = data; if (state.view === "players") render(); })
    .catch((err) => toast(err.message, true));
}

function restoreBox(p) {
  const box = h("div", { class: "stack" }, h("p", { class: "loading", text: "Loading the backups…" }));
  loadBackups().then((b) => {
    if (!b.local.length) {
      put(box, h("p", { class: "muted", text: "No local backup. Download one from GitHub (Backups below), unzip it into the backups folder of your project, then come back." }));
      return;
    }
    const select = h("select", { "aria-label": "Backup file" }, b.local.map((f) => h("option", { value: f.id, text: `${f.name} (${ago(f.modified)})` })));
    const allowOther = h("input", { type: "checkbox" });
    const recreate = h("input", { type: "checkbox" });
    const go = async () => {
      const base = { user_id: p.user_id, backup: select.value, allow_other_universe: allowOther.checked, recreate_missing: recreate.checked };
      try {
        const preview = await api(envPath("player/restore"), { ...base, preview: true });
        const env = currentEnv();
        openModal({
          title: `Restore player ${p.user_id} in ${env.name}?`,
          build: (body) => body.append(h("p", { text: `Their Data goes back to its state in ${select.selectedOptions[0].textContent}. Progress made since is lost; a copy of the current profile is kept in .robops/safety.` }), changesBlock(preview.changes)),
          okLabel: "Restore player", typed: env.protected ? env.name : null,
          onSubmit: async () => { const r = await api(envPath("player/restore"), { ...base, confirm: env.name }); refreshPlayer(); return r; },
        });
      } catch (err) { toast(err.message, true); }
    };
    put(box, select,
      h("label", { class: "check" }, allowOther, h("span", { text: "The backup comes from another environment (e.g. staging)" })),
      p.exists ? null : h("label", { class: "check" }, recreate, h("span", { text: "Recreate the profile: this player was not deleted at their request" })),
      h("div", {}, h("button", { type: "button", class: "btn ghost", onclick: go }, "Preview the restore")));
  }).catch((err) => put(box, notice(err.message, true)));
  return box;
}

function deletePlayer(p) {
  const env = currentEnv();
  openModal({
    title: `Delete the profile of ${p.user_id}?`,
    danger: true, okLabel: "Delete profile", typed: env.name,
    build: (body) => {
      const confirmUser = h("input", { type: "text", autocomplete: "off" });
      body.append(h("p", { text: `All saved data of this player in ${env.name} is deleted. A local copy is kept in .robops/safety.` }),
        h("label", {}, h("span", {}, "Type the UserId ", h("strong", { text: p.user_id })), confirmUser));
      return confirmUser;
    },
    onSubmit: async (confirmUser) => {
      const r = await api(envPath("player/delete"), { user_id: p.user_id, confirm: env.name, confirm_user: confirmUser.value.trim() });
      refreshPlayer();
      return r;
    },
  });
}

async function loadBackups(force = false) {
  if (state.backups && !force) return state.backups;
  state.backups = await api("/api/backups");
  return state.backups;
}

function backupsPanel() {
  const body = h("div", {}, h("p", { class: "loading", text: "Loading the backups…" }));
  const runNow = () => openModal({
    title: "Back up all players now?",
    build: (b) => b.append(h("p", { text: "Launches the Player Backup workflow on GitHub. The file appears in the list when the run ends (a few minutes)." })),
    okLabel: "Back up now",
    onSubmit: () => api("/api/backups/run", {}),
  });
  loadBackups(true).then((b) => {
    const local = b.local.length
      ? h("table", {}, h("thead", {}, h("tr", {}, h("th", { text: "On this PC" }), h("th", { class: "right", text: "Size" }), h("th", { text: "Saved" }))),
        h("tbody", {}, b.local.map((f) => h("tr", {}, h("td", { class: "code", text: f.id }), h("td", { class: "right num", text: size(f.size) }), h("td", { title: when(f.modified), text: ago(f.modified) })))))
      : h("p", { class: "empty", text: "No backup on this PC (folders backups/ and robops-backup/ of the project)." });
    let remote;
    if (b.github.error) remote = h("p", { class: "empty", text: `GitHub backups unavailable: ${b.github.error}` });
    else if (!b.github.ok.length) remote = h("p", { class: "empty", text: "No backup on GitHub yet: the workflow runs every night, or use Back up now." });
    else remote = h("table", {}, h("thead", {}, h("tr", {}, h("th", { text: "On GitHub" }), h("th", { class: "right", text: "Size" }), h("th", { text: "Saved" }), h("th", { text: "Deleted" }))),
      h("tbody", {}, b.github.ok.map((a) => h("tr", {},
        h("td", {}, h("a", { href: a.run_url, target: "_blank", rel: "noreferrer", text: a.name })),
        h("td", { class: "right num", text: size(a.size) }),
        h("td", { title: when(a.created_at), text: ago(a.created_at) }),
        h("td", { title: when(a.expires_at), text: a.expired ? "expired" : ago(a.expires_at) })))));
    put(body, local, remote, h("p", { class: "servers-legend", text: `${b.safety_copies} safety cop${b.safety_copies === 1 ? "y" : "ies"} in .robops/safety (one per change made here or with the CLI).` }));
  }).catch((err) => put(body, notice(err.message, true)));
  return panel("Backups", h("button", { type: "button", class: "btn ghost small", onclick: runNow }, "Back up now"), body);
}

/* ------------------------------------------------------------ DataStores */

function viewData(view) {
  const env = currentEnv();
  if (!env?.universe_id) { view.append(notice("This environment has a configuration problem: see Setup.", true)); return; }
  const stores = h("ul", { class: "pick" }, h("li", { class: "loading", text: "Loading…" }));
  const keys = h("ul", { class: "pick" });
  const entry = h("div", { class: "editor" }, h("p", { class: "muted", text: "Pick a DataStore, then a key." }));
  const storeFilter = h("input", { type: "search", placeholder: "Filter", "aria-label": "Filter DataStores" });
  const prefix = h("input", { type: "search", placeholder: "Key prefix", "aria-label": "Key prefix", value: state.ds.prefix });
  const scope = h("input", { type: "text", placeholder: "Scope (optional)", "aria-label": "Scope", value: state.ds.scope });
  const more = h("button", { type: "button", class: "btn ghost small", hidden: true }, "Load more");
  let allStores = [];

  const drawStores = () => {
    const f = storeFilter.value.trim().toLowerCase();
    const shown = allStores.filter((s) => s.toLowerCase().includes(f));
    put(stores, ...(shown.length ? shown.map((s) => h("li", {}, h("button", { type: "button", "aria-current": String(s === state.ds.store), onclick: () => pickStore(s) }, s)))
      : [h("li", { class: "empty", text: allStores.length ? "No match." : "No DataStore yet." })]));
  };
  const drawKeys = () => {
    put(keys, ...state.ds.ids.map((k) => h("li", {}, h("button", { type: "button", class: "code", "aria-current": String(k === state.ds.key), onclick: () => pickKey(k) }, k))));
    if (!state.ds.ids.length) keys.append(h("li", { class: "empty", text: "No key." }));
    more.hidden = !state.ds.next;
  };
  const loadKeys = async (append) => {
    state.ds.scope = scope.value.trim();
    state.ds.prefix = prefix.value.trim();
    const params = new URLSearchParams({ datastore: state.ds.store });
    if (state.ds.scope) params.set("scope", state.ds.scope);
    if (state.ds.prefix) params.set("prefix", state.ds.prefix);
    if (append && state.ds.next) params.set("cursor", state.ds.next);
    try {
      const r = await api(`${envPath("entries")}?${params}`);
      state.ds.ids = append ? state.ds.ids.concat(r.ids) : r.ids;
      state.ds.next = r.next;
      drawKeys();
    } catch (err) { put(keys, h("li", { class: "error-text", text: err.message })); }
  };
  const pickStore = (s) => {
    state.ds.store = s; state.ds.key = null; state.ds.next = null; state.ds.ids = [];
    drawStores();
    put(keys, h("li", { class: "loading", text: "Loading…" }));
    put(entry, h("p", { class: "muted", text: "Pick a key, or create one." }), newKeyButton());
    loadKeys(false);
  };
  const newKeyButton = () => h("button", { type: "button", class: "btn ghost small", onclick: () => drawEntry(null, null) }, "New key");
  const pickKey = async (k) => {
    state.ds.key = k;
    drawKeys();
    put(entry, h("p", { class: "loading", text: "Loading…" }));
    const params = new URLSearchParams({ datastore: state.ds.store, key: k });
    if (state.ds.scope) params.set("scope", state.ds.scope);
    try { drawEntry(k, await api(`${envPath("entry")}?${params}`)); }
    catch (err) { put(entry, notice(err.message, true)); }
  };
  const drawEntry = (key, data) => {
    const name = h("input", { type: "text", value: key || "", placeholder: "Key name", "aria-label": "Key", readOnly: !!key, class: "code" });
    const editor = h("textarea", { rows: 18, spellcheck: "false", "aria-label": "Value (JSON)" });
    editor.value = data ? JSON.stringify(data.value, null, 2) : "{}";
    const parseError = h("p", { class: "error-text", hidden: true });
    const parse = () => { try { const v = JSON.parse(editor.value); editor.classList.remove("invalid"); parseError.hidden = true; return v; } catch (err) { editor.classList.add("invalid"); parseError.textContent = `Invalid JSON: ${err.message}`; parseError.hidden = false; return undefined; } };
    editor.addEventListener("input", parse);
    const env = currentEnv();
    const save = () => {
      const value = parse(), k = name.value.trim();
      if (value === undefined || !k) { if (!k) toast("Give the key a name.", true); return; }
      openModal({
        title: `Save ${state.ds.store}/${k} in ${env.name}?`,
        build: (body) => body.append(h("p", { text: key ? "The current value is replaced (it stays in the key's version history on Roblox). Its users and attributes are kept." : "A new key is created with this value." })),
        okLabel: "Save key", typed: env.protected ? env.name : null,
        onSubmit: async () => { const r = await api(envPath("entry/set"), { datastore: state.ds.store, key: k, scope: state.ds.scope || null, value, confirm: env.name }); if (!key) loadKeys(false); return r; },
      });
    };
    const remove = () => openModal({
      title: `Delete ${state.ds.store}/${key}?`, danger: true, okLabel: "Delete key", typed: env.name,
      build: (body) => body.append(h("p", { text: "The key is marked as deleted; Roblox purges it within 30 days." })),
      onSubmit: async () => { const r = await api(envPath("entry/delete"), { datastore: state.ds.store, key, scope: state.ds.scope || null, confirm: env.name }); state.ds.key = null; loadKeys(false); put(entry, newKeyButton()); return r; },
    });
    put(entry, name,
      data ? h("dl", { class: "kv" },
        h("dt", { text: "Revision" }), h("dd", { class: "code", text: data.revisionId || "—" }),
        h("dt", { text: "Updated" }), h("dd", { text: when(data.revisionCreateTime || data.updateTime) || "—" }),
        h("dt", { text: "Users" }), h("dd", { text: (data.users || []).join(", ") || "—" }),
        h("dt", { text: "Attributes" }), h("dd", { class: "code", text: data.attributes ? JSON.stringify(data.attributes) : "—" })) : null,
      editor, parseError,
      h("div", { class: "row" }, h("button", { type: "button", class: "btn env", onclick: save }, "Save key"),
        key ? h("button", { type: "button", class: "btn danger", onclick: remove }, "Delete key") : null));
  };

  storeFilter.addEventListener("input", drawStores);
  more.addEventListener("click", () => loadKeys(true));
  const reload = (event) => { event.preventDefault(); if (state.ds.store) { put(keys, h("li", { class: "loading", text: "Loading…" })); loadKeys(false); } };

  const snapshot = () => openModal({
    title: `Snapshot the DataStores of ${env.name}?`,
    build: (body) => body.append(h("p", { text: "Guarantees a versioned copy of all the data, kept 30 days. One snapshot per UTC day; later ones have no effect." })),
    okLabel: "Take snapshot", typed: env.protected ? env.name : null,
    onSubmit: () => api(envPath("snapshot"), { confirm: env.name }),
  });

  view.append(panel("DataStores", h("button", { type: "button", class: "btn ghost small", onclick: snapshot }, "Take snapshot"),
    h("div", { class: "browser" },
      h("section", { "aria-label": "DataStores" }, h("h3", { text: "Stores" }), h("div", { class: "tools" }, storeFilter), stores),
      h("section", { "aria-label": "Keys" }, h("h3", { text: "Keys" }),
        h("form", { class: "tools", onsubmit: reload }, prefix, scope), keys, h("div", { class: "tools" }, more)),
      h("section", { "aria-label": "Value" }, h("h3", { text: "Value" }), entry))));

  api(envPath("datastores")).then((r) => {
    allStores = r.stores;
    drawStores();
    if (state.ds.store) pickStore(state.ds.store);
  }).catch((err) => put(stores, h("li", { class: "error-text", text: err.message })));
}

/* ----------------------------------------------------------------- setup */

function viewSetup(view) {
  const s = state.summary;
  const levelClass = { ok: "ok", info: "idle", warn: "busy", error: "bad" };
  const levelText = { ok: "OK", info: "Info", warn: "Warning", error: "Error" };
  for (const [i, env] of s.environments.entries()) {
    view.append(panel(`Environment ${env.name}`,
      envTag(env.name, i, env.protected ? "asks to type its name" : "one-click confirm"),
      h("ul", { class: "findings" }, env.findings.map((f) => h("li", {},
        h("span", { class: `lvl status ${levelClass[f.level]}`, text: levelText[f.level] }), h("span", { class: "msg", text: f.message.trim() }))))));
  }
  const ghSteps = h("ul", { class: "findings" }, h("li", { class: "loading", text: "Checking the GitHub connection…" }));
  const checkGitHub = () => {
    put(ghSteps, h("li", { class: "loading", text: "Checking the GitHub connection…" }));
    api("/api/github").then((r) => put(ghSteps, r.steps.map((f) => h("li", {},
      h("span", { class: `lvl status ${levelClass[f.level]}`, text: levelText[f.level] }), h("span", { class: "msg", text: f.message })))))
      .catch((err) => put(ghSteps, h("li", { class: "error-text", text: err.message })));
  };
  view.append(panel(`GitHub${s.github.repo ? ` (${s.github.repo})` : ""}`,
    h("button", { type: "button", class: "btn ghost small", onclick: checkGitHub }, "Check again"), ghSteps));
  checkGitHub();
  view.append(panel("This dashboard", null, h("div", { class: "panel-body" }, h("dl", { class: "kv" },
    h("dt", { text: "Version" }), h("dd", { text: `robops ${s.version}` }),
    h("dt", { text: "Configuration" }), h("dd", { class: "code", text: s.config }),
    h("dt", { text: "Action log" }), h("dd", {}, h("span", { class: "code", text: ".robops/audit.log" }), " lists every action made here"),
    h("dt", { text: "Edit the config" }), h("dd", { text: "Changes to robops.toml are picked up on the next refresh; API keys need a restart of robops dashboard." }),
  ))));
}

/* ------------------------------------------------------------------ boot */

/* Live views refresh every 30 s, every 10 s while a deployment is running. */
let lastRefresh = Date.now();
async function refresh() {
  if ($("modal").open || document.hidden || !["overview", "deployments"].includes(state.view)) return;
  const busy = state.deployments?.runs?.some((r) => r.status !== "completed");
  if (!busy && Date.now() - lastRefresh < 29000) return;
  lastRefresh = Date.now();
  try {
    state.summary = await api("/api/summary");
    if (state.view === "overview") await loadLive(true);
    await loadDeployments(true);
    if (["overview", "deployments"].includes(state.view)) render();
  } catch (err) { toast(err.message, true); }
}

async function boot() {
  const match = location.hash.match(/token=([\w-]+)/);
  if (match) {
    sessionStorage.setItem(TOKEN_KEY, match[1]);
    history.replaceState(null, "", location.pathname);
  }
  state.token = sessionStorage.getItem(TOKEN_KEY);
  for (const btn of $("nav").querySelectorAll("button")) btn.addEventListener("click", () => selectView(btn.dataset.view));
  try {
    state.summary = await api("/api/summary");
  } catch (err) {
    $("band-env").textContent = "Not connected";
    put($("view"), notice(`${err.message} Open the link printed in the terminal by robops dashboard.`, true));
    return;
  }
  const names = state.summary.environments.map((e) => e.name);
  const saved = sessionStorage.getItem("robops-env");
  state.env = names.includes(saved) ? saved : (names.find((n) => /prod|live/i.test(n)) || names[0]);
  const fromHash = location.hash.slice(1);
  state.view = VIEWS.includes(fromHash) ? fromHash : "overview";
  render();
  setInterval(refresh, 10000);
}

document.addEventListener("DOMContentLoaded", boot);
