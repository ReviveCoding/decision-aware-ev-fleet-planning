.PHONY: smoke medium pipeline lint test validate reproducibility reference serve dashboard clean build

smoke:
	OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 ev-fleet run --config configs/smoke.yaml

medium:
	OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 ev-fleet run --config configs/medium.yaml

pipeline:
	OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 ev-fleet run --config configs/full.yaml

lint:
	ruff check .
	ruff format --check .


test:
	OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest

validate:
	ev-fleet validate --config configs/smoke.yaml

reproducibility:
	OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 python scripts/check_reproducibility.py --config configs/smoke.yaml

reference:
	ev-fleet export-reference --config configs/smoke.yaml
	ev-fleet export-reference --config configs/medium.yaml

serve:
	ev-fleet serve --config configs/smoke.yaml --host 0.0.0.0 --port 8000

dashboard:
	streamlit run dashboard/app.py

build:
	python -m build

clean:
	ev-fleet clean --config configs/smoke.yaml
