from typing import Literal

from pydantic import BaseModel

CheckStatus = Literal["ok", "unavailable"]


class HealthResponse(BaseModel):
    status: Literal["ok"]


class ReadinessResponse(BaseModel):
    status: CheckStatus
    checks: dict[str, CheckStatus]
