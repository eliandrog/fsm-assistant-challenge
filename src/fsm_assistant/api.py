"""The HTTP API of the web chat.

Responsibility: offer the one `Assistant` of the process over HTTP, for a
browser on the same machine (docs/DECISIONS.md, D-31). `create_app` builds
the application; the `serve` command runs it.

- `POST /api/chat` takes `{conversation_id, message}` and returns the turn's
  `TurnResult`, as `model_dump_json` writes it: the one contract every
  interface reads. A turn that fails inside the assistant is still a 200
  whose result says so. A body that is not a valid request is a 422, and a
  conversation that stays busy a 409, each with an `ApiError` body.
- `GET /api/health` says what the interface needs before the first question:
  whether the database can be read, whether a key is set, the model, the
  limits and whether turns are traced. It calls no model and never returns a
  key.
- Any other path serves the built web interface from `frontend/dist`, when
  it has been built.

Turns of one conversation never overlap: each conversation has a lock, a
second request for it waits its turn for a bounded time and then runs after
the first, seeing what that one remembered. The handlers are plain functions,
which the framework runs on worker threads, so a turn does not block the
server.

A result goes to the browser that asked and nowhere else: nothing here
writes a request, a message or a result to a log, a file or a cache. There
is no authentication and no cross-origin access: the server is for local use
and listens on the loopback address unless told otherwise.
"""

import logging
import threading
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Final, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from fsm_assistant import db
from fsm_assistant.config import PROJECT_ROOT, Settings
from fsm_assistant.graph import Assistant
from fsm_assistant.nl2sql import PROMPT_VERSION
from fsm_assistant.presentation import EVIDENCE_NOTE
from fsm_assistant.result import TurnResult
from fsm_assistant.schema_context import build_schema_context
from fsm_assistant.tracing import Tracer

logger = logging.getLogger(__name__)

DEFAULT_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8000
DEFAULT_DIST: Final = PROJECT_ROOT / "frontend" / "dist"
LOOPBACK_HOSTS: Final = ("127.0.0.1", "localhost")
# How long a request waits for an earlier turn of its conversation to end.
DEFAULT_BUSY_WAIT_SECONDS: Final = 30.0
MAX_CONVERSATION_ID_CHARACTERS: Final = 64
NOT_BUILT: Final = (
    "The web interface is not built, so there is no page to show here.\n"
    "Build it once with `npm ci && npm run build` in frontend/, then reload; or run the "
    "development server with `npm run dev` in frontend/ and open the address it prints.\n"
    "The API itself is running: see /api/health.\n"
)

# Whose replies the assistant runs on: Claude, or the canned replies of `demo.py`.
Mode = Literal["live", "offline_demo"]
ApiErrorCode = Literal[
    "invalid_request", "conversation_busy", "not_found", "method_not_allowed", "internal"
]


class ChatRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Chosen by the client, one per conversation: it keys the assistant's memory.
    conversation_id: str = Field(
        min_length=1, max_length=MAX_CONVERSATION_ID_CHARACTERS, pattern=r"^[A-Za-z0-9_-]+$"
    )
    message: str = Field(min_length=1)


class FieldProblem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str
    problem: str


class ApiError(BaseModel):
    """The body of every response that is not a 2xx. Written by code; it quotes no input."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: ApiErrorCode
    message: str
    fields: tuple[FieldProblem, ...] = ()


class DatabaseHealth(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    present: bool
    readable: bool
    schema_context: bool  # the description of the schema sent to the model can be built


class TracingHealth(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool
    destination: str | None  # the LangSmith project or agent; never a key


class Limits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_message_characters: int
    row_cap: int


class Health(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # `degraded`: a question cannot be answered as things are.
    status: Literal["ok", "degraded"]
    mode: Mode
    database: DatabaseHealth
    key_configured: bool
    model_id: str
    prompt_version: str
    tracing: TracingHealth
    limits: Limits
    evidence_note: str  # the sentence shown with every month-by-month table
    demo_questions: tuple[str, ...]  # what the offline demo can answer; empty when live


class _Conversations:
    """One lock per conversation id seen. Private working state of the application."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def acquire(self, conversation_id: str, timeout: float) -> threading.Lock | None:
        with self._guard:
            lock = self._locks.setdefault(conversation_id, threading.Lock())
        return lock if lock.acquire(timeout=timeout) else None


class _DatabaseCheck:
    """The health of the database. The schema context is built until it succeeds once."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._schema_context = False

    def run(self) -> DatabaseHealth:
        settings = self._settings
        present, readable = settings.db_path.is_file(), False
        if present:
            try:
                db.run_query(
                    "SELECT 1 FROM transactions LIMIT 1",
                    db_path=settings.db_path,
                    row_cap=1,
                    timeout_seconds=settings.query_timeout_seconds,
                )
                readable = True
                if not self._schema_context:
                    build_schema_context(settings.db_path)
                    self._schema_context = True
            except db.DbError as error:
                logger.warning("health: the database check failed: %s", type(error).__name__)
        return DatabaseHealth(
            present=present, readable=readable, schema_context=readable and self._schema_context
        )


def create_app(
    settings: Settings,
    assistant: Assistant,
    *,
    mode: Mode = "live",
    model_id: str | None = None,
    demo_questions: Sequence[str] = (),
    tracer: Tracer | None = None,
    dist: Path = DEFAULT_DIST,
    allowed_hosts: Sequence[str] = LOOPBACK_HOSTS,
    busy_wait_seconds: float = DEFAULT_BUSY_WAIT_SECONDS,
) -> FastAPI:
    """The application over `assistant`. `tracer`, when given, is closed when the server stops."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        if tracer is not None:
            tracer.close()
        logger.info("web stopped")

    app = FastAPI(
        title="FSM assistant",
        lifespan=lifespan,
        openapi_url="/api/openapi.json",
        docs_url=None,
        redoc_url=None,
    )
    # A page on another site cannot reach the API under a name of its own (DNS rebinding).
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    conversations = _Conversations()
    database = _DatabaseCheck(settings)
    # Any: the framework's own type for a described response.
    refusals: dict[int | str, dict[str, Any]] = {
        409: {"model": ApiError},
        422: {"model": ApiError},
    }

    @app.post("/api/chat", response_model=TurnResult, responses=refusals)
    def chat(request: ChatRequest) -> Response:
        limit = settings.max_message_characters
        length = len(request.message.strip())
        if length == 0:
            return _refuse(422, "invalid_request", "The message is empty.", "message")
        if length > limit:
            return _refuse(
                422,
                "invalid_request",
                f"The message is {length:,} characters long; the limit is {limit:,}.",
                "message",
            )
        lock = conversations.acquire(request.conversation_id, busy_wait_seconds)
        if lock is None:
            return _refuse(
                409,
                "conversation_busy",
                "This conversation is still answering an earlier question. Ask again in a "
                "moment.",
            )
        try:
            result = assistant.run_turn(request.message, request.conversation_id)
        finally:
            lock.release()
        # Written as the model writes itself, so the wire format has one definition.
        return Response(content=result.model_dump_json(), media_type="application/json")

    @app.get("/api/health", response_model=Health)
    def health() -> Health:
        checked = database.run()
        key = settings.anthropic_api_key is not None
        usable = checked.readable and checked.schema_context and (key or mode == "offline_demo")
        return Health(
            status="ok" if usable else "degraded",
            mode=mode,
            database=checked,
            key_configured=key,
            model_id=model_id or settings.anthropic_model,
            prompt_version=PROMPT_VERSION,
            tracing=TracingHealth(
                enabled=tracer is not None,
                destination=None if tracer is None else tracer.target.describe(),
            ),
            limits=Limits(
                max_message_characters=settings.max_message_characters, row_cap=settings.row_cap
            ),
            evidence_note=EVIDENCE_NOTE,
            demo_questions=tuple(demo_questions),
        )

    @app.get("/{path:path}", include_in_schema=False)
    def page(path: str) -> Response:
        """A file of the built interface, or its page for any other address."""
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404)
        index = dist / "index.html"
        if not index.is_file():
            return PlainTextResponse(NOT_BUILT, status_code=404)
        root = dist.resolve()
        wanted = (root / path).resolve()
        if path and wanted.is_relative_to(root) and wanted.is_file():
            return FileResponse(wanted)
        # The page decides what to show for an address it does not know; never cached,
        # so a rebuilt interface is picked up on reload.
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    @app.exception_handler(RequestValidationError)
    def invalid(request: Request, error: RequestValidationError) -> Response:
        # The place and pydantic's sentence for each problem; never the value sent.
        problems = tuple(
            FieldProblem(
                field=".".join(str(part) for part in problem["loc"] if part != "body") or "body",
                problem=str(problem["msg"]),
            )
            for problem in error.errors()
        )
        return _error(422, "invalid_request", "The request is not valid.", problems)

    @app.exception_handler(HTTPException)
    def refused(request: Request, error: HTTPException) -> Response:
        code = _code_of(error.status_code)
        messages: dict[ApiErrorCode, str] = {
            "not_found": "There is nothing at this address.",
            "method_not_allowed": "This address does not take that method.",
            "internal": "The server could not handle the request.",
        }
        return _error(error.status_code, code, messages.get(code, "The request was refused."))

    @app.exception_handler(Exception)
    def crashed(request: Request, error: Exception) -> Response:
        logger.error("web request failed: %s", type(error).__name__)
        return _error(500, "internal", "The server could not handle the request.")

    return app


def describe_page(dist: Path = DEFAULT_DIST) -> str:
    """One line for the terminal saying whether the built interface will be served."""
    if (dist / "index.html").is_file():
        return "Web interface: the build in frontend/dist is served at /."
    return (
        "Web interface: not built. Run `npm ci && npm run build` in frontend/, or use "
        "`npm run dev` there; the API is served either way."
    )


def _code_of(status: int) -> ApiErrorCode:
    if status == 404:
        return "not_found"
    if status == 405:
        return "method_not_allowed"
    return "invalid_request" if status < 500 else "internal"


def _refuse(status: int, code: ApiErrorCode, message: str, field: str | None = None) -> Response:
    logger.info("chat request refused: status=%d code=%s", status, code)
    problems = () if field is None else (FieldProblem(field=field, problem=message),)
    return _error(status, code, message, problems)


def _error(
    status: int, code: ApiErrorCode, message: str, fields: tuple[FieldProblem, ...] = ()
) -> Response:
    body = ApiError(code=code, message=message, fields=fields)
    return JSONResponse(body.model_dump(mode="json"), status_code=status)
