# Repository cleanup plan

This plan separates runtime code, tests, and documentation into directories
with explicit owners. The migration replaces long filename prefixes with
Python packages, then reviews the boundaries inside those packages.

## Target layout

```text
src/hermes_gpt/
  server/          CLI, HTTP transport, MCP tool registration
  auth/            OAuth grants, token encoding, encrypted storage
  policy/          authorization, redaction, protected paths
  sessions/        profiles, history, managed tasks, job persistence
  browser/         attachments, tabs, task and profile browser access
  missions/        plans, budgets, evidence, controller decisions
  execution/       jobs, runners, delegations, contracts, swarms
  fleet/           peer discovery, A2A, Fabric routing and transport
  skills/          catalog, resolution, content, management
  workspace/       files, Git, configuration, diagnostics
  clients/         curated MCP clients and their configuration
  ui/              Python HTTP handlers for the web application
tests/             tests grouped by the same responsibilities
web/               React application and its own tests
site/              public project website
docs/              setup and operation guides
  development/     architecture and contributor instructions
  design/          historical design decisions
  releases/        historical release planning
examples/          copyable configuration and launch examples
tools/             packaging and contributor utilities
plugins/           ChatGPT plugin definition and packaging inputs
```

Keep the existing `web`, `site`, `examples`, `tools`, and `plugins` names.
They already describe their contents. Do not move generated builds or local
runtime state into the source package.

## Work sequence

1. Move Python tests into `tests`, group them by responsibility, and give shared
   fixtures an explicit home. Update test discovery and CI commands together.
2. Move runtime modules into `src/hermes_gpt`. Use ordinary package imports
   and setuptools package discovery. Update CLI entry points, subprocess
   launch paths, runtime mounts, packaging checks, and deployment examples.
3. Replace filename prefixes with package names. For example,
   `operator_browser_tabs.py` becomes `browser/tabs.py`. Change imports and
   references in the same commit. Avoid import hooks and `sys.modules` aliases.
4. Review dependency direction. Tool registration calls services; services
   enforce policy and use stores or adapters. Stores do not import tool
   registration. Keep security checks near the operations they protect.
5. Review data models at those boundaries. Use dataclasses for internal records
   with fixed fields, enums for closed states, and explicit validation for
   external input. Keep dictionaries at JSON and MCP boundaries. Preserve
   persisted formats unless a migration is implemented and tested.
6. Rewrite the README and documentation index around setup and common tasks.
   Keep detailed gates and failure behavior in the relevant guide. Preserve
   historical documents as history rather than rewriting their conclusions.
7. Review each operational guide for repetition, stale claims, vague names,
   and dense prose. Verify behavioral claims against implementation and tests.

## Naming and code rules

Name modules for the responsibility they own. Prefer `profiles`, `tabs`,
`grants`, `store`, and `policy` to numbered versions or broad `common` modules.
Name records for what they contain and functions for what they do. Split a
module when it owns separate responsibilities, rather than at an arbitrary
line count. Comment security assumptions and unusual compatibility behavior.

Do not introduce a second provider configuration or credential store. Keep
Hermes profile configuration authoritative. Preserve confirmation gates,
secret-path restrictions, bounded input and output, and observed-state checks.

## Verification and delivery

Use isolated commits with a short explanation of why each change exists.
For each move, check collection, affected tests, packaging, and installed CLI
imports. Run the full suite for the package migration. Exercise managed session
start and continuation, shared browser access, and service restart behavior
before deploying it. A passing source-checkout import does not prove that a
wheel contains the right files.

The live service stays on the current checkout until the migrated package
passes those checks. Update its launch command only when the replacement is
ready. Do not publish a release as part of this cleanup.
