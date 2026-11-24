"""信念空间规划器（BFS 最短序列 + 真实代价累计 + 搜索成本统计）。

规划/执行解耦边界：
- 规划器只消费显式快照（State.snapshot()），绝不导入执行器、不持有真世界；
  搜索过程的每一步推演都走分支模拟器（私有资源表、独立版本），
  取消分支或弹出已预约步骤不会消耗现场配件。
- 阶段政策（对未观察信息的明确处理）：
  泵未检查前，故障未知，任何维修动作都无法被证明相关：规划器只考虑
  导航、抓取、放下、检查、开箱这类信息收集动作，避免在搬运动作组合中爆炸。
- 检查获得故障后，开放完整维修动作集合进行目标搜索。
- BFS 保证动作步数最短；路径上每步的移动边权/资源代价另行累计为真实成本。
- 主目标不可达时返回最近感知动作计划（NEED_OBSERVATION），由执行层观察后重规划。
- 信念只含已观察事实与显式假设；规划器不读取真世界。
"""
from __future__ import annotations

import collections
from dataclasses import dataclass, field
from typing import Callable

from .model import State, Fact, working
from .actions import Action, available_actions
from .simulator import simulate_step
from .scenario import ADJACENCY

NODE_LIMIT = 8000

# 信息收集阶段允许的动作
SENSE_PHASE_KINDS = frozenset(
    {"move", "take", "drop", "inspect", "open_box"}
)


@dataclass
class PlanResult:
    status: str                 # OK / NEED_OBSERVATION / INFEASIBLE / PARTIAL
    actions: list[Action] = field(default_factory=list)
    cost: int = 0
    expanded: int = 0
    generated: int = 0
    reason: str = ""
    why_infeasible: list[str] = field(default_factory=list)
    # 计划所依据的信念快照指纹：执行器提交前据此复核前提读集
    snapshot_fingerprint: tuple = ()
    _step_states: list = field(default_factory=list, repr=False)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "cost": self.cost,
            "expanded": self.expanded,
            "generated": self.generated,
            "reason": self.reason,
            "why_infeasible": self.why_infeasible,
            "snapshot_fingerprint": [list(x) for x in self.snapshot_fingerprint],
            "steps": [
                {"action": a.label(), "kind": a.kind, "sense": a.sense,
                 "cost": a.cost, "rationale": a.rationale(s),
                 "pre": [str(p) for p in a.pre]}
                for a, s in self._step_states
            ],
        }


def _key(state: State) -> tuple:
    return tuple(sorted(state.facts)), tuple(sorted(state.counts)), \
        tuple(sorted(state.assumptions))


def _bfs_dist(src: str) -> dict[str, int]:
    dist = {src: 0}
    q = [src]
    while q:
        cur = q.pop(0)
        for nxt in ADJACENCY.get(cur, {}):
            if nxt not in dist:
                dist[nxt] = dist[cur] + 1
                q.append(nxt)
    return dist


def _expand_actions(state: State) -> list[Action]:
    actions = available_actions(state, ADJACENCY)
    if state.has(("inspected", "pump")):
        return actions
    return [a for a in actions if a.kind in SENSE_PHASE_KINDS]


def _search(start: State, is_goal: Callable[[State], bool],
            stop_at_sense: bool = False) -> tuple[list, int, int, int, str]:
    """BFS：返回 (路径, 真实代价累计, 扩展数, 生成数, 备注)。"""
    start_key = _key(start)
    queue = collections.deque([start])
    parent: dict[tuple, tuple | None] = {start_key: None}
    expanded = generated = 0
    note = ""

    while queue:
        state = queue.popleft()
        key = _key(state)
        expanded += 1
        if expanded > NODE_LIMIT:
            note = f"达到节点上限 {NODE_LIMIT}，停止搜索"
            return [], -1, expanded, generated, note

        if is_goal(state):
            return _rebuild(parent, key), _path_cost(parent, key), \
                expanded, generated, note

        for action in _expand_actions(state):
            if stop_at_sense and action.sense:
                path = _rebuild(parent, key)
                path.append((action, state))
                return path, _path_cost(parent, key) + action.cost, \
                    expanded, generated + 1, note
            # 搜索后继经分支模拟器生成：私有快照/资源表/版本，
            # 该分支被剪枝或弹出时不产生任何真实配件消耗。
            sim = simulate_step(state, action, ADJACENCY, sim_version=generated)
            if not sim.ok:
                continue
            nxt = sim.after
            nkey = _key(nxt)
            if nkey not in parent:
                parent[nkey] = (key, action, state)
                generated += 1
                queue.append(nxt)

    note = note or "搜索空间穷尽"
    return [], -1, expanded, generated, note


def _path_cost(parent: dict, key: tuple) -> int:
    total = 0
    while parent[key] is not None:
        pkey, action, _ = parent[key]
        total += action.cost
        key = pkey
    return total


# 兼容旧名称
_astar = _search


def _rebuild(parent: dict, key: tuple) -> list:
    path = []
    while parent[key] is not None:
        pkey, action, pstate = parent[key]
        path.append((action, pstate))
        key = pkey
    path.reverse()
    return path


def _infeasible_reasons(belief: State) -> list[str]:
    reasons: list[str] = []
    if not belief.matches(("at", "multimeter", "?")) and \
            not belief.matches(("holder", "?", "multimeter")):
        reasons.append("世界中没有万用表：无法检查设备故障，维修工序无法启动")
    if not belief.matches(("at", "wrench", "?")) and \
            not belief.matches(("holder", "?", "wrench")):
        reasons.append("世界中没有扳手：无法拆装阀门或紧固螺栓")
    if belief.has(("fault", "valve_seized", "pump")):
        options = [p for p in ("valve_new", "valve_donor")
                   if belief.location_of(p) is not None]
        if not options:
            reasons.append("卡死旧阀拆下即损坏，且无可用替代阀门（新阀/捐赠件均不可得）")
    if belief.has(("seal_broken", "pump")) and \
            not belief.matches(("reconditioned", "?")) and \
            belief.location_of("gasket_new") is None:
        reasons.append("翻新旧阀所需密封垫不存在，装回路线不成立")
    if not reasons:
        reasons.append("穷尽搜索空间后不存在满足目标的动作序列")
    return reasons


def plan(belief: State, goal: Fact = working("pump")) -> PlanResult:
    # 边界：规划器只消费显式快照。State 不可变，snapshot() 给出冻结入口，
    # 规划器后续不保留任何对外部真世界对象的引用。
    belief = belief.snapshot()
    fingerprint = belief.fingerprint()

    def _stamp(result: PlanResult) -> PlanResult:
        result.snapshot_fingerprint = fingerprint
        return result

    # 阶段一：未诊断，感知目标固定为检查泵（不提前开箱，避免无关节点）
    if not belief.has(("inspected", "pump")):
        sense_path, scost, sexp, sgen, note = _astar(
            belief,
            lambda s: s.has(("inspected", "pump")),
            stop_at_sense=False,
        )
        if scost >= 0:
            r = PlanResult(
                status="NEED_OBSERVATION",
                actions=[a for a, _ in sense_path], cost=scost,
                expanded=sexp, generated=sgen,
                reason="泵尚未检查、故障未知：信息收集阶段先取得万用表并检查，"
                       "观察前不臆测故障；检查后自动重新规划",
            )
            r._step_states = sense_path
            return _stamp(r)
        return _stamp(PlanResult(
            status="INFEASIBLE", expanded=sexp, generated=sgen, reason=note,
            why_infeasible=_infeasible_reasons(belief),
        ))

    # 阶段二：已诊断，完整目标搜索（可能基于未经观察证实的备件假设）
    path, cost, expanded, generated, note = _astar(
        belief, lambda s: s.has(goal), stop_at_sense=False
    )
    is_valve_fault = belief.has(("fault", "valve_worn", "pump")) or \
        belief.has(("fault", "valve_seized", "pump"))
    # 只有阀门维修才依赖备件箱里的新阀；其他故障的假设不影响维修路线
    has_unverified_assumption = is_valve_fault and bool(belief.assumptions)

    if cost >= 0 and not has_unverified_assumption:
        r = PlanResult(status="OK", actions=[a for a, _ in path], cost=cost,
                       expanded=expanded, generated=generated,
                       reason="已诊断故障，搜索到满足目标的完整维修序列")
        r._step_states = path
        return _stamp(r)

    # 若完整计划依赖未证实的备件假设，不直接宣布可执行：
    # 先规划到"旧阀已拆"的半成品，再在取件动作上由真世界证真/证伪。
    if has_unverified_assumption:
        half_goals = [
            ("damaged", "valve_old"),          # 卡死阀拆下即损坏
            ("at", "valve_old", "pump_room"),  # 普通阀拆下落地
        ]
        for hg in half_goals:
            if belief.has(hg):
                continue
            hpath, hcost, hexp, hgen, hnote = _astar(
                belief, lambda s, g=hg: s.has(g), stop_at_sense=False
            )
            if hcost >= 0:
                r = PlanResult(
                    status="PARTIAL",
                    actions=[a for a, _ in hpath], cost=hcost,
                    expanded=expanded + hexp, generated=generated + hgen,
                    reason="完整方案依赖未经观察证实的备件假设；先执行拆分工序"
                           "形成半成品，取件时再由真世界确认，假设证伪即重规划",
                )
                r._step_states = hpath
                return _stamp(r)

    if cost >= 0:
        r = PlanResult(status="OK", actions=[a for a, _ in path], cost=cost,
                       expanded=expanded, generated=generated,
                       reason="已诊断故障且备件已观察确认，搜索到完整维修序列")
        r._step_states = path
        return _stamp(r)

    # 目标暂不可达且仍有感知动作（如未开箱核实备件）：信息优先
    sense_path, scost, sexp, sgen, snote = _astar(
        belief, lambda s: False, stop_at_sense=True
    )
    if scost >= 0:
        r = PlanResult(
            status="NEED_OBSERVATION",
            actions=[a for a, _ in sense_path], cost=scost,
            expanded=expanded + sexp, generated=generated + sgen,
            reason="当前信念下完整方案不可达，先执行感知动作确认隐藏信息后再规划",
        )
        r._step_states = sense_path
        return _stamp(r)

    # 无感知动作、完整目标不可达：推进任何仍可做的不可逆拆分工序（半成品）
    progress_goals = [
        ("seal_broken", "pump"),
        ("at", "valve_old", "pump_room"),
        ("damaged", "valve_old"),
    ]
    for pg in progress_goals:
        if belief.has(pg):
            continue
        ppath, pcost, pexp, pgen, _ = _astar(
            belief, lambda s, g=pg: s.has(g), stop_at_sense=False
        )
        if pcost >= 0:
            r = PlanResult(
                status="PARTIAL",
                actions=[a for a, _ in ppath], cost=pcost,
                expanded=expanded + sexp + pexp,
                generated=generated + sgen + pgen,
                reason="完整维修方案当前不可达，但可先推进拆分工序形成半成品；"
                       "拆分后将基于新状态重新规划，不会提前把设备标记为修好",
            )
            r._step_states = ppath
            return _stamp(r)

    return _stamp(PlanResult(
        status="INFEASIBLE", expanded=expanded + sexp,
        generated=generated + sgen,
        reason=note or "主目标不可达、无感知动作、也无任何可推进的拆分工序",
        why_infeasible=_infeasible_reasons(belief),
    ))
