FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install "fastapi>=0.115,<1.0" "httpx>=0.27,<1.0" "uvicorn>=0.30,<1.0"
COPY myapp_ai ./myapp_ai
RUN pip install --no-deps .

EXPOSE 4010

CMD ["uvicorn", "myapp_ai.main:app", "--host", "0.0.0.0", "--port", "4010"]
