# Backend image — serves the Django API and runs the Celery ingestion worker (#23).
# One image, two commands: the compose `backend` service runs the API, `worker` runs Celery.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, from the lockfile and nothing else (#54, ADR-0018). Two things follow from
# installing the lock before the source is copied: the image resolves the exact versions CI tested
# rather than whatever PyPI published this morning, and a code edit no longer invalidates the
# dependency layer. `--require-hashes` makes a tampered or substituted artefact a build failure.
# psycopg[binary] and cryptography ship wheels, so slim needs no compiler toolchain.
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock

# The package itself, without deps: they are already installed above, and letting pip re-resolve
# from pyproject's floors here would reintroduce exactly the floating install the lock removes.
COPY pyproject.toml ./
COPY app ./app
COPY config ./config
COPY manage.py gunicorn.conf.py ./
RUN pip install --no-deps -e .

EXPOSE 8000

# Default to the production API server (#25, ADR-0022); its settings are in gunicorn.conf.py, which
# gunicorn reads from the working directory. Compose overrides `command` with runserver for the dev
# stack, which mounts the source over /app so that runserver's autoreload picks up edits live, and
# with celery for the worker.
CMD ["gunicorn", "config.wsgi"]
