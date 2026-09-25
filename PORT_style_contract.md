# 移植说明：`feat/style-contract`（c108a26）→ 当前 main（b91b7f6）

任务：`lg-port-style-contract`。工作树：`F:\agi\_scratch\worktrees\style-contract-port`，分支 `task/style-contract-port`。
对照源：`c108a26`（`fix(style): 按三席会审整改`，merge-base 与 main 为 `799df08`）。

## 0. 结论先说（含一个未解除的阻塞）

**代码移植已完成，但验收门未能实跑。** 本工作树内**任何 python/pytest 调用都被权限层拒绝**
（含验收命令原样、`python -m pytest ...`、`pytest --version`、`<venv>/python.exe --version`，
共 5 次尝试，均返回 `Error: Allow Bash to run: ...?` 而未执行）。
所以下面 §4 的"实跑命令与退出码"记为**未取得**，不做任何"测试已全绿"的声称。
文件级检查（§3）是我在只能使用只读 git/文件工具的条件下能给出的全部证据。

## 1. 改动文件清单（全部落在白名单内，无新增其它文件）

| 文件 | 状态 | 行数变化 |
| --- | --- | --- |
| `app/style_contract.py` | 新增（内容取自 `c108a26:app/style_contract.py`，零改动） | 168 行 |
| `tests/test_style_contract.py` | 新增（内容取自 `c108a26:tests/test_style_contract.py`，零改动） | 158 行 |
| `app/scene_runtime/contracts.py` | 修改 | `+3 -0`（`git diff --stat`） |
| `app/scene_runtime/pipeline.py` | 修改 | `+18 -4`（`git diff --stat` 合计 21 插入 / 4 删除，其中 contracts.py 占 3 插入） |
| `PORT_style_contract.md` | 新增（本文件，交付说明，任务要求） | — |

`git diff --stat` 实跑输出（跟踪文件）：
```
 app/scene_runtime/contracts.py |  3 +++
 app/scene_runtime/pipeline.py  | 22 ++++++++++++++++++----
 2 files changed, 21 insertions(+), 4 deletions(-)
```

## 2. 每处改动的依据

### 2.1 `app/style_contract.py`（新增，正文零改动）

依据：任务交付物 1。取正文方式：`git show c108a26:app/style_contract.py` 全文转录。
落盘后已 `Read` 复核，168 行、与源逐行一致（含 `_raw` 未取整判定、`obvious(text)` 用上游默认阈值不复制常量、
`SENSORY_WORDS` 表、`ProbeResult` 的 `_raw` 注释）——即 c108a26 那次"三席会审整改"的成果**未被回退**。
依赖 `from .ai_flavor import analyze_v2, obvious`：main 的 `app/ai_flavor.py` 仍导出 `analyze_v2` 与
`obvious(text, threshold=0.5)`，故无需改 `ai_flavor.py`。

### 2.2 `tests/test_style_contract.py`（新增，正文零改动）

依据：任务交付物 2。同样由 `git show c108a26:tests/test_style_contract.py` 全文转录。
其对 main 的适配性经静态核对：fixture 与 main 现有 `tests/test_scene_runtime.py::setup`（第 41-55 行）同构
（同 `book-a` / 同 `ScenePlan(min_chars=1, max_chars=500)` / 同 `KnowledgePackage(source_kind="empty")`），
`World`/`ScenePlan`/`KnowledgePackage`/`Budget` 字段在 main `contracts.py:28-110` 全部仍存在且未收紧，
`Store.create_world`、`SceneRunner.run` 签名未变，`commit` 收据含 `status`（main 测试 `test_scene_runtime.py:63` 即以
`result["status"] == "committed"` 为口径）。

### 2.3 `app/scene_runtime/contracts.py`：只加一个字段

`Budget`（第 102 行）首部插入第 103-105 行的注释 + `style_feedback: bool = False`，与
`git diff main...c108a26 -- app/scene_runtime/contracts.py` 的 3 行新增逐字一致。
该分支对 `contracts.py` 的改动**仅此 3 行**（`git diff --stat 799df08 c108a26` 已确认整分支只动
`contracts.py` 3 行、`pipeline.py` 26 行、两个新文件、一份文档），故不存在"把分支其它改动带进来"的风险。

默认值 `False` ⇒ 默认行为不变：`Contract` 为 `extra="forbid", strict=True`（第 25 行），旧 JSON
（如 `examples/scene_runtime/ferry.json:48` 的 budget 无此键）走默认值即可，无需数据迁移。

### 2.4 `app/scene_runtime/pipeline.py`：不整文件覆盖，只搬语义

main 的 pipeline 与分支版本自 `799df08` 后已分叉（main 侧新增 `_call_verified` 空响应重试、
A10 `state_patch_not_authorized_by_plan` 有上限返修等）。我用的是**逐 hunk 移植**，
main 现有代码（第 40-116 行的 `parse_result`/`align_quotes`/`_call`/`_call_verified`、
第 143-190 行的 verifier 返修与 A10 分支）逐字保留，未被覆盖。四处移植：

1. **第 14 行 import**（对照分支第 14 行）：
   `from ..style_contract import STYLE_CONTRACT, issues as style_issues, probe as style_probe`，
   置于 `from .client import ...` 之前。分支顺手重排了 `.contracts` import 的 `ScenePlan`/`RuntimeFault` 顺序，
   属纯格式噪声，**未搬**（保留 main 原顺序）。
2. **`WRITER_SYSTEM`（第 20-27 行）**：删去旧笼统条款"可自由组织动作、对话和句子"，换成
   "**计划里的 events 与 changes 是本次唯一允许的持久状态变化**…"并把 `STYLE_CONTRACT` 原样内联
   （`""" + STYLE_CONTRACT`）。
   依据：任务交付物 3 的接线要求（语感授权/忌用清单进写手提示词）＋ `tests/test_style_contract.py:68-74`
   直接钉死该形状（`STYLE_CONTRACT in WRITER_SYSTEM`、旧条款必须消失）。任务正文把"main 的 WRITER_SYSTEM
   没有语感授权/忌用清单"列为待修现状，故此处属于必做接线，非越界改动（文件在白名单内）。
   副作用已知且符合原设计：提示词文本是冻结请求的一部分（`store.reserve_call` 以此为幂等键），
   改文本 ⇒ 旧缓存自然失效，这正是 `WRITER_CONTRACT_VERSION` 注释所述语义。
3. **第 122 行 `style_checked = None`**：与分支同位置（`job = self.store.job(job_id)` 之后）。
4. **第 201-208 行接线（分支第 159 行附近）**：`budget.style_feedback` 为真时，在本轮 issue 清单
   （verifier issues + operator issues）之后追加语感指令，且 `style_issues(..., checked=style_checked)`
   复用同一份 probe ⇒ 每轮只检测一次。追加发生在 `errors = validate_review(...)`（第 192 行）**之后**、
   `if not errors:`（第 209 行）判定之前，且只写 `issues`（喂给下一轮写手的材料），
   **不写 `errors`**——所以 K5 判据、`k5_established`、defect 口径一律不受影响。
5. **第 214-219 行诊断出口**：`diagnostics = {"style": style_checked} if style_checked else {}`，
   只在 `stop_after_verified` 与 commit 两个返回字典末尾 `**diagnostics`。
   开关为 False 时 `style_checked is None` ⇒ `diagnostics == {}` ⇒ 返回值逐字不变；
   `reused` 早退分支（第 120 行）与分支一样不加 `style` 键。

**未搬进来的一行**：分支把 `from .contracts import` 里 `RuntimeFault` 挪位（格式噪声，见 2.4-1）。
**未新增文件**：分支还带了 `docs/audit/style-contract-20260920.md`（53 行）——**不在本任务白名单**，
按硬约束**没有落盘**（见 §5）。

## 3. 我已完成的检查（全部只读，逐条给出口令）

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 两个新文件与源提交正文一致 | `git show c108a26:app/style_contract.py` / `git show c108a26:tests/test_style_contract.py` 与落盘 `Read` 逐行比对 | 一致；转录中发现并已修正 1 处抄写误差（`issues()` 文档串末尾 `）。` 曾被误写为 `。`） |
| 分支改动面 | `git diff --stat 799df08 c108a26` | 5 文件：`contracts.py +3` / `pipeline.py 26` / 2 新文件 / 1 文档 ⇒ 我的接线覆盖面吻合（文档除外） |
| main 侧分叉面 | `git diff 799df08..HEAD -- app/scene_runtime/pipeline.py` | main 只加了 `_call_verified` 与 A10 返修块，两处移植插入点上下文未变 ⇒ 逐 hunk 移植可行，已按 main 现状落点 |
| 依赖符号存在 | `git show HEAD:app/ai_flavor.py` | `analyze_v2`、`obvious` 均在，签名兼容 |
| 无提示词/预算金标被硬编码 | 全库 `Grep` "你是中文小说场景写作者"（仅 `pipeline.py:20` 一处）、`max_input_chars.*24000`（仅 `ferry.json` + 契约默认值） | 改 `WRITER_SYSTEM`/加 `Budget` 字段不触碰任何字面量金标 |
| 越界文件检查 | `git diff --stat`（§1） | 仅 2 个跟踪文件被改，新增文件均在白名单内 |

## 4. 验收命令与退出码（**未执行** —— 权限阻塞）

应跑但未跑通（每次均被权限层拒绝，命令未进入执行）：

```
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_style_contract.py tests/test_scene_runtime.py tests/test_scene_runtime_audit.py -q
退出码：未取得（permission denied before execution，尝试 3 次，含换 cwd / 换 `python` / 换裸 `pytest` 变体共 5 次）
```

反向验证同样**无法实跑**，下面给出的是静态因果（改回默认态的动作无需执行，因为交付状态始终是默认 `False`）。

**反向验证设计（改 `contracts.py:105` 默认为 `True`，或把 `pipeline.py:201` 短路为 `if False and budget.style_feedback:`）**

| 变异 | 必定变红的用例 | 因果 |
| --- | --- | --- |
| 默认改 `True` | `tests/test_style_contract.py::test_budget_defaults_to_no_style_feedback`（第 77-78 行） | 直接断言 `Budget().style_feedback is False` |
| 默认改 `True` | `tests/test_style_contract.py::test_style_diagnostics_absent_by_default_and_present_when_enabled`（第 120-134 行） | 第一次跑用 `Budget()`，默认一旦为真 ⇒ 返回值多出 `"style"` 键 ⇒ 第 123 行 `assert "style" not in off` 失败 |
| 删/短路 pipeline 第 201-208 行接线 | 同上第 132 行 `on["style"]["chars"]`（`KeyError`），以及 `test_style_feedback_probes_once_per_scene`（第 137-150 行 `len(calls) == 1` 实得 0） | 证明"接进改写轮"这一段真的在被测路径上 |
| 不内联 `STYLE_CONTRACT` | `test_writer_prompt_keeps_hard_constraints_and_carries_contract`（第 68-74 行） | 契约必须原样进写手提示词 |

## 5. 我**没有**改动的部分

- **未 commit / 未 merge / 未 push / 未 stage**：新文件仍是未跟踪状态，`git` 索引未动。
- **未落盘 `docs/audit/style-contract-20260920.md`**（分支第 5 个文件）：不在允许编辑清单内，按硬约束放弃；
  若需一并移植，请把它加进白名单后另开一轮。
- **未改 `app/ai_flavor.py`**、未改 `app/scene_runtime/store.py`、`client.py`、`audit` 侧代码，
  未改 `scripts/k4_*.py`、`scripts/k5_*`、任何 K4/K5 判据或收据/台账口径，未改其它测试文件。
- **未把 `style_feedback` 接入任何调用方**：`scripts/`、`run_scene.py`、K4/K5 驱动器仍一律默认 `False`
  （任务只要求接线语义，不要求开启）。
- 语感体检**不参与 hard 判定**：`errors`（`validate_review` 结果）与 `mark_verified` / `rewrite_budget_exhausted`
  / 回滚 / 收据字段一律未受影响；K5 判据与 defect 口径零变化。
- 未删除、未改写 main 已有的 `_call_verified`（verifier 空响应重试）与 A10 `state_patch` 有上限返修两块逻辑。
- 未运行网络/服务、未改账号或网络设置、未在源码 checkout 或其它 worktree 落任何文件。
