# Implementation Map

| Evidence category | Implemented evidence |
|---|---|
| SQL and data fundamentals | packaged SQLite staging, past-only rolling features, lagged workload marts, capacity mart, fleet reconciliation, allowlisted reads |
| Data quality | schema, positivity, uniqueness, complete station/vehicle grid, parking, scenario-weight, route-ID, and artifact contracts |
| Statistical fundamentals | seasonal naive, Poisson, chronological fit/calibration/validation, split-conformal upper intervals, pinball loss, coverage, Wilson/bootstrap confidence bounds |
| Classical ML | quantile gradient boosting and optional lazy-loaded LightGBM |
| GPU advanced ML | optional temporal quantile Transformer with pre-calibration normalization, conformal upper-interval correction, CUDA auto-detection, and the same decision gate as classical models |
| Forecast-to-decision coupling | active-fleet commitment, station parking, physical redeployment, idle/shortage/movement cost, service rate, and shortage CVaR replay |
| OR fundamentals | joint-profile expected-value workload, fleet conservation, parking, redeployment, route assignment, emergency recourse, repair-adjusted availability, charge acceptance, aggregate charging constraints |
| Advanced OR | risk-neutral stochastic MILP, risk-aware CVaR MILP, exact fixed-allocation recourse VSS/EVPI, SAA, rolling reoptimization |
| Scientific validity | common random numbers, identical paired scenario/run keys, paired bootstrap, identical cost systems, separate formal and held-out stochastic-value estimates |
| Operational realism | cube/range compatibility, bounded emergency lease, uncertain lease fulfillment, failures, repair-bay recovery, late returns, charger derates, taper, mileage cost |
| Reliability specialty | RCCE vector gate, lower confidence bounds, solver termination/residual/objective-reconciliation gates, false-feasibility protection, bounded joint vehicle/charging corrective frontier |
| Safe operation | degraded fallback cannot release, parking/fleet-balance checks, strict override validation, approval-required independent override replay, automatic-release flag, transactional last-known-good promotion |
| Deployment | CLI/module entry point, run-bound FastAPI reads, manifest- and semantic-validated dashboard data contract, staged promotion, schema-versioned complete-inventory run-ID artifact/generated-data manifests, Docker Compose ordering, installed-package and source-checkout GitHub Actions |
| Reproducibility | seeded generation, configuration hash, semantic artifact hashes, direct-dependency constraints, source-only tests, wheel/external-CWD verification, checksummed release-candidate handoff |
| Stakeholder communication | executive memo, model/optimization card, planner moves, corrective actions, paired decision evidence, limitations |
