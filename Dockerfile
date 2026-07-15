FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

WORKDIR /app

COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --timeout 120 --retries 10 \
      fastapi==0.139.0 \
      httpx==0.28.1 \
      redis==6.4.0 \
      setuptools==83.0.0 \
      uvicorn==0.51.0 \
      wheel==0.47.0
COPY myapp_ai ./myapp_ai
RUN pip install --no-deps --no-build-isolation . && \
    pip uninstall --yes setuptools wheel
RUN pip check && \
    python -c "from importlib.resources import files; datasets = files('myapp_ai.evals.datasets'); assert datasets.joinpath('core.v1.jsonl').is_file(); assert datasets.joinpath('product_retrieval_zh_cn.v1.json').is_file()"
RUN groupadd --gid 10001 myapp-ai && \
    useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin myapp-ai

USER 10001:10001

FROM base AS test

COPY tests ./tests

CMD ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]

FROM base AS runtime

EXPOSE 4010

CMD ["uvicorn", "myapp_ai.main:app", "--host", "0.0.0.0", "--port", "4010"]
