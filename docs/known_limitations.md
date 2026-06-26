# Known Limitations — 0.7.1rc4

## Release blockers

None identified in the locally executable candidate gate.

## Recommended follow-up

1. **GitHub-hosted execution is not locally provable.** Ubuntu Python 3.11/3.13 and Windows Python 3.11 jobs are configured but require an actual GitHub run for E4 evidence.
2. **Docker is configuration-only in this environment.** No Docker CLI is available locally; image build, Compose startup, volume permissions, and container health must be exercised in the dedicated qualification stage.
3. **CUDA is not validated here.** The GPU challenger completed through the common decision gate on CPU. RTX/CUDA compatibility and performance remain unverified.
4. **Static type checking is not configured.** Ruff, tests, runtime contracts, and Pydantic cover many errors, but mypy/pyright is an optional follow-up rather than a candidate blocker.
5. **The full research profile was not executed.** Smoke, medium, and one-epoch GPU-smoke were executed; the large full profile remains hardware- and solver-budget dependent.

## Optional enhancements

- minute-level charger-to-vehicle scheduling;
- electrical distribution-grid constraints and V2G;
- exact IIS or globally minimal corrective-capacity proof;
- detailed maintenance-parts/work-order queues;
- durable authenticated multi-user override persistence;
- cloud rollout, monitoring integration, and production A/B evaluation.

## Claim boundaries

- All bundled operational data are synthetic or public-data-informed proxy data.
- Public Amazon/NLR adapters do not imply access to Amazon EV fleet, charger, DSP, cost, repair, or production decision data.
- Synthetic validation demonstrates correctness and integration, not production business impact.
- A CPU GPU-smoke run does not establish CUDA performance.
- The handoff status is `RELEASE_CANDIDATE_ONLY`, not `RELEASE QUALIFIED`.
