"""Gunicorn, the API's production server (#25, ADR-0022).

`manage.py runserver` is what compose runs, for its autoreload, and what the Kubernetes manifests ran
until #25. Django's own documentation says it has not gone through security audits or performance
tests and must not be used in production.

Gunicorn reads this file from its working directory (`/app` in the image), so the command is just
`gunicorn config.wsgi`. Anything here can be overridden without a rebuild through gunicorn's own
`GUNICORN_CMD_ARGS`, e.g. `GUNICORN_CMD_ARGS="--workers 4"`. They are not TENANTIQ_* settings
because Django never reads them, and test_k8s_manifests.py refuses a ConfigMap key settings.py
ignores.

tests/test_gunicorn.py runs a real gunicorn on this file and checks the claims below.
"""

from __future__ import annotations

import os

bind = "0.0.0.0:8000"

# Threads, not the default sync worker. An answer streams over SSE (ADR-0009) for as long as the
# model generates, and a sync worker spends its whole process on one request: two workers would
# serve two questions at a time, and every other request, a document list or a sign-in, would queue
# behind them. Under gthread a stream occupies one thread, so 2 x 8 is sixteen concurrent requests.
worker_class = "gthread"
workers = 2
threads = 8

# For a threaded worker this is a heartbeat and not a request deadline: the worker's main loop keeps
# reporting to the master while its threads stream. Under the sync worker the same 30 seconds would
# kill every answer that took longer than that to generate.
timeout = 30

# A rollout sends SIGTERM, and an answer that is mid-stream gets this long to finish. It stays under
# the pod's 30-second grace period so that gunicorn, not the kubelet's SIGKILL, ends the stragglers.
graceful_timeout = 25

# Longer than the 4 seconds the BFF's fetch (undici) keeps an idle connection open by default. With
# gunicorn's default of 2, the BFF can send a request down a connection gunicorn has just closed,
# and that request fails with a socket error the user sees as a broken page.
keepalive = 5

accesslog = "-"
errorlog = "-"

# The worker heartbeat calls fchmod on a temp file, which can block on a disk-backed /tmp. /dev/shm
# is memory in the container; it is absent on macOS, which is why this is conditional.
worker_tmp_dir = "/dev/shm" if os.path.isdir("/dev/shm") else None

# gunicorn 25.1 added a control socket under $HOME. Nothing in the pod uses it, and uid 1000 has no
# home directory in the image to create it in, so all it would do is log an error on every start.
control_socket_disable = True
