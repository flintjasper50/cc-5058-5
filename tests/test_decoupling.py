"""规划/执行解耦的边界性质测试。

运行（净目录，不依赖任何手改数据）：
    python3 -I tests/test_decoupling.py
也可把整个仓库复制到一个全新的空目录后运行同一命令复核。
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from repair_planner import (  # noqa: E402
    Engine, plan, simulate_step, BranchSimulator, ResourceTable,
    PreconditionReadSet, Receipt, facts_diff,
)
from repair_planner.actions import apply, available_actions  # noqa: E402
from repair_planner.scenario import scenarios, make_world, ADJACENCY  # noqa: E402
from repair_planner.model import at, carrying, working  # noqa: E402


def _run(key):
    eng = Engine(scenarios()[key])
    eng.replan()
    eng.run_all()
    return eng


# 1. 搜索过程直接改动真实装置状态：规划期真世界快照必须逐字节不变
def test_planner_search_never_touches_world():
    for key, scn in scenarios().items():
        world, belief = make_world(scn)
        fp_before = world.fingerprint()
        plan(belief)
        assert world.fingerprint() == fp_before, key
        # 模拟器对信念快照的推演也不得回写传入的信念对象
        fp_belief = belief.fingerprint()
        for action in available_actions(belief, ADJACENCY)[:3]:
            simulate_step(belief, action, ADJACENCY, sim_version=0)
        assert belief.fingerprint() == fp_belief, key


# 2. 取消一个已预约的步骤后，分支推演不得消耗现场配件
def test_cancelled_branch_reservation_consumes_no_parts():
    world, belief = make_world(scenarios()["save_a_reversible"])
    # 在分支里预约 move(office) -> take(multimeter)：分支资源表显示手持，现场不动
    sim = BranchSimulator(belief, ADJACENCY)
    sim.simulate(next(a for a in sim.legal_actions() if a.args == ("office",)))
    take = next(a for a in sim.legal_actions()
                if a.kind == "take" and a.args[0] == "multimeter")
    res = sim.simulate(take)
    assert res.ok
    assert "multimeter" in res.resources_after.held
    # 取消该分支（丢弃 sim）：真世界与原始信念中万用表仍在 office
    assert world.has(at("multimeter", "office"))
    assert belief.has(at("multimeter", "office"))
    assert not belief.matches(("holder", "me", "multimeter"))

    # BFS 内部剪枝同理：规划完成后所有可抓取物品仍在原现场，未被分支消耗
    plan(belief)
    for item in ("multimeter", "wrench", "pliers", "seal_sticker"):
        assert world.location_of(item) is not None
        assert not world.has(carrying("me", item)), item


# 3. 模拟前提与真实执行效果版本隔离：模拟版本独立，真世界版本只由 commit 推进
def test_simulation_and_world_versions_are_isolated():
    scn = scenarios()["bolt_case"]
    world, belief = make_world(scn)
    sim = BranchSimulator(belief, ADJACENCY)
    move = next(a for a in sim.legal_actions() if a.kind == "move")
    r1 = sim.simulate(move)
    r2 = sim.simulate(next(a for a in sim.legal_actions() if a.kind == "move"))
    assert r1.sim_version == 1 and r2.sim_version == 2
    # 真世界尚未提交，版本停在 0，位置仍是起点
    eng = Engine(scn)
    assert eng.world_version == 0
    assert eng.world.has(at("me", "corridor"))
    receipt = eng.commit(move)
    assert receipt.success
    assert receipt.world_version_after == 1
    # 模拟版本号与真世界版本号互不影响
    assert sim.version == 2 and eng.world_version == 1


# 4. 动作失败回执：现场零改动、版本不递增、不收费、confirmed=False
def test_failed_receipt_leaves_world_untouched():
    eng = Engine(scenarios()["save_b_seal_broken"])
    eng.replan()
    # 推进到 take(valve_new) 受阻点
    blocked_receipt = None
    guard = 0
    while not eng.failed and guard < 40:
        if not eng.plan_queue:
            eng.replan()
        action = eng.plan_queue.pop(0)
        before_fp = eng.world.fingerprint()
        before_ver = eng.world_version
        receipt = eng.commit(action)
        guard += 1
        if receipt.blocked:
            blocked_receipt = receipt
            assert receipt.action == "take(valve_new)"
            assert eng.world.fingerprint() == before_fp
            assert eng.world_version == before_ver
            assert receipt.cost_charged == 0
            assert receipt.confirmed is False
            assert receipt.delta.is_empty()
            break
    assert blocked_receipt is not None


# 5. 成功回执以前后差分作为世界效果证据，且 confirmed 需审计背书
def test_success_receipt_carries_fact_delta_evidence():
    eng = Engine(scenarios()["bolt_case"])
    eng.replan()
    move = next(a for a in eng.plan_queue if a.kind == "move")
    receipt = eng.commit(move)
    assert receipt.success and receipt.confirmed
    added = {f for f in receipt.delta.added}
    removed = {f for f in receipt.delta.removed}
    assert at("me", move.args[0]) in added
    assert any(f[0] == "at" and f[1] == "me" for f in removed)
    assert receipt.world_fingerprint_before != receipt.world_fingerprint_after
    assert receipt.read_set_verified and not receipt.missing_pre


# 6. 提交前复核前提读集：真世界缺前提时提交被拒
def test_commit_rechecks_precondition_read_set():
    eng = Engine(scenarios()["bolt_case"])
    # 构造一个真世界不成立的动作：未持有扳手却紧固螺栓
    from repair_planner.actions import Action
    bogus = Action("tighten_bolts", ("pump",), 2,
                   (at("me", "pump_room"), carrying("me", "wrench")))
    read_set = PreconditionReadSet.of(eng.world, bogus)
    ok, missing = read_set.verify(eng.world)
    assert not ok and missing
    receipt = eng.commit(bogus)
    assert receipt.blocked and receipt.success is False
    assert eng.world_version == 0


# 7. 计划合法性与世界效果对应到执行证据：结束/失败均与真世界一致，禁止伪报
def test_no_false_reporting_finished_matches_real_world():
    for key in scenarios():
        eng = _run(key)
        snap = eng.snapshot()
        really_working = eng.world.has(working("pump"))
        # finished 必须有真世界 working 背书
        assert eng.finished == really_working, key
        # 每条成功回执都有非空（或合法空）差分且审计通过；失败回执不改世界
        for r in eng.receipts:
            if r.success:
                assert r.audit_ok and r.confirmed
                assert r.world_version_after == r.world_version_before + 1
            else:
                assert r.world_version_after == r.world_version_before
                assert r.world_fingerprint_after == r.world_fingerprint_before
        # 快照里的异常标注不得把失败运行报成完成
        if eng.failed:
            assert snap["finished"] is False
            assert any("失败" in x or "不可行" in x or "受阻" in x
                       for x in snap["anomalies"]), key
        if eng.finished:
            assert any("已完成" in x for x in snap["anomalies"]), key


# 8. 模块边界：planner 不得导入 executor/scenario 真世界执行路径之外的执行器
def test_planner_module_dependency_boundary():
    planner_path = os.path.join(ROOT, "repair_planner", "planner.py")
    tree = ast.parse(open(planner_path, encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        if isinstance(node, ast.Import):
            imported.update(n.name for n in node.names)
    assert "repair_planner.executor" not in imported
    # 规划器只允许事实/动作/模拟器/场景常量，不允许直接 import 真世界执行入口
    assert all(not name.endswith(".executor") for name in imported)
    # 模拟器同样不得反向依赖执行器
    sim_path = os.path.join(ROOT, "repair_planner", "simulator.py")
    sim_tree = ast.parse(open(sim_path, encoding="utf-8").read())
    sim_imports = {n.module for n in ast.walk(sim_tree)
                   if isinstance(n, ast.ImportFrom) and n.module}
    assert "repair_planner.executor" not in sim_imports
    assert "repair_planner.planner" not in sim_imports


# 9. 兼容适配：旧 domain 入口与旧 Engine.step 行为仍可用
def test_legacy_entry_points_still_work():
    from repair_planner.domain import apply as legacy_apply, available_actions as legacy_av
    assert legacy_apply is apply and legacy_av is available_actions
    eng = Engine(scenarios()["bolt_case"])
    eng.replan()
    rec = eng.step()  # 旧单步入口
    assert rec.index == 1 and rec.action and isinstance(rec.anomalies, list)
    assert eng.world_version >= 1


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
