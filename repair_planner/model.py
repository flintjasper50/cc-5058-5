"""世界状态与事实模型。

State 是不可变快照：facts 为元组化事实集合，counts 记录可消耗资源（如封条）。
真世界(World)与角色信念(Belief)使用同一结构，但填充内容遵循观察政策：
信念中只允许出现角色已观察/被告知的事实，不允许直接读取隐藏状态。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Tuple


Fact = Tuple[str, ...]


def at(obj: str, place: str) -> Fact:
    return ("at", obj, place)


def carrying(who: str, obj: str) -> Fact:
    return ("holder", who, obj)


def installed(part: str, device: str) -> Fact:
    return ("installed", part, device)


def fault(f: str, device: str) -> Fact:
    return ("fault", f, device)


def inspected(device: str) -> Fact:
    return ("inspected", device)


def working(device: str) -> Fact:
    return ("working", device)


def damaged(part: str) -> Fact:
    return ("damaged", part)


def reconditioned(part: str) -> Fact:
    return ("reconditioned", part)


def seal_broken(device: str) -> Fact:
    return ("seal_broken", device)


def box_taken(box: str) -> Fact:
    return ("box_taken", box)


def observed_place(place: str) -> Fact:
    return ("observed_place", place)


@dataclass(frozen=True)
class State:
    facts: FrozenSet[Fact] = frozenset()
    counts: FrozenSet[Tuple[str, int]] = frozenset()
    # 假设事实：角色基于常识/任务简报的预期，未经观察确认。
    # 规划器允许据此推理；一旦观察证伪，执行器必须删除并触发重规划。
    assumptions: FrozenSet[Fact] = frozenset()

    def has(self, fact: Fact) -> bool:
        return fact in self.facts

    def believes(self, fact: Fact) -> bool:
        """信念成立 = 已确认事实或尚未被证伪的假设。"""
        return fact in self.facts or fact in self.assumptions

    def any_fact(self, prefix: Fact) -> bool:
        return any(f[: len(prefix)] == prefix for f in self.facts)

    def matches(self, pattern: Fact, include_assumptions: bool = False) -> Tuple[Fact, ...]:
        pool = list(self.facts)
        if include_assumptions:
            pool = pool + list(self.assumptions)
        out = []
        for f in pool:
            if len(f) != len(pattern):
                continue
            ok = all(p == "?" or p == x for p, x in zip(pattern, f))
            if ok:
                out.append(f)
        return tuple(out)

    def count(self, key: str) -> int:
        for k, v in self.counts:
            if k == key:
                return v
        return 0

    def with_facts(self, add: Tuple[Fact, ...] = (), remove: Tuple[Fact, ...] = ()) -> "State":
        facts = set(self.facts)
        facts.difference_update(remove)
        facts.update(add)
        return State(frozenset(facts), self.counts, self.assumptions)

    def with_count(self, key: str, value: int) -> "State":
        counts = {k: v for k, v in self.counts}
        counts[key] = value
        return State(self.facts, frozenset(counts.items()), self.assumptions)

    def with_assumptions(self, add: Tuple[Fact, ...] = (),
                         remove: Tuple[Fact, ...] = ()) -> "State":
        asm = set(self.assumptions)
        asm.difference_update(remove)
        asm.update(add)
        return State(self.facts, self.counts, frozenset(asm))

    def location_of(self, obj: str, who: str = "me",
                    include_assumptions: bool = False) -> str | None:
        for f in self.facts:
            if f[0] == "at" and f[1] == obj:
                return f[2]
        for f in self.facts:
            if f[0] == "holder" and f[1] == who and f[2] == obj:
                return who
        if include_assumptions:
            for f in self.assumptions:
                if f[0] == "at" and f[1] == obj:
                    return f[2]
        return None


# 所有实体化物品（工具 + 零件 + 封条），用于资源守恒审计
TRACKED_ITEMS = frozenset(
    {
        "wrench",
        "multimeter",
        "pliers",
        "valve_new",
        "valve_old",
        "valve_donor",
        "fuse_new",
        "filter_new",
        "gasket_new",
        "seal_sticker",
    }
)


def conservation_audit(state: State, expected_items: frozenset) -> Tuple[bool, list[str]]:
    """存在的受追踪物品必须恰好处于一个持有者/位置；不存在（未生成/已装入设备
    由 installed 表达）不算违规。封条库存计数必须与 at 事实一致。"""
    problems: list[str] = []
    for item in expected_items:
        holders = [f for f in state.facts if (f[0] == "at" and f[1] == item) or
                   (f[0] == "holder" and f[2] == item)]
        if len(holders) > 1:
            problems.append(f"{item}: 持有者数量={len(holders)} {holders}")
    # 同一件物品不能同时在两处
    seen: dict[str, list] = {}
    for f in state.facts:
        if f[0] in ("at", "holder"):
            item = f[1] if f[0] == "at" else f[2]
            if item in TRACKED_ITEMS:
                seen.setdefault(item, []).append(f)
    for item, fs in seen.items():
        if len(fs) > 1:
            problems.append(f"{item}: 同时占据多个位置 {fs}")
    seal_count = state.count("seal_sticker")
    seal_facts = state.matches(("at", "seal_sticker", "?"))
    if seal_count and seal_count != len(seal_facts):
        problems.append(f"封条计数={seal_count} 与位置事实={seal_facts} 不一致")
    return (not problems, problems)
