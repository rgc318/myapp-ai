FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml ./
COPY myapp_ai ./myapp_ai
RUN pip install --no-cache-dir .

EXPOSE 4010

CMD ["uvicorn", "myapp_ai.main:app", "--host", "0.0.0.0", "--port", "4010"]
