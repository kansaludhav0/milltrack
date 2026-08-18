from pydantic import BaseModel


class ErrorDetail(BaseModel):
    """Shape of every non-2xx response across the API — always {"detail": "..."}.
    Emitted uniformly whether the error comes from FastAPI (validation, HTTPException),
    a router's own HTTPException, or a translated Postgres error (see main.py)."""

    detail: str

    model_config = {"json_schema_extra": {"example": {"detail": "Bale not found"}}}
