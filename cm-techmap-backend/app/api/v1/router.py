"""CM TECHMAP — V1 API Router Aggregator"""

from fastapi import APIRouter

from app.api.v1.health import router as health_router
from app.api.v1.auth import router as auth_router
from app.api.v1.users import router as users_router
from app.api.v1.projects import router as projects_router
from app.api.v1.uploads import router as uploads_router
from app.api.v1.admin import router as admin_router
from app.api.v1.flights import router as flights_router
from app.api.v1.tiles import router as tiles_router
from app.api.v1.websocket import router as websocket_router
from app.api.v1.reports import router as reports_router
from app.api.v1.public_data import router as public_data_router
from app.api.v1.dashboard import router as dashboard_router
from app.api.v1.assets import router as assets_router
from app.api.v1.subscriptions import router as subscriptions_router
from app.api.v1.notifications import router as notifications_router
from app.api.v1.urban_analytics import router as urban_analytics_router
from app.api.v1.discrepancies import router as discrepancies_router
from app.api.v1.iptu_rules import router as iptu_rules_router
from app.api.v1.public_map import router as public_map_router
from app.api.v1.integration import router as integration_router
from app.api.v1.models import router as models_router

api_v1_router = APIRouter(prefix="/api/v1")

api_v1_router.include_router(health_router)
api_v1_router.include_router(auth_router)
api_v1_router.include_router(users_router)
api_v1_router.include_router(projects_router)
api_v1_router.include_router(uploads_router)
api_v1_router.include_router(admin_router)
api_v1_router.include_router(flights_router)
api_v1_router.include_router(tiles_router)
api_v1_router.include_router(websocket_router)
api_v1_router.include_router(reports_router)
api_v1_router.include_router(public_data_router)
api_v1_router.include_router(dashboard_router)
api_v1_router.include_router(assets_router)
api_v1_router.include_router(subscriptions_router)
api_v1_router.include_router(notifications_router)
api_v1_router.include_router(urban_analytics_router)
api_v1_router.include_router(discrepancies_router)
api_v1_router.include_router(iptu_rules_router)
api_v1_router.include_router(public_map_router)
api_v1_router.include_router(integration_router)
api_v1_router.include_router(models_router)

