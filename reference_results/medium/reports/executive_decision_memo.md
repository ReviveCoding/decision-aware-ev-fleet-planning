# Executive Decision Memo

## Recommendation

Use **deterministic_expected_value_resilience_q95_r2** as the current decision candidate. Its RCCE status is **RELEASE**. Automatic release is permitted by the RCCE gate.
The selected base policy was **deterministic**, and the selected recommendation source was **corrective_capacity_candidate**.

## Decision evidence

- Selected forecast model: `poisson` after calibration gating and station-capacity-aware active-fleet decision replay.
- Selected-model P90 coverage: 0.886
- Selected-model replay cost: 17,711.07
- Selected-model active-fleet commitment: 144.57 vehicles/day
- Selected-model shortage CVaR90: 34.333 routes
- Deterministic expected-value objective: 4,338.68
- Risk-neutral stochastic objective: 6,111.23
- Risk-aware CVaR objective: 9,257.37
- Recommended solver termination: `OPTIMAL`
- Recommended solver MIP gap: 0
- Recommended max linear-constraint violation: 0
- Recommended max integrality violation: 0
- Recommended objective reconciliation error: 0
- Formal sample VSS: 1,137.59
- Formal sample EVPI: 1,678.76
- Held-out empirical VSS: -69.33
- Held-out EVPI estimate: 5,354.31
- Risk-aware service lower bound: 0.973
- Corrective service lower bound: 0.993
- Corrective shortage CVaR95: 2.250
- Corrective fixed capacity/program cost: 13,200.00
- Mean failed vehicles in replay: 6.154
- Mean same-day repaired vehicles in replay: 3.108
- P95 individual charge-acceptance ratio: 0.242
- Rolling reoptimization expected cost savings under compound disruption: 2,573.32
- Rolling reoptimization service-rate gain: 0.0254

## Planned redeployment

- Move 3 `large_ev` vehicle(s) from STN_04 to STN_01.
- Move 1 `large_ev` vehicle(s) from STN_05 to STN_01.

## Corrective action assessment

- Add 1 temporary `compact_ev` vehicle(s) at STN_01 based on the 95% station-shortage quantile in corrective round 1.
- Add 5 temporary `delivery_ev` vehicle(s) at STN_01 based on the 95% station-shortage quantile in corrective round 1.
- Add 2 temporary `large_ev` vehicle(s) at STN_01 based on the 95% station-shortage quantile in corrective round 1.
- Add 1 temporary `compact_ev` vehicle(s) at STN_02 based on the 95% station-shortage quantile in corrective round 1.
- Add 2 temporary `delivery_ev` vehicle(s) at STN_02 based on the 95% station-shortage quantile in corrective round 1.
- Add 1 temporary `large_ev` vehicle(s) at STN_02 based on the 95% station-shortage quantile in corrective round 1.
- Add 1 temporary `delivery_ev` vehicle(s) at STN_03 based on the 95% station-shortage quantile in corrective round 1.
- Add 1 temporary `compact_ev` vehicle(s) at STN_04 based on the 95% station-shortage quantile in corrective round 1.
- Add 4 temporary `delivery_ev` vehicle(s) at STN_04 based on the 95% station-shortage quantile in corrective round 1.
- Add 2 temporary `large_ev` vehicle(s) at STN_04 based on the 95% station-shortage quantile in corrective round 1.
- Add 1 temporary `delivery_ev` vehicle(s) at STN_05 based on the 95% station-shortage quantile in corrective round 1.
- Apply a route-split or planned mid-route charging control with effective requirement factor 0.910 in corrective round 1.
- Add 1 temporary `delivery_ev` vehicle(s) at STN_02 based on the 95% station-shortage quantile in corrective round 2.
- Add 1 temporary `large_ev` vehicle(s) at STN_02 based on the 95% station-shortage quantile in corrective round 2.
- Add 1 temporary `compact_ev` vehicle(s) at STN_04 based on the 95% station-shortage quantile in corrective round 2.
- Add 2 temporary `delivery_ev` vehicle(s) at STN_04 based on the 95% station-shortage quantile in corrective round 2.
- Add 1 temporary `large_ev` vehicle(s) at STN_04 based on the 95% station-shortage quantile in corrective round 2.
- Add 1 temporary `delivery_ev` vehicle(s) at STN_05 based on the 95% station-shortage quantile in corrective round 2.

## Release interpretation

The system separates nominal solver feasibility from empirical reliability under an independently parameterized replay. Policies are compared with common exogenous random draws. Advanced policies are not promoted solely because they are more complex; base-policy selection uses reliability status, failed gates, tail shortage, and realized cost.

## Claim boundary

This is an offline, public-data-informed synthetic decision-support prototype. It does not use actual Amazon EV fleet, charging, repair, DSP allocation, or production cost records.
