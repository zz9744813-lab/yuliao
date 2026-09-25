# 盲评呈现绑定回归移植说明

> 本文记录两层：`## 补齐的机制` 及以下为**移植时**（`77619b4`/`f17cb93`）的说明，
> 其中描述 legacy 猜义路径（`legacy_review_id`）的段落已被**会审整改**推翻；
> 整改本身见文末「会审整改」章节（2026-09-25，lg-fix-blind-legacy-409），
> 与正文冲突处以整改章节为准。

## 改动范围

- `tests/test_blind_presentation.py`：采用 `6da2a18:tests/test_blind_presentation.py` 的 311 行正文，内容未改。实测工作树文件与源提交的 Git blob 均为 `b9a9418df9ffa2bf7c56d09a8f976638daf9e1a0`。
- `app/api.py`：按块补入呈现映射机制，保留当前分支已有的 `ReviewPresentation` 数据库路径，没有整文件覆盖。
- `app/static/index.html`：仅补入 `a_hash` / `b_hash` 在取题、会话快照、提交和改判链路中的保存与回传。
- `tests/test_review_batch.py`：游标断言改用调用时派生的 `_cursor_file()`。
- `tests/test_review_rejudge.py`：映射断言改用 `presentation_id`，无呈现时加强为 409 且不落库，FIFO 用例隔离落盘副作用。
- `PORT_blind_presentation.md`：本说明。

## 补齐的机制

1. **呈现不可变绑定**：`_BLIND_MAP` 为 `presentation_id → entry`，`_BLIND_LAST` 保存 `review_id → 最近 presentation_id`（**会审整改后该索引已无任何读取方**，仅为落盘格式/审计保留，见文末）；判定记录 `presentation_binding` 区分 `presentation_id` 与 `none` 两态——移植期一度引入的第三态 `legacy_review_id`（无 pid 经 `_blind_latest` 猜义放行）**已被会审 BLOCK 并删除**。
2. **容量边界**：新增 `_BLIND_LAST_CAP`，映射表与最近 pid 索引分别按 512 上限逐出；多 worker 落盘时先读盘合并再写，避免整表覆盖抹掉其他 worker 条目。（整改后无 pid 一律不按「最近呈现」猜义，`_BLIND_LAST` 逐出序不一致不再影响收拒行为，见文末处置。）
3. **一次性懒加载**：新增 `_blind_ensure_loaded` 与 `_BLIND_LOADED_FOR`；首次使用或 `DATA_DIR` 改变时整块切换并装载，不在 import 期固化状态。
4. **跨重启存活**：呈现映射原子落盘到 `blind_presentations.json`；进程重启后可从文件恢复。当前分支原有 `ReviewPresentation` 数据库行继续作为兜底。
5. **损坏文件处理**：JSON 损坏时备份为 `blind_presentations.json.corrupt` 并记录 warning，不静默伪装成正常空映射。
6. **调用时路径派生**：`_present_file()`、`_cursor_file()` 统一经 `_data_file()` 从当前 `config.DATA_DIR` 派生；保留 `_CURSOR_FILE` 仅兼容当前分支既有等值契约，运行时读写不再使用导入时固化值。
7. **呈现校验**：出题冻结 `a_hash` / `b_hash`；提交绑定 pid，pid 与 review 不符返回 400，内存/文件机制中的未知或过期 pid 返回 409，指纹不符返回 409。无 pid 且带 A/B 语义（winner A/B 或有批注）的提交：该题存在任何呈现行 ⇒ 409（A01 二轮契约，整改后与内存映射存活与否无关）；从无呈现行的历史题投 A/B 亦 409（本分支既有测试钉死，见文末），仅 tie/both_bad/cant_judge 走「存原始值」binding=none。
8. **改判安全降级**：`review/.../serve` 取不到刚生成呈现时不猜旧位置，返回 `no_presentation` 备注并告警，避免 500 或错误回填批注。
9. **前端配套**：页面保存并回传本次呈现 pid 和双侧指纹；会话内改判沿用同一组绑定，服务端重端改判则保存新 pid/指纹。

当前 main 的数据库呈现契约保留：原生 `PR-` 行不存在时仍按既有弱版回归返回 404；本次新增的未知/逐出呈现 pid 路径返回 409。

## 实跑检查

所有 pytest 命令均设置 `PYTHONDONTWRITEBYTECODE=1` 和 `PYTEST_ADDOPTS='-p no:cacheprovider'`，仅禁止生成字节码/pytest 缓存；pytest 参数与验收命令一致。

1. 最终验收：

   ```text
   F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_blind_presentation.py tests/test_review_batch.py tests/test_review_rejudge.py -q
   ```

   退出码 `0`，`37 passed`。

2. 当前 main 既有呈现、批注和前端回归：

   ```text
   F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_review_presentation.py tests/test_review_annotations.py tests/test_frontend_invariants.py tests/test_frontend_escape.py tests/test_diff_js.py tests/test_marking_js.py -q
   ```

   退出码 `0`。

3. 游标隔离回归：

   ```text
   F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_cursor_isolation.py -q
   ```

   退出码 `0`，`2 passed`。

4. 语法与补丁检查：Python `compile(...)`、Node `new Function(...)` 解析 `index.html` 内联脚本、`git diff --check` 均退出码 `0`。

pytest 仅报告当前分支既有的 `invalid escape sequence` 与 FastAPI `on_event` deprecated warnings，无失败。

## 反向验证

临时仅卸掉 `verdict()` 中比较 `a_hash` / `b_hash` 的 409 判据后，运行：

```text
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_blind_presentation.py::test_page_text_fingerprint_mismatch_rejected -q
```

退出码 `1`；对应用例 `tests/test_blind_presentation.py::test_page_text_fingerprint_mismatch_rejected` 立即变红，证据为 `assert 200 == 409`（错误指纹提交被错误接受）。随后恢复完整判据，同一用例退出码 `0`、1 passed；最终三文件验收再次退出码 `0`。交付状态为完整判据态。

## 未改动部分（移植轮记录，历史）

- 未整文件覆盖 `app/api.py`；当前 main 的 `ReviewPresentation` 持久化、原生 pid 404 契约、模型与数据库结构均保留。
- 未改 `app/models.py`、`app/db.py`、`app/config.py` 或其它应用模块。
- `app/static/index.html` 除呈现 pid/双指纹传递外，其余 UI 逻辑保持原样。
- 未创建临时脚本、smoke/repro 文件或其它白名单外文件。（注：本节及上面"实跑检查/反向验证"描述的是**移植验证时的 worktree 状态**——该变更集随后即被提交为 `77619b4` 并经主控合入为 `f17cb93`（merge 提交），并非"从未入库"；会审指出"未 commit、merge、push"表述歧义，在此更正。）

## 会审整改（lg-fix-blind-legacy-409，2026-09-25）

三席会审对 `f17cb93` 出 **BLOCK**（记录 `F:/Hermes/team/reviews/language-genome-f17cb93b09.md`）。
本节记录整改。工作树 `fix/blind-legacy-409`，基线 `f17cb93`；改动只落在
`app/api.py`、`tests/test_blind_presentation.py`、本文档三个白名单文件。

### 1. qwen 席 [严重]：无 pid 分支恢复「有呈现行即 409」契约

移植版把无 pid 提交经 `_blind_latest` **接受**并只记 `binding=legacy_review_id`——
「最近呈现」正是被重新端题后的**新**映射，旧页面提交会被误译并以 HTTP 200 写进最贵的
偏好标签；留痕不是防线，且内存映射存活/重启逐出两种状态下同一提交的命运不同
（客户端结局取决于服务端重启时序）。整改：

- `verdict()` 无 pid 分支不再调用任何猜义路径，恢复 `56ddc43:app/api.py:882-895` 的
  A01 二轮口径（注释逐字保留）：带 A/B 语义（`winner in ("A","B")` 或 `annotations`
  非空）且该题存在**任何**呈现行 ⇒ 409 拒收、不落库；
- 从无呈现行的历史题：`binding=none` 的「存原始值」unresolved 路径仅对
  **非 A/B 判定**（tie/both_bad/cant_judge）及无归属语义的纯批注保留——与本仓
  既有弱版口径一致。**如实标注一处与 main 字面契约的偏差**：main 原文在无呈现行时
  对 winner=A/B 也放行「存原始值」，但本分支先于整改已合入的测试
  （`test_review_rejudge.py::test_pending_never_served_now_rejected`、
  `test_blind_presentation.py::test_legacy_without_any_presentation_rejected`，
  均非本次可改范围且在验收命令内）把"从未端出的题投 A/B"钉为 409——A/B 依赖一个
  从未存在过的排列，存原始值同样不可解读。整改服从测试钉住的更严口径；
- `_blind_latest` 失去唯一调用方，成为死函数，**已删除**（不留死代码）。
  `_BLIND_LAST` 随之只剩写入方（出题/装载/落盘），无读取方：保留以维持
  `blind_presentations.json` 的 `last` 键落盘格式与审计可追溯，注释已改为如实说明
  "不再有读取方"；`presentation_binding` 取值注释同步收敛为 `presentation_id | none`。

### 2. 测试改写（交付物 2）

`tests/test_blind_presentation.py` 第 6 条 `test_legacy_submit_without_pid_is_marked`
改写为 `test_legacy_submit_without_pid_is_never_guessed`，钉死新契约：

- 有呈现行 + 无 pid + winner=A/B ⇒ 409，且响应文案含「呈现绑定」，且被拒提交不落库；
- 有呈现行 + 无 pid + tie 带批注 ⇒ 409（批注同样携带 A/B 归属语义）；
- 从无呈现行的历史题 + winner=tie ⇒ 200，`presentation_binding == "none"`、
  `presentation_id is None`、存原始值。

`test_legacy_without_any_presentation_rejected` 仅更新了 docstring 措辞（旧措辞暗示
"有最近呈现就会放行"，与整改后契约不符），断言未动。

### 3. 会审 [一般]/[建议] 项逐条处置

- **`_blind_save_locked` 逐出序不一致**（`merged_p` 按 `created_at` 逐旧、`merged_l`
  按插入序逐旧，可留下"rid→已逐出 pid"悬挂条目）——**不改**：整改删除 `_blind_latest`
  后 `_BLIND_LAST` 已无任何读取方，悬挂条目不再能影响任何收拒行为；改动落盘格式
  （如逐出 last 中指向已失 pid 的条目）会扩大本整改变更面，收益为零。上限约束
  （`_BLIND_LAST_CAP <= _BLIND_CAP`）仍由既有测试钉住。
- **`verdict()` 指纹证伪循环 `if sent and …` 可被省略 a_hash/b_hash 绕过**——**不改**：
  `a_hash/b_hash` 按 `Verdict` 模型契约是**可选回传**字段（注释原文"可选回传，用于
  证伪过期页面"），强制必填会把旧客户端/纯 API 客户端的正常提交打成 400 式误伤；
  防线主次在整改后已摆正：无 pid 带 A/B 语义一律 409（本轮恢复），指纹只补刀
  "带正确 pid 却拿着旧文本"的场景——省略指纹者本就只能带 pid 或不带 pid，前者
  文本必然出自该次呈现（同 pid 不同文本已被冻结指纹拦），后者已被 409 拦。
- **`_load_cursor` 告警缺文件路径**——**已修**（本轮 diff 内）：告警现在同时给出
  `_cursor_file()` 路径与批次键；同族的 `_save_cursor` 告警未动，会审该项只点名
  `_load_cursor`，保存路径失败时目录多半已由 `mkdir` 建出、按 DATA_DIR 可推知，
  不扩大改动。

### 4. 实跑检查与反向变异——**本轮未获执行，如实报告**

验收命令：

```text
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_blind_presentation.py tests/test_review_batch.py tests/test_review_rejudge.py -q
```

本整改会话内**未能执行**：所有调用 Python 解释器的 Bash 请求（含上述验收命令、
`python --version` 最小探针、`py --version`）均停在无法应答的权限询问上（上一轮
worker 的 exit 124 超时与此症状一致）。**没有真跑就没有原始输出可贴，本文档不声称
通过**；交付状态为"代码与测试已按契约改写、静态核对完毕、等待主控独立执行验收门"。

已完成的静态核对：

- `git diff` 逐行复核（仅 3 个白名单文件；工作树无其它改动，无临时脚本残留）；
- 全仓 grep：`_blind_latest`、`legacy_review_id` 已无任何代码引用（仅历史注释/本文档）；
- 三个验收测试文件内**全部** verdict 提交点逐一核对：带 pid、或本就断言 409/404，
  与新分支逻辑一致（`test_pending_restart_rejects_unbound_ab` 走「有呈现行」409、
  文案含「呈现绑定」；`test_pending_never_served_now_rejected` 走「从无呈现行」409）。

反向变异自检（交付物 2 要求，待主控执行复现）：将 `verdict()` 无 pid 分支两条
`raise HTTPException(409, …)` 的条件临时改为恒假（即"把 409 改回接受"），预期转红：
`test_legacy_submit_without_pid_is_never_guessed`、
`test_legacy_without_any_presentation_rejected`（以上 blind 文件）、
`test_pending_restart_rejects_unbound_ab`、`test_pending_never_served_now_rejected`
（以上 rejudge 文件）——至少 4 条；恢复后应回到全绿。若按另一方案只把新测试里的
409 断言改成 200，则该用例本身必红。**本节粘贴的是复现配方，不是运行结果**；
运行结果缺失即本整改的已知未闭环项。

### 5. 未改动部分（整改轮）

- `app/static/index.html`、`app/models.py`、`data/` 未动；无 pid 时前端会收到 409
  提示重取题——与 main 契约行为一致。
- 未 commit、merge、push、publish；本文即整改轮白名单内新建/更新的唯一说明文件
  （对既有文件的更新）。
