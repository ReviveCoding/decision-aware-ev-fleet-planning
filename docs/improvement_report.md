# Improvement Report — 0.7.1rc4

## Scope and authority

- Mode: `MODIFY_AND_RUN`
- Validation profile: `STANDARD`
- Baseline source: `decision-aware-ev-fleet-planning-v0.7.0.zip`
- Baseline source-manifest fingerprint: `cd2b0213ee244455a6e8e966abac764fcbebc6f49c3940ecf98cd07568f6c6cb`
- Git state: snapshot without `.git`; file manifests and round diffs were used as rollback evidence.
- Major improvement rounds: 4 of 5 allowed.

This candidate does not change forecast splits, metric definitions, optimization objectives, reliability thresholds, or synthetic-data generation. The scientific baseline is therefore expected to remain semantically identical.

## Baseline

| Gate | Baseline result |
|---|---|
| Test collection | 110 |
| Source-only tests | 109 passed, 1 failed |
| Editable-install tests | 110 passed |
| Coverage | 85% |
| Ruff | PASS |
| Build | wheel and sdist PASS |
| Smoke | PASS |
| Medium | PASS |
| GPU-smoke | PASS on CPU |
| API service smoke | PASS |
| Reproducibility | PASS, 15/15 semantic artifacts |

The sole source-only failure was `test_pipeline_lock_is_released_after_process_crash`: the child process did not inherit the repository `src/` import path. Editable installation masked the defect.

## Priority analysis

| Issue | Severity | Likelihood | Scope | Confidence | Cost | Priority rationale |
|---|---:|---:|---:|---:|---:|---|
| Source-only child process cannot import package | High (4) | 5 | 4 | 1.0 | 1 | 80.0; clean-checkout and claimed source execution break |
| Public-data CLI accepts no-op or partial Amazon input | Medium (2) | 4 | 2 | 1.0 | 1 | 16.0; silent operator error |
| Required release-candidate handoff evidence absent | High (4) | 5 | 4 | 1.0 | 2 | 40.0; candidate cannot be independently qualified |

## Round 1 — source-checkout portability

### Root cause

Pytest's `pythonpath = ["src"]` affects the test process, not a separately spawned Python interpreter. The crash-lock test launched a child without `PYTHONPATH`, so a clean checkout failed before exercising the lock.

### Change

- explicitly prepend `<project>/src` to the child process environment;
- add a GitHub Actions `source-checkout` job that installs dependencies but not the project and runs the full suite plus `python -m ev_fleet --help`.

### Verification

- targeted crash-lock test: PASS without relying on editable installation;
- full suite: PASS;
- workflow YAML parse: PASS.

## Round 2 — public-data CLI contract

### Root cause

The adapter returned success when no source was supplied and silently ignored a one-sided Amazon route/package input pair.

### Change

- reject no-source invocation with parser exit code 2;
- require `--amazon-route-data` and `--amazon-package-data` together;
- add subprocess regression tests for both cases.

### Verification

Targeted CLI tests pass and existing public-data adapter/calibration tests remain unchanged.

## Round 3 — evidence-grounded handoff

### Root cause

The repository had extensive validation documentation but no machine-readable candidate manifest tying source, dependencies, commands, metrics, limitations, and artifact hashes together.

### Change

- add `ev_fleet.release_handoff`;
- add the packaged `ev-fleet handoff` command;
- validate mandatory evidence fields and release artifacts;
- compute source fingerprint, dependency checksums, artifact SHA-256 and sizes;
- explicitly label the result `RELEASE_CANDIDATE_ONLY`;
- exclude the output handoff file and runtime/build directories from source fingerprinting.

### Verification

Unit tests cover deterministic source fingerprinting, artifact hashing, missing evidence, and missing artifacts. Final handoff generation is executed only after exact candidate artifacts exist.


## Round 4 — packaged handoff entrypoint

### Root cause

The handoff implementation was available through a repository script, but the script was not included in the generated source distribution. Installed-wheel and sdist users therefore did not have the same supported handoff interface.

### Change

- add the packaged `ev-fleet handoff` subcommand;
- remove the redundant script wrapper;
- keep the handoff implementation in the packaged `ev_fleet.release_handoff` module;
- add CLI regression coverage.

### Verification

The exact wheel and sdist contain the handoff module, the installed CLI exposes `handoff`, and targeted CLI/builder tests pass.

## Regression assessment

The changes do not touch forecasting, scenario generation, optimization, replay, RCCE, or policy selection. Final smoke, medium, GPU-smoke, API, and semantic-reproducibility results are recorded in `docs/release_candidate_evidence.json` and `release_candidate_handoff.json`.


## Round 5 — final operational CLI and CI action hardening

### Root cause

The final qualification checklist requires CLI `--version`, but the parser only exposed subcommands and `--help`. The GitHub workflow also needed a final current-documentation pass for action versions.

### Change

- add `ev-fleet --version`, backed by the single package version source;
- add the version command to the canonical local qualification script;
- add a regression test for the version path;
- update the workflow checkout action to `actions/checkout@v7` while retaining `actions/setup-python@v6`.

### Verification

Targeted CLI tests, two full-suite passes, canonical local qualification, three smoke runs, medium, GPU-smoke, API lifecycle, failure injection, reproducibility, reference export, clean source, and wheel execution all passed after this final delta.
