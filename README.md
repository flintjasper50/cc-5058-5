# 维修角色目标规划器

信念空间规划与真实执行反馈分离的维修规划演示：规划器只消费显式世界快照，
真世界装置状态只能由执行器的受控回执入口变更。

## 图形入口（维修规划主入口）

窗口界面是本项目的主用入口，命令行仅做辅助验证。

- 启动脚本（项目根目录执行）：
  ```bash
  ./start_window.sh            # 默认端口 8765
  ./start_window.sh 9000       # 指定端口
  ```
- 或直接运行窗口启动器（会尝试自动打开浏览器）：
  ```bash
  python3 run_window.py
  REPAIR_PORT=9000 python3 run_window.py
  ```
- 浏览器手动访问地址：`http://127.0.0.1:8765/`

窗口内可完成：选择预置场景或自定义目标（故障、备件箱、捐赠件、密封垫、
工具位置/移除工具）、单步执行、连续执行、重置，并查看：

1. 当前规划步骤（含每步前提读集、成立依据、搜索成本）
2. 执行轨迹与维修动作（预期效果 vs 已确认效果、真世界版本、事实前后差分）
3. 运行状态与“规划步骤/维修动作”相关异常标注
4. 真世界/信念物品核对与资源守恒审计（识别幽灵零件）

## 命令行（仅辅助维修规划）

命令行不启动窗口，只用于列出场景、查看初始规划、跑执行轨迹与审计：

```bash
python3 repair_cli.py list                 # 列出预置场景
python3 repair_cli.py plan  <场景key>       # 只做初始规划并打印搜索成本
python3 repair_cli.py run   <场景key>       # 完整执行并打印轨迹/守恒审计
python3 repair_cli.py serve [端口]          # 等同图形入口的 HTTP 服务
```

## 模块边界（规划/执行解耦）

依赖单向，禁止反向引用：

```
model      世界事实：不可变 State 快照、FactDelta 前后差分、守恒审计
  ^
actions    动作模式：前提与确定性效果的纯语义（available_actions / apply）
  ^
simulator  分支模拟器：私有资源表、模拟版本隔离、前提读集 PreconditionReadSet
  ^
planner    只消费 State.snapshot()，BFS 后继经模拟器生成，不导入执行器、不触达真世界
  ^
executor   唯一允许变更真世界的地方：受控入口 commit() 产出动作回执 Receipt
```

辅助模块：

- `scenario.py`：场景构建与观察政策（真世界/初始信念、inspect/open_box 观察）
- `server.py`：仅标准库的 HTTP 服务，托管窗口界面与 `/api/session*` 接口
- `domain.py`：旧 `repair_planner.domain` 导入路径的兼容适配层，再导出 `actions`

关键纪律：

- **私有资源表**：每个规划分支持有独立 `ResourceTable`，分支内的抓取/安装只是
  预约；取消分支或弹出已预约步骤不会消耗现场配件。
- **版本隔离**：模拟版本由 `BranchSimulator` 独立递增；真世界版本只在
  `Engine.commit()` 成功后 +1，失败/受阻版本与现场保持不变。
- **提交前复核前提读集**：`commit()` 先在真世界复核动作的 `PreconditionReadSet`，
  信念假设不构成真世界前提；复核不过即返回受阻回执，不套用任何效果。
- **受控回执**：`Receipt` 携带成败、前提读集复核结果、真世界前后指纹、
  `FactDelta` 事实差分与守恒审计；`confirmed` 仅在提交成功且审计通过时为真，
  计划合法性与世界效果都有执行证据，接口不允许伪报。
- **外部行为冻结**：旧 `Engine.step()`、`PlanResult`、`StepRecord` 字段与
  `/api/session*` 端点保持兼容，旧样例无需改动即可运行到底。

## 测试（净目录，不依赖手改数据）

```bash
python3 -I tests/test_properties.py    # 既有规划/执行性质（12 项）
python3 -I tests/test_decoupling.py    # 规划/执行解耦边界性质（9 项）
python3 -I tests/run_scenarios.py      # 全部预置场景轨迹与守恒审计
```

也可把仓库（排除 `.git`/`__pycache__`）复制到一个全新空目录后运行上述命令，
结果应一致：属性测试全过、7 个场景守恒审计零失败。
