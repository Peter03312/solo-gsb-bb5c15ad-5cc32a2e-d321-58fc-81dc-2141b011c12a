"""HTTP 请求/响应模型。"""
from typing import Literal

from pydantic import BaseModel, Field

RuleType = Literal["exact", "domain"]
TargetKind = Literal["alias", "mailbox"]


class TargetIn(BaseModel):
    kind: TargetKind
    address: str = Field(min_length=1)


class RuleIn(BaseModel):
    rule_type: RuleType
    match: str = Field(min_length=1)
    action: Literal["forward", "reject"]
    targets: list[TargetIn] = Field(default_factory=list)


class DraftReplaceIn(BaseModel):
    expected_revision: int = Field(ge=0)
    rules: list[RuleIn]


class RuleUpsertIn(BaseModel):
    expected_revision: int = Field(ge=0)
    rule_type: RuleType
    match: str = Field(min_length=1)
    action: Literal["forward", "reject"]
    targets: list[TargetIn] = Field(default_factory=list)


class RuleDeleteIn(BaseModel):
    expected_revision: int = Field(ge=0)


class PublishIn(BaseModel):
    expected_revision: int = Field(ge=0)


class ResolveIn(BaseModel):
    recipient: str = Field(min_length=1)
    version: int | str | None = Field(default=None, description="版本号整数；省略或 latest 表示当前发布版")
