# Release Notes — 0.7.1rc4 Qualified Bundle

This qualified bundle preserves the v0.7.1 science and API behavior while adding final rc3 CLI/CI hardening and rc4 local-qualification cleanup/fingerprint hardening.

## Operational hardening since candidate handoff

- Removed runtime outputs, caches, lock directories, SQLite files, generated data, and bytecode from the source ZIP.
- Replaced the unverified `actions/checkout@v6` workflow reference with the published `actions/checkout@v7` tag.
- Added `scripts/qualify_local.py` as a canonical local qualification command.
- Added `docs/release_qualification.md`, `qualification_manifest.json`, and `release_bundle_manifest.json` for final operational handoff.

## Qualification status

Local clean source and built wheel paths were validated. GitHub-hosted workflow execution was not run in this environment, so the final verdict remains conditionally qualified pending exact-commit CI.



## Final rc4 delta

- Added default runtime-output cleanup to `scripts/qualify_local.py` so local qualification does not leave `artifacts/`, profile artifacts, or `data/generated/` outputs in the source tree.
- Added `--preserve-runtime-outputs` for users who intentionally want to inspect generated artifacts after qualification.
- Added regression tests for qualification cleanup and the preserve-output escape hatch.
- Re-ran targeted tests, two full suites, lint, format, compile, canonical qualification, smoke, medium, GPU-smoke, and coverage.

## Previous rc3 delta

- Added `ev-fleet --version` and included it in the canonical local qualification script.
- Updated GitHub checkout action usage to `actions/checkout@v7` based on the current official action README.
- Re-ran impacted gates: 121 tests twice after final source changes, lint, format, compile, canonical qualification, smoke repeats, medium, GPU-smoke, API lifecycle, failure injection, reproducibility, reference export, clean source, and exact wheel external smoke/validate.
