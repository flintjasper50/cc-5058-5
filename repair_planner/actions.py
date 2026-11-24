"""动作模式（STRIPS 风格）：前提、效果、代价与感知标记。

边界纪律（规划/执行解耦）：
- 本模块只描述动作的纯语义：available_actions 生成候选、apply 给出确定性效果。
- 规划分支的"模拟前提/模拟效果"与执行器的"真实执行效果"调用同一个 apply，
  保证预期效果与确认效果语义一致；区别在于执行器先让真世界校验前提、
  并通过受控回执入口提交，模拟器只在私有快照上推演。
- 本模块不读取、不持有任何真世界状态，也不产生副作用。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from .model import (
    State,
    Fact,
    at,
    carrying,
    installed,
    fault,
    inspected,
    working,
    damaged,
    reconditioned,
    seal_broken,
    box_taken,
)
# 物品的标准存放点：放下操作只允许放回工位/货架，禁止随地丢弃，
# 避免无意义的物品-地点排列撑爆搜索空间（移动与抓取仍按真实代价计算）。
HOME_PLACE = {
    "wrench": "workshop",
    "multimeter": "office",
    "pliers": "storage",
    "seal_sticker": "storage",
    "gasket_new": "workshop",
    "fuse_new": "storage",
    "filter_new": "storage",
    "valve_new": "storage",
    "valve_old": "workshop",
    "valve_donor": "workshop",
}
GRABBABLE = frozenset(HOME_PLACE.keys())

CARRY_LIMIT = 2

# 故障 -> 可消除该故障的维修方式（角色的通用维修知识，非隐藏信息）
REPAIR_RULES = {
    "loose_bolts": {"kind": "tighten"},
    "fuse_blown": {"kind": "fuse", "part": "fuse_new"},
    "filter_clogged": {"kind": "filter", "part": "filter_new"},
    "valve_worn": {"kind": "valve"},
    "valve_seized": {"kind": "valve"},
}

VALVE_PARTS = ("valve_new", "valve_old", "valve_donor")


@dataclass(frozen=True)
class Action:
    kind: str
    args: Tuple[str, ...]
    cost: int
    pre: Tuple[Fact, ...]
    sense: bool = False  # 感知动作：执行后由观察更新信念，而非直接套用效果

    def label(self) -> str:
        return f"{self.kind}({', '.join(self.args)})"

    def rationale(self, state: State) -> str:
        """逐条列出该动作在当前状态下成立的前提依据。"""
        lines = [f"{'、'.join(str(p) for p in self.pre)} 均成立"]
        if self.kind == "move":
            lines.append(f"移动代价 {self.cost} 来自导航图边权")
        else:
            lines.append(f"动作资源代价 {self.cost}")
        return "；".join(lines)


def _held(state: State) -> list[str]:
    return [f[2] for f in state.matches(("holder", "me", "?"))]


def _my_place(state: State) -> str | None:
    r = state.matches(("at", "me", "?"))
    return r[0][2] if r else None


def _visible_items_here(state: State, place: str) -> list[str]:
    """当前地点可抓取物：已确认的 + 尚未证伪的假设位置。

    假设备件在备件箱内（at item spare_box）时，若角色已在箱子所在房间，
    规划层面视为可开箱取用；真世界执行 take 时再证真/证伪。
    """
    items = {f[1] for f in state.matches(("at", "?", place))}
    items |= {f[1] for f in state.assumptions
              if len(f) == 3 and f[0] == "at" and
              (f[2] == place or
               (f[2] == "spare_box" and
                state.location_of("spare_box") == place))}
    items.discard("me")
    return list(items)


def _pre_holds(state: State, pre: tuple) -> bool:
    return all(state.believes(p) for p in pre)


def available_actions(state: State, adjacency: dict) -> list[Action]:
    place = _my_place(state)
    if place is None:
        return []
    held = _held(state)
    actions: list[Action] = []

    # 1. 导航移动（代价为边权，不是统一常量）
    for nxt, weight in adjacency.get(place, {}).items():
        actions.append(Action("move", (nxt,), weight, (at("me", place),)))

    # 2. 抓取（含容量这一资源条件；假设位置也生成动作，执行时再证真/证伪）
    if len(held) < CARRY_LIMIT:
        for item in _visible_items_here(state, place):
            if item not in GRABBABLE:
                continue  # 设备与备件箱等固定物不可抓取
            pre = [at("me", place)]
            if state.believes(at(item, place)):
                pre.append(at(item, place))
            elif state.believes(at(item, "spare_box")):
                # 箱内假设件：前提是箱子就在此地（执行时由真世界证真/证伪）
                pre.append(at("spare_box", place))
            actions.append(Action("take", (item,), 1, tuple(pre)))

    # 3. 放下（仅允许放回标准存放点，控制搜索空间）
    for item in held:
        if HOME_PLACE.get(item) == place:
            actions.append(Action("drop", (item,), 1,
                                  (carrying("me", item), at("me", place))))

    here_devices = [f[1] for f in state.matches(("at", "?", place))
                    if f[1] in ("pump", "donor_unit")]

    # 4. 万用表检查设备（感知动作；当前维修任务只针对泵）
    if "multimeter" in held:
        for dev in here_devices:
            if dev != "pump" or state.has(inspected(dev)):
                continue
            actions.append(Action(
                "inspect", (dev,), 2,
                (at("me", place), at(dev, place), carrying("me", "multimeter")),
                sense=True,
            ))

    # 5. 打开备件箱（感知动作：同伴是否取走只有打开才知道；仅一次）
    if state.has(at("spare_box", place)):
        already_checked = state.any_fact(("box_checked",)) or \
            state.has(box_taken("spare_box"))
        if not already_checked:
            actions.append(Action(
                "open_box", ("spare_box",), 1,
                (at("me", place), at("spare_box", place)),
                sense=True,
            ))

    # 6. 拆封条（不可逆）
    for dev in here_devices:
        if not state.has(seal_broken(dev)) and state.matches(("seal_on", dev, "?")):
            actions.append(Action(
                "break_seal", (dev,), 1,
                (at("me", place), at(dev, place), ("seal_on", dev, "yes")),
            ))

    # 7. 拆阀门（主泵要求封条已拆；卡住的故障会损坏旧件——见 apply）
    for f in state.matches(("installed", "?", "?")):
        part, dev = f[1], f[2]
        dev_place = state.location_of(dev)
        if dev_place != place:
            continue
        if part not in VALVE_PARTS:
            continue
        pre = [at("me", place), at(dev, place), carrying("me", "wrench"),
               installed(part, dev)]
        if dev == "pump":
            pre.append(seal_broken("pump"))
        actions.append(Action("remove_valve", (part, dev), 3, tuple(pre)))

    # 8. 装阀门（旧件必须已翻新；翻新件需要新密封垫；新阀自带密封垫）
    for dev in here_devices:
        for part in VALVE_PARTS:
            if part not in held or state.has(damaged(part)):
                continue
            if part == "valve_old" and not state.has(reconditioned("valve_old")):
                continue
            if state.has(installed("valve_old", dev)) or \
               state.has(installed("valve_new", dev)) or \
               state.has(installed("valve_donor", dev)):
                continue
            pre = [at("me", place), at(dev, place), carrying("me", "wrench"),
                   carrying("me", part)]
            if part == "valve_old":
                pre.append(carrying("me", "gasket_new"))
            actions.append(Action("install_valve", (part, dev), 3, tuple(pre)))

    # 9. 紧固螺栓
    if "wrench" in held and state.has(installed("valve_old", "pump")):
        for dev in here_devices:
            actions.append(Action(
                "tighten_bolts", (dev,), 2,
                (at("me", place), carrying("me", "wrench"),
                 installed("valve_old", dev)),
            ))

    # 10. 换保险丝 / 换滤芯
    if "pliers" in held:
        for dev in here_devices:
            if "fuse_new" in held:
                actions.append(Action(
                    "replace_fuse", (dev,), 2,
                    (at("me", place), at(dev, place),
                     carrying("me", "pliers"), carrying("me", "fuse_new")),
                ))
            if "filter_new" in held:
                actions.append(Action(
                    "replace_filter", (dev,), 2,
                    (at("me", place), at(dev, place),
                     carrying("me", "pliers"), carrying("me", "filter_new")),
                ))

    # 11. 工作台翻新旧阀（未损坏才可翻新）
    if place == "workshop" and "wrench" in held:
        for part in ("valve_old",):
            loc = state.location_of(part)
            if loc in ("me", "workshop") and not state.has(damaged(part)) \
                    and not state.has(reconditioned(part)):
                pre = [at("me", "workshop"), carrying("me", "wrench")]
                pre.append(carrying("me", part) if loc == "me" else at(part, "workshop"))
                actions.append(Action("recondition", (part,), 3, tuple(pre)))

    # 12. 重新贴封条
    if "seal_sticker" in held:
        for dev in here_devices:
            if state.has(seal_broken(dev)):
                actions.append(Action(
                    "apply_seal", (dev,), 1,
                    (at("me", place), carrying("me", "seal_sticker"),
                     seal_broken(dev)),
                ))

    return [a for a in actions if _pre_holds(state, a.pre)]


def _remove_holder(state: State, item: str, place: str) -> State:
    return state.with_facts(
        add=(at(item, place),),
        remove=(carrying("me", item),),
    )


def apply(state: State, action: Action) -> State:
    """确定性效果推演（纯函数）。

    规划器的分支模拟器与执行器的真世界提交共用本函数，因此"模拟效果"与
    "真实执行效果"版本同源；任何一方都不得绕过本函数直接改写状态。
    """
    kind, args = action.kind, action.args

    if kind == "move":
        old = _my_place(state)
        return state.with_facts(add=(at("me", args[0]),), remove=(at("me", old),))

    if kind == "take":
        item = args[0]
        place = _my_place(state)
        return state.with_facts(
            add=(carrying("me", item),), remove=(at(item, place),)
        )

    if kind == "drop":
        item = args[0]
        place = _my_place(state)
        return _remove_holder(state, item, place)

    if kind == "inspect":
        return state.with_facts(add=(inspected(args[0]),))

    if kind == "open_box":
        return state.with_facts(add=(("box_checked", "spare_box"),))

    if kind == "break_seal":
        dev = args[0]
        return state.with_facts(
            add=(seal_broken(dev),), remove=(("seal_on", dev, "yes"),)
        )

    if kind == "remove_valve":
        part, dev = args
        place = state.location_of(dev)
        s = state.with_facts(
            add=(at(part, place),), remove=(installed(part, dev),)
        )
        # 卡住的阀门拆下即损坏（真实世界/已知故障的信念中才会出现该条件）
        if part == "valve_old" and s.has(fault("valve_seized", dev)):
            s = s.with_facts(add=(damaged("valve_old"),))
        return s

    if kind == "install_valve":
        part, dev = args
        place = _my_place(state)
        add = [installed(part, dev)]
        remove = [carrying("me", part)]
        s = state
        if part == "valve_old":
            # 翻新件消耗新密封垫，垫子进入设备，资源不凭空消失
            add.append(installed("gasket_new", dev))
            remove.append(carrying("me", "gasket_new"))
        s = s.with_facts(add=tuple(add), remove=tuple(remove))
        # 维修规则：阀门类故障装上完好阀门即修复
        for f in ("valve_worn", "valve_seized"):
            if s.has(fault(f, dev)):
                s = s.with_facts(add=(working(dev),), remove=(fault(f, dev),))
        return s

    if kind == "tighten_bolts":
        dev = args[0]
        if state.has(fault("loose_bolts", dev)):
            return state.with_facts(add=(working(dev),),
                                    remove=(fault("loose_bolts", dev),))
        return state

    if kind == "replace_fuse":
        dev = args[0]
        s = state.with_facts(
            add=(installed("fuse_new", dev),),
            remove=(carrying("me", "fuse_new"),),
        )
        if s.has(fault("fuse_blown", dev)):
            s = s.with_facts(add=(working(dev),),
                             remove=(fault("fuse_blown", dev),))
        return s

    if kind == "replace_filter":
        dev = args[0]
        s = state.with_facts(
            add=(installed("filter_new", dev),),
            remove=(carrying("me", "filter_new"),),
        )
        if s.has(fault("filter_clogged", dev)):
            s = s.with_facts(add=(working(dev),),
                             remove=(fault("filter_clogged", dev),))
        return s

    if kind == "recondition":
        part = args[0]
        s = state
        if s.has(carrying("me", part)):
            pass
        else:
            s = s.with_facts(add=(carrying("me", part),),
                             remove=(at(part, "workshop"),))
        return s.with_facts(add=(reconditioned(part),))

    if kind == "apply_seal":
        dev = args[0]
        return state.with_facts(
            add=(installed("seal_sticker", dev),),
            remove=(carrying("me", "seal_sticker"), seal_broken(dev)),
        )

    raise ValueError(f"未知动作 {kind}")
