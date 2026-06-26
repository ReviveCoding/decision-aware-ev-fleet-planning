# Limitations

- Public sources are used for distributional inspiration and optional calibration, not row-level joining.
- Vehicle, charging-infrastructure, cost, repair, and dependence assumptions are synthetic and versioned.
- The optimizer uses route-level assignment, vehicle charge-acceptance limits, and aggregate depot-energy constraints rather than a full minute-level charger schedule.
- Repair-bay capacity is represented through expected availability in optimization and probabilistic same-day recovery in replay; detailed work-order queues and parts logistics are out of scope.
- Emergency leasing is capacity-limited in optimization and probabilistically fulfilled in replay; this is still a synthetic operating assumption.
- The independent replay is a decision-quality stress environment, not a validated production digital twin.
- Formal VSS and EVPI are sample-average estimates; held-out values are reported separately and can differ.
- The corrective frontier is simulation-targeted but is not an exact minimum-capacity proof.
- Offline results are simulated and must not be described as deployed business impact.
