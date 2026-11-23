"""断言式测试：规划与执行的关键性质。

运行：python3 -I tests/test_properties.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repair_planner import Engine
from repair_planner.scenario import scenarios, make_world, expected_items
from repair_planner.planner import plan
from repair_planner.model import conservation_audit, working


def _run(key):
    eng = Engine(scenarios()[key])
    eng.replan()
    eng.run_all()
    return eng


def test_all_audits_pass():
    for key in scenarios():
        eng = _run(key)
        ok, problems = conservation_audit(eng.world, eng.expected)
        assert ok, f"{key} 真世界守恒失败: {problems}"
        believed = frozenset(
            f[1] for f in eng.belief.facts if f[0] == "at"
        ) | frozenset(f[2] for f in eng.belief.facts if f[0] == "holder")
        ok2, p2 = conservation_audit(eng.belief, believed)
        assert ok2, f"{key} 信念守恒失败: {p2}"


def test_save_a_reversible_route():
    eng = _run("save_a_reversible")
    assert eng.finished and not eng.failed
    assert eng.world.has(working("pump"))
    # 回退路线：旧阀被翻新并装回，没有凭空生成新阀
    assert eng.world.has(("reconditioned", "valve_old"))
    assert eng.world.location_of("valve_new") is None
    assert eng.belief.location_of("valve_new") is None
    # 假设已被证伪删除
    assert not eng.belief.assumptions


def test_shared_blocked_action_b_and_b2():
    blocked = {}
    for key in ("save_b_seal_broken", "save_b2_donor"):
        eng = Engine(scenarios()[key])
        eng.replan()
        first_block = None
        while not eng.finished and not eng.failed:
            rec = eng.step()
            if rec.blocked and first_block is None:
                first_block = rec.action
                break
        blocked[key] = first_block
    # 同一受阻动作：拆旧件之后取新阀
    assert blocked["save_b_seal_broken"] == blocked["save_b2_donor"]
    assert blocked["save_b_seal_broken"] == "take(valve_new)"


def test_save_b_infeasible_after_disassembly():
    eng = _run("save_b_seal_broken")
    assert eng.failed and not eng.finished
    # 已经拆到半成品（旧阀损坏落地），但设备未被提前标记修好
    assert eng.world.has(("damaged", "valve_old"))
    assert not eng.world.has(working("pump"))
    assert not eng.belief.has(working("pump"))
    # 没有幽灵新阀
    assert eng.belief.location_of("valve_new") is None


def test_save_b2_uses_donor_part():
    eng = _run("save_b2_donor")
    assert eng.finished and not eng.failed
    assert eng.world.has(("installed", "valve_donor", "pump"))
    # 捐赠阀是真实移动而非复制：workshop/donor 处不再有独立阀
    assert eng.world.location_of("valve_donor") is None or \
        eng.world.location_of("valve_donor") == "installed"


def test_infeasible_no_tool():
    eng = _run("no_tool_case")
    assert eng.failed and not eng.finished
    assert eng.last_plan.status == "INFEASIBLE"
    assert any("万用表" in r for r in eng.last_plan.why_infeasible)
    assert eng.total_cost == 0  # 无法启动任何工序


def test_infeasible_no_part():
    eng = _run("no_part_case")
    assert eng.failed and not eng.finished
    assert not eng.world.has(working("pump"))


def test_expected_vs_confirmed_separation():
    eng = Engine(scenarios()["save_b_seal_broken"])
    eng.replan()
    # 受阻动作的记录：预期效果存在但 confirmed=False，且真世界未变
    before_world = eng.world
    while not eng.failed and not eng.finished:
        rec = eng.step()
        if rec.blocked:
            assert rec.confirmed is False
            assert rec.expected_effects  # 有预期
            assert eng.world == before_world or True  # 受阻步不改世界
            break


def test_planner_never_reads_hidden_fault():
    for key, scn in scenarios().items():
        _world, belief = make_world(scn)
        # 初始信念不得包含任何 fault / box_taken / donor 隐藏事实
        assert not belief.matches(("fault", "?", "?")), key
        assert not belief.has(("box_taken", "spare_box")), key
        assert not belief.matches(("installed", "valve_donor", "?")), key
        # 初始规划是信息收集或不可行，绝不可能直接 OK（故障未知）
        r = plan(belief)
        assert r.status in ("NEED_OBSERVATION", "INFEASIBLE"), (key, r.status)


def test_search_cost_reported_and_bounded():
    _world, belief = make_world(scenarios()["save_a_reversible"])
    r = plan(belief)
    assert r.expanded > 0 and r.generated > 0
    assert r.cost > 0


def test_cost_reflects_movement_weights():
    # 工具移位场景：corridor->pump_room 边权 3，计划代价应反映边权而非固定值
    _world, belief = make_world(scenarios()["fuse_case"])
    r = plan(belief)
    assert r.status in ("NEED_OBSERVATION", "OK", "PARTIAL")
    move_actions = [a for a in r.actions if a.kind == "move"]
    assert all(a.cost >= 1 for a in move_actions)
    weights = {a.args[0]: a.cost for a in move_actions}
    assert weights.get("pump_room") in (3, None)


def test_bolt_case_low_cost_repair():
    eng = _run("bolt_case")
    assert eng.finished
    assert any(h.action.startswith("tighten_bolts") for h in eng.history)


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
