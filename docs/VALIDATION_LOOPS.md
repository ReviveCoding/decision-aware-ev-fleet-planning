# Strength, Weakness, and Reinforcement Loops

This document records the material weaknesses found after the first complete implementation, the corrective changes, and the verification evidence. The loop stopped only after remaining gaps became deliberate scope boundaries rather than correctness or integration defects.

## Loop 1: scientific comparison validity

**Weakness found**

- the deterministic policy used the first sampled scenario rather than an expected-value workload;
- plans were simulated with different random draws;
- the initial EVPI implementation re-evaluated optimized assignments with a greedy approximation.

**Resolution**

- build a weighted expected-value workload;
- use common random numbers across every policy;
- calculate fixed-allocation recourse and wait-and-see costs with the same MILP cost system.

**Verification**

- formal sample VSS and EVPI are nonnegative within numerical tolerance;
- held-out empirical VSS is labeled separately and may be negative;
- exogenous scenario IDs, regimes, energy multipliers, and replay draws match across policies.

## Loop 2: optimizer and simulator policy mismatch

**Weakness found**

The optimizer could use effectively unlimited emergency leases while replay allowed none.

**Resolution**

- bound emergency leases per station in the MILP;
- model uncertain emergency-lease fulfillment and realized leasing cost in replay;
- retain stricter replay-only failures, delay, charging taper, and outage effects.

**Verification**

- plan scenario metrics respect the configured station-level lease cap;
- replay reports emergency-lease use and cost;
- RCCE can still block a nominally feasible plan.

## Loop 3: complexity promotion

**Weakness found**

A CVaR plan was treated as the default advanced champion even when it produced the same allocation or weaker held-out evidence.

**Resolution**

- create deterministic, risk-neutral stochastic, and risk-aware CVaR policies separately;
- select the base policy by RCCE status, failed gates, realized cost, and tail shortage;
- report when the simpler policy wins.

**Verification**

- `base_policy_selection.csv` contains one reproducible champion;
- smoke mode retains the deterministic policy when stochastic complexity does not earn promotion.

## Loop 4: corrective-action arbitrariness and overreach

**Weakness found**

- one fixed shortage quantile could overbuild capacity or fail to satisfy the release gate;
- corrective action could run even when the base plan had already passed;
- vehicle additions could exceed parking headroom or create charging bottlenecks;
- route splitting reduced demand without consuming a second vehicle;
- temporary charging and route-split program costs were missing from realized cost.

**Resolution**

- generate a bounded reliability/cost frontier across shortage quantiles and corrective rounds;
- run correction only when the selected base policy is `REVIEW` or `BLOCK`;
- cap additions by parking and temporary-charging limits;
- jointly correct vehicle and charging capacity;
- make route splitting consume additional vehicle capacity, distance, and a fixed program cost;
- include all corrective fixed costs in exact and simulation-based evaluation;
- rank `RELEASE` candidates by realized cost before surplus reliability.

**Verification**

- `corrective_frontier.csv` exposes additions, charging capacity, route-split factor, reliability, and full realized cost;
- RCCE independently rechecks parking, fleet balance, charging, and implementability;
- smoke mode selects the least-cost releasing frontier candidate rather than the largest candidate.

## Loop 5: runtime and CI efficiency

**Weakness found**

Pandas grouping and row iteration were repeated inside every Monte Carlo run, and optional native ML libraries were imported during the CPU-only path.

**Resolution**

- precompile scenario, route, station, and vehicle structures;
- vectorize bootstrap resampling;
- lazy-load optional GPU and boosting dependencies;
- separate smoke, medium, and full scales.

**Verification**

- the complete smoke pipeline executes in seconds on CPU;
- the medium five-station profile executes locally without changing code;
- optional dependencies are absent from the default runtime path;
- CI uses the smoke profile on Ubuntu and Windows Python 3.11.

## Loop 6: forecast leakage and weak decision coupling

**Weakness found**

- rolling SQL features included the current target row;
- missing lags were filled from the current target;
- current-day cube, distance, and energy features leaked into next-day prediction;
- GPU normalization used the full period;
- forecast candidates were compared with a simplistic proportional allocation replay;
- the GPU model was trained but could not compete for production selection.

**Resolution**

- use past-only SQL windows and lagged workload covariates;
- impute with training-only statistics;
- construct next-day rows explicitly from known future calendar variables and last observed lag values;
- compute GPU normalization on pre-holdout data only;
- evaluate every candidate with station parking, physical redeployment, active-fleet commitment, idle cost, shortage cost, and tail risk;
- place the temporal quantile Transformer in the same calibration and downstream-decision gate as classical models.

**Verification**

- leakage tests inspect SQL windows and holdout behavior;
- `forecast_model_comparison.csv` contains predictive, calibration, active-fleet, redeployment, cost, and shortage metrics;
- the GPU challenger is promoted only when it beats the current champion on the configured decision rules.

## Loop 7: domain fields that were present but inactive

**Weakness found**

`repair_capacity`, vehicle `charge_power_kw`, and `variable_cost_per_mile` existed in the data contract but did not affect every decision/evaluation layer.

**Resolution**

- include expected same-day repair recovery in optimization availability;
- model repair-bay capacity and probabilistic same-day recovery in replay;
- enforce vehicle charge-acceptance feasibility separately from aggregate depot power;
- include vehicle-type mileage cost in optimization, exact recourse, approximate screening, and independent replay.

**Verification**

- monotonic and counterfactual tests show repair capacity and charge acceptance affect feasibility;
- reports expose failed/repaired vehicles, repair utilization, charge-acceptance ratio, and variable operating cost;
- cost calculations agree across the relevant layers on controlled instances.

## Loop 8: reliability-gate completeness

**Weakness found**

A solver-feasible plan could release despite a large MIP gap, impossible confidence targets, parking imbalance, or unresolved corrective capacity.

**Resolution**

- add solver-gap and degraded-mode gates;
- reject reliability thresholds that cannot be reached with the configured Monte Carlo run count;
- add hard fleet-balance, nonnegative allocation, parking, charging, and corrective-implementability checks;
- use lower confidence bounds rather than only sample means.

**Verification**

- invalid configurations fail before optimization;
- infeasible, degraded, high-gap, parking-overflow, and unresolved-correction cases cannot release;
- `recommended_rcce.json` records each component gate and reason.

## Loop 9: artifact, API, and dashboard contracts

**Weakness found**

- the dashboard referenced stale run-summary keys;
- writes could leave partial artifacts after interruption;
- relative paths could fail outside the repository working directory;
- overrides did not fully validate parking capacity.

**Resolution**

- centralize dashboard loading in a tested package module;
- use atomic file replacement for core artifacts;
- resolve paths from the configuration and package SQL resources inside the wheel;
- use strict Pydantic requests and independent override replay;
- add artifact manifests and schema checks;
- export compact, validated `reference_results/` snapshots so GitHub reviewers can inspect evidence without committing full runtime directories.

**Verification**

- API and dashboard loader tests use current artifacts;
- manifest verification detects missing or modified files;
- packaged entry points work with external configuration paths;
- invalid override counts and parking overflow are rejected;
- smoke and medium reference snapshots contain checksummed key plans, metrics, and reports.

## Loop 10: reproducibility, packaging, and claim discipline

**Weakness found**

- broad dependency ranges could drift across new machines;
- source-tree execution could hide missing package data;
- default documentation overstated optional public-data calibration;
- several simulation and optimization assumptions were hidden in code defaults.

**Resolution**

- provide a validated Python 3.11 direct-dependency constraints baseline;
- build a wheel and test it from an external working directory;
- package SQL transformations under `src/ev_fleet/sql`;
- distinguish `public-data-informed synthetic` defaults from optional source-file calibration;
- expose charge efficiency, taper, repair recovery, idle cost, reserve weight, and route-split cost in YAML configuration;
- record environment, configuration hash, artifact checksums, and limitations.

**Verification**

- reproducibility checks compare deterministic semantic artifacts across two full runs;
- wheel installation and external-directory execution are part of CI;
- default reports use the reduced claim boundary;
- configuration validation covers the newly exposed assumptions.

## Loop stop decision

No remaining issue was found that materially changes the project thesis, invalidates comparisons, breaks a pipeline contract, or creates an unsafe automatic release. The following are deliberate boundaries whose next implementation step would add substantial complexity for limited portfolio value:

- no minute-level charger-to-vehicle scheduling, distribution-grid power flow, or V2G;
- no detailed repair work-order queue, parts inventory, or maintenance-cost optimization;
- no proof that the bounded corrective frontier is globally minimal;
- no claim of actual Amazon fleet data, stakeholder adoption, production deployment, or realized business impact;
- no automatic use of optional public datasets without local licensed source files;
- no mandatory GPU model in the champion path when it does not improve downstream decisions.

These boundaries are documented in generated reports and do not prevent the smoke, medium, API, dashboard-data, packaging, and CI pipelines from operating end to end.

## Loop 11: profile isolation and finite-value contracts

**Weakness found**

- `full.yaml` shared smoke output paths;
- NaN or infinity values could bypass some numeric range checks;
- differently formatted representations of the same date could evade raw-string duplicate checks.

**Resolution**

- isolate smoke, medium, and full generated-data, database, and artifact directories;
- require finite numeric configuration, generated-data, and scenario values;
- normalize parsed dates before duplicate station-day detection.

**Verification**

- tests reject overlapping paths, external destructive paths, `.nan`, infinity, and duplicate parsed dates;
- all three profile contracts load independently.

## Loop 12: semantic readiness and generated-data integrity

**Weakness found**

A self-consistent artifact manifest could be rebuilt around logically inconsistent files, and released artifacts were not cryptographically bound to their generated inputs.

**Resolution**

- add cross-artifact semantic checks for forecast selection, base-policy selection, recommendation source, plan equality, RCCE state, and automatic-release flag;
- add `generated_data_manifest.json` with size and SHA-256 records;
- make API readiness validate both manifests and semantic contracts.

**Verification**

- readiness returns 503 for hash tampering, generated-data tampering, and semantic tampering even after artifact hashes are rebuilt;
- smoke and medium each verify six generated-data files and 39 artifact files.

## Loop 13: public calibration made executable

**Weakness found**

The public-data adapter exported profiles, but the default generator did not consume them, leaving optional calibration disconnected from the main pipeline.

**Resolution**

- add an optional Amazon route-profile calibration input;
- convert cross-station package-plus-stop workload ranks into dimensionless synthetic-station multipliers;
- preserve claim boundaries by avoiding identity mapping and EV-label inference;
- record input hash, method, station counts, and factors in provenance.

**Verification**

- a small calibration fixture changes synthetic station workload distributions deterministically;
- absence of the file remains a fully offline, deterministic `NOT_FOUND` path rather than an error.

## Loop 14: concurrency, stale-state, and destructive-path safety

**Weakness found**

Concurrent writers could overwrite the same artifacts, forced termination could leave locks/staging directories, and path mutation after config loading could bypass early safety checks.

**Resolution**

- use an atomic output-directory lock;
- record PID, host, token, and start time;
- recover dead same-host owners and sufficiently stale invalid/cross-host state;
- check writable runtime paths again immediately before pipeline execution;
- restrict cleanup and runtime output to safe project-local directories.

**Verification**

- overlapping writers are rejected;
- exception paths release locks;
- forced-process remnants are reclaimed on the next run;
- project-root and external destructive paths are rejected.

## Loop 15: transactional last-known-good preservation

**Weakness found**

The previous implementation deleted the current valid outputs before a new run completed, so a mid-run error could remove the last usable plan.

**Resolution**

- execute each run under `.ev_fleet_staging/<run-id>`;
- build data, SQL marts, forecasts, optimization, simulation, RCCE, reports, and manifests in staging;
- validate the complete staged repository contract;
- promote generated data and artifacts only after validation;
- preserve previous final directories when the staged run raises an exception;
- use generated-data verification to make the API return 503 during any brief cross-directory mismatch.

**Verification**

- a synthetic failure after a partial staged write leaves the prior `run_summary.json` byte-identical;
- partial files are never promoted;
- promoted summaries contain final paths and no staging paths;
- smoke and medium complete normally through the staged path.

## Loop 16: explicit CI behavior and structure cleanup

**Weakness found**

GitHub validation depended indirectly on a pytest fixture having generated artifacts, and an unused `models/` directory remained even though model checkpoints are versioned inside profile artifacts.

**Resolution**

- make CI explicitly execute `ev-fleet run` before `ev-fleet validate`;
- add `pip check` to test and package jobs;
- remove the unused shared models directory and cleanup branch;
- retain profile-local checkpoints in artifacts.

**Verification**

- workflow YAML parses;
- local commands corresponding to lint, tests, run, validate, reproducibility, and build pass;
- package layout no longer contains an unused runtime directory.

## Loop 17: convergence decision

After the transactional promotion, generated-data integrity, semantic readiness, finite-value contracts, public calibration path, concurrency recovery, and explicit CI changes, another static and dynamic review found no remaining defect with material impact on scientific validity, release safety, local execution, or GitHub reviewability.

Remaining improvements such as two-directory power-loss transactions, Kubernetes rollout, detailed grid simulation, or maintenance-parts queues are substantial new product scopes rather than repairs to this project. That provisional v0.4.0 convergence decision was reopened by the independent v0.5.0 audit documented below.

## Loop 18: source-checkout and version consistency

**Weakness found**

The source checkout test suite depended on installed package metadata, and version values were duplicated across package initialization, API metadata, and build configuration.

**Resolution**

- move the version to `ev_fleet._version`;
- make `pyproject.toml` read that attribute dynamically;
- use the same value in API and environment reports;
- make the version test valid before and after editable installation.

**Verification**

Source-tree and installed-package paths report version 0.5.0 consistently.

## Loop 19: strict configuration semantics

**Weakness found**

String booleans, YAML booleans in numeric fields, fractional counts, nulls, NaN, and infinity could produce surprising coercion or incomplete errors.

**Resolution**

- add strict boolean and integer validators;
- reject booleans in every numeric field;
- normalize invalid numeric conversion into explicit `ValueError` messages;
- validate finite corrective quantiles and positive integer SAA sizes;
- make configuration loading side-effect free.

**Verification**

Dedicated tests reject all malformed cases and confirm that `load_config` creates no runtime directories.

## Loop 20: interrupted promotion recovery

**Weakness found**

Recovery required a backup for every preexisting directory, even when the process had crashed before moving a later final directory. Journal creation and atomic file replacement could also leave temporary files after failure.

**Resolution**

- treat `backup missing + untouched final present` as a valid pre-swap state;
- atomically write a versioned promotion journal;
- remove empty pre-journal crash directories safely;
- preserve unknown or damaged journals for manual recovery;
- clean temporary JSON, CSV, and text files on replacement failure.

**Verification**

Tests cover crashes before the first swap, after one swap, full interrupted promotion, unsupported journal versions, and rollback failure with backup preservation.

## Loop 21: project-wide advisory locking

**Weakness found**

Profile-specific locks allowed smoke and medium writers to run concurrently while recovery scanned project-wide backup and staging state. Custom stale-file reclamation also had a race between owner inspection and lock-file removal.

**Resolution**

- replace ownership-by-file-existence with cross-platform OS advisory locking;
- use one project-wide writer lock for every profile and state-changing command;
- retain the lock file only as diagnostic metadata;
- rely on the OS to release ownership on process death.

**Verification**

Nested writers, cross-profile writers, exceptions, and a subprocess terminated with `os._exit` are tested. The next writer acquires the lock without manual cleanup.

## Loop 22: reference evidence transaction and path safety

**Weakness found**

Reference export deleted the previous snapshot before replacing it and custom targets could overlap runtime, generated data, calibration data, or source evidence.

**Resolution**

- promote reference snapshots through a temporary directory and backup;
- restore the previous snapshot on replacement failure;
- reject identical, ancestor, and descendant relationships with protected runtime and evidence paths;
- continue sanitizing machine-specific project paths before hashing.

**Verification**

Failure injection preserves the prior reference snapshot, and destructive target combinations are rejected.

## Loop 23: GPU command and entry-point consolidation

**Weakness found**

Standalone GPU training wrote new files into an already signed artifact directory, making its manifest stale. Several thin scripts duplicated safer CLI functionality and could diverge from transaction and lock behavior.

**Resolution**

- make `train-gpu` rerun the complete transactional pipeline;
- return the GPU challenger and final champion from the signed run;
- remove redundant prepare/run/validate script wrappers;
- retain one CLI as the authoritative operational interface;
- make default public-data ingest share the project writer lock.

**Verification**

The GPU CLI is unit-tested against the transactional pipeline contract, and the real GPU-smoke profile completes end to end without manifest drift.

## Loop 24: protected project trees and build context

**Weakness found**

A project-local but source-controlled directory such as `src/output` could be configured as a runtime path and later removed by cleanup. Docker also lacked a context exclusion file.

**Resolution**

- protect source, tests, configs, docs, scripts, dashboard, reference evidence, constraints, Git metadata, and the data root from runtime cleanup or promotion;
- resolve symlinks before the protected-tree check;
- add `.dockerignore` for runtime artifacts, caches, build outputs, and VCS metadata;
- rename the Python-version-specific constraints filename to `constraints/runtime.txt`.

**Verification**

Pipeline and clean commands reject source-controlled runtime targets. Lint, tests, compilation, and profile execution continue to pass.

## Loop 25: CI support range

**Weakness found**

The package declared Python 3.11 through 3.13 support, but CI exercised only Python 3.11.

**Resolution**

- test Ubuntu on Python 3.11 and 3.13;
- retain Windows Python 3.11 coverage;
- use the same runtime constraints across local, Docker, test, and package jobs;
- keep the isolated wheel job on Python 3.11.

**Verification**

The workflow parses and local Python 3.13 commands pass. Hosted Python 3.11 and Windows execution remain CI-configured boundaries rather than locally fabricated claims.

## Loop 26: final convergence decision

Version 0.5.0 passes 87 tests and all local smoke, medium, GPU-smoke, validation, reproducibility, lint, format, and compilation checks. The remaining extensions are new scientific or infrastructure scopes. Further changes would have minor portfolio value relative to the risk of obscuring the central decision-aware reliability contribution.

## Loop 27: reproducibility-process isolation

**Weakness found**

Running two complete forecasting and MILP pipelines sequentially inside one Python process could intermittently delay interpreter shutdown after the correct reproducibility result was printed. The scientific result was valid, but a CI job could time out during native numerical-runtime finalization.

**Resolution**

- run each reproducibility trial in its own `python -m ev_fleet` subprocess;
- set numerical thread limits in each child environment;
- apply an explicit per-run timeout;
- compare semantic artifact hashes in the lightweight parent process.

**Verification**

The isolated reproducibility command exits normally with 15 semantic artifacts checked and zero mismatches.

## Loop 28: clean-release artifact verification

**Weakness found**

A source tree can pass after editable installation while a reviewer-facing clean checkout or the exact final wheel still fails because of missing package data, stale metadata, or accidental dependence on runtime files.

**Resolution**

- construct the release directory from an explicit source/evidence allowlist;
- remove runtime artifacts, generated data, caches, locks, build outputs, and packaging metadata;
- retain only empty canonical runtime roots and checksummed reference evidence;
- test an exact clone of the clean release before archiving;
- rebuild wheel and source distribution from that release and reinstall the exact wheel into the isolated environment.

**Verification**

The package reports version 0.5.0 from source with distribution metadata absent, all 87 tests pass, lint/format/compilation pass, and the exact clean checkout completes smoke execution and repository validation. The rebuilt wheel passes `pip check`, contains packaged SQL resources, reports matching module and metadata versions, and completes smoke execution plus validation from a working directory outside the repository.

## Loop 29: numerical solver release contract

**Weakness found**

A solver incumbent could be labeled feasible without independently checking linear, bound, integrality, and objective-reconstruction residuals. Termination categories were also too coarse.

**Resolution**

- normalize HiGHS termination into optimal, limit-reached, infeasible, unbounded, error, and invalid-solution classes;
- compute bound, constraint, integrality, and objective-reconciliation diagnostics;
- accept incumbents only for allowed termination classes;
- degrade invalid incumbents to fallback;
- hard-block numerical failures in RCCE.

**Verification**

Small MILPs, monkeypatched invalid solutions, infeasible states, status/termination contradictions, and real smoke/medium plans all pass the expected contracts.

## Loop 30: joint expected-value route profiles

**Weakness found**

Independent quantiles for cube, distance, and energy could create a synthetic expected route that never appeared jointly.

**Resolution**

Select complete observed route profiles at weighted severity levels, preserving joint physical structure.

**Verification**

Expected profiles are members of the observed profile set, and smoke/medium MILPs remain optimal.

## Loop 31: rolling and paired-key integrity

**Weakness found**

Inner joins could silently compare only a subset when scenario/run keys differed, and rolling fleet conservation needed explicit evidence.

**Resolution**

Require identical paired keys, nonempty scenario sets, and zero rolling fleet-balance difference.

**Verification**

Missing-key tests fail loudly; valid rolling comparisons preserve total fleet.

## Loop 32: run-bound release artifacts

**Weakness found**

Manifest hashes prevented tampering but did not explicitly prove that summary, generated data, artifacts, and API reads belonged to the same promoted run.

**Resolution**

Assign a UUID run ID to every staged execution, bind both manifests and summary to it, validate cross-artifact identity, and recheck API reads after access.

**Verification**

Manifest run-ID mismatch and mid-request promotion simulations return HTTP 503.

## Loop 33: structural infeasibility diagnostics

**Weakness found**

Fallback explained that optimization failed but gave limited operational guidance.

**Resolution**

Add approximate structural diagnostics for impossible routes, vehicle lower-bound shortage, charging deficit, and parking excess, without claiming exact IIS minimality.

**Verification**

An intentionally impossible small route produces the expected diagnostic metadata and remains blocked.

## Loop 34: split-conformal calibration

**Weakness found**

P90 forecasts were evaluated for coverage but not calibrated on a dedicated pre-validation period.

**Resolution**

Use chronological fit/calibration/validation partitions and one-sided conformal corrections for classical and Transformer candidates. Apply the configured target coverage in both calibration metadata and model ranking.

**Verification**

Calibration coverage never decreases, corrections remain nonnegative, nondefault target tests pass, and GPU-smoke enters the common decision gate with calibrated intervals.

## Loop 35: semantic reproducibility after runtime diagnostics

**Weakness found**

Adding solver runtime to RCCE made otherwise identical runs hash differently.

**Resolution**

Canonical semantic hashing now removes all wall-clock runtime fields by naming rule while retaining scientific thresholds, decisions, residuals, and costs.

**Verification**

Two smoke runs match across 15 semantic artifacts; runtime-only changes hash identically, while a shortage-CVaR change does not.

## Loop 36: override consistency

**Weakness found**

A negative override could reduce planned fleet without withdrawing temporary additions, and override candidates could lose the inherited solver-release contract.

**Resolution**

Permit negative deltas only within existing temporary additions, adjust temporary count and lease cost together, preserve verified solver evidence, and replay independently.

**Verification**

Valid temporary withdrawal is balanced; permanent-fleet removal is rejected; override RCCE uses the same solver gate.

## Loop 37: solver evidence in stakeholder reports

**Weakness found**

Solver residuals affected RCCE but were not visible in the executive memo or model card.

**Resolution**

Expose termination class, MIP gap, maximum constraint/integrality residual, and objective reconciliation in reports and run summary.

**Verification**

Generated smoke reports contain the release evidence and the recommended residuals are within tolerance.

## Loop 38: source and installed version evidence

**Weakness found**

A stale editable distribution could override the authoritative source version in environment metadata.

**Resolution**

Use `_version.py` as the execution version and record installed distribution metadata separately.

**Verification**

A stale-metadata unit test reports the source version as authoritative while exposing the mismatch diagnostically.

## Loop 39: v0.6.0 convergence decision

Version 0.6.0 passes 103 tests plus lint, format, compilation, smoke, medium, GPU-smoke, validation, and semantic reproducibility. The remaining untested branches are primarily unavailable optional dependencies, OS-specific exception handling, or malformed-input defenses. Further modeling additions would be new scope with minor incremental portfolio value relative to the current evidence.

## Loop 40: exact v0.6.0 release artifact verification

**Weakness found**

Editable source success does not prove that the reviewer-facing clean directory or the exact rebuilt wheel contains every required resource and no accidental runtime dependency.

**Resolution**

- build the release from an explicit source/evidence allowlist;
- retain empty canonical runtime roots only;
- remove caches, locks, databases, checkpoints, build output, and packaging metadata;
- test source with project distribution metadata absent;
- rebuild wheel/sdist from the exact release;
- install the exact wheel into a separate environment and run outside the repository.

**Verification**

The clean source passes 103 tests, lint, format, compilation, CLI, smoke, and validation. The exact wheel passes `pip check`, reports matching 0.6.0 module/distribution versions, exposes packaged SQL, and completes external-working-directory smoke and validation.

## Loop 41: complete-directory manifest integrity

**Weakness found**

Manifest verification proved listed files but did not reject unlisted files. A stale or manually added file could coexist with a valid manifest, and symlinks could introduce content outside the signed directory.

**Resolution**

- add explicit manifest schema version 1;
- compare the complete actual file inventory with manifest records;
- reject untracked files and duplicate paths;
- reject symlinks in artifact and generated-data directories;
- preserve run-ID, size, and SHA-256 verification.

**Verification**

Pipeline, medium, and GPU checkpoint directories validate successfully. Added artifact/generated files, unsupported schema versions, traversal paths, and symlinks fail loudly.

## Loop 42: manual override governance

**Weakness found**

A manually modified allocation inherited the base plan's solver evidence and could return RCCE `RELEASE`, even though it was never re-optimized and was intended only as a non-persistent impact simulation.

**Resolution**

- mark inherited solver evidence as `base_plan_only`;
- require explicit planner approval for every override;
- force a successful override replay to remain `REVIEW` rather than automatic `RELEASE`;
- keep parking, fleet balance, temporary-capacity, and independent replay checks.

**Verification**

Real smoke override replay returns HTTP 200, RCCE `REVIEW`, `approval_required: true`, failed `manual_override_approval`, and no persistence.

## Loop 43: request configuration snapshot

**Weakness found**

API readiness validated one resolved configuration, then reloaded the configuration before reading the artifact. A concurrent config edit could redirect the second read.

**Resolution**

Return the validated `ProjectConfig` with readiness context and reuse that exact snapshot throughout the request. Run ID is still checked after the artifact read.

**Verification**

An integration test makes a second configuration load fail; `/plans/latest` succeeds with exactly one load.

## Loop 44: dashboard and reference release contract

**Weakness found**

The API enforced hashes and semantics, but dashboard reads and compact reference export used weaker subsets of the release contract.

**Resolution**

- dashboard verifies artifact manifest, run ID, cross-artifact policy/forecast/RCCE semantics, and post-read run stability;
- reference export requires full repository validation, including generated-data hashes, SQL/data contracts, and cross-artifact semantics.

**Verification**

Tampered files, rehashed policy contradictions, and semantically inconsistent reference candidates are rejected.

## Loop 45: data-tree destructive-path restriction

**Weakness found**

The prior safety rule allowed any descendant of `data/`, so artifacts or generated output could be pointed at raw or calibration-like directories and later removed by clean or promotion.

**Resolution**

Only paths explicitly used as generated data and named `generated*` may be modified under `data/`. Artifact directories under `data/`, arbitrary data descendants, the data root, and all protected source/evidence paths are rejected.

**Verification**

Small configuration tests reject `data/artifacts` and `data/raw-runtime`, while all standard profiles remain runnable.

## Loop 46: v0.7.0 convergence decision

Version 0.7.0 passes 110 tests, 85% source coverage, lint, format, compilation, smoke, medium, GPU-smoke, strict validation, semantic reproducibility, dashboard governance, and reference export. Remaining coverage gaps are optional GPU import absence, OS-specific branches, and deliberately injected recovery failures. Further changes would be new domain or infrastructure scope with minor incremental portfolio value.

## Loop 47: exact v0.7.0 release and wheel verification

**Weakness found**

Passing an editable/source environment does not prove that the exact reviewer-facing clean directory and its wheel contain every resource or run correctly outside the repository. An interrupted dependency installation also showed that `pip check` alone can miss partially copied package files when metadata is present.

**Resolution**

- construct the final release directory from an explicit source/evidence allowlist;
- exclude runtime artifacts, databases, GPU checkpoints, caches, locks, build outputs, and package metadata;
- test the exact clean source with project distribution metadata absent;
- build wheel and source distribution from a clone of that exact release;
- verify imports of the binary scientific dependencies in addition to `pip check`;
- verify module/distribution version parity and packaged SQL resources;
- execute the exact wheel from a working directory outside the repository.

**Verification**

The exact clean source passes 110 tests, lint, format, compilation, module CLI, smoke generation, and repository validation. The rebuilt wheel reports version 0.7.0 from both module and distribution metadata, loads NumPy/Pandas/SciPy/scikit-learn and packaged SQL, passes `pip check`, and completes external-working-directory smoke execution plus validation.

## Loop 48: source-only subprocess portability

**Weakness found**

A clean source checkout passed 109 tests but the child process in the crash-lock test could not import `ev_fleet`. Editable installation hid the failure.

**Resolution**

The test prepends the repository `src/` directory to the child environment. GitHub Actions now includes a dependency-only source-checkout job.

**Verification**

The targeted child-process test and full source-only suite pass.

## Loop 49: public-data CLI no-op prevention

**Weakness found**

The adapter could return success with no source and silently ignore a one-sided Amazon input pair.

**Resolution**

No-source and incomplete Amazon pairs are rejected with argparse exit code 2 and explicit messages.

**Verification**

Subprocess tests cover both invalid invocations; existing positive adapter tests remain green.

## Loop 50: release-candidate evidence handoff

**Weakness found**

Validation evidence existed in prose but was not tied to source, dependency manifests, command outcomes, and build artifact hashes in one machine-readable file.

**Resolution**

A handoff builder validates required evidence, computes source/dependency/artifact hashes, and emits `RELEASE_CANDIDATE_ONLY` metadata. Improvement and limitations documents were added.

**Verification**

Builder tests validate stable source fingerprinting, artifact hashes, and failure on incomplete evidence or missing artifacts. The final manifest is generated from exact release artifacts.

## Loop 51: convergence decision

After the three candidate-focused rounds, repeated analysis found no additional locally executable Critical or High correctness defect. Remaining gaps require hosted CI, Docker, Windows, CUDA, external data, or new product scope. Further code changes before qualification would add complexity with lower expected value.

## Loop 52: packaged handoff entrypoint

**Weakness found**

The repository script for handoff generation was not included in the source distribution, so installed and source-tree users did not share one supported interface.

**Resolution**

The handoff builder is exposed through the packaged `ev-fleet handoff` command, and the redundant wrapper script was removed.

**Verification**

The exact wheel/sdist include `ev_fleet.release_handoff`, installed CLI help lists `handoff`, and targeted CLI tests pass.

## Loop 53: candidate convergence

Version 0.7.1rc1 passes 114 tests with 85% source coverage, source-only execution, smoke/medium/GPU-smoke, API service smoke, semantic reproducibility, wheel/sdist build, and external-wheel validation. Remaining gaps require hosted CI, Docker, Windows, CUDA, full-scale compute, or new product scope.
