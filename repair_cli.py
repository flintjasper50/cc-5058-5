#!/usr/bin/env python3
"""终端任务：只做维修规划的辅助验证，不启动窗口。

窗口界面请用 ./start_window.sh（或 python3 run_window.py）启动。

用法：
  python3 repair_cli.py list                      列出预置场景
  python3 repair_cli.py plan  <场景key>           只做初始规划并打印搜索成本
  python3 repair_cli.py run   <场景key>           完整执行并打印轨迹/守恒审计
  python3 repair_cli.py serve [端口]              启动窗口 HTTP 服务
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from repair_planner import Engine, scenarios
from repair_planner.scenario import make_world
from repair_planner.planner import plan


def cmd_list(_args):
    for key, scn in scenarios().items():
        print(f"{key:20s} {scn.name}")
        print(f"{'':20s} 故障={scn.fault} 备件箱取走={scn.box_taken_by_mate} "
              f"捐赠件={scn.donor_valve_available} 密封垫={scn.gasket_available}")


def cmd_plan(args):
    if not args:
        print("缺少场景 key，用 list 查看")
        return 2
    scn = scenarios()[args[0]]
    _world, belief = make_world(scn)
    result = plan(belief)
    print(f"场景: {scn.name}")
    print(f"规划状态: {result.status} | 真实代价: {result.cost} | "
          f"扩展节点: {result.expanded} | 生成节点: {result.generated}")
    print(f"依据: {result.reason}")
    for i, a in enumerate(result.actions, 1):
        print(f"  {i:2d}. {a.label():32s} 代价={a.cost}")
    for r in result.why_infeasible:
        print(f"  不可行证据: {r}")
    return 0


def cmd_run(args):
    if not args:
        print("缺少场景 key，用 list 查看")
        return 2
    eng = Engine(scenarios()[args[0]])
    eng.replan()
    eng.run_all()
    snap = eng.snapshot()
    for h in snap["history"]:
        tag = "受阻" if h["blocked"] else ("感知" if h["sense"] else "执行")
        print(f"{h['index']:2d}. [{tag}] {h['action']:30s} "
              f"确认={h['confirmed']} 累计={h['cumulative_cost']}")
        if h["blocking_reason"]:
            print(f"      受阻: {h['blocking_reason']}")
        if h["decision"]:
            print(f"      决策: {h['decision']} -> {h['replan_status']}")
        for o in h["observation"]:
            print(f"      观察: {o}")
    print("-" * 60)
    print(f"结束={snap['finished']} 失败={snap['failed']} "
          f"总代价={snap['total_cost']} 守恒审计={snap['audit']}")
    print(f"真世界物品位置: {snap['world_locations']}")
    print(f"信念物品位置:   {snap['belief_locations']}")
    return 0 if snap["audit_ok"] else 1


def cmd_serve(args):
    from repair_planner.server import serve
    port = int(args[0]) if args else 8765
    serve(port=port)
    return 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, args = sys.argv[1], sys.argv[2:]
    table = {"list": cmd_list, "plan": cmd_plan, "run": cmd_run, "serve": cmd_serve}
    if cmd not in table:
        print(f"未知命令 {cmd}\n")
        print(__doc__)
        return 2
    return table[cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
