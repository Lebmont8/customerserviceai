FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

ARG APP_UID=1000

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY *.py ./

RUN useradd --create-home --uid ${APP_UID} appuser \
    && chown -R appuser:appuser /app

USER appuser

CMD ["python", "main.py"]
