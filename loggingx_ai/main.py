"""HTTP API that loggingx-java calls to diagnose an error log."""

import json
import logging
import secrets
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from loggingx_ai.config import Settings, get_settings
from loggingx_ai.diagnosis import Diagnoser
from loggingx_ai.llm import ChatClient, LlmError
from loggingx_ai.schemas import DiagnoseRequest, DiagnoseResponse

log = logging.getLogger(__name__)
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def create_app(settings: Settings | None = None, diagnoser: Diagnoser | None = None) -> FastAPI:
    settings = settings or get_settings()
    client = ChatClient(settings)
    diagnoser = diagnoser or Diagnoser(client, settings)

    app = FastAPI(
        title="LoggingX AI",
        description="多 agent 错误诊断：日志分析、代码分析、根因、修复、评审。",
        version="0.1.0",
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    def require_api_key(key: Annotated[str | None, Depends(_api_key_header)]) -> None:
        expected = settings.api_key.get_secret_value() if settings.api_key else None
        if expected and not (key and secrets.compare_digest(key, expected)):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    protected = [Depends(require_api_key)]

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, object]:
        return {"status": "ok", "providers": client.configured_providers()}

    @app.post("/api/v1/diagnoses", response_model=DiagnoseResponse, response_model_by_alias=True,
              dependencies=protected, tags=["diagnosis"])
    async def diagnose(payload: DiagnoseRequest) -> DiagnoseResponse:
        try:
            return await diagnoser.diagnose(payload)
        except LlmError as error:
            log.warning("event=diagnosis.failed error=%s", error)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from None

    @app.post("/api/v1/diagnoses/stream", dependencies=protected, tags=["diagnosis"])
    async def diagnose_stream(payload: DiagnoseRequest, request: Request) -> StreamingResponse:
        """SSE: an `agent` event as each agent finishes, then `result`, or `error`."""

        async def events() -> AsyncIterator[str]:
            try:
                async for node, update in diagnoser.stream(payload):
                    if await request.is_disconnected():
                        return
                    if node == "finalize":
                        yield _sse("result", update["result"])
                    else:
                        yield _sse("agent", {"agent": node, "output": _public(update)})
            except LlmError as error:
                yield _sse("error", {"message": str(error)})
            yield _sse("done", {})

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


def _public(update: dict | None) -> dict:
    """An agent's state update without the request it was given."""

    if not update:
        return {}
    return {key: value for key, value in update.items() if key not in {"request", "parsed", "steps"}}


def _sse(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(_jsonable(data), ensure_ascii=False)}\n\n"


def _jsonable(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


app = create_app()
