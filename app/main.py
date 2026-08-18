from contextlib import asynccontextmanager

from asyncpg import (
    CheckViolationError,
    ForeignKeyViolationError,
    NotNullViolationError,
    PostgresError,
    RaiseError,
    UniqueViolationError,
)
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.core.config import settings
from app.core.db import close_pool, init_pool

TAGS_METADATA = [
    {"name": "auth", "description": "Login for every role. One endpoint: employee_code+PIN (shop-floor) or email+password (GM/MD admin), both against `identity.users`."},
    {"name": "gate", "description": "Gateman: log every vehicle in/out. Create+read only, matching the paper gate-pass model."},
    {"name": "godown", "description": "Godown Manager (raw material): bale unloading/weighment/QR, and Blow Room mixing lots."},
    {"name": "supervisor", "description": "Production Supervisor: twice-daily shift logs (O/E, CARD, Draw Frame) and the independent ~20h DOFF cycle."},
    {"name": "packaging", "description": "Packaging & Dispatch Manager: loose stock -> bags -> dispatch."},
    {"name": "admin", "description": "GM/MD only: user provisioning, analytics dashboards, and cross-entity traceability search."},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    yield
    await close_pool()


app = FastAPI(
    title=settings.app_name,
    debug=settings.debug,
    lifespan=lifespan,
    openapi_tags=TAGS_METADATA,
    description=(
        "Every endpoint except /auth/login requires `Authorization: Bearer <token>` "
        "from that login call. Every error response — from FastAPI's own validation, "
        "a router's HTTPException, or a translated Postgres error below — is shaped "
        "`{\"detail\": ...}`; only automatic request-body validation errors (422) use "
        "FastAPI's standard field-level `detail` array instead of a plain string, per "
        "the FastAPI convention API consumers already expect."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.api_v1_prefix)


@app.exception_handler(PostgresError)
async def pg_error_handler(request: Request, exc: PostgresError) -> JSONResponse:
    # Centralizes translation of DB-level guarantees (unique constraints, CHECK bounds,
    # FK/NOT NULL references, trigger-raised business rules like "one active mixing lot"
    # or "LOADING vehicles only") into HTTP responses, so routes don't need try/except.
    if isinstance(exc, UniqueViolationError):
        return JSONResponse(status_code=409, content={"detail": "Duplicate or already-processed record"})
    if isinstance(exc, (CheckViolationError, ForeignKeyViolationError, NotNullViolationError)):
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    if isinstance(exc, RaiseError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    return JSONResponse(status_code=500, content={"detail": "Database error"})


@app.get("/", tags=["health"])
def root() -> dict:
    return {"message": f"{settings.app_name} API is running"}
