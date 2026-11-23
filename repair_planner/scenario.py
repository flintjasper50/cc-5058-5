"""场景构建与观察政策。

关键设计：
- world（真世界）与 belief（角色信念）是两份 State。规划器只接收 belief。
- 初始信念只包含公开布局信息；故障、备件箱是否被取走、捐赠件是否可用等
  隐藏信息必须通过 inspect/open_box 等感知动作观察后才进入信念。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from .model import (
    State,
    at,
    installed,
    fault,
    inspected,
    seal_broken,
    box_taken,
    observed_place,
)

# 导航图（无向），边权即移动代价
ADJACENCY = {
    "office": {"corridor": 2},
    "corridor": {"office": 2, "pump_room": 3, "storage": 2, "workshop": 2},
    "pump_room": {"corridor": 3},
    "storage": {"corridor": 2},
    "workshop": {"corridor": 2},
}
PLACES = tuple(ADJACENCY.keys())

DEVICES = ("pump", "donor_unit")
TOOL_DEFAULT = {
    "wrench": "workshop",
    "multimeter": "office",
    "pliers": "storage",
}
DEVICE_PLACE = {"pump": "pump_room", "donor_unit": "workshop"}

FAULT_TYPES = ("loose_bolts", "fuse_blown", "filter_clogged",
               "valve_worn", "valve_seized")


@dataclass
class Scenario:
    name: str
    fault: str                          # 真故障（对角色隐藏）
    tool_at: dict                       # 工具位置变体
    box_taken_by_mate: bool             # 备件箱是否被同伴取走
    donor_valve_available: bool         # 捐赠设备上是否有可拆阀门
    gasket_available: bool              # 工作台柜中是否有新密封垫
    seal_intact: bool = True
    description: str = ""

    def goal(self) -> tuple:
        return ("working", "pump")


def _build_world(scn: Scenario) -> State:
    s = State()
    facts = [at("me", "corridor")]
    # 工具
    for tool, place in scn.tool_at.items():
        facts.append(at(tool, place))
    # 设备
    facts.append(at("pump", "pump_room"))
    facts.append(installed("valve_old", "pump"))
    facts.append(at("donor_unit", "workshop"))
    if scn.donor_valve_available:
        facts.append(installed("valve_donor", "donor_unit"))
    # 封条（seal_on 是泵上的出厂封条；storage 中的 seal_sticker 是备用贴纸库存）
    facts.append(("seal_on", "pump", "yes") if scn.seal_intact else
                 seal_broken("pump"))
    facts.append(at("seal_sticker", "storage"))
    # 备件箱
    facts.append(at("spare_box", "storage"))
    if scn.box_taken_by_mate:
        facts.append(box_taken("spare_box"))
    else:
        facts.append(at("valve_new", "spare_box"))
    # 与故障配套的小件备件放在 storage 货架（公开可见）
    if scn.fault == "fuse_blown":
        facts.append(at("fuse_new", "storage"))
    if scn.fault == "filter_clogged":
        facts.append(at("filter_new", "storage"))
    # 工作台密封垫（封条已拆路线翻新旧阀所需）
    if scn.gasket_available:
        facts.append(at("gasket_new", "workshop"))
    # 真故障
    facts.append(fault(scn.fault, "pump"))
    return State(frozenset(facts), frozenset(), frozenset())


def expected_items(scn: Scenario) -> frozenset[str]:
    """该场景中真实存在的物品集合（守恒审计范围）。"""
    items = set(scn.tool_at.keys()) | {"valve_old", "seal_sticker"}
    if not scn.box_taken_by_mate:
        items.add("valve_new")
    if scn.donor_valve_available:
        items.add("valve_donor")
    if scn.gasket_available:
        items.add("gasket_new")
    if scn.fault == "fuse_blown":
        items.add("fuse_new")
    if scn.fault == "filter_clogged":
        items.add("filter_new")
    return frozenset(items)


def _build_initial_belief(scn: Scenario) -> State:
    """角色开场知道的内容：地图、物品公开位置；不知道故障与箱内情况。"""
    facts = [at("me", "corridor"), observed_place("corridor")]
    for tool, place in scn.tool_at.items():
        facts.append(at(tool, place))
        facts.append(observed_place(place))
    facts += [
        at("pump", "pump_room"),
        installed("valve_old", "pump"),
        at("donor_unit", "workshop"),
        at("spare_box", "storage"),
        at("seal_sticker", "storage"),
    ]
    # 捐赠设备上的阀门属于"另寻配件"信息：角色抵达工作台观察前不进入信念，
    # 但真世界始终存在，观察后即可取用（不凭空生成，守恒审计可核对）。
    if scn.gasket_available:
        facts.append(at("gasket_new", "workshop"))
    if scn.fault == "fuse_blown":
        facts.append(at("fuse_new", "storage"))
    if scn.fault == "filter_clogged":
        facts.append(at("filter_new", "storage"))
    # 泵上有封条是可远观的公开信息
    if scn.seal_intact:
        facts.append(("seal_on", "pump", "yes"))
    # 任务简报预期"备件箱里有新阀"——这是未经观察的假设，不是已知事实
    assumptions = (at("valve_new", "spare_box"),)
    # 注意：不加入 fault、box_taken、valve_donor、damaged 等隐藏事实
    return State(frozenset(facts), frozenset(), frozenset(assumptions))


def make_world(scn: Scenario) -> tuple[State, State]:
    return _build_world(scn), _build_initial_belief(scn)


def observe_place(world: State, belief: State, place: str) -> State:
    """进入某地后：该地公开摆放的物品进入信念；容器内部仍不暴露。"""
    add = [observed_place(place)]
    # 角色位置以真世界为准，先清除信念中的旧位置
    old_positions = belief.matches(("at", "me", "?"))
    s = belief.with_facts(
        add=tuple(add + [at("me", place)]),
        remove=tuple(old_positions),
    )
    for f in world.matches(("at", "?", place)):
        item = f[1]
        if item == "spare_box":
            s = s.with_facts(add=(at("spare_box", place),))  # 只看到箱子，看不到箱内
        else:
            s = s.with_facts(add=(f,))
    for f in world.matches(("holder", "?", "?")):
        s = s.with_facts(add=(f,))
    for f in world.matches(("installed", "?", "?")):
        dev = f[2]
        if world.location_of(dev) == place:
            # 已拆封设备的内部件、以及捐赠设备的外置阀门可直接观察
            if world.has(seal_broken(dev)) or f[1] == "gasket_new" or \
                    dev == "donor_unit":
                s = s.with_facts(add=(f,))
    return s


def observe_inspection(world: State, belief: State, device: str) -> State:
    """万用表检查：故障类型、封条状态、当前内部件进入信念（真世界取样）。"""
    add = [inspected(device)]
    remove: list = []
    for f in world.matches(("fault", "?", device)):
        add.append(f)
    if world.has(seal_broken(device)):
        add.append(seal_broken(device))
        remove.append(("seal_on", device, "yes"))
    for f in world.matches(("seal_on", device, "?")):
        add.append(f)
        remove.append(seal_broken(device))
    for f in world.matches(("installed", "?", device)):
        add.append(f)
    for f in world.matches(("damaged", "?")):
        if world.location_of(f[1]) is not None:
            add.append(f)
    return belief.with_facts(add=tuple(add), remove=tuple(remove))


def observe_open_box(world: State, belief: State) -> State:
    """打开备件箱：同伴取走则只获得 box_taken 并删除箱内假设；否则箱内物品进入信念。"""
    add = [("box_checked", "spare_box")]
    if world.has(box_taken("spare_box")):
        add.append(box_taken("spare_box"))
        # 观察证伪：删除关于箱内物品的全部假设（防止凭规划回滚造出零件）
        removed = tuple(f for f in belief.assumptions
                        if f[0] == "at" and f[2] == "spare_box")
        return belief.with_facts(add=tuple(add)).with_assumptions(remove=removed)
    for f in world.matches(("at", "?", "spare_box")):
        add.append(f)
    # 箱内物品被观察确认：从假设提升为事实
    promoted = tuple(f for f in belief.assumptions
                     if f[0] == "at" and f[2] == "spare_box" and f in add)
    return belief.with_facts(add=tuple(add)).with_assumptions(remove=promoted)


# ---- 预置场景 ----
def scenarios() -> dict[str, Scenario]:
    return {
        "save_a_reversible": Scenario(
            name="存档A：磨损阀门，旧件可翻新装回",
            fault="valve_worn",
            tool_at=dict(TOOL_DEFAULT),
            box_taken_by_mate=True,
            donor_valve_available=False,
            gasket_available=True,
            description="拆下旧阀后发现备件箱被同伴取走；磨损旧件未损坏，"
                        "可回退为：翻新旧阀 + 新密封垫装回。",
        ),
        "save_b_seal_broken": Scenario(
            name="存档B：阀门卡死且封条已拆，捐赠件不可用",
            fault="valve_seized",
            tool_at=dict(TOOL_DEFAULT),
            box_taken_by_mate=True,
            donor_valve_available=False,
            gasket_available=True,
            seal_intact=False,
            description="拆封已发生且卡死阀门拆下即损坏；无法装回旧件，"
                        "也无捐赠件，目标不可行，规划器必须给出无解结论。",
        ),
        "save_b2_donor": Scenario(
            name="存档B2：卡死阀门，但捐赠设备有可用阀门",
            fault="valve_seized",
            tool_at=dict(TOOL_DEFAULT),
            box_taken_by_mate=True,
            donor_valve_available=True,
            gasket_available=True,
            seal_intact=False,
            description="封条已拆、旧阀损坏；只能从当前半成品转用捐赠阀门路线。",
        ),
        "bolt_case": Scenario(
            name="螺栓松动：低成本故障变体",
            fault="loose_bolts",
            tool_at=dict(TOOL_DEFAULT),
            box_taken_by_mate=False,
            donor_valve_available=False,
            gasket_available=False,
            description="无需备件的维修路线，用于核对代价与固定动画播放不同。",
        ),
        "fuse_case": Scenario(
            name="保险丝熔断 + 工具移位变体",
            fault="fuse_blown",
            tool_at={"wrench": "storage", "multimeter": "pump_room",
                     "pliers": "workshop"},
            box_taken_by_mate=False,
            donor_valve_available=False,
            gasket_available=False,
            description="工具位置全部换位，核对导航代价驱动的计划差异。",
        ),
        "no_tool_case": Scenario(
            name="不可行目标：万用表被移除，无法诊断",
            fault="fuse_blown",
            tool_at={"wrench": "workshop", "pliers": "storage"},  # 无 multimeter
            box_taken_by_mate=False,
            donor_valve_available=False,
            gasket_available=False,
            description="移除关键工具使目标不可行，核对无解报告与搜索成本。",
        ),
        "no_part_case": Scenario(
            name="不可行目标：备件全无且无法恢复旧件",
            fault="valve_seized",
            tool_at=dict(TOOL_DEFAULT),
            box_taken_by_mate=True,
            donor_valve_available=False,
            gasket_available=False,
            seal_intact=False,
            description="新阀被取走、旧阀损坏、无捐赠件、无密封垫，多重不可行。",
        ),
    }
