# 盲评呈现绑定回归移植说明

## 改动范围

- `tests/test_blind_presentation.py`：采用 `6da2a18:tests/test_blind_presentation.py` 的 311 行正文，内容未改。实测工作树文件与源提交的 Git blob 均为 `b9a9418df9ffa2bf7c56d09a8f976638daf9e1a0`。
- `app/api.py`：按块补入呈现映射机制，保留当前分支已有的 `ReviewPresentation` 数据库路径，没有整文件覆盖。
- `app/static/index.html`：仅补入 `a_hash` / `b_hash` 在取题、会话快照、提交和改判链路中的保存与回传。
- `tests/test_review_batch.py`：游标断言改用调用时派生的 `_cursor_file()`。
- `tests/test_review_rejudge.py`：映射断言改用 `presentation_id`，无呈现时加强为 409 且不落库，FIFO 用例隔离落盘副作用。
- `PORT_blind_presentation.md`：本说明。

## 补齐的机制

1. **呈现不可变绑定**：`_BLIND_MAP` 改为 `presentation_id → entry`，`_BLIND_LAST` 保存 `review_id → 最近 presentation_id`；判定记录新增 `presentation_binding`，区分 `presentation_id`、`legacy_review_id` 和 `none`。
2. **容量边界**：新增 `_BLIND_LAST_CAP`，映射表和最近 pid 索引分别按 512 上限逐出；多 worker 落盘时先读盘合并再写，避免整表覆盖抹掉其他 worker 条目。
3. **一次性懒加载**：新增 `_blind_ensure_loaded` 与 `_BLIND_LOADED_FOR`；首次使用或 `DATA_DIR` 改变时整块切换并装载，不在 import 期固化状态。
4. **跨重启存活**：呈现映射原子落盘到 `blind_presentations.json`；进程重启后可从文件恢复。当前分支原有 `ReviewPresentation` 数据库行继续作为兜底。
5. **损坏文件处理**：JSON 损坏时备份为 `blind_presentations.json.corrupt` 并记录 warning，不静默伪装成正常空映射。
6. **调用时路径派生**：`_present_file()`、`_cursor_file()` 统一经 `_data_file()` 从当前 `config.DATA_DIR` 派生；保留 `_CURSOR_FILE` 仅兼容当前分支既有等值契约，运行时读写不再使用导入时固化值。
7. **呈现校验**：出题冻结 `a_hash` / `b_hash`；提交绑定 pid，pid 与 review 不符返回 400，内存/文件机制中的未知或过期 pid 返回 409，指纹不符返回 409。无可解释呈现的 legacy A/B 提交返回 409。
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

## 未改动部分

- 未整文件覆盖 `app/api.py`；当前 main 的 `ReviewPresentation` 持久化、原生 pid 404 契约、模型与数据库结构均保留。
- 未改 `app/models.py`、`app/db.py`、`app/config.py` 或其它应用模块。
- `app/static/index.html` 除呈现 pid/双指纹传递外，其余 UI 逻辑保持原样。
- 未创建临时脚本、smoke/repro 文件或其它白名单外文件；未 commit、merge、push、publish 或启动服务。
