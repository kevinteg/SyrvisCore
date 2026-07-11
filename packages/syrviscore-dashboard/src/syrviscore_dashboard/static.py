"""Serve the built React SPA from FastAPI (single-origin with the API).

The image copies the Vite ``dist/`` into ``DASHBOARD_STATIC_DIR``. Real files are
served directly; unknown non-API paths fall back to ``index.html`` for client-side
routing. If no build is present (backend-only dev / tests) this is a no-op.
"""

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# Prefixes owned by the backend — never shadowed by the SPA fallback.
_RESERVED = ("api/", "auth/", "healthz")


def mount_spa(app: FastAPI, static_dir: str) -> None:
    if not static_dir:
        return
    root = Path(static_dir)
    index = root / "index.html"
    if not index.is_file():
        return

    assets = root / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    root_resolved = root.resolve()

    @app.get("/", include_in_schema=False)
    def spa_root() -> FileResponse:
        return FileResponse(index)

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str) -> FileResponse:
        if full_path.startswith(_RESERVED):
            raise HTTPException(status_code=404, detail="not found")
        candidate = (root / full_path).resolve()
        # Path-traversal guard: only serve files under the static root.
        if str(candidate).startswith(str(root_resolved)) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)
