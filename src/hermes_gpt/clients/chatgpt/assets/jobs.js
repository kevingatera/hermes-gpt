let currentJob = null;
let jobSession = null;
let pollTimer = null;
let pollCount = 0;
function stopPolling() {
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = null;
}
function followJob(result) {
  uncertainSubmission = false;
  currentJob = result.job_id;
  jobSession = result.session_id;
  pollCount = 0;
  el("job").hidden = false;
  el("answer").textContent = "";
  el("job-reference").textContent = `Job ${currentJob}`;
  el("job-status").textContent = "Hermes is working. This turn continues if you close the panel.";
  el("cancel-job").disabled = false;
  perform(checkJob);
}
async function checkJob() {
  if (!currentJob) return;
  stopPolling();
  const jobId = currentJob;
  const result = await callTool("hermes_session_job_status", { job_id: jobId });
  if (currentJob !== jobId) return;
  const job = result.job || {};
  if (job.profile && job.profile !== el("profile").value && profiles.some(row => row.profile === job.profile)) {
    // A pasted job may belong to another authorized profile. Keep its
    // conversation under that profile before offering a follow-up turn.
    el("profile").value = job.profile;
    await loadConversations();
  }
  jobSession = job.session_id || jobSession;
  el("job-status").textContent = `Hermes: ${job.status || "unknown"}`;
  if (["completed", "failed", "timed_out", "cancelled", "orphaned"].includes(job.status)) {
    const answer = await callTool("hermes_session_job_result", { job_id: jobId });
    el("answer").textContent = answer.response || "This turn returned no answer.";
    el("cancel-job").disabled = true;
    currentJob = null;
    el("controls").disabled = profiles.length === 0;
    if (jobSession) {
      if (![...el("conversation").options].some(option => option.value === jobSession)) {
        el("conversation").add(new Option(`Current conversation · ${jobSession}`, jobSession));
      }
      el("conversation").value = jobSession;
    }
    return;
  }
  // Automatic polling is bounded. The user can continue checking a durable
  // job manually without resubmitting or cancelling it.
  if (++pollCount < 150) pollTimer = setTimeout(() => perform(checkJob), 2000);
  else el("job-status").textContent += " · Automatic checks paused. Use Check result.";
}
el("check-job").addEventListener("click", () => perform(checkJob));
el("cancel-job").addEventListener("click", () => {
  if (!currentJob) return;
  perform(async () => {
    await callTool("hermes_session_job_cancel", { job_id: currentJob });
    await checkJob();
  });
});
window.addEventListener("pagehide", stopPolling);

el("follow-job").addEventListener("click", () => perform(async () => {
  const id = el("job-id").value.trim();
  if (!/^[a-f0-9]{32}$/.test(id)) throw new Error("Enter the job reference returned by Hermes.");
  followJob({ job_id: id });
  el("controls").disabled = true;
}));
