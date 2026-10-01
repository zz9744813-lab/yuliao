# DELIVERY：live 通道模型身份诚实化 + Draft 契约失败可恢复重试（2026-10-01）

worktree：`F:\agi\_scratch\worktrees\lg-model-identity-retry`（分支 `task/model-identity-retry`，
基线 a64a8df）。**未 commit、未 push、未写真库、未调真实模型**（全程注入式
httpx MockTransport 假网关）。口径文档：`docs/MODEL_IDENTITY_AND_RETRY.md`。

> 上一轮执行 note：上一窗口代码/测试已落盘但在交付文档写出前超时（exit 124），
> 验证门报「test_model_identity.py 不存在 + DELIVERY 缺失」。本轮先复核上轮
> 代码跑回归，发现**一处真实回归**（下节「回归与修正」），修完后重跑全部证据
> 并补齐两份文档。验证门报红的那两项现已消除（文件在、命令 exit=0）。

## 1. 回归与修正（本轮相对上轮的唯一代码改动）

上轮把 fail-closed 拦截放在 `GatewayClient.invoke` 内，与既有钉死测试
`tests/test_scene_runtime.py::test_gateway_one_request_actual_model_and_no_hidden_retry`
（该文件**不在允许编辑清单**）冲突：它钉的是 invoke 直接调用侧
「记录 actual_model、不拦截、不隐藏重试」的旧契约。回归实跑抓到 1 failed。

修正（只动允许清单内文件）：
- `client.invoke` 回到**只记录**语义（回复里恒带 `requested_model` /
  `actual_model` / `model_identity_ok` / `substituted`，绝不静默）；
- 拦截移到 **live 通道** `SceneRunner._call`（任务书标题即「live 通道」）：
  `substituted` 且未开 `LG_ALLOW_MODEL_SUBSTITUTION` ⇒ 在 try 内抛
  `RuntimeFault("model_identity_mismatch:<req>-><actual>")`，抛错路径把已收到
  回复连同 requested/actual 一起落 calls 台账（失败行也带真相）。
  生产侧所有 `GatewayClient` 用法（`scripts/k4_paired_scenes.py`、
  `scripts/run_scene.py`、`scripts/runtime_parallel.py`）都经 SceneRunner，
  拦截覆盖面不变。

## 2. 交付清单（全部在允许编辑白名单内）

| 文件 | 状态 | 内容 |
|---|---|---|
| `app/scene_runtime/client.py` | 改 | 每次调用记录 `actual_model`（网关响应体 `model` 字段）+ `substituted`/`model_identity_ok` 标记；`ModelIdentityMismatch`（携带回复供落账）；`model_substitution_allowed()` 调用时读 env |
| `app/scene_runtime/pipeline.py` | 改 | A2：`_call` 内 live 通道 fail-closed 拦截（默认抛 `model_identity_mismatch:req->actual`，含 `->unreported`；`LG_ALLOW_MODEL_SUBSTITUTION=1` 显式降级为如实记录）；B：`_parse_attempt`+`_parse_or_retry`+`_verified_answer_or_retry`——契约失败同模型重试 1 次（stage `.retry`，输入带 `previous_reply`+`contract_error` 原文），重试仍不合 ⇒ 照旧 raise；结构性禁止换模型 |
| `app/scene_runtime/store.py` | 改 | calls 表加性迁移新列 `requested_model`/`actual_model`/`model_substituted`（不抬 RUNTIME_VERSION，旧库可开、幂等）；`reserve_call`/`finish_call` 双 phase 落账（失败行也落真相）；`usage()` 区分 `contract_retries` 与 `verifier_invalid_retries` 两口径 |
| `scripts/k4_paired_scenes.py` | 改 | live 逐臂收据新增 `models_actual`（实际服务模型；逐次换皮如实列清单）、`model_identity_ok`（无请求侧记录⇒False）、`verifier_attempts[*].model_actual`、`usage.contract_retries`；`models` 语义不变=请求；离线收据键集逐字不变 |
| `tests/test_model_identity.py` | 新 | 8 条（正向落账/降级落账/夹具口径/legacy 迁移/live 收据形状；反向：默认不一致必抛且失败行落真相、网关不报模型=unreported 必抛、两请求名同一上游⇒identity_ok=False） |
| `tests/test_contract_retry.py` | 新 | 12 条（parametrize 5 形态：多包一层/缺字段/截断/双围栏/围栏外套话 重试后成功走**真** `_parse_contract`+真 `model_validate`；反向：重试仍不合必抛、重试计预算 `max_calls=2` 实证、换模型重试结构性不存在、契约重试≠无效重试口径） |
| `docs/MODEL_IDENTITY_AND_RETRY.md` | 新 | 口径、开关、消费侧复核方法 |

任务书条目核对：A1 ✓ A2 ✓（默认 fail-closed + env 降级，两模式均落账）
A3 ✓（models_actual/model_identity_ok，models 语义不变）
B1 ✓（同模型、stage `.retry`、附错误原文、不进改写额度）
B2 ✓（仍不合照旧 raise）B3 ✓（计入 calls 表与 usage.calls）
B4 ✓（role→model 唯一映射，无换模型路径；`effect_gate_snapshot.py` 一字未动）。

## 3. 真跑命令与真跑输出原文

以下均在 `F:\agi\_scratch\worktrees\lg-model-identity-retry` 实跑
（venv python：`F:/Hermes/hermes-agent/venv/Scripts/python.exe`；
`LG_LOCK_DIR=/tmp/...` 仅为隔离测试锁，见 conftest 卫生警告，不影响判据）。

### 3.1 验收命令（20 tests = 身份 8 + 契约重试 12）

```
$ LG_LOCK_DIR=/tmp/lg_lock_f F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_model_identity.py tests/test_contract_retry.py -q
....................                                                     [100%]
exit=0
```

收集数（`--collect-only` 尾行原文）：

```
20 tests collected in 0.96s
```

（本仓 conftest 把 `-q` 的 passed 汇总行吞掉，故以点计数 + `exit=0` +
collect 数三证。）

### 3.2 全量相关回归（scene_runtime / k4 / 语义门 / 收据形状 17 个测试文件）

```
$ FILES=$(grep -l "scene_runtime\|k4_paired_scenes\|effect_gate" tests/*.py)
$ LG_LOCK_DIR=/tmp/lg_lock_h F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest $FILES -q
........................................................................ [ 28%]
........................................................................ [ 57%]
........................................................................ [ 85%]
....................................                                     [100%]
exit=0
```

252 项全过。含既有钉死项：`test_scene_runtime.py`（invoke 旧契约、无隐藏重试）、
`test_k4_worlds_dir_receipt.py`（离线收据键集不漂移）、
`test_docs_code_reconcile.py`（文档-代码对账）。

修正前的反证（证明 §1 回归真实存在，非臆测）：拦截在 invoke 时同一命令
`FAILED tests/test_scene_runtime.py::test_gateway_one_request_actual_model_and_no_hidden_retry`
（`ModelIdentityMismatch: model_identity_mismatch:requested-w->actual-revision`），
共 1 failed；移到 `_call` 后归零。

### 3.3 台账新列直查探针（stdin 一次性命令，不落脚本文件）

```
$ LG_LOCK_DIR=/tmp/lg_lock_j F:/Hermes/hermes-agent/venv/Scripts/python.exe - <<'EOF'
import sys, tempfile, pathlib
sys.path.insert(0, r"F:\agi\_scratch\worktrees\lg-model-identity-retry")
from app.scene_runtime.store import Store
p = pathlib.Path(tempfile.mkdtemp()) / "probe.sqlite"
s = Store(p)
with s.connection() as db:
    cols = [r[1] for r in db.execute("PRAGMA table_info(calls)")]
print("calls columns:", cols)
assert {"requested_model", "actual_model", "model_substituted"} <= set(cols)
print("OK: identity columns persisted, additive migration in place")
EOF
calls columns: ['job', 'stage', 'request_hash', 'status', 'request', 'response', 'error', 'started_at', 'duration_ms', 'requested_model', 'actual_model', 'model_substituted']
OK: identity columns persisted, additive migration in place
```

失败行落真相、旧库加性迁移、live 收据新键这三项的可复核证据即以
3.1/3.2 的 20+252 条测试为凭（对应测试名见 §2 表）。

## 4. 未做 / 边界（不谎报）

- **未做**真实模型端到端验证（任务书纪律明令「不调真实模型」）：真网关下
  `model_identity_mismatch` 与契约重试的实际命中率，待下轮值班用
  `scripts/k4_paired_scenes.py --live` 小批真跑后按 §复核 查账。
- **未做**对既有 K5 失败臂的补跑（本任务只交付机制，不改历史产物）。
- **未改**任何判据/门禁：`effect_gate_snapshot.py` 与全部 tests/* 既有文件
  一字未动（§3.2 为其通过证据）；离线收据键集逐字不变。
- **未写真库**：`D:\language-genome-data\language_genome.db` 未触碰；测试全部
  tmp_path 副本库。
- **未 commit / 未 push**（等会审门禁）。
- 拦截位置由「client.invoke 内」修正为「SceneRunner._call 内」：任务书 A2
  未指定文件级位置，live 通道语义等价且覆盖面相同（生产侧全部经 SceneRunner）；
  直接调用 invoke 的旧契约测试得以保全。此为本轮相对任务书字面的唯一口径
  澄清，已写入 `docs/MODEL_IDENTITY_AND_RETRY.md` §1.1–1.2。

## 5. 复核入口

- 查账 SQL / 收据 jq / 开关语义：`docs/MODEL_IDENTITY_AND_RETRY.md` §3–§4。
- 存量污染核对（只读）：探针 `F:\Hermes\cache\scratch\model_subst_scan.py`。
