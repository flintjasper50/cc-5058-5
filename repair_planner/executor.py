"""规划执行器：在真世界中逐步执行，结果反馈回规划状态。

核心纪律：
1. 每个动作先在真世界校验前提（信念中的假设不构成真世界前提）。
2. 规划产生的效果只是"预期效果"；执行成功并经观察确认后才进入信念。
3. 失败时不提前修改设备状态（working 只由成功动作的确认观察产生）。
4. 受阻动作先尝试复用剩余计划（逐动作验证前提），不成立才整段重算。
5. 任何一步后做资源守恒审计，发现幽灵物品立即报错。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

from .model import (
    State,
    at,
    carrying,
    installed,
    conservation_audit,
)
from .domain import Action, available_actions, apply, _my_place, _held
from .planner import plan, PlanResult
from .scenario import (
    Scenario,
    make_world,
    observe_place,
    observe_inspection,
    observe_open_box,
    expected_items,
    ADJACENCY,
)


@dataclass
class StepRecord:
    index: int
    action: str
    sense: bool
    action_cost: int
    expected_effects: list[str]
    confirmed: bool
    blocked: bool
    blocking_reason: str
    observation: list[str]
    decision: str
    replan_status: str
    cumulative_cost: int
    audit_ok: bool
    audit_problems: list[str]
    rationale: str


class Engine:
    def __init__(self, scn: Scenario):
        self.scn = scn
        self.world, self.belief = make_world(scn)
        self.expected = expected_items(scn)
        self.plan_queue: list[Action] = []
        self.history: list[StepRecord] = []
        self.total_cost = 0
        self.last_plan: PlanResult | None = None
        self.finished = False
        self.failed = False

    # ---------- 规划 ----------
    def replan(self) -> PlanResult:
        result = plan(self.belief)
        self.last_plan = result
        if result.status in ("OK", "NEED_OBSERVATION", "PARTIAL"):
            self.plan_queue = list(result.actions)
        else:
            self.plan_queue = []
        return result

    # ---------- 真世界前提校验 ----------
    def _real_preconditions_hold(self, action: Action) -> tuple[bool, str]:
        kind, args = action.kind, action.args
        w = self.world
        place = _my_place(w)
        if kind == "move":
            if args[0] not in ADJACENCY.get(place, {}):
                return False, f"导航不通：{place} -> {args[0]}"
            return True, ""
        if kind == "take":
            item = args[0]
            if w.location_of(item) != place:
                return False, (f"抓取受阻：{item} 实际不在 {place}"
                               f"（真世界位置={w.location_of(item)}，"
                               f"信念位置={self.belief.location_of(item, include_assumptions=True)}）")
            if len(_held(w)) >= 2:
                return False, "容量已满（最多携带 2 件）"
            return True, ""
        if kind == "drop":
            if not w.has(carrying("me", args[0])):
                return False, f"未持有 {args[0]}"
            return True, ""
        if kind == "inspect":
            if not w.has(carrying("me", "multimeter")):
                return False, "未持有万用表"
            if w.location_of(args[0]) != place:
                return False, f"{args[0]} 不在此地"
            return True, ""
        if kind == "open_box":
            if w.location_of("spare_box") != place:
                return False, "备件箱不在此地"
            return True, ""
        if kind == "break_seal":
            if not w.matches(("seal_on", args[0], "?")):
                return False, f"{args[0]} 上没有完好封条"
            return True, ""
        if kind == "remove_valve":
            part, dev = args
            if not w.has(carrying("me", "wrench")):
                return False, "未持有扳手"
            if not w.has(installed(part, dev)):
                return False, f"{part} 未安装在 {dev}"
            if dev == "pump" and not w.has(("seal_broken", "pump")):
                return False, "泵的封条未拆，不能拆阀"
            return True, ""
        if kind == "install_valve":
            part, dev = args
            if not w.has(carrying("me", "wrench")):
                return False, "未持有扳手"
            if not w.has(carrying("me", part)):
                return False, f"未持有 {part}"
            if w.has(("damaged", part)):
                return False, f"{part} 已损坏，不能安装"
            if part == "valve_old" and not w.has(("reconditioned", "valve_old")):
                return False, "旧阀未翻新"
            if part == "valve_old" and not w.has(carrying("me", "gasket_new")):
                return False, "翻新旧阀必须同时携带新密封垫"
            return True, ""
        if kind == "tighten_bolts":
            if not w.has(carrying("me", "wrench")):
                return False, "未持有扳手"
            return True, ""
        if kind in ("replace_fuse", "replace_filter"):
            need = "fuse_new" if kind == "replace_fuse" else "filter_new"
            if not w.has(carrying("me", "pliers")):
                return False, "未持有钳子"
            if not w.has(carrying("me", need)):
                return False, f"未持有 {need}"
            return True, ""
        if kind == "recondition":
            part = args[0]
            if place != "workshop":
                return False, "翻新只能在工作台进行"
            if not w.has(carrying("me", "wrench")):
                return False, "未持有扳手"
            if w.location_of(part) not in ("me", "workshop"):
                return False, f"{part} 不在工作台"
            if w.has(("damaged", part)):
                return False, f"{part} 已损坏，无法翻新"
            return True, ""
        if kind == "apply_seal":
            if not w.has(carrying("me", "seal_sticker")):
                return False, "未持有封条贴纸"
            return True, ""
        return False, f"未知动作 {kind}"

    # ---------- 观察反馈 ----------
    def _observe_after(self, action: Action) -> list[str]:
        """动作成功后，按观察政策把真世界可见信息同步进信念。"""
        kind, args = action.kind, action.args
        notes: list[str] = []
        place = _my_place(self.world)
        if kind == "move":
            before = self.belief
            self.belief = observe_place(self.world, self.belief, args[0])
            new_facts = self.belief.facts - before.facts
            notes.append(f"观察 {args[0]}：新增可见事实 "
                         f"{[str(f) for f in new_facts] or '无'}")
        elif kind == "inspect":
            self.belief = observe_inspection(self.world, self.belief, args[0])
            faults = self.belief.matches(("fault", "?", args[0]))
            notes.append(f"万用表读数：{[str(f) for f in faults]}")
        elif kind == "open_box":
            before_asm = set(self.belief.assumptions)
            self.belief = observe_open_box(self.world, self.belief)
            if self.world.has(("box_taken", "spare_box")):
                notes.append("观察：备件箱已被同伴取空，关于箱内新阀的假设被证伪并删除")
            else:
                notes.append("观察：箱内物品确认，假设提升为事实")
            notes.append(f"信念假设变化：{[str(a) for a in before_asm]} -> "
                         f"{[str(a) for a in self.belief.assumptions]}")
        else:
            # 操作类动作：真世界效果经确认后整体同步（成功即确认）
            before = self.belief
            self.belief = self._sync_world_visible(before)
            notes.append("操作结果已观察确认并写入信念")
        return notes

    def _sync_world_visible(self, before: State) -> State:
        """把当前地点可观察的真世界事实同步为确认事实，并清除被动作推翻的旧事实。"""
        place = _my_place(self.world)
        s = observe_place(self.world, before, place)
        # 手持物以真世界为准：清掉信念中手持物的旧 at，补全真世界 holder
        for f in self.world.matches(("holder", "me", "?")):
            item = f[2]
            stale = s.matches(("at", item, "?"))
            s = s.with_facts(add=(f,), remove=tuple(stale))
        # 信念中仍标记为手持、但真世界已放下的物品：同步真世界位置
        for f in list(s.matches(("holder", "me", "?"))):
            item = f[2]
            if not self.world.has(f):
                real = self.world.location_of(item)
                s = s.with_facts(
                    add=(at(item, real),) if real else (),
                    remove=(f,),
                )
        for dev in ("pump", "donor_unit"):
            if self.world.location_of(dev) == place:
                for f in self.world.matches(("installed", "?", dev)):
                    s = s.with_facts(add=(f,))
                # 已不在设备上的旧安装事实要撤销
                for f in list(s.matches(("installed", "?", dev))):
                    if not self.world.has(f):
                        s = s.with_facts(remove=(f,))
                for f in self.world.matches(("fault", "?", dev)):
                    if s.has(("inspected", dev)):
                        s = s.with_facts(add=(f,))
                # 已排除的故障从信念撤销
                for f in list(s.matches(("fault", "?", dev))):
                    if not self.world.has(f):
                        s = s.with_facts(remove=(f,))
                if self.world.has(("working", dev)) and s.has(("inspected", dev)):
                    s = s.with_facts(add=(("working", dev),))
                elif not self.world.has(("working", dev)):
                    s = s.with_facts(remove=(("working", dev),))
                for f in self.world.matches(("damaged", "?")):
                    s = s.with_facts(add=(f,))
                for f in self.world.matches(("reconditioned", "?")):
                    s = s.with_facts(add=(f,))
        return s

    # ---------- 受阻后的计划决策 ----------
    def _try_reuse_suffix(self, failed: Action) -> tuple[bool, str]:
        """复用剩余计划（失败动作已出队）：从下一动作起在当前信念中重新落地。"""
        suffix = self.plan_queue
        if not suffix:
            return False, "剩余计划为空，无法复用"
        probe = self.belief
        for a in suffix:
            grounded = self._reground(a, probe)
            if grounded is None:
                return False, f"剩余动作 {a.label()} 在当前信念下无法实例化"
            ok, why = self._belief_pre(probe, grounded)
            if not ok:
                return False, f"剩余动作 {grounded.label()} 前提不成立：{why}"
            probe = apply(probe, grounded)
        return True, f"剩余 {len(suffix)} 步前提经信念推演全部仍成立，复用原计划"

    def _reground(self, action: Action, belief: State) -> Action | None:
        """同名同参动作在当前可用动作集合中重新查找（可能已被其他动作取代）。"""
        for cand in available_actions(belief, ADJACENCY):
            if cand.kind == action.kind and cand.args == action.args:
                return cand
        return None

    def _belief_pre(self, belief: State, action: Action) -> tuple[bool, str]:
        for p in action.pre:
            if not belief.believes(p):
                return False, f"信念中缺少 {p}"
        return True, ""

    def _handle_blocked(self, action: Action, why: str) -> tuple[str, str]:
        reused, reuse_note = self._try_reuse_suffix(action)
        if reused:
            return f"受阻后复用剩余计划：{reuse_note}", "REUSED"
        # 整段重算；新计划的第一个动作不得与刚失败的动作完全相同，
        # 否则说明信念未更新，标记为卡死而不是无限重试
        r = self.replan()
        if r.actions and r.actions[0].label() == action.label():
            self.failed = True
            return (f"复用不成立（{reuse_note}）；重算计划仍以同一受阻动作 "
                    f"{action.label()} 开头，状态无新观察，判定卡死", "STUCK")
        return f"复用不成立（{reuse_note}），整段重算", r.status

    # ---------- 单步执行 ----------
    def step(self) -> StepRecord:
        idx = len(self.history) + 1
        if not self.plan_queue:
            r = self.replan()
            if r.status == "INFEASIBLE":
                self.failed = True
                rec = StepRecord(
                    idx, "<重规划>", False, 0, [], False, True,
                    "无可用计划", ["规划器返回不可行"], "整段重算",
                    "INFEASIBLE", self.total_cost,
                    *self._audit(), r.reason,
                )
                self.history.append(rec)
                return rec
        action = self.plan_queue.pop(0)
        ok, why = self._real_preconditions_hold(action)
        expected = [str(p) for p in action.pre]

        if not ok:
            # 受阻：真世界不改动；但抓取假设物品失败本身是一次观察，
            # 必须据此对账信念（证伪假设、登记备件被取走），预期效果不落实。
            if action.kind == "take":
                item = action.args[0]
                assumed = tuple(f for f in self.belief.assumptions
                                if f[0] == "at" and f[1] == item)
                if assumed:
                    self.belief = self.belief.with_assumptions(remove=assumed)
                    # 若真世界显示备件箱已空，登记观察事实
                    if self.world.has(("box_taken", "spare_box")) and \
                            not self.belief.has(("box_taken", "spare_box")):
                        self.belief = self.belief.with_facts(
                            add=(("box_taken", "spare_box"),
                                 ("box_checked", "spare_box")))
            decision, replan_status = self._handle_blocked(action, why)
            rec = StepRecord(
                idx, action.label(), action.sense, action.cost,
                [f"预期（未落实）：{action.label()}"], False, True, why,
                [], decision, replan_status, self.total_cost,
                *self._audit(), action.rationale(self.belief),
            )
            self.history.append(rec)
            if replan_status == "INFEASIBLE":
                self.failed = True
            return rec

        # 真世界执行
        self.world = apply(self.world, action)
        self.total_cost += action.cost
        obs = self._observe_after(action)
        audit_ok, audit_problems = self._audit()

        confirmed = audit_ok
        decision = "按计划执行"
        replan_status = self.last_plan.status if self.last_plan else ""

        # 感知动作完成后主动重规划（故障/箱情刚刚进入信念）
        if action.sense:
            r = self.replan()
            decision = "感知完成，按新观察整段重规划"
            replan_status = r.status
            if r.status == "INFEASIBLE":
                self.failed = True
        elif not self.plan_queue:
            r = self.replan()
            if r.status in ("OK", "PARTIAL") and r.actions:
                decision = ("原计划用尽但目标未达成，整段重算补充计划"
                            if r.status == "OK" else
                            "完整方案不可达，重算得到可推进的半成品拆分工序")
                replan_status = r.status
            else:
                replan_status = r.status
                if r.status == "INFEASIBLE":
                    self.failed = True

        if self.belief.has(("working", "pump")):
            self.finished = True

        rec = StepRecord(
            idx, action.label(), action.sense, action.cost,
            [f"预期：{action.label()} -> 真世界已应用"], confirmed, False, "",
            obs, decision, replan_status, self.total_cost,
            audit_ok, audit_problems, action.rationale(self.belief),
        )
        self.history.append(rec)
        return rec

    def _audit(self) -> tuple[bool, list[str]]:
        ok_w, prob_w = conservation_audit(self.world, self.expected)
        # 信念中已确认存在的物品（观察到的子集）也要满足不重复占位
        believed_items = frozenset(
            f[1] for f in self.belief.facts if f[0] == "at"
        ) | frozenset(
            f[2] for f in self.belief.facts if f[0] == "holder"
        )
        ok_b, prob_b = conservation_audit(self.belief, believed_items)
        # 假设物品不得被当作已确认物品；被观察证伪的假设必须已删除
        phantom = [str(a) for a in self.belief.assumptions
                   if a in self.belief.facts]
        problems = prob_w + prob_b
        if phantom:
            problems.append(f"假设与事实冲突: {phantom}")
        return (ok_w and ok_b and not phantom), problems

    def run_all(self, max_steps: int = 40) -> list[StepRecord]:
        while not self.finished and not self.failed and len(self.history) < max_steps:
            self.step()
        return self.history

    # ---------- 快照（供界面/审计） ----------
    def snapshot(self) -> dict[str, Any]:
        def state_view(s: State, assumed: bool = False) -> dict:
            return {
                "facts": [str(f) for f in s.facts],
                "assumptions": [str(f) for f in s.assumptions],
                "counts": dict(s.counts),
            }
        return {
            "scenario": self.scn.name,
            "description": self.scn.description,
            "goal": "working(pump)",
            "finished": self.finished,
            "failed": self.failed,
            "total_cost": self.total_cost,
            "world": state_view(self.world),
            "belief": state_view(self.belief),
            "plan_queue": [a.label() for a in self.plan_queue],
            "last_plan": self.last_plan.to_dict() if self.last_plan else None,
            "history": [asdict(h) for h in self.history],
            "audit": self._audit()[1] or ["守恒检查通过"],
            "audit_ok": self._audit()[0],
            "world_locations": self._location_table(self.world),
            "belief_locations": self._location_table(self.belief),
        }

    def _location_table(self, s: State) -> dict[str, str]:
        table = {}
        for f in s.facts:
            if f[0] == "at":
                table[f[1]] = f[2]
            elif f[0] == "holder":
                table[f[2]] = f"carried by {f[1]}"
            elif f[0] == "installed":
                table[f[1]] = f"installed in {f[2]}"
        for f in s.assumptions:
            if f[0] == "at" and f[1] not in table:
                table[f[1]] = f"ASSUMED {f[2]}（未观察）"
        return table
