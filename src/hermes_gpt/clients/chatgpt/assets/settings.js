let settingsRevision = null;
function settingCheckbox(container, name, checked, kind) {
  const label = document.createElement("label");
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = checked;
  input.dataset.setting = name;
  input.dataset.kind = kind;
  label.append(input, document.createTextNode(name.replaceAll("_", " ")));
  container.append(label);
}
function applySettings(data) {
  el("settings-panel").hidden = !data?.can_reconfigure;
  if (!data?.can_reconfigure) return;
  settingsRevision = data.persisted.revision;
  el("setting-features").replaceChildren();
  el("setting-profiles").replaceChildren();
  for (const [name, enabled] of Object.entries(data.features)) {
    settingCheckbox(el("setting-features"), name, enabled, "feature");
  }
  for (const name of data.profile_ceiling) {
    settingCheckbox(el("setting-profiles"), name, data.profiles.includes(name), "profile");
  }
  el("apply-mode").value = data.apply_mode;
}
el("save-settings").addEventListener("click", () => perform(async () => {
  if (busy || currentJob || uncertainSubmission) throw new Error("Wait for the current work before changing settings.");
  const features = {};
  for (const input of document.querySelectorAll('[data-kind="feature"]')) features[input.dataset.setting] = input.checked;
  const profiles = [...document.querySelectorAll('[data-kind="profile"]')].filter(input => input.checked).map(input => input.dataset.setting);
  el("save-settings").disabled = true;
  try {
    await callTool("hermes_connection_configure", { features, profiles,
      apply_mode: el("apply-mode").value, expected_revision: settingsRevision, confirm: true, dry_run: false });
    await refreshPanel();
    el("settings-status").textContent = "Settings applied. No plugin refresh is needed.";
  } finally { el("save-settings").disabled = false; }
}));
