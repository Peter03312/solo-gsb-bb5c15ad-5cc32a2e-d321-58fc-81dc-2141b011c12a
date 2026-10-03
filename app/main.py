"""FastAPI 入口：路由判定与草稿/版本管理 API。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import service
from .config import settings
from .db import init_db
from .errors import APIError, api_error_handler, validation_error
from .schemas import (
    DraftReplaceIn,
    PublishIn,
    ResolveIn,
    RuleDeleteIn,
    RuleIn,
    RuleUpsertIn,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="邮件别名路由判定服务",
    version="1.0.0",
    description="投递前的别名路由展开：精确规则 + 域兜底，历史版本可复现。",
    lifespan=lifespan,
)
app.add_exception_handler(APIError, api_error_handler)


@app.exception_handler(RequestValidationError)
async def _request_validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "invalid_request", "message": "请求参数不合法", "details": exc.errors()}},
    )


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/")
def index() -> dict:
    return {"service": "mail-alias-router", "docs": "/docs", "health": "/healthz"}


# ---------- 路由判定 ----------

@app.post("/api/v1/resolve")
def resolve_api(body: ResolveIn) -> dict:
    return service.resolve(body.recipient, body.version, settings.max_hops)


@app.get("/api/v1/resolve")
def resolve_get_api(recipient: str, version: str | None = None) -> dict:
    return service.resolve(recipient, version, settings.max_hops)


# ---------- 草稿 ----------

@app.get("/api/v1/draft")
def get_draft_api() -> dict:
    return service.get_draft()


@app.put("/api/v1/draft")
def replace_draft_api(body: DraftReplaceIn) -> dict:
    return service.replace_draft(body.expected_revision, body.rules)


@app.put("/api/v1/draft/rules")
def upsert_rule_api(body: RuleUpsertIn) -> dict:
    rule = RuleIn(rule_type=body.rule_type, match=body.match, action=body.action, targets=body.targets)
    return service.upsert_rule(body.expected_revision, rule)


@app.post("/api/v1/draft/rules/delete")
def delete_rule_api(rule_type: str, match: str, body: RuleDeleteIn) -> dict:
    if rule_type not in ("exact", "domain"):
        raise validation_error("rule_type 必须是 exact 或 domain", value=rule_type)
    return service.delete_rule(body.expected_revision, rule_type, match)


# ---------- 发布 ----------

@app.post("/api/v1/publish")
def publish_api(body: PublishIn) -> dict:
    return service.publish(body.expected_revision)


@app.get("/api/v1/versions")
def list_versions_api() -> dict:
    return service.list_versions()


@app.get("/api/v1/versions/{version}")
def get_version_api(version: int) -> dict:
    return service.get_version(version)
