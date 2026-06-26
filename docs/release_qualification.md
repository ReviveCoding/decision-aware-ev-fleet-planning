# Release Qualification Report — 0.7.1rc4 Operational Hardening

This document records the final local qualification scope for the `decision-aware-ev-fleet-planning` release candidate. It is intentionally stricter than the candidate handoff: it checks that the release bundle is a clean source artifact, that installed-package execution works outside the repository, and that GitHub workflow configuration is locally reproducible where GitHub-hosted runner access is unavailable.

## Qualification profile

- Profile: STANDARD local qualification.
- Local evidence levels reached: Q1 current worktree, Q2 clean extracted source, Q3 built wheel in an isolated environment.
- GitHub evidence level: Q1/Q2 configuration and local workflow-equivalent only; no remote push or hosted runner execution was performed.
- Final verdict is limited to `CONDITIONALLY QUALIFIED` until the exact candidate commit passes GitHub-hosted workflow runs.

## Blockers found during qualification

1. The uploaded prior candidate ZIP contained runtime artifacts, generated data, SQLite output, caches, locks, and bytecode. These were removed from the final qualified source ZIP, leaving only source files, configs, tests, docs, reference evidence, packaging metadata, and empty runtime roots.
2. The CI workflow was rechecked against current official action documentation and now uses `actions/checkout@v7` and `actions/setup-python@v6` with repository-level read-only permissions.
3. The repository lacked a canonical local qualification command and machine-readable qualification/bundle manifests. `scripts/qualify_local.py`, `qualification_manifest.json`, and `release_bundle_manifest.json` now provide those handoff artifacts.

## Canonical local qualification command

```bash
python scripts/qualify_local.py --timeout-seconds 300 --output qualification_local_summary.json
```

For source-only validation without installing the project package:

```bash
PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
PYTHONPATH=src python -m ev_fleet --help
```

For installed-package smoke:

```bash
python -m pip install -c constraints/runtime.txt .
ev-fleet run --config configs/smoke.yaml
ev-fleet validate --config configs/smoke.yaml
```

## GitHub qualification boundary

GitHub Actions configuration uses source-checkout, test matrix, and package jobs with minimal read permissions. The action versions were cross-checked against current public release documentation. Actual GitHub-hosted execution requires pushing the final source to a repository and must be recorded separately before claiming `RELEASE QUALIFIED`.

## Final local qualification results

- Candidate version after operational fixes: `0.7.1rc4`.
- Test collection: 121 tests collected.
- Final full suite: 121 passed.
- Coverage: 85% total line coverage using `coverage run --source=src/ev_fleet -m pytest -q`.
- Smoke runs: 3/3 passed after final code changes.
- Medium profile: passed; selected forecast `poisson`, selected base policy `deterministic`, corrective RCCE `RELEASE`.
- GPU-smoke config passed in this environment using the CPU PyTorch path; the Transformer challenger remained unpromoted and the final champion stayed `seasonal_naive`.
- API lifecycle: startup/readiness/latest-plan/latest-reliability/latest-run/404/shutdown passed across 3 separate processes.
- Failure injection: missing config returned non-zero; corrupted artifact returned non-zero; rerun restored valid artifacts.
- Performance observation: smoke run wall time 6.83 seconds, peak RSS 379,244 KB in this Linux Python 3.13 environment.
- GitHub status: configuration and local parity only; hosted run not executed.

Final verdict: `CONDITIONALLY QUALIFIED` because local clean source and wheel gates pass, but GitHub-hosted workflow execution was not available.


## Previous rc3 delta

The last qualification loop added the public `ev-fleet --version` CLI contract, made `scripts/qualify_local.py` verify it, updated GitHub checkout usage to `actions/checkout@v7`, and re-ran impacted local gates. No forecast split, optimization objective, RCCE threshold, metric, or synthetic-data generation logic changed.


## Exact final artifact verification

- Exact clean source without installed package: PASS.
- `scripts/qualify_local.py --skip-e2e` includes both `--help` and `--version`: PASS.
- Exact rebuilt wheel: PASS in an isolated environment, including dependency imports, `pip check`, packaged SQL resource lookup, `ev-fleet --version`, external smoke run, and validation.
- Exact source ZIP residue audit: PASS.


## Final rc4 delta

The final loop found that the canonical local qualification script left smoke runtime outputs in the repository after a successful run. This was not a model or science defect, but it could dirty a user working tree before push. The script now cleans `artifacts*` and `data/generated*` by default and preserves them only when `--preserve-runtime-outputs` is explicitly supplied. Targeted cleanup tests, two full test-suite passes, lint, format, compile, smoke, medium, GPU-smoke, and coverage were rerun.
