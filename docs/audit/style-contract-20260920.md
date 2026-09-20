# 语感契约 + 干瘪体检（2026-09-20）

## 一、问题与证据

反馈：「语义语感不行」。定位到**产线写手提示词**，不是模型的锅。

A/B 对照（同场景卡、同模型、只换写作提示词）：现状产出 555 字、交易流水式；
加语感契约后 900 字，物象/潜台词/节奏同时出现（"钥匙在我手里，不代表我会借"
直接呼应提示词里的人类锚点）。代价：B 版突破事实边界（1 枚钱写成 2 枚、提前写第二场）
→ 语感契约必须与事实预算/验证器配套，不能单独放开。

## 二、两次失败的尝试（留档，别再走）

1. **自制词表"语感评分门"**：给 A/B 两份文本**都打了满分 1.0**。原因：干瘪的特征是
   **缺席**（没有细节），不是**出现**（有坏词），词表法结构性抓不到。已废弃。
2. **拿项目自己的 `ai_flavor`（集霸 63 条批注训出的 AI 味检测）打真实产物**：
   **0.003 分 / 不判为 AI 味**。即：反馈指的是「信息交付式的干瘪」，
   与「AI 味过重」是**相反的失败模式** —— 体检项必须同时覆盖两侧，只防 AI 味不够。

## 三、本次改动

| 文件 | 改动 |
|---|---|
| `app/style_contract.py`（新） | 契约正文单一真源 + `WRITER_CONTRACT_VERSION` + `probe()` 可数代理指标 + `issues()` 修稿指令 |
| `app/scene_runtime/pipeline.py` | `WRITER_SYSTEM` 内联契约；事实预算措辞精确化（"计划里的 events 与 changes 是唯一允许的持久状态变化"）；修稿轮追加语感指令（每轮只检测一次，复用同一份 probe）；开关打开时返回值带 `style` 体检数据 |
| `app/scene_runtime/contracts.py` | `Budget.style_feedback: bool = False`（默认关） |
| `tests/test_style_contract.py`（新） | 10 例：句切分、干瘪/丰盈区分度、只出 style 意见、逐维触发、超上限、提示词防回归、默认关、开关开、单次检测、语感不升级为 hard 失败 |

**边界（会审意见已纳入）**：语感**永不**产生 hard 结论、**永不**改变"何时算通过"的语义，
避免把文风问题变成死锁（`test_style_feedback_never_creates_hard_failure` 锁住）。
`probe()` 的阈值是**未验证的粗代理**（模块 docstring 明写"不得当作质量结论"），
`issues()` 只在 `style_feedback=True` 时参与修稿轮。

**提示词生效范围要看清**：`WRITER_SYSTEM` 的契约文本是**无条件生效**的（它就是写手每一轮的
system prompt），`Budget.style_feedback` 只控制**体检数据与修稿指令**。
提示词文本本身是冻结请求的一部分（`store.reserve_call` 以请求为幂等键），
改文本即自动失效旧缓存、不会静默复用旧提示词的结果；契约版本记在
`WRITER_CONTRACT_VERSION`，体检数据里回传该版本号。

## 四、验证

- 命中改动范围：`tests/test_scene_runtime.py tests/test_style_contract.py` → **46 例全绿**；
  含 `test_writer.py` / `test_websrc_contract.py` 的更大命中集 **69 例全绿**。
- 全仓：598 例 → 3 例红，全部为 **fresh worktree 缺 gitignored 本地数据**
  （`data/genome.db`、`data/llg.db`、`data/models/flavor_span_v1.npz`）。
  **决定性对照**：把本次改动 `git stash` 后在干净 HEAD 上跑同样用例 → **同样失败**，与本改动无关。

## 五、未做（需单独决策）

- `app/prompts_ctx.py` 的 RECON 两份模板（"骨架中没有的信息不要添加"）是**实验基线**，
  改它要升 `prompt_version` 并进 `SERVABLE_PROMPT_VERSIONS` 白名单、旧 candidate 作废，
  属于独立决策，本次未动。
- 语感判定的最终裁决仍应走项目既有的 judge 线（对照人类语料段），不在本次范围内。
