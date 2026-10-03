"""路由展开引擎。

输入一个不可变的规则集合（草稿快照或某历史版本快照），
对给定收件地址逐跳展开：

* 每一跳先查精确规则，再查域兜底规则，都没有即 NO_ROUTE；
* 命中 reject 规则即 REJECTED，整次判定失败；
* forward 规则中：
    - alias 目标继续逐跳展开（可能再次命中精确/兜底规则）；
    - mailbox 目标即终点，不再递归；
* 终点去重，并保留到达每个终点的全部路径；
* 任一分支拒收 / 无路由 / 成环，整次失败，不返回部分结果。
"""
from dataclasses import dataclass, field

from .validators import split_domain


@dataclass(frozen=True)
class Target:
    kind: str          # "alias" | "mailbox"
    address: str


@dataclass(frozen=True)
class Rule:
    rule_type: str     # "exact" | "domain"
    match_key: str
    action: str        # "forward" | "reject"
    targets: tuple[Target, ...]


@dataclass
class RuleSet:
    exact: dict[str, Rule] = field(default_factory=dict)
    domain: dict[str, Rule] = field(default_factory=dict)

    def lookup(self, address: str) -> Rule | None:
        """逐跳选路：精确规则优先，其次域兜底。"""
        rule = self.exact.get(address)
        if rule is not None:
            return rule
        return self.domain.get(split_domain(address))


class ResolveError(Exception):
    def __init__(self, code: str, message: str, path: list[str]):
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path


def resolve(rule_set: RuleSet, recipient: str, max_hops: int = 100) -> dict[str, list[list[str]]]:
    """展开收件地址。

    成功返回 {终点邮箱: [路径...]}，路径为从收件人到终点的地址序列；
    失败抛 ResolveError（第一个出错的分支）。
    """
    results: dict[str, list[tuple[str, ...]]] = {}

    def expand(addr: str, path: tuple[str, ...], hops: int) -> None:
        # 成环：当前路径上已经展开过该地址
        if addr in path:
            cycle = list(path[path.index(addr):]) + [addr]
            raise ResolveError("cycle", f"检测到路由环: {' -> '.join(cycle)}", list(path + (addr,)))
        if hops > max_hops:
            raise ResolveError(
                "max_hops_exceeded",
                f"展开跳数超过上限 {max_hops}，疑似存在环路",
                list(path + (addr,)),
            )

        rule = rule_set.lookup(addr)
        if rule is None:
            raise ResolveError("no_route", f"无路由规则: {addr}", list(path + (addr,)))
        if rule.action == "reject":
            raise ResolveError("rejected", f"规则拒收: {addr}", list(path + (addr,)))

        new_path = path + (addr,)
        for target in rule.targets:
            if target.kind == "mailbox":
                bucket = results.setdefault(target.address, [])
                if new_path + (target.address,) not in bucket:
                    bucket.append(new_path + (target.address,))
            else:
                expand(target.address, new_path, hops + 1)

    expand(recipient, (), 0)

    # 稳定输出：终点按地址排序，同终点路径按字典序排序
    ordered: dict[str, list[list[str]]] = {}
    for mailbox in sorted(results):
        ordered[mailbox] = [list(p) for p in sorted(results[mailbox])]
    return ordered


def validate_publish(rule_set: RuleSet) -> list[dict]:
    """发布前校验：所有 alias 目标可解析且规则图无环。

    以“规则”为节点、alias 边为边做 DFS 三色遍历。
    返回问题列表（为空表示通过）。
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[tuple[str, str], int] = {}
    issues: list[dict] = []

    def rule_id(rule: Rule) -> tuple[str, str]:
        return rule.rule_type, rule.match_key

    def visit(rule: Rule, stack: list[tuple[str, str]]) -> None:
        rid = rule_id(rule)
        if color.get(rid) == GRAY:
            idx = stack.index(rid)
            issues.append(
                {
                    "type": "cycle",
                    "cycle": [f"{t}:{k}" for t, k in stack[idx:]]
                    + [f"{rid[0]}:{rid[1]}"],
                }
            )
            return
        if color.get(rid) == BLACK:
            return
        color[rid] = GRAY
        stack.append(rid)
        for target in rule.targets:
            if target.kind != "alias":
                continue
            child = rule_set.lookup(target.address)
            if child is None:
                issues.append(
                    {
                        "type": "unresolvable_alias",
                        "alias": target.address,
                        "from": f"{rule.rule_type}:{rule.match_key}",
                    }
                )
                continue
            visit(child, stack)
        stack.pop()
        color[rid] = BLACK

    # 遍历顺序固定，错误信息可复现
    all_rules = sorted(rule_set.exact.values(), key=lambda r: r.match_key) + sorted(
        rule_set.domain.values(), key=lambda r: r.match_key
    )
    for rule in all_rules:
        if rule.action == "forward" and color.get(rule_id(rule), WHITE) == WHITE:
            visit(rule, [])
    return issues
