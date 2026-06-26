# Model and Optimization Card

## Forecast ladder

| model | mae | worst_station_mae | pinball_p90 | p90_coverage | p90_calibration_error | mean_interval_width | conformal_correction | calibration_samples | calibration_coverage_before | calibration_coverage_after | target_coverage | decision_cost | decision_service_rate | decision_shortage_cvar90 | decision_redeployment_count | decision_redeployment_cost | decision_active_fleet | calibration_gate | selected |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| poisson | 6.80949 | 8.32817 | 1.74687 | 0.904762 | 0.0047619 | 17.4906 | 4.40581 | 42 | 0.857143 | 0.952381 | 0.9 | 6337.5 | 0.941854 | 11.5 | 0.0714286 | 6.42857 | 73.8571 | True | True |
| quantile_boosting | 7.63201 | 8.7772 | 1.97018 | 0.97619 | 0.0761905 | 21.3849 | 10.2431 | 42 | 0.809524 | 0.952381 | 0.9 | 6661.07 | 0.941412 | 11 | 0.285714 | 25.7143 | 76.3571 | True | False |
| seasonal_naive | 9.7381 | 11.1429 | 2.55667 | 0.97619 | 0.0761905 | 23.9 | 0 | 42 | 0.952381 | 0.952381 | 0.9 | 7911.43 | 0.937879 | 14 | 5.07143 | 456.429 | 86 | True | False |

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

- Formal sample VSS: 288.360
- Formal sample EVPI: 1,264.926
- Held-out empirical VSS: -889.998
- Held-out EVPI estimate: 3,513.765

Formal quantities use the same optimization sample and a risk-neutral policy. Held-out VSS measures generalization and may be negative without violating the stochastic-programming inequality.

## Solver release evidence

| policy | plan_status | termination | mip_gap | max_constraint_violation | max_integrality_violation | objective_reconciliation_error |
| --- | --- | --- | --- | --- | --- | --- |
| deterministic | OPTIMAL | OPTIMAL | 0 | 0 | 1.95153e-14 | 0 |
| risk_neutral | OPTIMAL | OPTIMAL | 0.00139317 | 0 | 0 | 1.15388e-16 |
| risk_aware | OPTIMAL | OPTIMAL | 0.00277545 | 0 | 6.59384e-12 | 0 |
| recommended | CORRECTIVE_CANDIDATE | OPTIMAL | 0.00139317 | 0 | 0 | 1.15388e-16 |

An incumbent is release-eligible only when its termination class is allowed and its bound, linear-constraint, integrality, and objective-reconciliation residuals remain within configured tolerances. Solver feasibility is necessary but not sufficient; RCCE independently evaluates replay reliability.

## Paired policy comparison

| comparison | paired_runs | mean_cost_savings | cost_savings_ci95_low | cost_savings_ci95_high | mean_service_gain | service_gain_ci95_low | service_gain_ci95_high | mean_shortage_reduction | shortage_reduction_ci95_low | shortage_reduction_ci95_high | challenger_win_rate_cost |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| deterministic_to_risk_neutral | 120 | 19.9492 | -225.105 | 256.321 | -0.00105305 | -0.00426962 | 0.00217713 | -0.0583333 | -0.25 | 0.141667 | 0.375 |
| risk_neutral_to_risk_aware | 120 | -724.761 | -840.814 | -611.055 | -0.00995255 | -0.0117037 | -0.00838607 | -0.575 | -0.658333 | -0.491667 | 0.025 |
| risk_neutral_to_corrective | 120 | -8281.03 | -8758.14 | -7796.09 | 0.0347208 | 0.029957 | 0.0398876 | 2.45833 | 2 | 2.925 | 0.00833333 |

## Corrective frontier

| candidate | shortage_quantile | round | temporary_additions | temporary_charging_capacity_kwh | status | failed_checks | service_coverage_lcb | charging_feasibility_lcb | shortage_cvar95 | mean_realized_total_cost | mean_emergency_leases | route_split_factor | triggered |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| stochastic_risk_neutral_resilience_q75_r1 | 0.75 | 1 | 19 | 668 | BLOCK | 2 | 0.97733 | 0.784804 | 8.5 | 20834.8 | 1.6 | 0.936544 | True |
| stochastic_risk_neutral_resilience_q95_r1 | 0.95 | 1 | 30 | 668 | BLOCK | 3 | 0.989788 | 0.747241 | 3.42857 | 26024.8 | 1.08333 | 0.936544 | True |
| stochastic_risk_neutral_resilience_q95_r2 | 0.95 | 2 | 31 | 1428 | BLOCK | 4 | 0.989788 | 0.775323 | 3.42857 | 27322.7 | 1.05833 | 0.936544 | True |
| stochastic_risk_neutral_resilience_q95_r3 | 0.95 | 3 | 32 | 1440 | BLOCK | 4 | 0.989788 | 0.775323 | 3.42857 | 27875.2 | 1.05833 | 0.936544 | True |
| stochastic_risk_neutral_resilience_q90_r1 | 0.9 | 1 | 29 | 668 | BLOCK | 4 | 0.988602 | 0.747241 | 4.5 | 25715.9 | 1.41667 | 0.936544 | True |
| stochastic_risk_neutral_resilience_q75_r2 | 0.75 | 2 | 25 | 1428 | BLOCK | 4 | 0.982432 | 0.823445 | 6.2381 | 24381.8 | 1.00833 | 0.936544 | True |
| stochastic_risk_neutral_resilience_q75_r3 | 0.75 | 3 | 28 | 1528 | BLOCK | 4 | 0.982432 | 0.823445 | 6.2381 | 26140 | 1.00833 | 0.936544 | True |
| stochastic_risk_neutral_resilience_q90_r2 | 0.9 | 2 | 30 | 1253 | BLOCK | 5 | 0.988602 | 0.775323 | 4.5 | 26745.5 | 1.39167 | 0.936544 | True |
| stochastic_risk_neutral_resilience_q90_r3 | 0.9 | 3 | 31 | 1266 | BLOCK | 5 | 0.988602 | 0.775323 | 4.5 | 27299.7 | 1.39167 | 0.936544 | True |

## Reliability specialty

RCCE tracks service coverage, critical coverage, range/energy feasibility, individual charge acceptance, aggregate depot charging feasibility, shortage CVaR, overcapacity cost, allocation churn, and solver degradation as a vector gate rather than a single opaque score. Repair-bay capacity and probabilistic same-day repair success affect replay availability and are reported separately.

## Corrective-action claim boundary

Corrective candidates are triggered only when the selected base policy does not pass RCCE. They are derived from station-level shortage and charging-overage quantiles observed in independent replay and allocated under parking and temporary-charging limits. They are compared as a small reliability/cost frontier. Exact global minimality is not claimed.

## SAA runs

| scenario_size | replication | in_sample_objective | out_of_sample_cost | out_of_sample_service_rate | out_of_sample_shortage_cvar95 | status | mip_gap | allocation_churn |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2 | 0 | 9356.88 | 7970.54 | 0.951303 | 16 | OPTIMAL | 0.0148416 | 2 |
| 2 | 1 | 12221.4 | 12670 | 0.906607 | 17 | OPTIMAL | 0 | 10 |
| 3 | 0 | 4759.01 | 7135.64 | 0.959142 | 15 | OPTIMAL | 0 | 0 |
| 3 | 1 | 7056.96 | 9522.56 | 0.935055 | 19 | OPTIMAL | 0.0279554 | 5 |
