"""Client guidance shared by MCP discovery and capability reporting."""

SESSION_INSTRUCTIONS = """If hermes_console is available, use it when the user asks to open the Hermes panel or configure how they interact with Hermes.
If hermes_connection_settings is available, inspect it to discover live controls.
On an explicit user request, use hermes_connection_configure to change features,
authorized profiles, or apply mode. Preview, then apply with confirm=true and
dry_run=false. Changes apply immediately without refreshing ChatGPT. Never
change settings just to satisfy a read request. Profile config changes apply
to the next Hermes turn. Use hermes_schedule_create and hermes_schedule_action
for requested schedule changes. A queued run is not a completed result.
Use Hermes for tasks that need its configured tools and accounts.
Prefer hermes_ask for ordinary requests. It returns an answer when ready or a
job_id to follow without launching another turn.
First discover an authorized profile with hermes_session_profiles. For ordinary
requests, such as checking email, start or continue a regular Hermes session.
Managed tasks are for explicitly confined workspace work. Omit model and
reasoning_effort unless the user requests an override. Hermes loads the selected
profile's providers, credentials, skills, and enabled tools. Availability of an
account integration must be checked by Hermes; never promise access merely
because session delegation is enabled.
After starting a turn, retain its job_id and use hermes_session_job_status to await the
actual result in bounded waits (25 seconds by default, completed answer included).
hermes_wait is an optional equivalent when available. Give one short working update; do not narrate
every observation or run rapid status polling. If the chat turn must end,
say the job continues and offer the Hermes panel or a later result request.
Never promise an automatic later chat reply. Open hermes_console(job_id=...) to follow and cancel
the same job without asking the user to paste a reference. Retrieve hermes_session_job_result if hermes_wait is unavailable. Report the actual result or
failure, not just that work was submitted. Do not confuse job, session, and task
IDs. Do not restart a running turn or launch duplicate jobs while polling.
For existing scheduled jobs use hermes_cron_list and hermes_cron_status with the
profile listed in capabilities.cron_read.profiles. This can differ from the
profile used for delegated work. Disabled cron planning or creation does not disable
read-only inspection. Bridge capability flags describe direct MCP tools, not
all tools available inside Hermes. Do not change configuration to answer a read
request. Browser authentication in ChatGPT's app browser is separate from a
Hermes browser profile. Use only a discovered authorized Hermes browser.
"""
