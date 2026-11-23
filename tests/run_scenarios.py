"""终端自测：跑全部预置场景，打印计划、受阻决策、守恒审计与最终世界状态。

用法：python3 tests/run_scenarios.py [场景key ...]
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repair_planner import Engine, scenarios
from repair_planner.scenario import make_world
from repair_planner.planner import plan


def main(keys):
    scns = scenarios()
    keys = keys or list(scns.keys())
    failures = 0
    for key in keys:
        scn = scns[key]
        print("=" * 78)
        print(f"场景 {key}: {scn.name}")
        print(f"  真故障（对角色隐藏）: {scn.fault} | 备件箱被取走: {scn.box_taken_by_mate}"
              f" | 捐赠件: {scn.donor_valve_available} | 密封垫: {scn.gasket_available}"
              f" | 封条完好: {scn.seal_intact}")

        world0, belief0 = make_world(scn)
        first = plan(belief0)
        print(f"  初始规划: status={first.status} cost={first.cost} "
              f"expanded={first.expanded} generated={first.generated}")
        for i, a in enumerate(first.actions):
            print(f"    {i+1:2d}. {a.label():32s} 代价={a.cost}")

        eng = Engine(scn)
        eng.replan()
        eng.run_all()
        snap = eng.snapshot()

        print("  执行轨迹:")
        for h in snap["history"]:
            tag = "受阻" if h["blocked"] else ("感知" if h["sense"] else "执行")
            print(f"    {h['index']:2d}. [{tag}] {h['action']:30s} "
                  f"确认={h['confirmed']} 累计代价={h['cumulative_cost']}")
            if h["blocking_reason"]:
                print(f"        受阻原因: {h['blocking_reason']}")
            if h["decision"]:
                print(f"        决策: {h['decision']} -> {h['replan_status']}")
            for o in h["observation"]:
                print(f"        观察: {o}")
            if not h["audit_ok"]:
                print(f"        审计异常: {h['audit_problems']}")

        print(f"  最终: finished={snap['finished']} failed={snap['failed']} "
              f"总代价={snap['total_cost']} 审计通过={snap['audit_ok']}")
        print(f"  真世界物品位置: {snap['world_locations']}")
        print(f"  信念物品位置:   {snap['belief_locations']}")
        print(f"  真世界 working(pump)={('working', 'pump') in [tuple(f.strip('()').split(', ')) and f for f in snap['world']['facts']]}")
        has_working = any(f == "('working', 'pump')" for f in snap["world"]["facts"])
        print(f"  真世界泵已修好: {has_working}")
        if not snap["audit_ok"]:
            failures += 1
            print("  !!! 资源守恒审计失败")
        print()
    print("=" * 78)
    print(f"完成 {len(keys)} 个场景，审计失败 {failures} 个")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main(sys.argv[1:])
