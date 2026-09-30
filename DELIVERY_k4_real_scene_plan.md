# DELIVERY：K4 真实场景包闸（`real_scene_plan_unverified` 卡通路）

- 日期：2026-09-30
- 工作区：`F:/agi/_scratch/worktrees/lg-k4-real-scene-plan`（分支 `task/k4-real-scene-plan`，基线 `75ac752`）
- 设计文档：`docs/K4_REAL_SCENE_PLAN.md`
- 回归：`tests/test_k4_real_scene_plan.py`（35 例）
- **零真实模型调用、零真库写**：全部证据用临时 SQLite 夹具（`C:/Users/6/AppData/Local/Temp/opencode/k4rsp/`）+ `tmp_path` + `FxClient` 顶替 `GatewayClient`。
- 未 commit / 未 merge / 未 push。

## 1. 交付了什么

`preflight_world()` 里写死的

```python
world_reason = "real_scene_plan_unverified:缺真实作品场景卡与逐臂预算合同"
ready = False
```

已换成**可核验的四读数判据**。`--live` 复用同一个 `ready`（同一函数、同一判据），未过闸时
在 `GatewayClient` 构造之前退出。不传 `--scene-bundle` 时拒绝行为与今天**逐字一致**。

变更文件（全部在白名单内，无新增文件）：

| 路径 | 变更 |
|---|---|
| `scripts/k4_paired_scenes.py` | +797 / −52：真实场景包 schema 与校验链、闸判据、`--emit-scene-pack`、执行侧接包、live 复用闸 |
| `tests/test_k4_real_scene_plan.py` | 新增（707 行，35 例） |
| `docs/K4_REAL_SCENE_PLAN.md` | 新增（判据表 / 校验链 / 拒绝码全表 / 已知限制） |
| `DELIVERY_k4_real_scene_plan.md` | 本文件 |

`data/k4_scene_bundles/` **未新增**：`data/` 在 `.gitignore` 内且不在本件白名单，出包走
`--emit-scene-pack`，包哈希随预检报告落账（见 §6 限制 8）。

## 2. 判据表（`preflight_world` 单一来源）

| # | 读数 | 判据 | 缺失时原因码 |
|---|---|---|---|
| ① | `pack_ok` | 场景包通过全部校验（schema/自摘要/钉值/绑定/A 臂策略/预算合同/逐场 `validate_plan`） | `no_scene_pack`（未传）/ `scene_pack_rejected:<码>` |
| ② | `registered` | `work_sources` 有该 `book_id` 行 | `world_not_registered` |
| ③ | A 臂非空 | `k3_status=matched` 且 `selected_ids` 非空 | `empty_package` |
| ④ | `review_status` | 既有语义审查收据可核验（= `verified`，本件**不放宽**） | `semantic_review_unverifiable` |

齐 → `ready=True`，`world_reason` 以 `real_scene_plan_pack_verified:` 开头并给逐项读数；
缺任一 → `ready=False`，`world_reason = "real_scene_plan_unverified:" + 分号连接的阻塞码`。

包报告里 `live_ready` **恒 False**（`pack_alone_never_authorizes_live`）：包本身从不授权真跑。

## 3. 正例（真实三场包 + 已登记 + A 臂非空 + 审查链可核验）

夹具库：`tests/test_k5_promotion_write.py::_reviewable_round` 造的**临时**库
（`WK-A`/`WK-B` 登记、`ESV2-T` 经两席票 + `commit_promotion` 升到 `verified`、scope `GENRE`）——
复用的是 K5 晋升写侧同一套取证，不是本件自造状态。

### 3.1 出包

```
$ python scripts/k4_paired_scenes.py --emit-scene-pack <tmp>/pack.json --book-id WK-A
[k4_paired_scenes] 真实场景包已写 C:\Users\6\AppData\Local\Temp\opencode\k4rsp\pack.json
{
 "book_id": "WK-A",
 "scene_ids": [
  "zhl-c1-s1",
  "zhl-c1-s2",
  "zhl-c1-s3"
 ],
 "scene_count": 3,
 "self_sha256": "b8a5f4eb24131f1b517263dfc3249f35fe2884c450f80983f672133027aa7994",
 "origin_book_id": "zhutian-hongyanlu",
 "book_id_remapped": true,
 "a_arm_policy_sha256": "e8e4853927b930cf689a802da4dec3dbf4a3908405380a88f682d63828bc917a",
 "budget_contract_arms": [
  "A",
  "B"
 ],
 "deferred_canon": [
  "现有章合同未指定救援区内谁有资格代表受影响居民签署；本候选不代签、不让临时契生效",
  "谁担任独立见证人、公开复议何时举行，须在正式场景卡定稿前确认",
  "顾砚舟的告别记忆损失和泊界城坐标暴露属于后续真正接锚时的代价，本三场不提前结算"
 ]
}
exit=0
```

### 3.2 预检 ⇒ `ready=true`，退出码 0

```
$ python scripts/k4_paired_scenes.py --preflight --book-id WK-A \
    --scene-bundle <tmp>/pack.json --expected-pack-sha256 b8a5f4eb…aa7994
{
 "book_id": "WK-A",
 "registered": true,
 "k3_status": "matched",
 "selected_ids": ["ESV2-T"],
 "n_techniques": 1,
 "empty_reason": null,
 "review_status": "verified",
 "knowledge_ready": true,
 "pack_ok": true,
 "pack_reason": null,
 "pack_sha256": "b8a5f4eb24131f1b517263dfc3249f35fe2884c450f80983f672133027aa7994",
 "world_reason": "real_scene_plan_pack_verified:pack_sha256=b8a5f4eb2413;artifact_sha256=110980d0d9e8;scenes=3[zhl-c1-s1,zhl-c1-s2,zhl-c1-s3];world_sha256=023e8cbd584b→250c4853bec6;budget_contract=arms[A,B]×3=6条;max_calls=6,max_rewrites=2,max_elapsed_seconds=600;a_arm_policy=e8e4853927b9;registered=True;k3_status=matched;a_arm_techniques=1;review_status=verified",
 "ready": true,
 "scene_pack": {
  "kind": "real_scene_pack",
  "schema_version": "k4-real-scene-pack/1",
  "pack_valid": true,
  "live_ready": false,
  "live_ready_reason": "pack_alone_never_authorizes_live:registered_world+nonempty_a_arm+review_chain_decide",
  "model_calls": 0,
  "db_registration_checked": false,
  "origin_book_id": "zhutian-hongyanlu",
  "book_id_remapped": true,
  "pov_by_scene": {"zhl-c1-s1": "CHAR-GU", "zhl-c1-s2": "CHAR-NING", "zhl-c1-s3": "CHAR-GU"},
  "initial_world_sha256": "023e8cbd584b13b19256f1fce05e1ce5016b61a29c6bbc2097a66378e1d11e4e",
  "final_world_sha256": "250c4853bec65bd87798a25762e4eeb87a600e704567655e3bfa3ccf24b604de",
  "plans_sha256": "56dbd6114662e825495ff35a44ed8eb4cfe219be4091f74316c1a2b587cfc48c",
  "artifact_sha256": "110980d0d9e88572c3a51252120d470e09bf424c30e315b318bcb182ceeaa0ae",
  "a_arm_policy": {"policy_sha256": "e8e4853927b930cf689a802da4dec3dbf4a3908405380a88f682d63828bc917a", "bound": true},
  "budget_contract": {"arms": ["A","B"], "entries": 6, "complete": true,
    "effective_limits": {"style_feedback": false, "max_calls": 6, "max_rewrites": 2,
                         "max_input_chars": 24000, "max_output_tokens": 3000,
                         "max_elapsed_seconds": 600}},
  "canon": {"status": "scoped_pack_canon_closed", "deferred_canon": [ …三条… ]},
  "source_pack_file_checked": false,
  "source": {"title": "诸天红颜录",
             "origin_ref": "F:/Hermes/team/K4_ZHUTIAN_OFFLINE_SCENES_20260928.json",
             "origin_sha256": "8263a6fb14a44d1a9e3918bb2be2ada8c5ba2a9dd92c8ec8d571edb60e9ec85e",
             "scene_card_basis": "docs/K4_世界目录收据_20260924.md"}
 }
}
[preflight] 通过：book_id=WK-A 已登记，A 臂包非空（n_techniques=1）；real_scene_plan_pack_verified:pack_sha256=b8a5f4eb2413;artifact_sha256=110980d0d9e8;scenes=3[zhl-c1-s1,zhl-c1-s2,zhl-c1-s3];world_sha256=023e8cbd584b→250c4853bec6;budget_contract=arms[A,B]×3=6条;max_calls=6,max_rewrites=2,max_elapsed_seconds=600;a_arm_policy=e8e4853927b9;registered=True;k3_status=matched;a_arm_techniques=1;review_status=verified
exit=0
```

（上面为了可读性省略了 `approval_manifest` 的 8 行逐票字段与部分重复键；实际 stdout 是完整
JSON，`selected_ids`/`approval_manifest` 逐条在册。）

**关于「A 臂包非空」**：本夹具里 A 臂确实非空（`n_techniques=1`），所以 `ready=true` 不是
靠放行换来的。若真实库上 A 臂为空，闸给的是 `empty_package`（见 N13），**不是**包的问题——
那半属 `lg-k3-query-unblock`，本件没有为了让数字好看而放行。

### 3.3 出包 → 用包同源

`--emit-scene-pack` 生成的包直接喂 `--preflight` ⇒ 同一 `ready=true`（回归
`test_emit_scene_pack_round_trips_through_the_gate`）；`--emit-scene-pack` 目标已存在即拒，
不覆盖（见 N17）。

## 4. 负例（27 条，逐条仍拒 + 原因码可读；**每条 exit=1**）

包类（闸之前，`ScenePackError` ⇒ `[scene-pack] 拒绝：<码>`）：

| # | 场景 | 逐字输出 |
|---|---|---|
| N1 | 包缺一场（plans 只留 2 场） | `[scene-pack] 拒绝：pack_scene_count_mismatch:2!=3` |
| N2 | 预算合同缺一臂（删 `B:zhl-c1-s3`） | `[scene-pack] 拒绝：pack_budget_contract_incomplete:missing=B:zhl-c1-s3` |
| N3 | 包 SHA 不匹配（换包冒充） | `[scene-pack] 拒绝：pack_expected_sha256_mismatch` |
| N4 | 未登记 book_id（包绑定到 `WK-NOT-REGISTERED`） | `world_reason: "real_scene_plan_unverified:world_not_registered;empty_package;semantic_review_unverifiable"`，`"ready": false` |
| N5 | 场景未过 `validate_plan`（s2 before/after 与世界实际不符） | `[scene-pack] 拒绝：pack_plan_invalid:zhl-c1-s2:plan_precondition_conflict` |
| N6 | 角色可见性冲突（`rescue_scope` 只对 `CHAR-GU` 可见，s2 的 POV 是 `CHAR-NING` 却改它） | `[scene-pack] 拒绝：pack_plan_invalid:zhl-c1-s2:planned_fact_outside_pov` |
| N7 | 包内自摘要被改（偷改 `temporary_terms` 不重算） | `[scene-pack] 拒绝：pack_self_sha256_mismatch` |
| N8 | 预算越契约域（`max_calls=99`，域 2..20） | `[scene-pack] 拒绝：pack_budget_out_of_contract:A:zhl-c1-s1` |
| N9 | 期望哈希格式非法（`NOTAHASH`） | `[scene-pack] 拒绝：pack_expected_sha256_invalid` |
| N10 | 修订顺序错位（s3 按 rev1 声明） | `[scene-pack] 拒绝：pack_plan_invalid:zhl-c1-s3:world_revision_conflict` |
| N11 | 触碰不可变事实（`anchor_link`） | `[scene-pack] 拒绝：pack_plan_invalid:zhl-c1-s1:unknown_or_immutable_fact` |
| N12 | 不传 `--scene-bundle` | `world_reason: "real_scene_plan_unverified:no_scene_pack:未传 --scene-bundle，内置世界/场景仍是合成夹具（林穗/三枚钱），缺真实作品场景卡与逐臂预算合同"`，`"ready": false` |
| N13 | A 臂空（包**完全合格**，库内策略被降级出 `eligible_statuses`） | `"n_techniques": 0, "empty_reason": "empty_package:k3_status=empty；全库策略状态分布={'hypothesis': 1}，verified=0（eligible_statuses 只认 verified ⇒ A 臂知识包恒空，无合格证据可进包）", "world_reason": "real_scene_plan_unverified:empty_package;semantic_review_unverifiable", "ready": false` |
| N14 | `--live` 不带场景包（`LLM_MODE=real`，K2/K3 全绿） | `[preflight] 拒绝 --live 起跑（非零退出，零真实调用）：real_scene_plan_unverified:no_scene_pack:未传 --scene-bundle…（book_id=WK-A, k3_status=matched）` |
| N15 | `--live` 带未登记世界的包 | `[preflight] 拒绝 --live 起跑（非零退出，零真实调用）：world_not_registered:work_sources 无 book_id=WK-NOT-REGISTERED 的登记行（K4 必须先用有登记、可匹配的真实试点世界，禁止用未登记 WK-K4 虚构场景当真跑证据）（book_id=WK-NOT-REGISTERED, k3_status=empty）` |
| N16 | legacy 离线候选包（`status=offline_candidate_not_canon_approved`） | `"structure_pass": true, "live_ready": false, "pack_ok": false, "ready": false`，`world_reason` 仍 `real_scene_plan_unverified:no_scene_pack:…` |
| N17 | `--emit-scene-pack` 覆盖既有包 | `--emit-scene-pack 目标已存在：…\pack.json——不覆盖既有包（换新路径，防拿旧包冒充）` |
| N18 | 缺 `--expected-pack-sha256` | `--scene-bundle 需要 --expected-pack-sha256` |
| N19 | 真实包接离线跑（无 `--preflight`/`--live`） | `真实场景包（--scene-bundle）只接 --preflight/--live 的场景包闸；离线跑仍用内置合成夹具` |
| N20 | 包绑别的 book（`--book-id WK-B`） | `[scene-pack] 拒绝：pack_book_id_mismatch` |
| N21 | 场景卡分支错位（`plans[0].branch_id="alt"`） | `[scene-pack] 拒绝：pack_scene_scope_mismatch` |
| N22 | 合同 `arms` 只剩一臂（entries 六条齐全也不行） | `[scene-pack] 拒绝：pack_budget_contract_incomplete:arms=A` |
| N23 | 合同同一 (arm, scene) 两条上限 | `[scene-pack] 拒绝：pack_budget_contract_duplicate:A:zhl-c1-s1` |
| N24 | 合同多出一条包外场次 `A:zhl-c1-s9` | `[scene-pack] 拒绝：pack_budget_contract_extra:A:zhl-c1-s9` |
| N25 | 重复 JSON 键（`"a":1,"a":2`） | `[scene-bundle] 拒绝：pack_json_invalid` |
| N26 | 包文件 > 1 MiB | `[scene-bundle] 拒绝：pack_size_invalid` |
| N27 | 包文件不存在 | `[scene-bundle] 拒绝：pack_unreadable` |

补充（CLI 未单列、但回归里有 `assert` 逐条钉住的 6 条）：
A 臂策略不匹配 `pack_a_arm_policy_mismatch`、`pack_schema_invalid`
（类型越界 / `canon_status` 越权）、`pack_scene_identity_duplicate`、
`--scenes 2` 与包内三场不符、预算合同与 `--budget-calls` 冲突
（`ValueError: 与场景包内 A:zhl-c1-s1`）、审查收据不可核验
（新一轮 freeze ⇒ 旧签认作废 ⇒ `semantic_review_unverifiable`）。

## 5. 旧行为保留（离线合成夹具路径逐字不变）

```
$ python scripts/k4_paired_scenes.py --out <tmp>/off1
exit=0
top_keys= ['analysis', 'artifacts', 'channel_changed', 'live', 'worlds_dir']  live= False
scenes= ['s1', 's1', 's2', 's2', 's3', 's3']
receipt_keys= ['arm', 'budget_calls', 'channel_changed', 'gateway_host', 'job_id',
               'live', 'models', 'retried', 'scene', 'usage', 'verifier_attempts']
prose0= 林穗把一枚钱放在桌上，又收了回去。（空包对照）
failures= []
[k4_paired_scenes] PASS：3 场×2 臂全 committed，3 个 A 臂包（freeze=False）
```

产物键集、收据键集（**无** `scene_pack_sha256`）、场景 id、夹具正文与改动前一致。

## 6. 验收命令逐字输出

```
$ F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_k4_real_scene_plan.py -q
...................................                                      [100%]
============================== warnings summary ===============================
tests\conftest.py:100
  …[R6 测试卫生 OPEN-4] 未设 LG_LOCK_DIR：本轮 pytest 以源检出目录 …\data 为整轮锁位…建议跑套件前设 LG_LOCK_DIR=<临时目录> 把锁隔离到检出之外…
    config.issue_config_time_warning(

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
exit=0
```

`pyproject.toml` 的 `addopts = "-q"` 与命令行 `-q` 叠加成 `-qq`，会把「N passed」计数行
一并吞掉；同一条命令加 `-o addopts=""` 拿到计数原文：

```
$ F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_k4_real_scene_plan.py -o addopts="" -q
...................................                                      [100%]
35 passed, 1 warning in 22.06s
exit=0
```

相邻回归（同一工作区，`LG_LOCK_DIR` 指向检出之外的临时目录）：

```
$ … -m pytest tests/test_k4_paired.py tests/test_k4_receipt_worlds_dir.py tests/test_docs_code_reconcile.py -q
83 passed in 29.32s          exit=0
$ … -m pytest tests/test_k4_accept_report.py tests/test_k4_registered_world_preflight.py \
      tests/test_k4_scene_bundle_check.py tests/test_k4_worlds_dir_receipt.py \
      tests/test_k45_acceptance.py tests/test_k5_promotion_write.py \
      tests/test_knowledge_query_v2.py tests/test_knowledge_freeze.py \
      tests/test_semantic_k3_freeze.py tests/test_knowledge_v2.py -q
145 passed in 90.57s        exit=0
```

## 7. 回归用例分布（35 例 = 3 + 27 + 5）

- ① 正例 3 条：闸真会翻（含 `world_reason` 逐项读数）、CLI `--preflight` 零退出、
  `--emit-scene-pack` → 用包同源。
- ② 包/闸负例 27 条：§4 的 N1–N27 全部有 CLI 逐字输出（N20 的回归版用另一个未登记
  id `WK-OTHER` 触发同一码 `pack_book_id_mismatch`），另加上述 6 条。
- ③ live 与执行侧 5 条：无包仍拒且零客户端构造；过闸后才构造客户端并按包内
  世界/场景卡/逐臂预算合同执行（产物世界库里是**真实角色名**，不是「林穗」夹具）；
  `run_paired` 逐场预算取自包内合同（只把 `A:zhl-c1-s1` 改成 3，其余六条仍读 6——
  若实现退回 `Budget()` 默认值这条会一起变 6）；无包时离线路径逐字不变；
  逐臂上限域与 `contracts.Budget` 同源（六个越界值各抛一次）。
- 零真实调用可证：把 `GatewayClient` 换成「一构造就记账并炸」的替身，未过闸的用例断言
  `built == []`。

## 8. 已知限制（诚实写）

1. **A 臂策略的 `semantic_requirements` 仍来自合成夹具 s1 计划**（`pov="lin"`、
   `goal="支付一枚钱（s1）"`）。本件只保证「包与当前策略逐字一致、策略一改包即失效」，
   **没有**把策略改写成描述真实场景卡；`build_a_arm_policy` 属 `app/scene_runtime/**`
   语义面（只消费不改），改它属另一件任务。
2. **`query_knowledge` 是全库只读查询，不按 `book_id` 收窄**。「A 臂非空」取决于
   「世界已登记」+「全库至少一条 verified 策略」两条，与登记作品内容无关。
3. **预算合同的数值源是未批准草案**：源文件 `runtime_budget_draft` 自称
   `route_and_price_verified=false` / `monetary_cap_approved=false`。本包把
   6/3000/600 冻结进包并逐条过 `Budget` 域，但**价格与资金上限的批准状态并未确立**。
4. **`source.source_pack_file_checked=false`**：闸不重读源文件（离线、无 FS 依赖），
   只核对包内记录的源路径与源摘要。转录忠实性靠人工比对 + 本回归保证，不是机械校验。
   （源文件本轮已实测存在：5812 字节、`sha256=8263a6fb…9ec85e`，与包内记录一致。）
5. **canon 只在三场范围内闭合**：`deferred_canon` 三条（签署资格、独立见证人与公开复议
   时间、顾砚舟的告别记忆损失与泊界城坐标暴露）**未结算**；本包不代签、不让临时契生效、
   不宣称救援完成。
6. **幂等键相对源文件有改动**（`-offline-v1` → `-k4-v1`，避免离线/真跑撞键），复核源文件时需注意。
7. **正例的 live 段只有进程内证据**：CLI 的 `--live` 在 `LLM_MODE=mock` 下会构造
   `GatewayClient` 并被 `live_client_requires_real_mode` 拒（既有不变量，未动），
   在 `real` 下则真烧钱。故「过闸后按包执行」由回归用夹具网关证明（零真实调用）；
   真跑那一次仍待资金批准时由人执行。
8. **场景包 JSON 未入库**（`data/` 在 `.gitignore` 内，且不在本件白名单），出包走
   `--emit-scene-pack`，哈希随报告落账（复核人可任选 `self_sha256` 或 `artifact_sha256` 核对）。
9. 未改 `app/knowledge_query.py`（属 `lg-k3-query-unblock`）、未改 `app/scene_runtime/**`
   任何既有语义（只消费）。`app/` 与 `tools/` 无任何改动，`git status` 只含上表四个路径。
