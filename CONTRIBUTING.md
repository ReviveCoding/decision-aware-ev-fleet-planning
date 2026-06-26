# Contributing

1. Create a Python 3.11+ virtual environment.
2. Install `pip install -e ".[dev]"`.
3. Run `ev-fleet run --config configs/smoke.yaml`.
4. Run `pytest` and `ev-fleet validate --config configs/smoke.yaml`.
5. Keep public-data and synthetic-data claims separated.
6. Add a test for every optimizer constraint, schema change, or release-gate change.

Do not commit upstream public datasets, credentials, generated caches, or claims of production deployment.

## Source-checkout validation

To exercise the repository without installing the project package, install the direct and dev dependencies, then run:

```bash
PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest
PYTHONPATH=src python -m ev_fleet --help
```

## Candidate handoff

After final artifacts and `docs/release_candidate_evidence.json` exist:

```bash
ev-fleet handoff \
  --evidence docs/release_candidate_evidence.json \
  --artifact /path/to/source.zip \
  --artifact /path/to/package.whl \
  --artifact /path/to/package.tar.gz \
  --output /path/to/release_candidate_handoff.json
```
