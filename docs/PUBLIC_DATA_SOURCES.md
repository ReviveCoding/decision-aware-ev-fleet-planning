# Public Data Sources and Optional Calibration

## Amazon Last Mile Routing Research Challenge

Use for route-, stop-, and package-level workload structure. Do not infer actual EV fleet or charging attributes.

Registry: https://registry.opendata.aws/amazon-last-mile-challenges/

## NLR National Depot-Based MHDV Operations Summary

Use for national marginal summaries of daily driving distance, domicile dwell duration, and normalized depot charging profiles.

Catalog: https://data.nlr.gov/submissions/231

## NLR Heavy-Duty Depot Charging Profiles

Use for external shape checks of 15-minute charging profiles across fleet sizes and charging strategies.

Catalog: https://data.nlr.gov/submissions/162

## Integration rule

Never perform a row-level join across unrelated public datasets and present it as a jointly observed fleet. Use distributional calibration, provenance tags, sensitivity analysis, and explicit synthetic assumptions.
