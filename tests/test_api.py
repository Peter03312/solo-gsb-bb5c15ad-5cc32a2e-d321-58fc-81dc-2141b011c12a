"""端到端 API 测试：编辑、乐观锁、发布校验、判定与历史复现。"""
import copy

# 一份合法草稿：
#   team@corp.example     -> alias sales@corp.example, mailbox boss@corp.example
#   sales@corp.example    -> alias west@corp.example, alias east@corp.example
#   west@corp.example     -> mailbox A@CORP.EXAMPLE（大小写归一为 a@corp.example）
#   east@corp.example     -> mailbox b@corp.example
#   域兜底 corp.example    -> mailbox catch@corp.example
VALID_RULES = [
    {
        "rule_type": "exact",
        "match": "team@corp.example",
        "action": "forward",
        "targets": [
            {"kind": "alias", "address": "sales@corp.example"},
            {"kind": "mailbox", "address": "boss@corp.example"},
        ],
    },
    {
        "rule_type": "exact",
        "match": "sales@corp.example",
        "action": "forward",
        "targets": [
            {"kind": "alias", "address": "west@corp.example"},
            {"kind": "alias", "address": "east@corp.example"},
        ],
    },
    {
        "rule_type": "exact",
        "match": "west@corp.example",
        "action": "forward",
        "targets": [
            {"kind": "mailbox", "address": "A@CORP.EXAMPLE"},
        ],
    },
    {
        "rule_type": "exact",
        "match": "east@corp.example",
        "action": "forward",
        "targets": [{"kind": "mailbox", "address": "b@corp.example"}],
    },
    {
        "rule_type": "domain",
        "match": "corp.example",
        "action": "forward",
        "targets": [{"kind": "mailbox", "address": "catch@corp.example"}],
    },
]


def _put_draft(client, revision, rules):
    return client.put(
        "/api/v1/draft", json={"expected_revision": revision, "rules": rules}
    )


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_draft_full_flow_and_resolve(client):
    r = _put_draft(client, 0, VALID_RULES)
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 1

    r = client.post("/api/v1/publish", json={"expected_revision": 1})
    assert r.status_code == 200, r.text
    v1 = r.json()["version"]
    assert v1 >= 1
    assert r.json()["rule_count"] == 5

    r = client.post(
        "/api/v1/resolve",
        json={"recipient": "team@corp.example", "version": v1},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    endpoints = {e["address"]: e["paths"] for e in body["endpoints"]}
    assert set(endpoints) == {"a@corp.example", "b@corp.example", "boss@corp.example"}
    # 终点 a 为大小写归一后的地址
    assert "a@corp.example" in endpoints
    assert len(endpoints["a@corp.example"]) == 1
    assert endpoints["a@corp.example"][0][0] == "team@corp.example"
    assert endpoints["a@corp.example"][0][-1] == "a@corp.example"
    # 路径完整记录每一跳
    assert endpoints["b@corp.example"][0] == [
        "team@corp.example",
        "sales@corp.example",
        "east@corp.example",
        "b@corp.example",
    ]


def test_exact_rule_takes_precedence_over_domain(client):
    rules = copy.deepcopy(VALID_RULES)
    _put_draft(client, 0, rules)
    client.post("/api/v1/publish", json={"expected_revision": 1})

    # west@corp.example 命中精确规则 -> a@...；域兜底不应被使用
    r = client.post(
        "/api/v1/resolve", json={"recipient": "west@corp.example"}
    )
    assert r.status_code == 200
    assert {e["address"] for e in r.json()["endpoints"]} == {"a@corp.example"}

    # 无精确规则的地址落到域兜底
    r = client.post("/api/v1/resolve", json={"recipient": "nobody@corp.example"})
    assert r.status_code == 200
    assert {e["address"] for e in r.json()["endpoints"]} == {"catch@corp.example"}


def test_no_route_fails_without_partial_results(client):
    # 已发布版本只覆盖 corp.example；收件人落在无任何规则的地址上
    rules = [
        {
            "rule_type": "exact",
            "match": "x@corp.example",
            "action": "forward",
            "targets": [{"kind": "mailbox", "address": "ok@corp.example"}],
        },
        {
            "rule_type": "domain",
            "match": "corp.example",
            "action": "forward",
            "targets": [{"kind": "mailbox", "address": "catch@corp.example"}],
        },
    ]
    _put_draft(client, 0, rules)
    pub = client.post("/api/v1/publish", json={"expected_revision": 1})
    assert pub.status_code == 200, pub.text

    r = client.post("/api/v1/resolve", json={"recipient": "ghost@other.example"})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "no_route"
    assert err["details"]["path"] == ["ghost@other.example"]
    # 失败响应不携带任何终点
    assert "endpoints" not in r.json()


def test_rejected_branch_fails_whole_request(client):
    rules = [
        {
            "rule_type": "exact",
            "match": "p@corp.example",
            "action": "forward",
            "targets": [
                {"kind": "mailbox", "address": "ok@corp.example"},
                {"kind": "alias", "address": "blocked@corp.example"},
            ],
        },
        {
            "rule_type": "exact",
            "match": "blocked@corp.example",
            "action": "reject",
            "targets": [],
        },
    ]
    _put_draft(client, 0, rules)
    client.post("/api/v1/publish", json={"expected_revision": 1})

    r = client.post("/api/v1/resolve", json={"recipient": "p@corp.example"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "rejected"


def test_unknown_domain_no_route(client):
    _put_draft(client, 0, VALID_RULES)
    client.post("/api/v1/publish", json={"expected_revision": 1})
    r = client.post(
        "/api/v1/resolve", json={"recipient": "x@nowhere.example"}
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "no_route"


def test_cycle_rejected_at_publish(client):
    rules = [
        {
            "rule_type": "exact",
            "match": "a@corp.example",
            "action": "forward",
            "targets": [{"kind": "alias", "address": "b@corp.example"}],
        },
        {
            "rule_type": "exact",
            "match": "b@corp.example",
            "action": "forward",
            "targets": [{"kind": "alias", "address": "a@corp.example"}],
        },
    ]
    r = _put_draft(client, 0, rules)
    assert r.status_code == 200
    r = client.post("/api/v1/publish", json={"expected_revision": 1})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    assert any(i["type"] == "cycle" for i in err["details"]["issues"])


def test_self_loop_via_domain_rejected_at_publish(client):
    rules = [
        {
            "rule_type": "domain",
            "match": "corp.example",
            "action": "forward",
            "targets": [{"kind": "alias", "address": "self@corp.example"}],
        },
    ]
    _put_draft(client, 0, rules)
    r = client.post("/api/v1/publish", json={"expected_revision": 1})
    assert r.status_code == 422
    assert any(
        i["type"] == "cycle"
        for i in r.json()["error"]["details"]["issues"]
    )


def test_unresolvable_alias_rejected_at_publish(client):
    rules = [
        {
            "rule_type": "exact",
            "match": "a@corp.example",
            "action": "forward",
            "targets": [{"kind": "alias", "address": "missing@corp.example"}],
        }
    ]
    _put_draft(client, 0, rules)
    r = client.post("/api/v1/publish", json={"expected_revision": 1})
    assert r.status_code == 422
    issues = r.json()["error"]["details"]["issues"]
    assert any(i["type"] == "unresolvable_alias" for i in issues)


def test_optimistic_lock_conflict(client):
    _put_draft(client, 0, VALID_RULES)  # revision -> 1
    r = _put_draft(client, 0, [])       # 用过期修订号
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "conflict"
    assert err["details"]["expected_revision"] == 0
    assert err["details"]["current_revision"] == 1

    # 用正确修订号成功
    r = _put_draft(client, 1, [])
    assert r.status_code == 200
    assert r.json()["revision"] == 2


def test_upsert_and_delete_rule(client):
    rule = {
        "rule_type": "exact",
        "match": "one@corp.example",
        "action": "forward",
        "targets": [{"kind": "mailbox", "address": "dest@corp.example"}],
    }
    r = client.put("/api/v1/draft/rules", json={"expected_revision": 0, **rule})
    assert r.status_code == 200
    assert r.json()["revision"] == 1

    # 更新（改为 reject）
    r = client.put(
        "/api/v1/draft/rules",
        json={
            "expected_revision": 1,
            "rule_type": "exact",
            "match": "one@corp.example",
            "action": "reject",
            "targets": [],
        },
    )
    assert r.status_code == 200
    assert r.json()["rules"][0]["action"] == "reject"

    r = client.post(
        "/api/v1/draft/rules/delete?rule_type=exact&match=one@corp.example",
        json={"expected_revision": 2},
    )
    assert r.status_code == 200
    assert r.json()["rules"] == []


def test_delete_missing_rule_404(client):
    r = client.post(
        "/api/v1/draft/rules/delete?rule_type=exact&match=nope@corp.example",
        json={"expected_revision": 0},
    )
    assert r.status_code == 404


def test_invalid_address_rejected(client):
    bad_rules = [
        {
            "rule_type": "exact",
            "match": "not-an-email",
            "action": "forward",
            "targets": [{"kind": "mailbox", "address": "a@corp.example"}],
        }
    ]
    r = _put_draft(client, 0, bad_rules)
    assert r.status_code == 422

    bad_domain = [
        {
            "rule_type": "domain",
            "match": "not_a_domain",
            "action": "reject",
            "targets": [],
        }
    ]
    r = _put_draft(client, 0, bad_domain)
    assert r.status_code == 422

    r = client.post("/api/v1/resolve", json={"recipient": "bad@@x.io"})
    assert r.status_code == 422


def test_duplicate_rules_rejected(client):
    dup = copy.deepcopy(VALID_RULES)
    dup.append(copy.deepcopy(VALID_RULES[0]))
    r = _put_draft(client, 0, dup)
    assert r.status_code == 422
    assert "重复规则" in r.json()["error"]["message"]


def test_empty_targets_and_reject_with_targets_rejected(client):
    r = _put_draft(
        client,
        0,
        [
            {
                "rule_type": "exact",
                "match": "a@corp.example",
                "action": "forward",
                "targets": [],
            }
        ],
    )
    assert r.status_code == 422

    r = _put_draft(
        client,
        0,
        [
            {
                "rule_type": "exact",
                "match": "a@corp.example",
                "action": "reject",
                "targets": [{"kind": "mailbox", "address": "x@corp.example"}],
            }
        ],
    )
    assert r.status_code == 422


def test_immutable_versions_reproduce_history(client):
    # v1: a -> mailbox m@corp.example
    rules_v1 = [
        {
            "rule_type": "exact",
            "match": "a@corp.example",
            "action": "forward",
            "targets": [{"kind": "mailbox", "address": "m@corp.example"}],
        }
    ]
    _put_draft(client, 0, rules_v1)
    pub1 = client.post("/api/v1/publish", json={"expected_revision": 1}).json()
    v1 = pub1["version"]

    # 草稿改成 a -> mailbox n@corp.example 并发布 v2
    rules_v2 = [
        {
            "rule_type": "exact",
            "match": "a@corp.example",
            "action": "forward",
            "targets": [{"kind": "mailbox", "address": "n@corp.example"}],
        }
    ]
    _put_draft(client, 1, rules_v2)
    pub2 = client.post("/api/v1/publish", json={"expected_revision": 2}).json()
    v2 = pub2["version"]
    assert v2 > v1

    # 当前版解析到 n
    r = client.post("/api/v1/resolve", json={"recipient": "a@corp.example"})
    assert {e["address"] for e in r.json()["endpoints"]} == {"n@corp.example"}
    assert r.json()["version"] == v2

    # 指定历史 v1 仍复现旧结果
    r = client.post(
        "/api/v1/resolve", json={"recipient": "a@corp.example", "version": v1}
    )
    assert r.status_code == 200
    assert {e["address"] for e in r.json()["endpoints"]} == {"m@corp.example"}
    assert r.json()["version"] == v1

    # 版本快照接口可查询
    snap = client.get(f"/api/v1/versions/{v1}").json()
    assert snap["revision"] == 1
    assert snap["rules"][0]["targets"][0]["address"] == "m@corp.example"

    versions = client.get("/api/v1/versions").json()["versions"]
    assert [v["version"] for v in versions] == [v2, v1]


def test_failed_publish_keeps_old_version(client):
    # 先成功发布 v1
    rules = [
        {
            "rule_type": "exact",
            "match": "a@corp.example",
            "action": "forward",
            "targets": [{"kind": "mailbox", "address": "m@corp.example"}],
        }
    ]
    _put_draft(client, 0, rules)
    v1 = client.post("/api/v1/publish", json={"expected_revision": 1}).json()["version"]

    # 草稿改成带环，发布失败
    cyclic = [
        {
            "rule_type": "exact",
            "match": "x@corp.example",
            "action": "forward",
            "targets": [{"kind": "alias", "address": "y@corp.example"}],
        },
        {
            "rule_type": "exact",
            "match": "y@corp.example",
            "action": "forward",
            "targets": [{"kind": "alias", "address": "x@corp.example"}],
        },
    ]
    _put_draft(client, 1, cyclic)
    r = client.post("/api/v1/publish", json={"expected_revision": 2})
    assert r.status_code == 422

    # latest 仍指向旧版 v1 且结果不变
    r = client.post("/api/v1/resolve", json={"recipient": "a@corp.example"})
    assert r.status_code == 200
    assert r.json()["version"] == v1
    assert {e["address"] for e in r.json()["endpoints"]} == {"m@corp.example"}


def test_publish_requires_matching_revision(client):
    _put_draft(client, 0, VALID_RULES)
    r = client.post("/api/v1/publish", json={"expected_revision": 0})
    assert r.status_code == 409
    assert r.json()["error"]["details"]["current_revision"] == 1


def test_resolve_without_published_version(client):
    r = client.post(
        "/api/v1/resolve", json={"recipient": "a@corp.example"}
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


def test_resolve_get_form(client):
    _put_draft(client, 0, VALID_RULES)
    client.post("/api/v1/publish", json={"expected_revision": 1})
    r = client.get("/api/v1/resolve", params={"recipient": "east@corp.example"})
    assert r.status_code == 200
    assert {e["address"] for e in r.json()["endpoints"]} == {"b@corp.example"}


def test_nonexistent_version_404(client):
    r = client.post(
        "/api/v1/resolve", json={"recipient": "a@corp.example", "version": 999}
    )
    assert r.status_code == 404


def test_diamond_expansion_dedup_keeps_all_paths(client):
    # 菱形：top -> l, r ; l -> bottom ; r -> bottom ; bottom -> mailbox z
    rules = [
        {
            "rule_type": "exact", "match": "top@corp.example", "action": "forward",
            "targets": [
                {"kind": "alias", "address": "l@corp.example"},
                {"kind": "alias", "address": "r@corp.example"},
            ],
        },
        {
            "rule_type": "exact", "match": "l@corp.example", "action": "forward",
            "targets": [{"kind": "alias", "address": "bottom@corp.example"}],
        },
        {
            "rule_type": "exact", "match": "r@corp.example", "action": "forward",
            "targets": [{"kind": "alias", "address": "bottom@corp.example"}],
        },
        {
            "rule_type": "exact", "match": "bottom@corp.example", "action": "forward",
            "targets": [{"kind": "mailbox", "address": "z@corp.example"}],
        },
    ]
    _put_draft(client, 0, rules)
    client.post("/api/v1/publish", json={"expected_revision": 1})
    r = client.post("/api/v1/resolve", json={"recipient": "top@corp.example"})
    assert r.status_code == 200
    endpoints = {e["address"]: e["paths"] for e in r.json()["endpoints"]}
    assert set(endpoints) == {"z@corp.example"}
    paths = endpoints["z@corp.example"]
    assert len(paths) == 2  # 终点去重，但两条到达路径都保留
    assert ["top@corp.example", "l@corp.example", "bottom@corp.example", "z@corp.example"] in paths
    assert ["top@corp.example", "r@corp.example", "bottom@corp.example", "z@corp.example"] in paths


def test_alias_target_type_must_resolve_but_mailbox_need_not(client):
    # mailbox 目标不需要有规则；alias 目标必须可解析
    rules = [
        {
            "rule_type": "exact", "match": "a@corp.example", "action": "forward",
            "targets": [{"kind": "mailbox", "address": "free@external.example"}],
        },
    ]
    _put_draft(client, 0, rules)
    r = client.post("/api/v1/publish", json={"expected_revision": 1})
    assert r.status_code == 200
    r = client.post("/api/v1/resolve", json={"recipient": "a@corp.example"})
    assert r.status_code == 200
    assert {e["address"] for e in r.json()["endpoints"]} == {"free@external.example"}
