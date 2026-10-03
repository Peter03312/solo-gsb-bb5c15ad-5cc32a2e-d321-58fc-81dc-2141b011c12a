"""引擎层单元测试：直接构造 RuleSet 验证展开、去重、路径与环检测。"""
import pytest

from app import engine


def _rs(rules):
    rs = engine.RuleSet()
    for rt, key, action, targets in rules:
        rule = engine.Rule(rt, key, action, tuple(engine.Target(k, a) for k, a in targets))
        (rs.exact if rt == "exact" else rs.domain)[key] = rule
    return rs


def test_simple_alias_chain_and_paths():
    rs = _rs([
        ("exact", "a@x.io", "forward", [("alias", "b@x.io")]),
        ("exact", "b@x.io", "forward", [("mailbox", "z@x.io")]),
    ])
    out = engine.resolve(rs, "a@x.io")
    assert out == {"z@x.io": [["a@x.io", "b@x.io", "z@x.io"]]}


def test_domain_fallback_used_per_hop():
    rs = _rs([
        ("exact", "a@x.io", "forward", [("alias", "u@y.io")]),
        ("domain", "y.io", "forward", [("mailbox", "catch@y.io")]),
    ])
    out = engine.resolve(rs, "a@x.io")
    assert out == {"catch@y.io": [["a@x.io", "u@y.io", "catch@y.io"]]}


def test_diamond_dedup_endpoint_keep_paths():
    rs = _rs([
        ("exact", "t@x.io", "forward", [("alias", "l@x.io"), ("alias", "r@x.io")]),
        ("exact", "l@x.io", "forward", [("alias", "m@x.io")]),
        ("exact", "r@x.io", "forward", [("alias", "m@x.io")]),
        ("exact", "m@x.io", "forward", [("mailbox", "z@x.io")]),
    ])
    out = engine.resolve(rs, "t@x.io")
    assert len(out) == 1
    assert sorted(out["z@x.io"]) == [
        ["t@x.io", "l@x.io", "m@x.io", "z@x.io"],
        ["t@x.io", "r@x.io", "m@x.io", "z@x.io"],
    ]


def test_runtime_cycle_raises_and_no_partial():
    # 绕过发布校验直接构造：a->b->a 成环
    rs = _rs([
        ("exact", "a@x.io", "forward", [("alias", "b@x.io")]),
        ("exact", "b@x.io", "forward", [("alias", "a@x.io")]),
    ])
    with pytest.raises(engine.ResolveError) as ei:
        engine.resolve(rs, "a@x.io")
    assert ei.value.code == "cycle"
    assert ei.value.path[-1] == "a@x.io"


def test_reject_raises():
    rs = _rs([("exact", "a@x.io", "reject", [])])
    with pytest.raises(engine.ResolveError) as ei:
        engine.resolve(rs, "a@x.io")
    assert ei.value.code == "rejected"


def test_no_route_raises():
    rs = engine.RuleSet()
    with pytest.raises(engine.ResolveError) as ei:
        engine.resolve(rs, "a@x.io")
    assert ei.value.code == "no_route"


def test_validate_publish_finds_unresolvable_and_cycle():
    rs = _rs([
        ("exact", "a@x.io", "forward", [("alias", "ghost@x.io")]),
        ("exact", "b@x.io", "forward", [("alias", "c@x.io")]),
        ("exact", "c@x.io", "forward", [("alias", "b@x.io")]),
    ])
    issues = engine.validate_publish(rs)
    kinds = {i["type"] for i in issues}
    assert kinds == {"unresolvable_alias", "cycle"}


def test_validate_publish_clean_graph():
    rs = _rs([
        ("exact", "a@x.io", "forward", [
            ("alias", "b@x.io"),
            ("mailbox", "keep@x.io"),
        ]),
        ("exact", "b@x.io", "forward", [("mailbox", "z@x.io")]),
        ("domain", "x.io", "forward", [("mailbox", "catch@x.io")]),
    ])
    assert engine.validate_publish(rs) == []
