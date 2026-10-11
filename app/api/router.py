from fastapi import APIRouter

from app.api.routes import admin, app_release, auth, config, gate, godown, health, packaging, supervisor

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(config.router)
api_router.include_router(gate.router)
api_router.include_router(godown.router)
api_router.include_router(supervisor.router)
api_router.include_router(packaging.router)
api_router.include_router(admin.router)
api_router.include_router(app_release.router)
