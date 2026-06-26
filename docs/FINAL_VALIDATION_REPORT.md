# Final Validation Report

Release candidate: **0.7.1rc4**  
Audit date: **2026-06-17**  
Primary validated runtime: **Linux, Python 3.13.5**

## Candidate decision

Status: **RELEASE_CANDIDATE_ONLY**. Version 0.7.1rc4 preserves the scientific results and numerical release contract of v0.6.0 while closing the remaining material gaps in complete-directory integrity, manual-override governance, API configuration consistency, dashboard trust, reference evidence validation, and destructive runtime-path safety. Further improvements now fall primarily into new scientific or infrastructure scope.


## v0.7.1rc4 candidate handoff delta

- clean source subprocess tests propagate the repository `src/` import path;
- GitHub Actions includes a source-checkout job that does not install the project package;
- public-data ingest rejects no-source and incomplete Amazon input pairs;
- a machine-readable handoff builder binds source, dependencies, command evidence, metrics, limitations, and exact release artifact hashes;
- `docs/improvement_report.md` and `docs/known_limitations.md` document executed evidence and qualification boundaries.

These changes do not modify forecast, scenario, optimization, simulation, RCCE, metric, split, or threshold behavior.

## End-to-end result

```text
validated/provenanced inputs
→ SQL feature and capacity marts
→ classical and optional GPU forecasts
→ temporal conformal calibration
→ decision-aware forecast champion gate
→ deterministic, risk-neutral, and CVaR MILPs
→ exact VSS/EVPI recourse analysis
→ common-random-number operational replay
→ numerical solver and RCCE release gate
→ bounded corrective frontier
→ rolling reoptimization
→ signed complete-directory manifests
→ run-bound API, dashboard, reference evidence, and reports
```

## v0.7.0 hardening

### Complete signed inventory

Both artifact and generated-data manifests contain `schema_version: 1`. Validation rejects:

- files not listed in the manifest;
- missing or changed files;
- duplicate records;
- unsupported schema versions;
- path traversal;
- symlinks inside signed directories;
- mismatched run IDs.

This prevents a valid subset of files from masking stale or manually added operational inputs.

### Manual override governance

Planner overrides remain useful for impact analysis, but they no longer appear automatically releasable. Every override:

- is independently replayed;
- is not persisted automatically;
- returns `approval_required: true`;
- records that the inherited solver certificate applies to the base plan only;
- receives at best RCCE `REVIEW`, never automatic `RELEASE`.

Negative deltas remain limited to withdrawing existing temporary capacity.

### API, dashboard, and evidence consistency

- each API request uses one configuration snapshot from readiness through artifact access;
- run identity is checked again after reads;
- dashboard payloads validate manifest hashes and cross-artifact semantics before display;
- reference export requires complete repository validation rather than hash verification alone;
- rehashed but semantically contradictory artifacts are rejected by API, dashboard, and reference export.

### Runtime-path safety

Destructive runtime operations reject project root, source/evidence trees, external paths, the `data/` root, arbitrary data descendants, and symlink-resolved protected locations. Only configured generated-data directories named `generated*` may be modified under `data/`.

## Scientific results

### Smoke profile

- pipeline: PASS;
- forecast champion: `seasonal_naive`;
- selected base policy: risk-neutral;
- base RCCE: BLOCK;
- corrective RCCE: RELEASE;
- formal sample VSS: 741.6551;
- formal sample EVPI: 340.7227;
- held-out empirical VSS: 738.1042;
- solver termination: OPTIMAL;
- recommended MIP gap: 0.0;
- maximum recommended constraint violation: approximately 3.64e-12;
- semantic reproducibility: 15/15 artifacts identical.

### Medium profile

- five stations and 750 station-day rows;
- 490 optimization routes and 1,100 evaluation routes;
- forecast champion: Poisson;
- selected base policy: deterministic;
- corrective RCCE: RELEASE;
- formal sample VSS: 605.5676;
- formal sample EVPI: 2,055.2959;
- held-out empirical VSS: **-408.8598**;
- rolling expected savings: 516.0037.

The positive in-sample VSS and negative held-out VSS are reported separately; the stochastic policy is not promoted when it fails the held-out decision test.

### GPU-smoke profile

- pipeline: PASS;
- device in this audit: CPU;
- one training epoch plus one refit epoch;
- conformal correction: 2.3323;
- validation MAE: 6.7820;
- P90 coverage: 0.8810 against target 0.90;
- downstream decision cost: 6,969.64;
- shortage CVaR90: 13.0;
- promoted to champion: no;
- final champion: `seasonal_naive`.

CUDA auto-detection remains implemented, but CUDA execution was not available and is not claimed as locally validated.

## Engineering verification

- **114 pytest tests passed**;
- source coverage: **85%**;
- optimization coverage: 97%;
- simulation coverage: 97%;
- forecasting coverage: 90%;
- reliability coverage: 100%;
- Ruff lint and format: PASS;
- Python compilation: PASS;
- smoke, medium, and GPU-smoke: PASS;
- all three profile validations: PASS;
- semantic reproducibility: PASS;
- strict manifest inventory and schema tests: PASS;
- untracked-file and symlink rejection: PASS;
- config-snapshot TOCTOU test: PASS;
- manual override approval test: PASS;
- dashboard hash and semantic contradiction tests: PASS;
- reference-export semantic validation test: PASS;
- source-controlled and arbitrary-data runtime path tests: PASS;
- exact clean source without installed distribution metadata: PASS;
- exact clean source tests, lint, format, compilation, module CLI, smoke, and validation: PASS;
- exact rebuilt wheel dependency imports, `pip check`, version parity, packaged SQL, external smoke, and validation: PASS.

## Packaging and CI boundary

The project retains a `src/` package layout, dynamic single-source version, packaged SQL, CLI entry point, constrained dependencies, wheel/sdist builds, and an external-working-directory wheel test. GitHub Actions is configured for Ubuntu Python 3.11 and 3.13 plus Windows Python 3.11, with a separate isolated-wheel job.

Local Docker, Windows, Python 3.11, and CUDA execution are not fabricated; they remain CI/configuration boundaries unless run in those environments.

## Remaining deliberate scope boundaries

- minute-level vehicle-to-charger scheduling;
- electrical distribution-grid power flow and V2G;
- maintenance parts and work-order queues;
- exact IIS and globally minimal corrective capacity proof;
- durable multi-user override storage and authentication;
- cloud rollout and production A/B impact;
- actual enterprise fleet data;
- hardware-specific CUDA benchmark.

These are new product or research scopes rather than repairs to the current repository.
