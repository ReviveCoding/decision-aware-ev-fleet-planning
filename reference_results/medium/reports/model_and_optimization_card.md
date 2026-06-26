# Model and Optimization Card

## Forecast ladder

| model | mae | worst_station_mae | pinball_p90 | p90_coverage | p90_calibration_error | mean_interval_width | conformal_correction | calibration_samples | calibration_coverage_before | calibration_coverage_after | target_coverage | decision_cost | decision_service_rate | decision_shortage_cvar90 | decision_redeployment_count | decision_redeployment_cost | decision_active_fleet | calibration_gate | selected |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| poisson | 8.90292 | 12.4371 | 2.36801 | 0.885714 | 0.0142857 | 17.0569 | 0 | 70 | 0.942857 | 0.942857 | 0.9 | 17711.1 | 0.912038 | 34.3333 | 0.0714286 | 6.42857 | 144.571 | True | True |
| seasonal_naive | 12.8429 | 19.6071 | 3.08857 | 0.942857 | 0.0428571 | 26.1 | 0 | 70 | 0.942857 | 0.942857 | 0.9 | 18052.5 | 0.929619 | 34.3333 | 12.0357 | 1083.21 | 173.714 | True | False |
| quantile_boosting | 9.15587 | 13.131 | 2.41712 | 0.85 | 0.05 | 17.3245 | 1.81503 | 70 | 0.885714 | 0.928571 | 0.9 | 20998.4 | 0.887922 | 45.3333 | 0.785714 | 70.7143 | 133.357 | True | False |

## Optimization ladder

1. Carry-forward and proportional business baselines
2. Deterministic MILP built from a weighted expected-value workload
3. Risk-neutral two-stage scenario MILP for formal VSS/EVPI analysis
4. Risk-aware scenario MILP with CVaR tail-risk penalty
5. Independent held-out exact-recourse and Monte Carlo evaluation
6. Reliability-based base-policy selection rather than automatic complexity promotion
7. RCCE-triggered temporary-capacity frontier
8. Compound-disruption rolling reoptimization
9. SAA out-of-sample stability analysis and degraded-mode fallback

## Stochastic-value interpretation

- Formal sample VSS: 1,137.593
- Formal sample EVPI: 1,678.762
- Held-out empirical VSS: -69.329
- Held-out EVPI estimate: 5,354.311

Formal quantities use the same optimization sample and a risk-neutral policy. Held-out VSS measures generalization and may be negative without violating the stochastic-programming inequality.

## Solver release evidence

| policy | plan_status | termination | mip_gap | max_constraint_violation | max_integrality_violation | objective_reconciliation_error |
| --- | --- | --- | --- | --- | --- | --- |
| deterministic | OPTIMAL | OPTIMAL | 0 | 0 | 0 | 0 |
| risk_neutral | OPTIMAL | OPTIMAL | 0 | 1.81899e-12 | 0 | 1.48823e-16 |
| risk_aware | OPTIMAL | OPTIMAL | 0 | 4.08562e-14 | 1.6076e-13 | 0 |
| recommended | CORRECTIVE_CANDIDATE | OPTIMAL | 0 | 0 | 0 | 0 |

An incumbent is release-eligible only when its termination class is allowed and its bound, linear-constraint, integrality, and objective-reconciliation residuals remain within configured tolerances. Solver feasibility is necessary but not sufficient; RCCE independently evaluates replay reliability.

## Paired policy comparison

| comparison | paired_runs | mean_cost_savings | cost_savings_ci95_low | cost_savings_ci95_high | mean_service_gain | service_gain_ci95_low | service_gain_ci95_high | mean_shortage_reduction | shortage_reduction_ci95_low | shortage_reduction_ci95_high | challenger_win_rate_cost |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| deterministic_to_risk_neutral | 240 | 125.061 | -93.9476 | 359.657 | 0.00420338 | 0.00259745 | 0.00580945 | 0.429167 | 0.266667 | 0.591667 | 0.425 |
| risk_neutral_to_risk_aware | 240 | -92.8608 | -98.1588 | -88.7158 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| deterministic_to_corrective | 240 | -11891.1 | -12253.4 | -11517.7 | 0.0228799 | 0.020401 | 0.0254086 | 2.2625 | 2.0125 | 2.50844 | 0 |

## Corrective frontier

| candidate | shortage_quantile | round | temporary_additions | temporary_charging_capacity_kwh | status | failed_checks | service_coverage_lcb | charging_feasibility_lcb | shortage_cvar95 | mean_realized_total_cost | mean_emergency_leases | route_split_factor | triggered |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| deterministic_expected_value_resilience_q95_r2 | 0.95 | 2 | 28 | 0 | RELEASE | 0 | 0.99342 | 0.952166 | 2.25 | 28658.8 | 2.02083 | 0.91 | True |
| deterministic_expected_value_resilience_q95_r1 | 0.95 | 1 | 21 | 0 | REVIEW | 1 | 0.989871 | 0.952166 | 3.26087 | 25476.1 | 2.30833 | 0.91 | True |
| deterministic_expected_value_resilience_q90_r3 | 0.9 | 3 | 22 | 0 | REVIEW | 1 | 0.991479 | 0.952166 | 3.45 | 25660.3 | 2.0375 | 0.91 | True |
| deterministic_expected_value_resilience_q75_r3 | 0.75 | 3 | 20 | 0 | REVIEW | 1 | 0.985822 | 0.952166 | 4.55 | 25420.2 | 2.4125 | 0.91 | True |
| deterministic_expected_value_resilience_q90_r2 | 0.9 | 2 | 19 | 0 | REVIEW | 1 | 0.990406 | 0.952166 | 4.75 | 24157.5 | 2.0375 | 0.91 | True |
| deterministic_expected_value_resilience_q75_r2 | 0.75 | 2 | 16 | 0 | REVIEW | 1 | 0.984779 | 0.952166 | 5.64286 | 23384.3 | 2.4125 | 0.91 | True |
| deterministic_expected_value_resilience_q90_r1 | 0.9 | 1 | 13 | 0 | REVIEW | 1 | 0.985962 | 0.952166 | 5.75 | 21608.6 | 2.325 | 0.91 | True |
| deterministic_expected_value_resilience_q75_r1 | 0.75 | 1 | 10 | 0 | REVIEW | 1 | 0.980558 | 0.952166 | 6.75 | 20769.5 | 2.6 | 0.91 | True |

## Reliability specialty

RCCE tracks service coverage, critical coverage, range/energy feasibility, individual charge acceptance, aggregate depot charging feasibility, shortage CVaR, overcapacity cost, allocation churn, and solver degradation as a vector gate rather than a single opaque score. Repair-bay capacity and probabilistic same-day repair success affect replay availability and are reported separately.

## Corrective-action claim boundary

Corrective candidates are triggered only when the selected base policy does not pass RCCE. They are derived from station-level shortage and charging-overage quantiles observed in independent replay and allocated under parking and temporary-charging limits. They are compared as a small reliability/cost frontier. Exact global minimality is not claimed.

## SAA runs

| scenario_size | replication | in_sample_objective | out_of_sample_cost | out_of_sample_service_rate | out_of_sample_shortage_cvar95 | status | mip_gap | allocation_churn |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 3 | 0 | 4553.51 | 8030.99 | 0.971356 | 13 | OPTIMAL | 0 | 3 |
| 3 | 1 | 7304.44 | 11041.5 | 0.951431 | 18 | OPTIMAL | 0 | 9 |
| 5 | 0 | 7239.3 | 7955.01 | 0.972751 | 12 | OPTIMAL | 0 | 2 |
| 5 | 1 | 6776.05 | 8029.19 | 0.973482 | 12 | OPTIMAL | 0.00225835 | 6 |
