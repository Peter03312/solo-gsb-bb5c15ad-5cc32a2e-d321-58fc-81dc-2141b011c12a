"""业务服务层：草稿编辑（乐观锁）、发布快照、版本与判定查询。"""
import hashlib
import json
import sqlite3

from . import engine
from .db import get_conn, immediate_tx
from .errors import conflict, not_found, validation_error
from .schemas import RuleIn
from .validators import normalize_address, normalize_domain

# 归一化后的规则结构:
# (rule_type, match_key, action, ((kind, address), ...))
NormRule = tuple[str, str, str, tuple[tuple[str, str], ...]]


# ---------- 输入归一化与校验 ----------

def normalize_rule(rule: RuleIn) -> NormRule:
    """归一化单条规则；非法地址/空目标/拒收携带目标一律 422。"""
    if rule.rule_type == "exact":
        key = normalize_address(rule.match, field="match")
    else:
        key = normalize_domain(rule.match, field="match")

    target_list: list[tuple[str, str]] = []
    seen_targets: set[str] = set()
    for t in rule.targets:
        addr = normalize_address(t.address, field="targets.address")
        if addr in seen_targets:
            raise validation_error(
                f"规则 {rule.rule_type}:{key} 存在重复目标: {addr}",
                rule=f"{rule.rule_type}:{key}",
                duplicate=addr,
            )
        seen_targets.add(addr)
        target_list.append((t.kind, addr))

    if rule.action == "reject" and target_list:
        raise validation_error(
            f"拒收规则不能携带目标: {rule.rule_type}:{key}",
            rule=f"{rule.rule_type}:{key}",
        )
    if rule.action == "forward" and not target_list:
        raise validation_error(
            f"转发规则目标不能为空: {rule.rule_type}:{key}",
            rule=f"{rule.rule_type}:{key}",
        )

    return rule.rule_type, key, rule.action, tuple(target_list)


def prepare_rules(rules: list[RuleIn]) -> list[NormRule]:
    """归一化整份草稿，并拒绝重复规则。"""
    prepared: list[NormRule] = []
    seen: set[tuple[str, str]] = set()
    for rule in rules:
        norm = normalize_rule(rule)
        ident = (norm[0], norm[1])
        if ident in seen:
            raise validation_error(
                f"重复规则: {norm[0]}:{norm[1]}",
                duplicate_rule=f"{norm[0]}:{norm[1]}",
            )
        seen.add(ident)
        prepared.append(norm)
    return prepared


# ---------- 读取 ----------

def _current_revision(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT revision FROM draft_meta WHERE id = 1").fetchone()
    return int(row["revision"])


def _check_revision(conn: sqlite3.Connection, expected: int) -> int:
    revision = _current_revision(conn)
    if revision != expected:
        raise conflict(
            f"修订号冲突：期望 {expected}，当前 {revision}，请重新获取草稿后再编辑",
            expected_revision=expected,
            current_revision=revision,
        )
    return revision


def _bump_revision(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE draft_meta SET revision = revision + 1, "
        "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = 1"
    )


def _load_ruleset(
    conn: sqlite3.Connection,
    *,
    version: int | None = None,
    draft: bool = False,
) -> engine.RuleSet:
    rs = engine.RuleSet()
    if draft:
        rules = conn.execute(
            "SELECT rule_id, rule_type, match_key, action FROM draft_rules ORDER BY position, rule_id"
        ).fetchall()
        targets = conn.execute(
            "SELECT rule_id, kind, address FROM draft_targets ORDER BY position"
        ).fetchall()
    else:
        rules = conn.execute(
            "SELECT rule_type, match_key, action FROM version_rules "
            "WHERE version = ? ORDER BY position",
            (version,),
        ).fetchall()
        targets = conn.execute(
            "SELECT rule_type, match_key, kind, address FROM version_targets "
            "WHERE version = ? ORDER BY position",
            (version,),
        ).fetchall()

    target_map: dict = {}
    for t in targets:
        if draft:
            key = t["rule_id"]
        else:
            key = (t["rule_type"], t["match_key"])
        target_map.setdefault(key, []).append(engine.Target(t["kind"], t["address"]))

    for r in rules:
        key = r["rule_id"] if draft else (r["rule_type"], r["match_key"])
        rule = engine.Rule(
            r["rule_type"], r["match_key"], r["action"], tuple(target_map.get(key, []))
        )
        if rule.rule_type == "exact":
            rs.exact[rule.match_key] = rule
        else:
            rs.domain[rule.match_key] = rule
    return rs


def _serialize_ruleset(rs: engine.RuleSet) -> list[dict]:
    out = []
    for rule in list(rs.exact.values()) + list(rs.domain.values()):
        out.append(
            {
                "rule_type": rule.rule_type,
                "match": rule.match_key,
                "action": rule.action,
                "targets": [
                    {"kind": t.kind, "address": t.address} for t in rule.targets
                ],
            }
        )
    return out


def get_draft() -> dict:
    with get_conn() as conn:
        revision = _current_revision(conn)
        rs = _load_ruleset(conn, draft=True)
    return {"revision": revision, "rules": _serialize_ruleset(rs)}


# ---------- 草稿编辑 ----------

def _replace_rules(conn: sqlite3.Connection, rules: list[NormRule]) -> None:
    conn.execute("DELETE FROM draft_rules")
    for position, (rule_type, key, action, targets) in enumerate(rules):
        cur = conn.execute(
            "INSERT INTO draft_rules (rule_type, match_key, action, position) "
            "VALUES (?, ?, ?, ?)",
            (rule_type, key, action, position),
        )
        rule_id = cur.lastrowid
        for tpos, (kind, address) in enumerate(targets):
            conn.execute(
                "INSERT INTO draft_targets (rule_id, position, kind, address) "
                "VALUES (?, ?, ?, ?)",
                (rule_id, tpos, kind, address),
            )


def replace_draft(expected: int, rules: list[RuleIn]) -> dict:
    prepared = prepare_rules(rules)
    with immediate_tx() as conn:
        _check_revision(conn, expected)
        _replace_rules(conn, prepared)
        _bump_revision(conn)
        new_revision = _current_revision(conn)
        rs = _load_ruleset(conn, draft=True)
    return {"revision": new_revision, "rules": _serialize_ruleset(rs)}


def upsert_rule(expected: int, rule: RuleIn) -> dict:
    norm = normalize_rule(rule)
    rule_type, key, action, targets = norm
    with immediate_tx() as conn:
        _check_revision(conn, expected)
        row = conn.execute(
            "SELECT rule_id, position FROM draft_rules WHERE rule_type = ? AND match_key = ?",
            (rule_type, key),
        ).fetchone()
        if row is None:
            pos_row = conn.execute(
                "SELECT COALESCE(MAX(position) + 1, 0) AS p FROM draft_rules"
            ).fetchone()
            position = pos_row["p"]
            cur = conn.execute(
                "INSERT INTO draft_rules (rule_type, match_key, action, position) "
                "VALUES (?, ?, ?, ?)",
                (rule_type, key, action, position),
            )
            rule_id = cur.lastrowid
        else:
            rule_id = row["rule_id"]
            conn.execute(
                "UPDATE draft_rules SET action = ? WHERE rule_id = ?",
                (action, rule_id),
            )
            conn.execute("DELETE FROM draft_targets WHERE rule_id = ?", (rule_id,))
        for tpos, (kind, address) in enumerate(targets):
            conn.execute(
                "INSERT INTO draft_targets (rule_id, position, kind, address) "
                "VALUES (?, ?, ?, ?)",
                (rule_id, tpos, kind, address),
            )
        _bump_revision(conn)
        new_revision = _current_revision(conn)
        rs = _load_ruleset(conn, draft=True)
    return {"revision": new_revision, "rules": _serialize_ruleset(rs)}


def delete_rule(expected: int, rule_type: str, match: str) -> dict:
    key = normalize_address(match) if rule_type == "exact" else normalize_domain(match)
    with immediate_tx() as conn:
        _check_revision(conn, expected)
        row = conn.execute(
            "SELECT rule_id FROM draft_rules WHERE rule_type = ? AND match_key = ?",
            (rule_type, key),
        ).fetchone()
        if row is None:
            raise not_found(f"规则不存在: {rule_type}:{key}")
        conn.execute("DELETE FROM draft_rules WHERE rule_id = ?", (row["rule_id"],))
        _bump_revision(conn)
        new_revision = _current_revision(conn)
        rs = _load_ruleset(conn, draft=True)
    return {"revision": new_revision, "rules": _serialize_ruleset(rs)}


# ---------- 发布 ----------

def _checksum_ruleset(rs: engine.RuleSet) -> str:
    payload = sorted(
        (
            rule.rule_type,
            rule.match_key,
            rule.action,
            sorted((t.kind, t.address) for t in rule.targets),
        )
        for rule in list(rs.exact.values()) + list(rs.domain.values())
    )
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def publish(expected: int) -> dict:
    """原子发布：核对修订号 → 校验可解析/无环 → 生成不可变快照。

    任一步失败整体回滚，线上继续保持旧版本。
    """
    with immediate_tx() as conn:
        _check_revision(conn, expected)
        rs = _load_ruleset(conn, draft=True)

        issues = engine.validate_publish(rs)
        if issues:
            raise validation_error(
                "草稿未通过发布校验（存在不可解析别名或路由环），未生成新版本，旧发布版保持不变",
                issues=issues,
            )

        checksum = _checksum_ruleset(rs)
        rule_count = len(rs.exact) + len(rs.domain)
        cur = conn.execute(
            "INSERT INTO versions (revision, rule_count, checksum) VALUES (?, ?, ?)",
            (expected, rule_count, checksum),
        )
        version = int(cur.lastrowid)

        ordered = sorted(rs.exact.values(), key=lambda r: r.match_key) + sorted(
            rs.domain.values(), key=lambda r: r.match_key
        )
        for position, rule in enumerate(ordered):
            conn.execute(
                "INSERT INTO version_rules (version, rule_type, match_key, action, position) "
                "VALUES (?, ?, ?, ?, ?)",
                (version, rule.rule_type, rule.match_key, rule.action, position),
            )
            for tpos, target in enumerate(rule.targets):
                conn.execute(
                    "INSERT INTO version_targets "
                    "(version, rule_type, match_key, position, kind, address) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (version, rule.rule_type, rule.match_key, tpos, target.kind, target.address),
                )
    return get_version(version)


# ---------- 版本查询 ----------

def list_versions() -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT version, revision, created_at, rule_count, checksum "
            "FROM versions ORDER BY version DESC"
        ).fetchall()
    return {"versions": [dict(r) for r in rows]}


def get_version(version: int) -> dict:
    with get_conn() as conn:
        meta = conn.execute(
            "SELECT version, revision, created_at, rule_count, checksum "
            "FROM versions WHERE version = ?",
            (version,),
        ).fetchone()
        if meta is None:
            raise not_found(f"版本不存在: {version}", version=version)
        rs = _load_ruleset(conn, version=version)
    body = dict(meta)
    body["rules"] = _serialize_ruleset(rs)
    return body


def _resolve_version(conn: sqlite3.Connection, version: int | str | None) -> int:
    if version is None or version == "" or version == "latest":
        row = conn.execute("SELECT MAX(version) AS v FROM versions").fetchone()
        if row["v"] is None:
            raise conflict("当前没有已发布版本，请先发布草稿")
        return int(row["v"])
    try:
        v = int(version)
    except (TypeError, ValueError):
        raise validation_error("version 必须是正整数或 latest", value=str(version))
    if v < 1:
        raise validation_error("version 必须是正整数", value=str(version))
    exists = conn.execute("SELECT 1 FROM versions WHERE version = ?", (v,)).fetchone()
    if exists is None:
        raise not_found(f"版本不存在: {v}", version=v)
    return v


def resolve(recipient: str, version: int | str | None, max_hops: int) -> dict:
    recipient = normalize_address(recipient, field="recipient")
    with get_conn() as conn:
        v = _resolve_version(conn, version)
        rs = _load_ruleset(conn, version=v)
    try:
        endpoints = engine.resolve(rs, recipient, max_hops=max_hops)
    except engine.ResolveError as exc:
        # 整次失败：返回错误，不携带任何部分展开结果
        from .errors import APIError

        status = {
            "rejected": (422, "rejected"),
            "no_route": (422, "no_route"),
            "cycle": (422, "cycle"),
            "max_hops_exceeded": (422, "max_hops_exceeded"),
        }[exc.code]
        raise APIError(
            status[0],
            status[1],
            exc.message,
            {"version": v, "recipient": recipient, "path": exc.path},
        )
    return {
        "recipient": recipient,
        "version": v,
        "endpoints": [
            {"address": mailbox, "paths": paths}
            for mailbox, paths in endpoints.items()
        ],
    }
