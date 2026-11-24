"""兼容适配层：动作模式已拆分到 repair_planner.actions。

规划/执行解耦后，动作的前提与效果语义统一收敛在 actions.py；本模块仅保留
旧导入路径（repair_planner.domain）的再导出，不新增任何状态或副作用，
使历史调用方无需改动即可运行到底。
"""
from __future__ import annotations

from .actions import (
    Action,
    HOME_PLACE,
    GRABBABLE,
    CARRY_LIMIT,
    REPAIR_RULES,
    VALVE_PARTS,
    available_actions,
    apply,
)
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

__all__ = [
    "Action", "HOME_PLACE", "GRABBABLE", "CARRY_LIMIT", "REPAIR_RULES",
    "VALVE_PARTS", "available_actions", "apply",
    "State", "Fact", "at", "carrying", "installed", "fault", "inspected",
    "working", "damaged", "reconditioned", "seal_broken", "box_taken",
]
