FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY constraints ./constraints
COPY src ./src
COPY scripts ./scripts
COPY configs ./configs
COPY dashboard ./dashboard
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -c constraints/runtime.txt .

RUN addgroup --system app && adduser --system --ingroup app app \
    && mkdir -p /app/artifacts /app/data/generated \
    && chown -R app:app /app
USER app

CMD ["ev-fleet", "run", "--config", "configs/smoke.yaml"]
