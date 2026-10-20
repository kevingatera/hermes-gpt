# Upstream integration, October 20, 2026

This merge brings upstream `bbe7f7ec50965dee7eec9b5e678b987109205f43`
into `integrate/2026-10-20`, starting at fork commit `234425c`.
The common ancestor is `43c244a`. The merge used
`git merge -X find-renames upstream/master` with Git 2.34.

All runtime changes use the `src/hermes_gpt` package. Modified upstream root
modules and new root tests were removed after their changes were ported.
Existing profile-aware skill checks already covered upstream's first three
commits and their complete test set, so those implementations were retained.

## Commit map

Paths below are relative to `src/hermes_gpt` unless another root is shown.

| Upstream commit | Change | Destination |
| --- | --- | --- |
| `e4cc5f5` | Profile-aware skill requirements | Existing `skills/{loader,catalog,resolution}.py`, `missions/{capability_manifest,plan_tools,placement}.py`, `execution/{contract_schema,contract,swarm_model}.py`; tests under `tests/skills`, `tests/missions`, and `tests/execution` |
| `a4ff161` | Explicit skill loading and fail-closed loader errors | Existing `skills/loader.py` and `skills/resolution.py`; `tests/skills/test_skill_resolution.py` |
| `187c462` | Disable inline shell preprocessing during skill probes | Existing `skills/loader.py`; `tests/skills/test_skill_resolution.py`; `.github/workflows/ci.yml` real Agent loader lane |
| `7dc6acc` | Make the real Agent loader executable in CI | Existing Python 3.14 Agent install lane, corrected to import `skills.loader`; temporary gitlink removal has no net upstream change |
| `42ec9f1` | Restore unrelated gitlink pointers | Cancels upstream's preceding pointer removal. Fork cleanup keeps those historical pointers absent |
| `f4151d9` | Durable, default-off Autopilot, DAG scheduling, advancement, approval frontier, bounded recovery, limits, event wakeups, status and UI read model | New `missions/autopilot.py` and `server/tools/autopilot.py`; `server/app.py`; existing `missions/{plan_schema,plan_store,plan_tools,controller_l2,runtime,observations}.py`; `ui/missions.py`; Autopilot, rework, supersession, and UI tests in their domain directories |
| `5971911` | Refresh the v0.13 site | `site/index.html` and the v0.13 site asset |
| `2e18846` | v0.13 README asset and release evidence corrections | `assets/hermes-gpt-v0.13.0-readme-hero.jpg`; historical `docs/releases/v0.13.0-release-notes.md`; fork README structure retained |
| `20967cc` | Deliverable isolation, recovery hardening, plan replacement guards, Windows supervision, portable Pi pipe reads, database cleanup, bundled Flight Deck | `missions/{autopilot,plan_tools,runtime,store}.py`; `execution/{job_store,job_supervisor,runner_common,runner_workers,delegation_store}.py`; `workspace/{gateway,diagnostics_checks,live_events}.py`; `policy/confinement.py`; `ui/security.py`; `web/src`; CI, manifest, packaging and portable tests |
| `bb8e810` | v0.14 declared artifact acceptance and bounded validation recovery | `execution/{contract_schema,contract_checks,contract_observations,contract,delegation_store,delegation_reconcile}.py`; `missions/{autopilot,plan_schema}.py`; `ui/missions.py`; `web/src/flight`; `tests/execution/test_v014_acceptance.py`; package version and public release docs |
| `41b8a7f` | Bind artifact size and digest to consistent observations | `execution/contract_checks.py`; `tests/execution/test_v014_acceptance.py` |
| `8306b95` | Compare compatible Windows artifact timestamps | `execution/contract_checks.py`; `tests/execution/test_v014_acceptance.py` |
| `f50ded3` | v0.14 site, docs hub and deployment provenance | `site/index.html`, `site/docs.html`, v0.14 assets; fork task-oriented README and docs index updated; `docs/runtime-checkout.md` explains independent service/package verification |
| `bbe7f7e` | Match built-in fleet ceilings to manifest peers | `fleet/authority.py`, including `gaming-4090`, `macbook-m5`, `mac-mini-m5`, and `omarchy` |

## Resolution decisions

- Retain setuptools package discovery, package entrypoints, grouped tests, and
  split implementation modules. Do not restore upstream's flat module list.
- Keep the fork's existing explicit-load skill resolver, profile checks,
  dispatch safeguards, and Mission Control naming. Its skill tests include
  every upstream skill test. The new upstream tests were checked by function
  name against the migrated suite; none are missing.
- Preserve the fork's Swarm dispatch bookkeeping for ambiguous submissions.
  Rejecting missing skills now leaves stage state unchanged, as upstream
  requires. No runner starts on that rejection.
- Keep upstream's structured plan-validation error response and plan replacement
  guards. Adapt the older fork assertion to the structured response.
- Port upstream test patches to the modules that own the implementation. Keep
  real child-process tests, including partial Pi lines, cancellation, worker
  restarts, and the final approval boundary.
- Retain the fork's reader-oriented documentation. Add Autopilot and acceptance
  guidance, preserve both changelogs, and keep historical release documents
  historical. Upstream's deployment receipts do not establish this host's
  running version.
- Ship the built Flight Deck through wheel/sdist data files, resolve installed
  assets through distribution metadata, and carry the web artifact through CI
  and publication jobs. Preserve the fork's ChatGPT panel assets.
- Keep upstream's Windows CI matrix while retaining the fork's package paths,
  MCP SDK matrix, panel tests, and real Agent loader lane.

## Validation

Run on this Linux host with Python 3.11.15 from `/tmp/hgpt-venv`, an editable
install of this checkout with `mcp` 1.28.1. The Python suite used CI's
`HERMES_HTTP_TEST=1` and `python -m pytest -q -o addopts='' --tb=short`.

- Full Python suite: 1,931 passed, 3 skipped. Two skips are the MCP SDK 2
  protocol tests, which need the `mcp==2.x` CI lane. One is the Windows-only
  command test.
- CI's Ruff file list passed. `git diff --check` and `compileall` on
  `src/hermes_gpt` passed.
- Flight Deck (`web/`): `npm test` passed, 31 tests in 8 files. `npm run build`
  passed.
- ChatGPT panel Node tests: 15 passed.
- `python -m build` produced the wheel and sdist. `twine check` and
  `tools/check_package_hygiene.py` passed for both. The wheel carries the Flight
  Deck bundle under `share/hermes-gpt/web/`, which `ui/security.py` resolves
  through the distribution RECORD when running installed.
- Every `test_*` function name in upstream's root tests exists in the migrated
  `tests/` tree. The ported behavior listed above was spot-checked in the merged
  modules.

Not run here: the Windows runners, the `mcp==2.x` lane, the Python 3.14 real
Hermes Agent loader lane (it needs a Hermes Agent checkout), and an
installed-wheel smoke test.

No upstream runtime feature or fix was omitted, as far as these checks show.
The historical gitlink pointers remain excluded by the fork's cleanup. No push,
release, or service deployment is part of this merge.
