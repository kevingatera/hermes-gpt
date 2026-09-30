# Check which code a service runs

A running service may use a different checkout or Python environment from your
shell. Verify both before deciding that a change is deployed. A Git commit ID
alone cannot prove which modules the process loaded.

## Inspect the launcher

For a systemd user service, substitute its unit name:

```bash
systemctl --user show hermes-gpt.service \
  -p ExecStart -p WorkingDirectory -p ExecMainStartTimestamp
```

Check the Python executable and launch command. Current installations use
`hermes-gpt` or `python -m hermes_gpt`. Older launchers may still point to the
removed root `server.py`; update them when installing the new package layout.
Keep service output containing private paths or credentials local.

## Find the installed code

Use the same interpreter as the service:

```bash
/path/to/venv/bin/python -c \
  'import hermes_gpt; print(hermes_gpt.__file__)'
```

An editable installation points into its checkout's `src/hermes_gpt` directory.
A wheel points into the environment's `site-packages`. The shell's Python may
resolve a different installation.

For a source checkout, inspect its state:

```bash
git -C /path/to/checkout rev-parse HEAD
git -C /path/to/checkout status --short
```

Uncommitted files matter in an editable installation. Preserve them while
reviewing the deployment. The service start time is useful context, but it does
not establish that a long-running process reloaded files edited afterward.

## Verify after installing

Wait for active managed turns to finish before restarting. Then check service
health and call a tool through the actual MCP connection. Record the checkout
or wheel version, the interpreter, and the observed result in local deployment
notes rather than putting host-specific state in this shared guide.

Saved sessions can survive a restart. Isolated browser tabs and process
ownership have different lifetimes; see [managed sessions](managed-hermes-sessions.md)
and [session control](session-control.md).
