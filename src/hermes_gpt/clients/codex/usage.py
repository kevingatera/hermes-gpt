"""Client guidance shared by MCP discovery and capability reporting."""

SESSION_INSTRUCTIONS = """Use Hermes for tasks that need its configured tools and accounts.
Prefer hermes_ask for ordinary requests. It returns an answer when ready or a
job_id to follow without launching another turn.
First discover an authorized profile with hermes_session_profiles. For ordinary
requests, such as checking email, start or continue a regular Hermes session.
Managed tasks are for explicitly confined workspace work. Omit model and
reasoning_effort unless the user requests an override. Hermes loads the selected
profile's providers, credentials, skills, and enabled tools. Availability of an
account integration must be checked by Hermes; never promise access merely
because session delegation is enabled.
After starting a turn, retain its job_id, poll hermes_session_job_status, and
retrieve hermes_session_job_result when completed. Report the actual result or
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
