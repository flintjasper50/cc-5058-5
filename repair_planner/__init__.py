"""维修规划包：世界事实 / 动作模型 / 分支模拟器 / 规划器 / 场景执行器 分层。

边界方向（依赖单向，禁止反向）：
    model（世界事实）
      ^
    actions（动作前提/效果纯语义）
      ^
    simulator（分支模拟：私有资源表、版本隔离、前提读集）
      ^
    planner（只消费显式快照，不触达真世界）
      ^
    executor（受控回执入口，唯一允许变更真世界）

domain 为旧导入路径的兼容适配层；scenario 负责场景构建与观察政策。
"""
from .model import State, FactDelta, facts_diff, conservation_audit
from .actions import Action, available_actions, apply
from .domain import (  # 兼容：旧路径 repair_planner.domain 再导出
    HOME_PLACE, GRABBABLE, CARRY_LIMIT, REPAIR_RULES, VALVE_PARTS,
)
from .simulator import (
    BranchSimulator, ResourceTable, PreconditionReadSet, SimResult,
    simulate_step,
)
from .planner import plan, PlanResult
from .executor import Engine, StepRecord, Receipt
from .scenario import scenarios, Scenario, make_world

__all__ = [
    "State", "FactDelta", "facts_diff", "conservation_audit",
    "Action", "available_actions", "apply",
    "HOME_PLACE", "GRABBABLE", "CARRY_LIMIT", "REPAIR_RULES", "VALVE_PARTS",
    "BranchSimulator", "ResourceTable", "PreconditionReadSet", "SimResult",
    "simulate_step",
    "plan", "PlanResult",
    "Engine", "StepRecord", "Receipt",
    "scenarios", "Scenario", "make_world",
]
