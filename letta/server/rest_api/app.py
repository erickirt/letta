import importlib.util
import json
import logging
import os
import platform
import sys
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from letta.__init__ import __version__ as letta_version
from letta.agents.exceptions import IncompatibleAgentType
from letta.constants import ADMIN_PREFIX, API_PREFIX, OPENAI_API_PREFIX
from letta.errors import BedrockPermissionError, LettaAgentNotFoundError, LettaUserNotFoundError
from letta.helpers.pinecone_utils import get_pinecone_indices, should_use_pinecone, upsert_pinecone_indices
from letta.jobs.scheduler import start_scheduler_with_leader_election
from letta.log import get_logger
from letta.orm.errors import DatabaseTimeoutError, ForeignKeyConstraintViolationError, NoResultFound, UniqueConstraintViolationError
from letta.schemas.letta_message import create_letta_message_union_schema
from letta.schemas.letta_message_content import (
    create_letta_assistant_message_content_union_schema,
    create_letta_message_content_union_schema,
    create_letta_user_message_content_union_schema,
)
from letta.schemas.letta_ping import create_letta_ping_schema
from letta.server.constants import REST_DEFAULT_PORT
from letta.server.db import db_registry

# NOTE(charles): these are extra routes that are not part of v1 but we still need to mount to pass tests
from letta.server.rest_api.auth.index import setup_auth_router  # TODO: probably remove right?
from letta.server.rest_api.interface import StreamingServerInterface
from letta.server.rest_api.middleware import CheckPasswordMiddleware, ProfilerContextMiddleware
from letta.server.rest_api.routers.openai.chat_completions.chat_completions import router as openai_chat_completions_router
from letta.server.rest_api.routers.v1 import ROUTERS as v1_routes
from letta.server.rest_api.routers.v1.organizations import router as organizations_router
from letta.server.rest_api.routers.v1.users import router as users_router  # TODO: decide on admin
from letta.server.rest_api.static_files import mount_static_files
from letta.server.rest_api.utils import SENTRY_ENABLED
from letta.server.server import SyncServer
from letta.settings import settings, telemetry_settings

if SENTRY_ENABLED:
    import sentry_sdk

IS_WINDOWS = platform.system() == "Windows"

# NOTE(charles): @ethan I had to add this to get the global as the bottom to work
interface: type = StreamingServerInterface
server = SyncServer(default_interface_factory=lambda: interface())
logger = get_logger(__name__)


def generate_openapi_schema(app: FastAPI):
    # Update the OpenAPI schema
    if not app.openapi_schema:
        app.openapi_schema = app.openapi()

    letta_docs = app.openapi_schema.copy()
    letta_docs["paths"] = {k: v for k, v in letta_docs["paths"].items() if not k.startswith("/openai")}
    letta_docs["info"]["title"] = "Letta API"
    letta_docs["components"]["schemas"]["LettaMessageUnion"] = create_letta_message_union_schema()
    letta_docs["components"]["schemas"]["LettaMessageContentUnion"] = create_letta_message_content_union_schema()
    letta_docs["components"]["schemas"]["LettaAssistantMessageContentUnion"] = create_letta_assistant_message_content_union_schema()
    letta_docs["components"]["schemas"]["LettaUserMessageContentUnion"] = create_letta_user_message_content_union_schema()
    letta_docs["components"]["schemas"]["LettaPing"] = create_letta_ping_schema()

    # Update the app's schema with our modified version
    app.openapi_schema = letta_docs

    for name, docs in [
        (
            "letta",
            letta_docs,
        ),
    ]:
        if settings.cors_origins:
            docs["servers"] = [{"url": host} for host in settings.cors_origins]
        Path(f"openapi_{name}.json").write_text(json.dumps(docs, indent=2))


# middleware that only allows requests to pass through if user provides a password thats randomly generated and stored in memory
def generate_password():
    import secrets

    return secrets.token_urlsafe(16)


random_password = os.getenv("LETTA_SERVER_PASSWORD") or generate_password()


@asynccontextmanager
async def lifespan(app_: FastAPI):
    """
    FastAPI lifespan context manager with setup before the app starts pre-yield and on shutdown after the yield.
    """
    worker_id = os.getpid()

    if telemetry_settings.profiler:
        try:
            import googlecloudprofiler

            googlecloudprofiler.start(
                service="memgpt-server",
                service_version=str(letta_version),
                verbose=3,
            )
            logger.info("Profiler started.")
        except Exception as exc:
            logger.info("Profiler not enabled: %", exc)

    logger.info(f"[Worker {worker_id}] Starting lifespan initialization")
    logger.info(f"[Worker {worker_id}] Initializing database connections")
    db_registry.initialize_sync()
    db_registry.initialize_async()
    logger.info(f"[Worker {worker_id}] Database connections initialized")

    if should_use_pinecone():
        if settings.upsert_pinecone_indices:
            logger.info(f"[Worker {worker_id}] Upserting pinecone indices: {get_pinecone_indices()}")
            await upsert_pinecone_indices()
            logger.info(f"[Worker {worker_id}] Upserted pinecone indices")
        else:
            logger.info(f"[Worker {worker_id}] Enabled pinecone")
    else:
        logger.info(f"[Worker {worker_id}] Disabled pinecone")

    logger.info(f"[Worker {worker_id}] Starting scheduler with leader election")
    global server
    try:
        await start_scheduler_with_leader_election(server)
        logger.info(f"[Worker {worker_id}] Scheduler initialization completed")
    except Exception as e:
        logger.error(f"[Worker {worker_id}] Scheduler initialization failed: {e}", exc_info=True)
    logger.info(f"[Worker {worker_id}] Lifespan startup completed")
    yield

    # Cleanup on shutdown
    logger.info(f"[Worker {worker_id}] Starting lifespan shutdown")
    try:
        from letta.jobs.scheduler import shutdown_scheduler_and_release_lock

        await shutdown_scheduler_and_release_lock()
        logger.info(f"[Worker {worker_id}] Scheduler shutdown completed")
    except Exception as e:
        logger.error(f"[Worker {worker_id}] Scheduler shutdown failed: {e}", exc_info=True)

    # Cleanup SQLAlchemy instrumentation
    if not settings.disable_tracing and settings.sqlalchemy_tracing:
        try:
            from letta.otel.sqlalchemy_instrumentation_integration import teardown_letta_db_instrumentation

            teardown_letta_db_instrumentation()
            logger.info(f"[Worker {worker_id}] SQLAlchemy instrumentation shutdown completed")
        except Exception as e:
            logger.warning(f"[Worker {worker_id}] SQLAlchemy instrumentation shutdown failed: {e}")

    logger.info(f"[Worker {worker_id}] Lifespan shutdown completed")


def create_application() -> "FastAPI":
    """the application start routine"""
    # global server
    # server = SyncServer(default_interface_factory=lambda: interface())
    print(f"\n[[ Letta server // v{letta_version} ]]")

    if SENTRY_ENABLED:
        sentry_sdk.init(
            dsn=os.getenv("SENTRY_DSN"),
            traces_sample_rate=1.0,
            _experiments={
                "continuous_profiling_auto_start": True,
            },
        )
        logger.info("Sentry enabled.")

    debug_mode = "--debug" in sys.argv
    app = FastAPI(
        swagger_ui_parameters={"docExpansion": "none"},
        # openapi_tags=TAGS_METADATA,
        title="Letta",
        summary="Create LLM agents with long-term memory and custom tools 📚🦙",
        version=letta_version,
        debug=debug_mode,  # if True, the stack trace will be printed in the response
        lifespan=lifespan,
    )

    # === Exception Handlers ===
    # TODO (cliandy): move to separate file

    @app.exception_handler(Exception)
    async def generic_error_handler(request: Request, exc: Exception):
        logger.error(f"Unhandled error: {str(exc)}", exc_info=True)
        if SENTRY_ENABLED:
            sentry_sdk.capture_exception(exc)

        return JSONResponse(
            status_code=500,
            content={
                "detail": "An internal server error occurred",
                # Only include error details in debug/development mode
                # "debug_info": str(exc) if settings.debug else None
            },
        )

    async def error_handler_with_code(request: Request, exc: Exception, code: int, detail: str | None = None):
        logger.error(f"{type(exc).__name__}", exc_info=exc)
        if SENTRY_ENABLED:
            sentry_sdk.capture_exception(exc)

        if not detail:
            detail = str(exc)
        return JSONResponse(
            status_code=code,
            content={"detail": detail},
        )

    _error_handler_400 = partial(error_handler_with_code, code=400)
    _error_handler_404 = partial(error_handler_with_code, code=404)
    _error_handler_404_agent = partial(_error_handler_404, detail="Agent not found")
    _error_handler_404_user = partial(_error_handler_404, detail="User not found")
    _error_handler_409 = partial(error_handler_with_code, code=409)

    app.add_exception_handler(ValueError, _error_handler_400)
    app.add_exception_handler(NoResultFound, _error_handler_404)
    app.add_exception_handler(LettaAgentNotFoundError, _error_handler_404_agent)
    app.add_exception_handler(LettaUserNotFoundError, _error_handler_404_user)
    app.add_exception_handler(ForeignKeyConstraintViolationError, _error_handler_409)
    app.add_exception_handler(UniqueConstraintViolationError, _error_handler_409)

    @app.exception_handler(IncompatibleAgentType)
    async def handle_incompatible_agent_type(request: Request, exc: IncompatibleAgentType):
        logger.error("Incompatible agent types. Expected: %s, Actual: %s", exc.expected_type, exc.actual_type)
        if SENTRY_ENABLED:
            sentry_sdk.capture_exception(exc)

        return JSONResponse(
            status_code=400,
            content={
                "detail": str(exc),
                "expected_type": exc.expected_type,
                "actual_type": exc.actual_type,
            },
        )

    @app.exception_handler(DatabaseTimeoutError)
    async def database_timeout_error_handler(request: Request, exc: DatabaseTimeoutError):
        logger.error(f"Timeout occurred: {exc}. Original exception: {exc.original_exception}")
        if SENTRY_ENABLED:
            sentry_sdk.capture_exception(exc)

        return JSONResponse(
            status_code=503,
            content={"detail": "The database is temporarily unavailable. Please try again later."},
        )

    @app.exception_handler(BedrockPermissionError)
    async def bedrock_permission_error_handler(request, exc: BedrockPermissionError):
        logger.error(f"Bedrock permission denied.")
        if SENTRY_ENABLED:
            sentry_sdk.capture_exception(exc)

        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "type": "bedrock_permission_denied",
                    "message": "Unable to access the required AI model. Please check your Bedrock permissions or contact support.",
                    "detail": {str(exc)},
                }
            },
        )

    settings.cors_origins.append("https://app.letta.com")

    if (os.getenv("LETTA_SERVER_SECURE") == "true") or "--secure" in sys.argv:
        print(f"▶ Using secure mode with password: {random_password}")
        app.add_middleware(CheckPasswordMiddleware, password=random_password)

    # Add reverse proxy middleware to handle X-Forwarded-* headers
    # app.add_middleware(ReverseProxyMiddleware, base_path=settings.server_base_path)

    if telemetry_settings.profiler:
        app.add_middleware(ProfilerContextMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Set up OpenTelemetry tracing
    otlp_endpoint = settings.otel_exporter_otlp_endpoint
    if otlp_endpoint and not settings.disable_tracing:
        print(f"▶ Using OTLP tracing with endpoint: {otlp_endpoint}")
        env_name_suffix = os.getenv("ENV_NAME")
        service_name = f"letta-server-{env_name_suffix.lower()}" if env_name_suffix else "letta-server"
        from letta.otel.metrics import setup_metrics
        from letta.otel.tracing import setup_tracing

        setup_tracing(
            endpoint=otlp_endpoint,
            app=app,
            service_name=service_name,
        )
        setup_metrics(endpoint=otlp_endpoint, app=app, service_name=service_name)

        # Set up SQLAlchemy synchronous operation instrumentation
        if settings.sqlalchemy_tracing:
            from letta.otel.sqlalchemy_instrumentation_integration import setup_letta_db_instrumentation

            try:
                setup_letta_db_instrumentation(
                    enable_joined_monitoring=True,  # Monitor joined loading operations
                    sql_truncate_length=1500,  # Longer SQL statements for debugging
                )
                print("▶ SQLAlchemy synchronous operation instrumentation enabled")
            except Exception as e:
                logger.warning(f"Failed to setup SQLAlchemy instrumentation: {e}")
                # Don't fail startup if instrumentation fails

    for route in v1_routes:
        app.include_router(route, prefix=API_PREFIX)
        # this gives undocumented routes for "latest" and bare api calls.
        # we should always tie this to the newest version of the api.
        # app.include_router(route, prefix="", include_in_schema=False)
        app.include_router(route, prefix="/latest", include_in_schema=False)

    # NOTE: ethan these are the extra routes
    # TODO(ethan) remove

    # admin/users
    app.include_router(users_router, prefix=ADMIN_PREFIX)
    app.include_router(organizations_router, prefix=ADMIN_PREFIX)

    # openai
    app.include_router(openai_chat_completions_router, prefix=OPENAI_API_PREFIX)

    # /api/auth endpoints
    app.include_router(setup_auth_router(server, interface, random_password), prefix=API_PREFIX)

    # / static files
    mount_static_files(app)

    no_generation = "--no-generation" in sys.argv

    # Generate OpenAPI schema after all routes are mounted
    if not no_generation:
        generate_openapi_schema(app)

    return app


app = create_application()


def start_server(
    port: Optional[int] = None,
    host: Optional[str] = None,
    debug: bool = False,
    reload: bool = False,
):
    """Convenience method to start the server from within Python"""
    if debug:
        from letta.server.server import logger as server_logger

        # Set the logging level
        server_logger.setLevel(logging.DEBUG)
        # Create a StreamHandler
        stream_handler = logging.StreamHandler()
        # Set the formatter (optional)
        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        stream_handler.setFormatter(formatter)
        # Add the handler to the logger
        server_logger.addHandler(stream_handler)

    # Experimental UV Loop Support
    try:
        if settings.use_uvloop:
            print("Running server asyncio loop on uvloop...")
            import asyncio

            import uvloop

            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except:
        pass

    if (os.getenv("LOCAL_HTTPS") == "true") or "--localhttps" in sys.argv:
        print(f"▶ Server running at: https://{host or 'localhost'}:{port or REST_DEFAULT_PORT}")
        print("▶ View using ADE at: https://app.letta.com/development-servers/local/dashboard\n")
        if importlib.util.find_spec("granian") is not None and settings.use_granian:
            from granian import Granian

            # Experimental Granian engine
            Granian(
                target="letta.server.rest_api.app:app",
                # factory=True,
                interface="asgi",
                address=host or "127.0.0.1",  # Note granian address must be an ip address
                port=port or REST_DEFAULT_PORT,
                workers=settings.uvicorn_workers,
                # runtime_blocking_threads=
                # runtime_threads=
                reload=reload or settings.uvicorn_reload,
                reload_paths=["letta/"],
                reload_ignore_worker_failure=True,
                reload_tick=4000,  # set to 4s to prevent crashing on weird state
                # log_level="info"
                ssl_keyfile="certs/localhost-key.pem",
                ssl_cert="certs/localhost.pem",
            ).serve()
        else:
            uvicorn.run(
                "letta.server.rest_api.app:app",
                host=host or "localhost",
                port=port or REST_DEFAULT_PORT,
                workers=settings.uvicorn_workers,
                reload=reload or settings.uvicorn_reload,
                timeout_keep_alive=settings.uvicorn_timeout_keep_alive,
                ssl_keyfile="certs/localhost-key.pem",
                ssl_certfile="certs/localhost.pem",
            )

    else:
        if IS_WINDOWS:
            # Windows doesn't those the fancy unicode characters
            print(f"Server running at: http://{host or 'localhost'}:{port or REST_DEFAULT_PORT}")
            print(f"View using ADE at: https://app.letta.com/development-servers/local/dashboard\n")
        else:
            print(f"▶ Server running at: http://{host or 'localhost'}:{port or REST_DEFAULT_PORT}")
            print(f"▶ View using ADE at: https://app.letta.com/development-servers/local/dashboard\n")

        if importlib.util.find_spec("granian") is not None and settings.use_granian:
            # Experimental Granian engine
            from granian import Granian

            Granian(
                target="letta.server.rest_api.app:app",
                # factory=True,
                interface="asgi",
                address=host or "127.0.0.1",  # Note granian address must be an ip address
                port=port or REST_DEFAULT_PORT,
                workers=settings.uvicorn_workers,
                # runtime_blocking_threads=
                # runtime_threads=
                reload=reload or settings.uvicorn_reload,
                reload_paths=["letta/"],
                reload_ignore_worker_failure=True,
                reload_tick=4000,  # set to 4s to prevent crashing on weird state
                # log_level="info"
            ).serve()
        else:
            uvicorn.run(
                "letta.server.rest_api.app:app",
                host=host or "localhost",
                port=port or REST_DEFAULT_PORT,
                workers=settings.uvicorn_workers,
                reload=reload or settings.uvicorn_reload,
                timeout_keep_alive=settings.uvicorn_timeout_keep_alive,
            )
