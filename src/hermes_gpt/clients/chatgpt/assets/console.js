const el = id => document.getElementById(id);
let configuration = null;
let profiles = [];
let busy = false;
let uncertainSubmission = false;

function applyTheme(context) {
  if (context?.theme === "dark" || context?.theme === "light") {
    document.documentElement.dataset.theme = context.theme;
  }
}
function showError(error) {
  el("error").textContent = error.message + (error.traceId ? ` Request: ${error.traceId}` : "");
  el("error").hidden = false;
}
function clearError() { el("error").hidden = true; }
function setOptions(select, rows, emptyLabel = null) {
  const selected = select.value;
  select.replaceChildren();
  if (emptyLabel) select.add(new Option(emptyLabel, ""));
  for (const row of rows) select.add(new Option(row.label, row.value));
  if ([...select.options].some(option => option.value === selected)) select.value = selected;
}
function applyToolResult(result) {
  const data = decodeResult(result);
  if (data.console_version !== "1") return;
  configuration = data;
  profiles = data.profiles?.profiles || [];
  setOptions(el("profile"), profiles.map(row => ({ label: row.profile, value: row.profile })));
  const cronProfiles = data.capabilities?.capabilities?.cron_read?.profiles || [];
  // An administrator can authorize a wildcard; it is not a profile name.
  const namedProfiles = cronProfiles.includes("*") ? ["default", ...profiles.map(row => row.profile)] : cronProfiles;
  setOptions(el("cron-profile"), [...new Set(namedProfiles)].map(name => ({ label: name, value: name })));
  const efforts = data.profiles?.reasoning_efforts || [];
  setOptions(el("effort"), efforts.map(name => ({ label: name, value: name })), "Use profile default");
  el("controls").disabled = busy || Boolean(currentJob) || uncertainSubmission || profiles.length === 0;
  el("load-cron").disabled = namedProfiles.length === 0;
  el("load-diagnostics").disabled = false;
  el("connection").textContent = profiles.length ? "Connected to Hermes" : "Connected. No profiles are authorized for work.";
  updateDefaults();
  applySettings(data.connection_settings);
}
function updateDefaults() {
  const profile = profiles.find(row => row.profile === el("profile").value);
  el("defaults").textContent = profile ? `Profile defaults: ${profile.default_model || "configured model"}, ${profile.configured_reasoning_effort || "configured effort"}.` : "";
}
async function loadConversations() {
  updateDefaults();
  setOptions(el("conversation"), [], "New conversation");
  if (!configuration?.capabilities?.capabilities?.history?.enabled || !el("profile").value) return;
  const result = await callTool("hermes_session_list", { profile: el("profile").value, limit: 20 });
  const rows = (result.sessions || []).map(row => ({ value: row.id || row.session_id,
    label: row.title || `${row.id || row.session_id} · ${row.message_count || 0} messages` }));
  setOptions(el("conversation"), rows.filter(row => row.value), "New conversation");
}
async function refreshPanel() {
  clearError();
  applyToolResult({ structuredContent: await callTool("hermes_console", {}) });
  await loadConversations();
}
function listRows(target, rows, describe) {
  target.replaceChildren();
  for (const row of rows) {
    const li = document.createElement("li");
    // Tool results and page content are untrusted. Never render them as HTML.
    li.textContent = describe(row);
    target.appendChild(li);
  }
}
async function perform(action) {
  clearError();
  try { await action(); } catch (error) { showError(error); }
}
el("refresh").addEventListener("click", () => perform(refreshPanel));
el("profile").addEventListener("change", () => perform(loadConversations));
el("load-cron").addEventListener("click", () => perform(async () => {
  const result = await callTool("hermes_cron_list", { profile: el("cron-profile").value, include_disabled: true });
  listRows(el("cron-list"), result.jobs || [], row => `${row.name} · ${row.schedule} · ${row.state} · last run: ${row.last_status || "none"}`);
  el("cron-empty").textContent = result.count ? "" : "No scheduled jobs in this profile.";
}));
el("load-diagnostics").addEventListener("click", () => perform(async () => {
  const result = await callTool("hermes_request_diagnostics", { limit: 12 });
  listRows(el("diagnostics"), result.records || [], row => `${row.tool} · ${row.outcome || row.phase}${row.error_code ? ` · ${row.error_code}` : ""} · ${row.request_id}`);
}));
el("ask-form").addEventListener("submit", event => {
  event.preventDefault();
  if (busy || currentJob || uncertainSubmission || !el("prompt").value.trim()) return;
  perform(async () => {
    busy = true;
    el("controls").disabled = true;
    el("send").textContent = "Sending…";
    try {
      const args = { prompt: el("prompt").value.trim(), profile: el("profile").value, wait_seconds: 0 };
      if (el("conversation").value) args.session_id = el("conversation").value;
      if (el("model").value.trim()) args.model = el("model").value.trim();
      if (el("effort").value) args.reasoning_effort = el("effort").value;
      const result = await callTool("hermes_ask", args);
      followJob(result);
    } catch (error) {
      uncertainSubmission = Boolean(error.ambiguous);
      throw error;
    } finally {
      busy = false;
      el("controls").disabled = Boolean(currentJob) || uncertainSubmission || profiles.length === 0;
      el("send").textContent = "Ask Hermes";
    }
  });
});
connectHost().then(refreshPanel).catch(error => {
  el("connection").textContent = "Unable to connect. You can still ask Hermes in the conversation.";
  showError(error);
});
