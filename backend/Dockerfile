# Backend image — serves the Django API and runs the Celery ingestion worker (#23).
# One image, two commands: the compose `backend` service runs the API, `worker` runs Celery.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first (their own layer) so code edits don't re-resolve the whole tree.
# The runtime deps live in pyproject; psycopg[binary] and cryptography ship wheels, so no compiler
# toolchain is needed on slim.
COPY pyproject.toml ./
COPY app ./app
COPY config ./config
COPY manage.py ./
RUN pip install -e .

EXPOSE 8000

# Default to the API server; the worker service overrides `command` in docker-compose.yml. The dev
# stack mounts the source over /app (see compose) so runserver's autoreload picks up edits live.
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
