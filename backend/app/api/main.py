import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routers import health
from app.config import get_settings

logger = logging.getLogger("money_radar")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Startup: load config now so a bad .env stops the server before it accepts traffic.
    settings = get_settings()
    logger.info(
        "providers: classifier=%s llm=%s",
        settings.classifier_provider,
        settings.llm_provider,
    )
    yield
    # Shutdown: nothing yet (0.8 will dispose the DB engine here).


def create_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s - %(message)s")
    app = FastAPI(title="Money Radar", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router)
    return app


app = create_app()
