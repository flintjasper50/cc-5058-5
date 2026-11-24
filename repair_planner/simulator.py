"""分支模拟器：规划分支的私有推演环境（与真世界版本隔离）。

解耦纪律：
1. 规划器只把显式快照（State.snapshot()）交给模拟器；模拟器内部持有该快照的
   私有工作副本，搜索过程的任何推演都不触碰真世界对象。
2. 每个分支带私有资源表 ResourceTable：抓取/安装等资源占用只在分支内登记，
   取消一个已"预约"的分支步骤（放弃该分支或弹出该动作）不会消耗现场配件。
3. 每个模拟步骤记录前提读集 PreconditionReadSet：执行器在真世界提交前据此刻
   复核，读集所依赖的事实仍成立才允许提交（提交前复核前提读集）。
4. 模拟效果与真实执行效果共用 actions.apply 的同一套纯语义，仅版本不同：
   模拟器版本号独立递增，真世界版本由执行器受控回执推进。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Tuple

from .model import State, Fact, facts_diff, FactDelta
from .actions import Action, available_actions, apply, CARRY_LIMIT


@dataclass(frozen=True)
class ResourceTable:
    """分支私有资源表：物品占位（位置/手持/装入设备）的只读投影。

    由当前快照事实派生；分支每推进一步重新派生，因此放弃分支即丢弃其全部
    资源预约，现场（真世界）配件不受任何影响。
    """
    location: FrozenSet[Tuple[str, str]]  # (物品, 地点或持有者)
    held: FrozenSet[str]                  # 手持物品集合
    installed: FrozenSet[Tuple[str, str]]  # (零件, 设备)

    @classmethod
    def from_state(cls, state: State) -> "ResourceTable":
        location: set[tuple[str, str]] = set()
        held: set[str] = set()
        for f in state.facts:
            if f[0] == "at":
                location.add((f[1], f[2]))
            elif f[0] == "holder":
                location.add((f[2], f[1]))
                held.add(f[2])
        installed = frozenset(
            (f[1], f[2]) for f in state.facts if f[0] == "installed"
        )
        return cls(frozenset(location), frozenset(held), installed)

    def held_count(self) -> int:
        return len(self.held)

    def has_capacity(self) -> bool:
        return self.held_count() < CARRY_LIMIT

    def locate(self, item: str) -> str | None:
        for it, where in self.location:
            if it == item:
                return where
        return None


@dataclass(frozen=True)
class PreconditionReadSet:
    """动作前提读集：提交前在真世界复核的事实指纹与逐条前提。"""
    pre: Tuple[Fact, ...]
    state_fingerprint: tuple

    @classmethod
    def of(cls, state: State, action: Action) -> "PreconditionReadSet":
        return cls(tuple(action.pre), state.fingerprint())

    def verify(self, world: State) -> tuple[bool, tuple]:
        """在真世界快照上复核：逐条前提必须是已确认事实（假设不算数）。"""
        missing = tuple(p for p in self.pre if not world.has(p))
        return (not missing), missing

    def verify_belief(self, belief: State) -> tuple[bool, tuple]:
        """在信念快照上复核：已确认事实或未证伪假设均可成立（规划层语义）。"""
        missing = tuple(p for p in self.pre if not belief.believes(p))
        return (not missing), missing


@dataclass(frozen=True)
class SimResult:
    """单步模拟结果：仅限分支内部，绝不回写真世界。"""
    ok: bool
    action: Action
    before: State
    after: State
    resources_before: ResourceTable
    resources_after: ResourceTable
    read_set: PreconditionReadSet
    delta: FactDelta
    sim_version: int
    reason: str = ""


class BranchSimulator:
    """规划分支的私有模拟器：状态版本独立，资源表随分支生灭。"""

    def __init__(self, snapshot: State, adjacency: dict,
                 base_version: int = 0):
        # 显式快照入口：拒绝外部传入可变对象的隐式引用语义
        self._state: State = snapshot.snapshot()
        self._adjacency = adjacency
        self._version = base_version
        self._resource = ResourceTable.from_state(self._state)

    # ---------- 只读视图 ----------
    @property
    def state(self) -> State:
        return self._state

    @property
    def version(self) -> int:
        return self._version

    @property
    def resources(self) -> ResourceTable:
        return self._resource

    def legal_actions(self) -> list[Action]:
        return available_actions(self._state, self._adjacency)

    def read_set_of(self, action: Action) -> PreconditionReadSet:
        return PreconditionReadSet.of(self._state, action)

    # ---------- 私有推演 ----------
    def simulate(self, action: Action) -> SimResult:
        """在私有快照上推进一步；返回分支内结果，真世界零改动。"""
        read_set = PreconditionReadSet.of(self._state, action)
        holds, missing = read_set.verify_belief(self._state)
        before_res = self._resource
        if not holds:
            return SimResult(
                ok=False, action=action, before=self._state, after=self._state,
                resources_before=before_res, resources_after=before_res,
                read_set=read_set, delta=facts_diff(self._state, self._state),
                sim_version=self._version,
                reason=f"分支前提不成立，缺失 {[str(m) for m in missing]}",
            )
        nxt = apply(self._state, action)
        after_res = ResourceTable.from_state(nxt)
        delta = facts_diff(self._state, nxt)
        result = SimResult(
            ok=True, action=action, before=self._state, after=nxt,
            resources_before=before_res, resources_after=after_res,
            read_set=read_set, delta=delta, sim_version=self._version + 1,
        )
        # 仅推进分支自身的私有版本与资源表
        self._state = nxt
        self._resource = after_res
        self._version += 1
        return result

    def branch(self) -> "BranchSimulator":
        """分叉出独立子分支：副本拥有独立资源表与版本，取消子分支不影响本分支。"""
        return BranchSimulator(self._state, self._adjacency, self._version)


def simulate_step(snapshot: State, action: Action, adjacency: dict,
                  sim_version: int) -> SimResult:
    """函数式单步推演：BFS 的每个搜索节点都是一条独立分支。

    调用方得到的是新快照与新资源表，原快照不被修改；分支被剪枝/弹出即等同于
    取消预约，现场配件不会被消耗。真世界对象永远不会出现在这里。
    adjacency 由规划器显式传入，模拟器不反向依赖场景定义。
    """
    sim = BranchSimulator(snapshot, adjacency=adjacency,
                          base_version=sim_version)
    return sim.simulate(action)
