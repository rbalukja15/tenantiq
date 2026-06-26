"""Request middleware that bounds the tenant-context lifecycle.

The tenant is *activated* by the DRF authenticator — the first point the verified tenant exists in
the request (see ``app.auth.authentication``). This middleware's sole job is to guarantee the
contextvar is cleared once the response is produced, so a reused worker thread can never carry one
request's tenant into the next. The Postgres GUC needs no cleanup here: ``SET LOCAL`` resets at the
end of each request's transaction (``ATOMIC_REQUESTS``).
"""

from __future__ import annotations

from app.tenant_context import clear_current_tenant


class TenantContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            return self.get_response(request)
        finally:
            clear_current_tenant()
