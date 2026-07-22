"""Backward-compatible ASGI entrypoint.

New deployments must use ``backend.app.main:app``. Keeping this tiny adapter
allows older Kubernetes command overrides to start while manifests roll out.
"""

from backend.app.main import app

__all__ = ["app"]
