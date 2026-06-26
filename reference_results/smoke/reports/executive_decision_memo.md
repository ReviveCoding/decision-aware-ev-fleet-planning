# Executive Decision Memo

## Recommendation

Use **stochastic_risk_neutral_resilience_q75_r1** as the current decision candidate. Its RCCE status is **BLOCK**. Automatic release is not permitted; planner review is required.
The selected base policy was **risk_neutral**, and the selected recommendation source was **corrective_capacity_candidate**.

## Decision evidence

- Selected forecast model: `poisson` after calibration gating and station-capacity-aware active-fleet decision replay.
- Selected-model P90 coverage: 0.905
- Selected-model replay cost: 6,337.50
- Selected-model active-fleet commitment: 73.86 vehicles/day
- Selected-model shortage CVaR90: 11.500 routes
- Deterministic expected-value objective: 8,194.16
- Risk-neutral stochastic objective: 7,882.08
- Risk-aware CVaR objective: 12,797.81
- Recommended solver termination: `OPTIMAL`
- Recommended solver MIP gap: 0.00139
- Recommended max linear-constraint violation: 0
- Recommended max integrality violation: 0
- Recommended objective reconciliation error: 1.15e-16
- Formal sample VSS: 288.36
- Formal sample EVPI: 1,264.93
- Held-out empirical VSS: -890.00
- Held-out EVPI estimate: 3,513.77
- Risk-aware service lower bound: 0.928
- Corrective service lower bound: 0.977
- Corrective shortage CVaR95: 8.500
- Corrective fixed capacity/program cost: 10,486.00
- Mean failed vehicles in replay: 4.050
- Mean same-day repaired vehicles in replay: 2.083
- P95 individual charge-acceptance ratio: 0.271
- Rolling reoptimization expected cost savings under compound disruption: 5,981.27
- Rolling reoptimization service-rate gain: 0.1077

## Planned redeployment

- Move 1 `delivery_ev` vehicle(s) from STN_02 to STN_01.
- Move 1 `delivery_ev` vehicle(s) from STN_02 to STN_03.
- Move 2 `large_ev` vehicle(s) from STN_03 to STN_01.

## Corrective action assessment

- Add 1 temporary `compact_ev` vehicle(s) at STN_01 based on the 75% station-shortage quantile in corrective round 1.
- Add 2 temporary `delivery_ev` vehicle(s) at STN_01 based on the 75% station-shortage quantile in corrective round 1.
- Add 1 temporary `compact_ev` vehicle(s) at STN_02 based on the 75% station-shortage quantile in corrective round 1.
- Add 4 temporary `delivery_ev` vehicle(s) at STN_02 based on the 75% station-shortage quantile in corrective round 1.
- Add 3 temporary `large_ev` vehicle(s) at STN_02 based on the 75% station-shortage quantile in corrective round 1.
- Add 1 temporary `compact_ev` vehicle(s) at STN_03 based on the 75% station-shortage quantile in corrective round 1.
- Add 4 temporary `delivery_ev` vehicle(s) at STN_03 based on the 75% station-shortage quantile in corrective round 1.
- Add 3 temporary `large_ev` vehicle(s) at STN_03 based on the 75% station-shortage quantile in corrective round 1.
- Add 163.0 kWh/day of temporary overnight charging capacity or managed charging-window extension at STN_01 in corrective round 1.
- Add 6.0 kWh/day of temporary overnight charging capacity or managed charging-window extension at STN_02 in corrective round 1.
- Add 499.0 kWh/day of temporary overnight charging capacity or managed charging-window extension at STN_03 in corrective round 1.
- Apply a route-split or planned mid-route charging control with effective requirement factor 0.937 in corrective round 1.

## Release interpretation

The system separates nominal solver feasibility from empirical reliability under an independently parameterized replay. Policies are compared with common exogenous random draws. Advanced policies are not promoted solely because they are more complex; base-policy selection uses reliability status, failed gates, tail shortage, and realized cost.

## Claim boundary

This is an offline, public-data-informed synthetic decision-support prototype. It does not use actual Amazon EV fleet, charging, repair, DSP allocation, or production cost records.
