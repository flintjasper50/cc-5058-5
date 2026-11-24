"""规划执行器：在真世界中逐步执行，结果经受控回执反馈回规划状态。

规划/执行解耦边界（本模块是唯一允许变更真世界的地方）：
1. 真世界状态只能通过受控入口 Engine.commit() 改变；commit 先在真世界复核
   动作前提读集（信念中的假设不构成真世界前提），通过后才用与模拟器同源的
   actions.apply 提交，并产生动作回执 Receipt。
2. 搜索/规划过程不直接改动真实装置：分支模拟器的推演结果进不了真世界，
   只有 commit 成功的回执才推进真世界版本号。
3. 回执携带动作失败信息与事实前后差分（FactDelta），作为计划合法性与世界
   效果的执行证据；confirmed 只在提交成功且守恒审计通过时为真，禁止伪报。
4. 失败/受阻时真世界零改动（版本号不变、现场配件不被消耗）；失败观察仍经
   受控的信念更新入口登记（如备件箱被取空的证伪）。
5. 受阻动作先尝试复用剩余计划（逐动作验证前提），不成立才整段重算。
6. 任何一步后做资源守恒审计，发现幽灵物品立即在回执/记录中标为异常。

兼容适配：旧入口 Engine.step() 保留，内部改为调用 commit 并把回执翻译成
历史 StepRecord，旧调用方与窗口界面的外部行为保持冻结。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any

from .model import (
    State,
    FactDelta,
    facts_diff,
    at,
    carrying,
    installed,
    conservation_audit,
)
from .actions import Action, available_actions, apply, _my_place, _held
from .simulator import PreconditionReadSet
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
class Receipt:
    """动作回执：真世界一次提交的受控证据。

    成功时 world_before -> world_after 经事实差分给出可核对的世界效果；
    失败时 success=False，world_after 与 world_before 同版本、同指纹，
    现场不发生任何变化。
    """
    action: str
    kind: str
    sense: bool
    success: bool
    blocked: bool
    reason: str
    pre: list[str]
    read_set_verified: bool
    missing_pre: list[str]
    world_version_before: int
    world_version_after: int
    world_fingerprint_before: tuple
    world_fingerprint_after: tuple
    delta: FactDelta
    audit_ok: bool
    audit_problems: list[str]
    observation: list[str]
    cost_charged: int

    @property
    def confirmed(self) -> bool:
        """效果确认 = 提交成功且守恒审计通过；无审计背书不得宣称确认。"""
        return self.success and self.audit_ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action, "kind": self.kind, "sense": self.sense,
            "success": self.success, "blocked": self.blocked,
            "reason": self.reason, "pre": self.pre,
            "read_set_verified": self.read_set_verified,
            "missing_pre": self.missing_pre,
            "world_version_before": self.world_version_before,
            "world_version_after": self.world_version_after,
            "world_fingerprint_changed":
                self.world_fingerprint_before != self.world_fingerprint_after,
            "delta": self.delta.to_dict(),
            "audit_ok": self.audit_ok,
            "audit_problems": self.audit_problems,
            "observation": self.observation,
            "cost_charged": self.cost_charged,
            "confirmed": self.confirmed,
        }


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
    # 解耦后新增：规划步骤/维修动作相关异常与执行证据（不破坏既有字段）
    anomalies: list[str] = field(default_factory=list)
    world_version: int = 0
    fact_added: list[str] = field(default_factory=list)
    fact_removed: list[str] = field(default_factory=list)


class Engine:
    def __init__(self, scn: Scenario):
        self.scn = scn
        self.world, self.belief = make_world(scn)
        self.expected = expected_items(scn)
        self.plan_queue: list[Action] = []
        self.history: list[StepRecord] = []
        self.receipts: list[Receipt] = []
        self.total_cost = 0
        self.last_plan: PlanResult | None = None
        self.finished = False
        self.failed = False
        # 真世界版本：仅由成功的 commit 推进；失败/取消不递增
        self.world_version = 0

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

    # ---------- 受控信念更新入口（回执的观察部分只能走这里） ----------
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

    def _register_failed_observation(self, action: Action) -> list[str]:
        """受阻动作的观察登记：只更新信念，绝不触碰真世界。"""
        notes: list[str] = []
        if action.kind == "take":
            item = action.args[0]
            assumed = tuple(f for f in self.belief.assumptions
                            if f[0] == "at" and f[1] == item)
            if assumed:
                self.belief = self.belief.with_assumptions(remove=assumed)
                notes.append(f"抓取失败即观察：删除未证实假设 "
                             f"{[str(a) for a in assumed]}")
                if self.world.has(("box_taken", "spare_box")) and \
                        not self.belief.has(("box_taken", "spare_box")):
                    self.belief = self.belief.with_facts(
                        add=(("box_taken", "spare_box"),
                             ("box_checked", "spare_box")))
                    notes.append("观察登记：备件箱已被同伴取空")
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

    # ---------- 受控提交入口 ----------
    def commit(self, action: Action) -> Receipt:
        """真世界唯一变更入口：复核前提读集 -> 提交 -> 审计 -> 观察 -> 回执。"""
        world_before = self.world
        fp_before = world_before.fingerprint()
        ver_before = self.world_version
        pre_labels = [str(p) for p in action.pre]

        # 1. 提交前复核前提读集：动作生成时的每条前提在真世界必须仍是确认事实
        read_set = PreconditionReadSet.of(world_before, action)
        read_ok, missing = read_set.verify(world_before)
        # 2. 领域级真世界前提（容量、导航连通性等读集之外的物理约束）
        phys_ok, why = self._real_preconditions_hold(action)

        if not (read_ok and phys_ok):
            reason = why or f"前提读集复核失败，缺失 {[str(m) for m in missing]}"
            # 失败观察仍经受控入口登记；真世界与版本号保持不变
            obs = self._register_failed_observation(action)
            audit_ok, audit_problems = self._audit()
            receipt = Receipt(
                action=action.label(), kind=action.kind, sense=action.sense,
                success=False, blocked=True, reason=reason, pre=pre_labels,
                read_set_verified=False,
                missing_pre=[str(m) for m in missing],
                world_version_before=ver_before, world_version_after=ver_before,
                world_fingerprint_before=fp_before,
                world_fingerprint_after=world_before.fingerprint(),
                delta=facts_diff(world_before, self.world),
                audit_ok=audit_ok, audit_problems=audit_problems,
                observation=obs, cost_charged=0,
            )
            self.receipts.append(receipt)
            return receipt

        # 3. 真世界提交：与分支模拟器同源的 actions.apply；仅此处在改真世界
        self.world = apply(world_before, action)
        self.world_version = ver_before + 1
        self.total_cost += action.cost
        delta = facts_diff(world_before, self.world)

        # 4. 守恒审计（无审计背书不得确认效果）
        audit_ok, audit_problems = self._audit()

        # 5. 受控观察：把真世界可见效果同步进信念
        obs = self._observe_after(action)

        receipt = Receipt(
            action=action.label(), kind=action.kind, sense=action.sense,
            success=True, blocked=False, reason="", pre=pre_labels,
            read_set_verified=True, missing_pre=[],
            world_version_before=ver_before,
            world_version_after=self.world_version,
            world_fingerprint_before=fp_before,
            world_fingerprint_after=self.world.fingerprint(),
            delta=delta, audit_ok=audit_ok, audit_problems=audit_problems,
            observation=obs, cost_charged=action.cost,
        )
        self.receipts.append(receipt)
        return receipt

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

    # ---------- 异常标注（规划步骤/维修动作相关，界面可见） ----------
    def _detect_anomalies(self, receipt: Receipt) -> list[str]:
        anomalies: list[str] = []
        if receipt.blocked:
            anomalies.append(f"动作受阻：{receipt.reason}")
        if not receipt.success and not receipt.blocked:
            anomalies.append("动作失败且未产生世界效果")
        if not receipt.audit_ok:
            anomalies.append(f"资源守恒审计异常：{'；'.join(receipt.audit_problems)}")
        if receipt.success and not receipt.confirmed:
            anomalies.append("动作已提交但效果未获审计确认")
        if receipt.success and receipt.world_version_after <= receipt.world_version_before:
            anomalies.append("成功回执未推进真世界版本")
        # 计划合法性/世界效果禁止伪报：信念宣称修好时真世界必须确有 working
        if self.belief.has(("working", "pump")) and \
                not self.world.has(("working", "pump")):
            anomalies.append("信念伪报 working(pump)：真世界尚未修好")
        if self.finished and not self.world.has(("working", "pump")):
            anomalies.append("结束状态伪报：真世界不存在 working(pump)")
        return anomalies

    # ---------- 兼容适配：旧单步入口 ----------
    def step(self) -> StepRecord:
        """旧入口：内部改为受控 commit + 重规划决策，翻译成历史 StepRecord。"""
        idx = len(self.history) + 1
        if not self.plan_queue:
            r = self.replan()
            if r.status == "INFEASIBLE":
                self.failed = True
                audit_ok, audit_problems = self._audit()
                rec = StepRecord(
                    idx, "<重规划>", False, 0, [], False, True,
                    "无可用计划", ["规划器返回不可行"], "整段重算",
                    "INFEASIBLE", self.total_cost, audit_ok, audit_problems,
                    r.reason,
                    anomalies=["规划不可行：" + "；".join(r.why_infeasible) or r.reason],
                    world_version=self.world_version,
                )
                self.history.append(rec)
                return rec

        action = self.plan_queue.pop(0)
        receipt = self.commit(action)
        expected = [f"预期：{action.label()} -> 前提读集已在真世界复核"]

        if not receipt.success:
            decision, replan_status = self._handle_blocked(action, receipt.reason)
            anomalies = self._detect_anomalies(receipt)
            rec = StepRecord(
                idx, action.label(), action.sense, action.cost,
                [f"预期（未落实）：{action.label()}"], False, True,
                receipt.reason, list(receipt.observation), decision,
                replan_status, self.total_cost,
                receipt.audit_ok, list(receipt.audit_problems),
                action.rationale(self.belief),
                anomalies=anomalies, world_version=self.world_version,
                fact_added=[], fact_removed=[],
            )
            self.history.append(rec)
            if replan_status == "INFEASIBLE":
                self.failed = True
            return rec

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

        # 结束判定以真世界证据为准，禁止仅凭信念伪报
        if self.world.has(("working", "pump")) and \
                self.belief.has(("working", "pump")):
            self.finished = True

        anomalies = self._detect_anomalies(receipt)
        rec = StepRecord(
            idx, action.label(), action.sense, action.cost,
            expected, receipt.confirmed, False, "",
            list(receipt.observation), decision, replan_status,
            self.total_cost, receipt.audit_ok, list(receipt.audit_problems),
            action.rationale(self.belief),
            anomalies=anomalies, world_version=self.world_version,
            fact_added=[str(f) for f in receipt.delta.added],
            fact_removed=[str(f) for f in receipt.delta.removed],
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
            "world_version": self.world_version,
            "world": state_view(self.world),
            "belief": state_view(self.belief),
            "plan_queue": [a.label() for a in self.plan_queue],
            "last_plan": self.last_plan.to_dict() if self.last_plan else None,
            "history": [asdict(h) for h in self.history],
            "receipts": [r.to_dict() for r in self.receipts],
            "audit": self._audit()[1] or ["守恒检查通过"],
            "audit_ok": self._audit()[0],
            "anomalies": self._current_anomalies(),
            "world_locations": self._location_table(self.world),
            "belief_locations": self._location_table(self.belief),
        }

    def _current_anomalies(self) -> list[str]:
        latest = self.receipts[-1] if self.receipts else None
        anomalies = list(self._detect_anomalies(latest)) if latest else []
        if self.failed and not self.finished:
            anomalies.append("运行状态：已失败/不可行")
        elif self.finished:
            anomalies.append("运行状态：已完成（真世界 working(pump) 已确认）")
        elif self.last_plan is not None and self.last_plan.status == "INFEASIBLE":
            anomalies.append("运行状态：规划不可行，等待场景调整")
        else:
            anomalies.append("运行状态：执行中")
        return anomalies

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
