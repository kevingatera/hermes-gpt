let currentJob = null;
let jobSession = null;
let pollTimer = null;
let pollCount = 0;
let jobStartedAt = null;
function stopPolling() {
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = null;
}
function followJob(result) {
  currentJob = result.job_id;
  jobSession = result.session_id;
  pollCount = 0;
  jobStartedAt = null;
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
  let result;
  try {
    result = await callTool("hermes_session_job_status", { job_id: jobId, wait_seconds: 0 });
  } catch (error) {
    // A failed observation does not mean the durable job failed.
    if (currentJob === jobId) {
      if (error.code === "JOB_NOT_FOUND") {
        // A confirmed missing job is different from a failed observation.
        currentJob = null;
        el("cancel-job").disabled = true;
        el("controls").disabled = uncertainSubmission || profiles.length === 0;
        el("job-status").textContent = "This job was not found. Check its reference.";
      } else scheduleCheck();
    }
    throw error;
  }
  if (currentJob !== jobId) return;
  const job = result.job || {};
  if (job.status) uncertainSubmission = false;
  if (job.profile && job.profile !== el("profile").value && profiles.some(row => row.profile === job.profile)) {
    // A pasted job may belong to another authorized profile. Keep its
    // conversation under that profile before offering a follow-up turn.
    el("profile").value = job.profile;
    await loadConversations();
  }
  jobSession = job.session_id || jobSession;
  jobStartedAt = job.started_at || jobStartedAt;
  const observedEnd = job.ended_at ? Date.parse(job.ended_at) : Date.now();
  const seconds = jobStartedAt ? Math.max(0, Math.floor((observedEnd - Date.parse(jobStartedAt)) / 1000)) : null;
  const elapsed = seconds !== null && Number.isFinite(seconds) ? ` · ${Math.floor(seconds / 60)}m ${seconds % 60}s elapsed` : "";
  el("job-status").textContent = `Hermes: ${job.status || "unknown"}${elapsed}`;
  if (["completed", "failed", "timed_out", "cancelled", "orphaned"].includes(job.status)) {
    let answer;
    try {
      answer = await callTool("hermes_session_job_result", { job_id: jobId });
    } catch (error) {
      if (currentJob === jobId) scheduleCheck();
      throw error;
    }
    if (currentJob !== jobId) return;
    const emptyAnswers = {
      completed: "Hermes finished without returning an answer.",
      failed: "Hermes could not finish this work. Use the job reference when asking for diagnostics.",
      timed_out: "The time limit was reached. Ask Hermes to continue the existing conversation with a longer time limit.",
      cancelled: "This work was cancelled.",
      orphaned: "The server can no longer follow this process. Check diagnostics before starting replacement work."
    };
    el("answer").textContent = answer.response || emptyAnswers[job.status];
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
  scheduleCheck();
}
function scheduleCheck() {
  // Back off to ten seconds for long work. Bound total automatic checks,
  // while keeping the durable job available for manual observation.
  if (++pollCount < 720) pollTimer = setTimeout(() => perform(checkJob), Math.min(10000, 2000 + pollCount * 250));
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
