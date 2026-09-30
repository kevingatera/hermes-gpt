# Contributing

Read [AGENTS.md](../../AGENTS.md) for the security rules and
[the documentation index](../README.md) for the guide that owns the behavior
you are changing. Work in an isolated checkout when a local service uses the
repository.

## Find the owner

Keep CLI and MCP registration separate from operations. Registration declares
tool arguments and calls a service. The service enforces policy before it
launches a process, changes a file, or writes a record. Storage code handles
persisted formats and atomic writes. Browser and provider adapters handle
external tools.

Keep shared code near its consumers. A helper used by two session modules
belongs with sessions. Move it into a shared package only when other
subsystems actually need it. Avoid `common` modules that collect unrelated
functions.

## Choose a data structure

Use an immutable dataclass for validated internal values with a fixed shape.
`ModelOverrides` keeps model and effort choices together without resolving
the profile defaults. Callers read named fields instead of unpacking a
positional pair.

Dictionaries are appropriate for MCP responses and JSON documents. Validate
external values before treating them as internal records. Keep persisted field
names and optional-field behavior stable; a new class does not justify silently
changing old records. Use an enum when a closed set of states has shared
transition logic. Do not add a class that only renames a dictionary.

## Make a change reviewable

Separate file moves from behavior changes where possible. Use explicit imports
and inspect subprocess paths after a move. A worker may run from another
directory or interpreter, so imports that work in the test process can still
fail in production.

Comment assumptions that a reader cannot infer from the code, especially
process ownership, secret-path checks, narrow runtime mounts, and compatibility
with older stored records. Give each module one responsibility. Extract a
second responsibility rather than splitting at a line-count target.

## Check the result

Python tests live in `tests`, grouped by subsystem. Shared server fixtures
live in `tests/support`. The top-level test configuration isolates Hermes data
and credentials; use it rather than reading the developer's live profile.

Run affected tests first. For imports, process launch, policy, or package moves,
also run the full suite and check an installed wheel from outside the checkout.
Build checks are listed in [the release checklist](../../RELEASE_CHECKLIST.md).
Verify managed sessions and browser access with real tools before replacing a
running deployment.

## Write the documentation

Tell the reader what to do, what happens, and what can fail. Put setup before
implementation history. State defaults and required gates exactly, then link
to the guide that owns the details. Keep version history in the changelog.

Use concrete words and varied sentences. Cut promotional claims, filler,
repeated warnings, and labels that do not help a reader act. Poteto's `unslop`
skill is used for this cleanup. Preserve technical names when changing them
would make a command or a behavior harder to identify.
