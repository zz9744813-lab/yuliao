# K4 真实场景包闸（`real_scene_plan_unverified` 的真实场景卡通路）

日期：2026-09-30　范围：`scripts/k4_paired_scenes.py`、`tests/test_k4_real_scene_plan.py`
本件**零真实模型调用、不写真库**（验证用临时 SQLite 夹具 + `tmp_path`）。

## 1. 病灶

改动前 `preflight_world()` 的最后两行是写死常量：

```python
world_reason = "real_scene_plan_unverified:缺真实作品场景卡与逐臂预算合同"
ready = False
```

于是无论世界是否登记、A 臂是否非空，`ready` 恒 `False`；`--live` 入口据此在
`GatewayClient` 构造前直接拒绝（「当前 K4 世界和场景仍为合成夹具」）。**结论：
K4 三场正式验收在代码层不可能发生**——与 K2/K3 那两处「写死 FAIL」同类。
`--scene-bundle` / `--expected-pack-sha256` 两个参数已存在，但只走
`check_offline_scene_bundle` 的结构检查，没有任何通往 `ready=True` 的校验通路。

本件把 `real_scene_plan_unverified` 从**写死的拒绝**改成**可核验的判据**：真包过校验
且世界侧读数齐全才放行，缺任一项仍拒并给具体原因码。

## 2. 两种包的分派（`scene_bundle_kind`）

`--scene-bundle` 现在按顶层 `schema_version` 键**是否存在**分派，行为各自独立：

| 包 | 判据 | 行为 |
|---|---|---|
| legacy 离线候选包 | 无 `schema_version`，`status=offline_candidate_not_canon_approved` + `unresolved_canon` 非空 | **与改动前逐字不变**：仅 `--preflight` 结构检查、仍不放行闸、仍不许进 `--live` |
| 真实场景包 `k4-real-scene-pack/1` | `schema_version="k4-real-scene-pack/1"` | 逐场校验 → 接 `--preflight` / `--live` 同一道闸 |

为什么另立一种包而不改 legacy 包：legacy 包**自己声明**「不是正典、未决 canon 未清」
（源文件里 `status` 与三条 `unresolved_canon` 是它自己的字段），因此它永远只能当结构
参考。真实场景包必须能声明「这三场范围内 canon 已闭」并携带逐臂逐场预算合同，否则闸
无从判断——这也正是 K4 缺的东西。

`schema_version` **存在但取值不对**判为 real（让真实通路报 `pack_schema_invalid`），
而不是掉进 legacy 的 `bundle_schema_invalid`：错码会误导复核人。

## 3. 真实场景包结构

`build_real_scene_pack(book_id)` 生成（纯函数、离线、零库、零模型调用）：

| 键 | 含义 |
|---|---|
| `schema_version` | `k4-real-scene-pack/1`（唯一版本位） |
| `book_id` / `branch_id` | 绑定到**已登记**世界（`work_sources.work_id`） |
| `canon_status` | 固定 `scoped_pack_canon_closed`——**仅声明三场范围内** canon 已闭 |
| `canon_scope_note` / `deferred_canon` | 闭的范围逐字写明 + 未决项逐条搬入（不提前结算） |
| `source` | 标题、源文件路径、源文件 sha256、源自称的 production_pack_sha256、`source_pack_file_checked` |
| `world` | `contracts.World`（角色/事实/规则），rev0 |
| `plans` | `contracts.ScenePlan` × 3，顺序即场序 |
| `a_arm_policy` | `build_a_arm_policy(book_id)` **逐字**拷贝 |
| `budget_contract` | 逐臂逐场上限：`arms` + `entries[{arm, scene_id, limits}]`（3 场 × 2 臂 = 6 条） |
| `self_sha256` | 包自摘要（口径见 §5） |

`book_id` 与草案自带的 `origin_book_id`（`zhutian-hongyanlu`）不同时**显式改写**，并由
报告里的 `origin_book_id` + `book_id_remapped=true` 标出——复核人看得到改写事实，
不靠猜。

### 3.1 场景卡来源（转录，只读一次后固化在脚本里）

源：`F:/Hermes/team/K4_ZHUTIAN_OFFLINE_SCENES_20260928.json`
（5812 字节，`sha256=8263a6fb14a44d1a9e3918bb2be2ada8c5ba2a9dd92c8ec8d571edb60e9ec85e`，
自称 `production_pack_sha256=22317fda…c5376`），契约口径见
`docs/K4_世界目录收据_20260924.md`。

三场（`zhl-c1-s1/s2/s3`，POV 依次 `CHAR-GU` / `CHAR-NING` / `CHAR-GU`）：

1. 确认幸存者 → 只划定可核实救援范围（不替全城许诺归附）
2. 拒绝永久归附 → 提出十二时辰临时条款
3. 自列首位担责者 → 停在待签（明说受影响居民签署资格仍缺）

**转录与源文件的一处有意差异**：幂等键 `zhl-c1-sN-offline-v1` → `zhl-c1-sN-k4-v1`。
离线候选键与真跑键分开，避免离线产物与真跑产物在同一世界库撞键。
世界事实、规则、事件、before/after、长度带（450..1200）均逐字一致。

**逐臂预算合同取值**：`max_calls=6` / `max_output_tokens=3000` /
`max_elapsed_seconds=600` 取自源文件 `runtime_budget_draft`；`max_rewrites=2` /
`max_input_chars=24000` / `style_feedback=false` 取 `contracts.Budget` 默认值
（域唯一来源见 §4③）。两臂同上限：A 臂带知识包、B 臂空包对照，调用预算对齐才可比。

## 4. 校验链（`check_real_scene_pack`，零库零模型、只读文件）

按顺序，任何一步失败即 `ScenePackError(<稳定码>)`：

- **① 文件层**：`pack_unreadable` / `pack_size_invalid`（>1 MiB）/ `pack_json_invalid`
  （禁重复键、禁 NaN/Infinity、须 UTF-8）。
- **② schema**：`RealScenePack` 用 `extra="forbid" + strict`；world/plans 直接复用
  `contracts.World` / `contracts.ScenePlan`，**不另立一套结构**。失败 → `pack_schema_invalid`。
- **③ 自摘要**：剔除 `self_sha256` 键后按 `contracts.canonical`（键序无关、无空白、
  `ensure_ascii=False`）取 SHA-256，与包内声明不符 → `pack_self_sha256_mismatch`。
  这是「改内容不重算摘要」的直接检出点。
- **④ 钉值**：`--expected-pack-sha256` 格式非法 → `pack_expected_sha256_invalid`；
  与包内 `self_sha256` 不符 → `pack_expected_sha256_mismatch`（防换包冒充）。
- **⑤ 绑定**：`book_id` 不符 → `pack_book_id_mismatch`；场数与 `--scenes` 不符 →
  `pack_scene_count_mismatch:<包内>!=<CLI>`（不静默跑前两场）；场景 id / 幂等键重复 →
  `pack_scene_identity_duplicate`；plan 的 book/branch 与包不符 → `pack_scene_scope_mismatch`。
- **⑥ A 臂策略绑定**：`canonical(pack.a_arm_policy) != canonical(build_a_arm_policy(book_id))`
  → `pack_a_arm_policy_mismatch`。查询策略一改，旧包立即失效，不会出现
  「包按旧策略、闸按新策略」。
- **⑦ 预算合同**：`arms` 必须是 `[A,B]` 全体（缺 → `pack_budget_contract_incomplete:arms=`）；
  逐 (arm, scene) 覆盖必须齐全（缺 → `…:missing=`）；重复 → `…_duplicate:`；
  多出包外场次 → `…_extra:`。每条上限**逐条**过 `contracts.Budget`（域唯一来源：
  `max_calls` 2..20 / `max_rewrites` 0..2 / `max_output_tokens` 100..8000 /
  `max_input_chars` 100..100000 / `max_elapsed_seconds` 1..3600），
  越界 → `pack_budget_out_of_contract:<arm>:<scene>`。**不另立一套更宽的域。**
- **⑧ 逐场 `validate_plan` + 推进世界**（`_replay_scene_pack`）：结构、事件、
  可观察事实 before/after、修订顺序、POV 与角色可见性逐条机械校验，并把三场事件链
  离线推到底（rev0 → rev3），给出 `initial_world_sha256` / `final_world_sha256`
  两个可核对读数。失败码形如 `pack_plan_invalid:<scene_id>:<contracts 码>`
  （`world_revision_conflict` / `plan_precondition_conflict` /
  `planned_fact_outside_pov` / `unknown_or_immutable_fact` / `empty_state_change` /
  `fact_type_change_not_supported` / `unknown_pov` / `knowledge_scope_conflict`）。
  knowledge 只作 scope 载体（`techniques=[]`）——**包绑的是策略不是技巧**，技巧内容
  离线不可知（K3 查询在 freeze 时才发生）。

报告里的 `live_ready` **恒 False**：`pack_alone_never_authorizes_live`。包本身从不授权
真跑；放行只由 §5 的闸决定。

## 5. 闸判据（`preflight_world` 单一来源）

`--preflight` 与 `--live` 走**同一个** `preflight_world`（`--live` 经
`_preflight_with_pack`，不另写一套）。四读数**缺一即拒**：

| # | 读数 | 判据 | 缺失时原因码 |
|---|---|---|---|
| ① | `pack_ok` | 场景包通过 §4 全部校验（含 `--expected-pack-sha256` 一致） | `no_scene_pack`（未传）/ `scene_pack_rejected:<码>` |
| ② | `registered` | `work_sources` 有该 `book_id` 行 | `world_not_registered` |
| ③ | A 臂非空 | `k3_status=matched` 且 `selected_ids` 非空 | `empty_package` |
| ④ | `review_status` | 既有语义审查收据可核验（= `verified`） | `semantic_review_unverifiable` |

- ①②③④ 齐 → `ready=True`，`world_reason` 以 `real_scene_plan_pack_verified:` 开头并给
  逐项读数（`pack_sha256` / `artifact_sha256` / `scenes` / `world_sha256` 首→末 /
  `budget_contract` 条数与上限 / `a_arm_policy` 摘要 / `registered` / `k3_status` /
  `a_arm_techniques` / `review_status`）。
- 任一缺 → `ready=False`，`world_reason = "real_scene_plan_unverified:" + 分号连接的阻塞码`。
  ④ **沿用既有 K2 收据链判据，本件不放宽**——不为凑 `ready=true` 降低既有门。
- `pack_ok` 只认校验器给的 `pack_valid`，不看任何调用方自报（防「报告里写个 ready 就放行」）。

### 保留的旧行为

- 不传 `--scene-bundle` ⇒ `ready` 恒 False，`world_reason` 仍以 `real_scene_plan_unverified`
  开头，**合成夹具路径的拒绝行为逐字不变**；离线跑（无 `--preflight`/`--live`）产物键集、
  收据键集、场景 id（`s1/s2/s3`）与夹具正文均未变。
- `--live` 在 `LLM_MODE=real` 下复用同一个 `ready`；未过闸 ⇒ 零真实调用退出。
  未带真实场景包时保留原拒绝（「内置 build_world/build_plan 仍是合成夹具」）——这不是
  另立一套判据：`pack_report is None` 与闸里 `pack_ok=False` 是同一个事实，只是把它挡在
  `GatewayClient` 构造之前。

## 6. 执行侧（`run_paired` 接包）

`scene_plans` / `budget_contract` 缺省 ⇒ 逐字走合成夹具路径（`build_plan` + `Budget()`）。
给了 `scene_plans` ⇒ 每场直接用包内 `ScenePlan`（幂等键逐字保留，不加臂后缀——两臂各有
独立世界库不会撞键），逐场预算取 `budget_contract[(arm, scene_id)]`；与 CLI
`--budget-calls` 冲突时**抛错**（合同优先，不静默取其一）；`scene_plans` 场数与 `n_scenes`
不符时运行期再拒一道。live 收据逐条加 `scene_pack_sha256` / `scene_pack_arms`，
产物顶层加 `scene_pack`——这次真跑按哪份包跑的，消费侧不必猜。

`fit_fixture_text`：真实场景卡按小说口径给 450..1200，离线夹具正文（19 字）落在带外会触发
`text_length_outside_plan` 并耗尽修稿额度——那测的是夹具长度不是闸。补白用确定性重复句，
零随机、零真实调用。默认合成场景（`min_chars=1`）下这段**逐字不改**。

## 7. 用法

```bash
# ① 出包（纯离线、零库、零模型调用；已存在即拒，不覆盖）
python scripts/k4_paired_scenes.py --emit-scene-pack pack.json --book-id WK-A
#    打印 self_sha256（下面 <self_sha256> 就用它）

# ② 预检（read-only；ready=true ⇒ 退出码 0）
python scripts/k4_paired_scenes.py --preflight --book-id WK-A \
    --scene-bundle pack.json --expected-pack-sha256 <self_sha256>

# ③ 真跑（过闸后才构造 GatewayClient；未过闸零真实调用退出）
K4_ALLOW_LIVE=1 python scripts/k4_paired_scenes.py --live --book-id WK-A \
    --scene-bundle pack.json --expected-pack-sha256 <self_sha256> \
    --writer-model <w> --verifier-model <v>
```

两个摘要口径都打进报告，复核人可任选其一核对：
`self_sha256`＝剔除该键后的规范摘要（键序无关）；`artifact_sha256`＝文件字节 SHA-256。

## 8. 拒绝码全表

`pack_unreadable` / `pack_size_invalid` / `pack_json_invalid` / `pack_schema_invalid` /
`pack_self_sha256_mismatch` / `pack_expected_sha256_invalid` /
`pack_expected_sha256_mismatch` / `pack_book_id_mismatch` /
`pack_scene_count_mismatch:<n>!=<m>` / `pack_scene_identity_duplicate` /
`pack_scene_scope_mismatch` / `pack_a_arm_policy_mismatch` /
`pack_budget_contract_incomplete[:arms=|:missing=]` /
`pack_budget_contract_duplicate:` / `pack_budget_contract_extra:` /
`pack_budget_out_of_contract:<arm>:<scene>` / `pack_plan_invalid:<scene>:<contracts 码>` /
`pack_replay_invalid:<scene>`。全部 `ScenePackError`（`ValueError` 子类），**不携带剧情正文**。

## 9. 已知限制（诚实写）

1. **A 臂策略的 `semantic_requirements` 仍来自合成夹具 s1 计划**（`pov="lin"`、
   `goal="支付一枚钱（s1）"`）。本件只保证「包与当前策略逐字一致、策略一改包即失效」，
   **没有**把策略改写成描述真实场景卡。`build_a_arm_policy` 属 `app/scene_runtime/**`
   语义面（只消费不改），改它属另一件任务。
2. **`query_knowledge` 是全库只读查询，不按 `book_id` 收窄**。「A 臂非空」取决于
   「世界已登记」+「全库至少一条 verified 策略」两条，与登记作品的内容无关。
3. **预算合同的数值源是未批准草案**：源文件 `runtime_budget_draft` 自称
   `route_and_price_verified=false` / `monetary_cap_approved=false`。本包把
   6/3000/600 **冻结**进包并逐条过 `Budget` 域，但**价格与资金上限的批准状态并未确立**。
4. **`source.source_pack_file_checked=false`**：闸不重读源文件（离线、无 FS 依赖），
   只核对包内记录的源路径与源摘要。转录忠实性靠 §3.1 的人工比对与本回归保证，不是机械校验。
5. **canon 只在三场范围内闭合**。`deferred_canon` 三条（签署资格、独立见证人与公开复议
   时间、顾砚舟的告别记忆损失与泊界城坐标暴露）**未结算**，本包不代签、不让临时契生效、
   不宣称救援完成。
6. **幂等键相对源文件有改动**（`-offline-v1` → `-k4-v1`，见 §3.1），复核源文件时需注意。
7. **正例的 live 段只有进程内证据**：CLI 的 `--live` 在 `LLM_MODE=mock` 下会构造
   `GatewayClient` 并被 `live_client_requires_real_mode` 拒（既有不变量，未动），
   在 `real` 下则会真烧钱。故「过闸后按包执行」由回归用夹具网关
   （`FxClient` 顶替 `GatewayClient`）证明，**零真实调用**；真跑那一次仍待有资金批准时
   由人执行。
8. 场景包 JSON **未入库**（`data/` 在 `.gitignore` 内，且本件白名单不含该路径），
   出包走 `--emit-scene-pack`，哈希随报告落账。
