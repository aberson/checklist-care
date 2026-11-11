"""``create_app(db_path)``: the FastAPI app behind ``checklist-care serve`` (plan §5, §6).

* ``/api/*`` comes from the ``routes_*`` routers. API errors have the §6 shape
  ``{"error": {"code": "CC_E_…", "message": "…", "fields": [...]}}``: a route raises
  an ``errors.ApiError`` (400, 404 or 409), and a request FastAPI cannot validate
  (a wrong type, an unknown field, a broken JSON body) is a 400 ``CC_E_VALIDATION``
  naming its fields, never FastAPI's own 422.
* Every other GET path is a file of the built app in ``web/dist``. The requested
  path is checked lexically before any filesystem access (no ``..`` or ``.``
  segment, no empty segment, no absolute path, drive, ``:`` or NUL), then
  resolved and required to stay inside ``dist``; anything else is 404. Without a
  build, ``/`` is a page naming ``npm --prefix web run build``. The app uses hash
  routes (``#/review``), so the server needs no fallback for page paths.
* A request whose ``Host`` is not ``127.0.0.1`` or ``localhost`` (any port) is a 400
  (``TrustedHostMiddleware``): the binding keeps other machines out, and this keeps
  a DNS-rebinding web page from reading the API through the operator's browser.
* The database is one handle (``server/database.py``), opened at startup and
  closed on lifespan shutdown.

The static-asset handling follows ``dev-observatory/src/dev_observatory/ambition/
routes.py`` (``_asset_dir()``, ``_is_relative_asset_path()``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from checklist_care import __version__
from checklist_care.errors import API_ERRORS, CODE_NOT_FOUND, CODE_VALIDATION, ApiError
from checklist_care.server import (
    ALLOWED_HOSTS,
    API_PREFIX,
    BUILD_COMMAND,
    DIST_DIR,
    routes_buckets,
    routes_lists,
    routes_meta,
    routes_print,
    routes_review,
    routes_sources,
    routes_tips,
)
from checklist_care.server.database import Database

INDEX_FILE: Final = "index.html"

API_ERROR_CODES: Final = {error.status: error.code for error in API_ERRORS}
"""The three API error statuses and their codes (plan §6), from ``errors.py``."""

ROUTERS: Final = (
    routes_meta,
    routes_tips,
    routes_review,
    routes_lists,
    routes_buckets,
    routes_sources,
    routes_print,
)
"""The modules whose ``router`` serves ``/api``."""

_VALUE_ERROR_PREFIX: Final = "Value error, "

MEDIA_TYPES: Final = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}
"""Media types of the built files, fixed here: Windows ``mimetypes`` reads the
registry, which can map ``.js`` to ``text/plain``, and browsers refuse a module
script served that way."""

_NO_STORE: Final = {"Cache-Control": "no-store"}
_NO_CACHE: Final = {"Cache-Control": "no-cache"}
_NOSNIFF: Final = {"X-Content-Type-Options": "nosniff"}


def error_response(
    status: int, code: str, message: str, fields: Sequence[str] = ()
) -> JSONResponse:
    """An API error in the §6 shape."""

    body = {"error": {"code": code, "message": message, "fields": list(fields)}}
    return JSONResponse(body, status_code=status)


def is_safe_relative_path(path: str) -> bool:
    """True when ``path`` names a file below a directory by plain names only.

    Lexical, before any filesystem access: every ``/``- or ``\\``-separated segment
    must be non-empty and neither ``.`` nor ``..``; a leading separator, a drive, a
    ``:`` (also an NTFS alternate data stream) or a NUL is refused.
    """

    if not path or "\x00" in path or ":" in path or path.startswith(("/", "\\")):
        return False
    return all(part not in {"", ".", ".."} for part in path.replace("\\", "/").split("/"))


def validation_fields(error: RequestValidationError) -> list[str]:
    """The request fields a validation error names, in order, each once: the first
    part of each error's location after ``body``, ``query`` or ``path`` (``body``
    itself when the whole body is wrong)."""

    fields: list[str] = []
    for item in error.errors():
        location = [str(part) for part in item.get("loc", ())]
        name = location[1] if len(location) > 1 else (location[0] if location else "body")
        if name not in fields:
            fields.append(name)
    return fields


def validation_message(error: RequestValidationError) -> str:
    """One line naming each invalid field and why, such as ``name: must not be empty``."""

    parts: list[str] = []
    for item in error.errors():
        location = [str(part) for part in item.get("loc", ())][1:] or ["body"]
        message = str(item.get("msg", "is invalid")).removeprefix(_VALUE_ERROR_PREFIX)
        parts.append(f"{'.'.join(location)}: {message}")
    return "; ".join(parts) or "the request is invalid"


def build_required_page() -> str:
    """The page ``/`` serves while ``web/dist`` holds no built app."""

    command = escape(BUILD_COMMAND)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        "<title>checklist-care: build required</title></head><body><main>"
        "<h1>The checklist-care app is not built yet</h1>"
        f"<p>Build it from the checkout root with <code>{command}</code> "
        "(or start the server with <code>checklist-care serve --build</code>), "
        "then reload this page.</p>"
        f'<p>The API is running: <a href="{API_PREFIX}/health">{API_PREFIX}/health</a>.</p>'
        "</main></body></html>"
    )


def _is_api_path(path: str) -> bool:
    return path == API_PREFIX or path.startswith(f"{API_PREFIX}/")


def _media_type(path: Path) -> str | None:
    return MEDIA_TYPES.get(path.suffix.lower())


def create_app(db_path: Path, *, dist_dir: Path | None = None) -> FastAPI:
    """The app for the database at ``db_path``, serving the built app from
    ``dist_dir`` (default ``web/dist`` of this checkout)."""

    database = Database(db_path)
    static_root = DIST_DIR if dist_dir is None else dist_dir

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        database.open()
        try:
            yield
        finally:
            database.close()

    app = FastAPI(
        title="checklist-care",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=f"{API_PREFIX}/openapi.json",
    )
    app.state.database = database
    # Outermost: a foreign Host (DNS rebinding) is refused before any route runs.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(ALLOWED_HOSTS), www_redirect=False)
    for module in ROUTERS:
        app.include_router(module.router)

    @app.exception_handler(ApiError)
    async def api_error(_: Request, error: ApiError) -> Response:
        return error_response(error.status, error.code, error.message, error.fields)

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(_: Request, error: RequestValidationError) -> Response:
        return error_response(
            400, CODE_VALIDATION, validation_message(error), validation_fields(error)
        )

    @app.exception_handler(StarletteHTTPException)
    async def api_http_error(request: Request, error: StarletteHTTPException) -> Response:
        code = API_ERROR_CODES.get(error.status_code)
        if code is None or not _is_api_path(request.url.path):
            return await http_exception_handler(request, error)
        return error_response(error.status_code, code, str(error.detail))

    @app.get("/", include_in_schema=False)
    def index() -> Response:
        index_file = static_root / INDEX_FILE
        if not index_file.is_file():
            return HTMLResponse(build_required_page(), status_code=503, headers=_NO_STORE)
        return FileResponse(
            index_file, media_type=MEDIA_TYPES[".html"], headers={**_NO_CACHE, **_NOSNIFF}
        )

    @app.get("/{file_path:path}", include_in_schema=False)
    def static_file(file_path: str, request: Request) -> Response:
        if _is_api_path(request.url.path):
            return error_response(404, CODE_NOT_FOUND, f"no API route {request.url.path}")
        if not is_safe_relative_path(file_path):
            return _not_found()
        root = static_root.resolve()
        candidate = (root / file_path).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            return _not_found()
        return FileResponse(candidate, media_type=_media_type(candidate), headers=_NOSNIFF)

    return app


def _not_found() -> Response:
    headers: dict[str, Any] = {**_NO_STORE, **_NOSNIFF}
    return Response("not found", status_code=404, media_type="text/plain", headers=headers)
