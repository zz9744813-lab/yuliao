# DELIVERY — K2 v2 合并卡（S1/S2）端到端可核链

- 工作区：`F:/agi/_scratch/worktrees/lg-k2-v2-cards`（分支 `task/k2-v2-cards`，基线 `75ac752`）
- 新增文件：`scripts/k2_v2_build.py`（800 行）、`tests/test_k2_v2_build.py`（915 行，**30 例**）
- 本件：**未 commit / 未 merge / 未 push**；真库 `D:\language-genome-data\language_genome.db`
  本轮**一个字节都没写**（只在 `mode=ro` 下被读）；所有写操作只发生在副本库
  `G:/lg_tmp/k2v2/language_genome_copy.db`（52 GB 拷贝）。

---

## 0. 验收命令结果

```
$ LG_LOCK_DIR=<临时目录> F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_k2_v2_build.py -q
..............................                                           [100%]
EXIT=0
```

同一轮加 `-rA` 时取到的 `N passed`（本机 pytest 终端汇总行**时刷时不刷**，
见 §6.8；为免「汇总行没出现」被误读成失败，另用 junitxml 取同一轮的机读计数）：

```
30 passed in 27.91s
```

```
$ ... -m pytest tests/test_k2_v2_build.py -q --junitxml=<临时目录>/junit.xml ; echo EXIT=$?
EXIT=0
tests= 30 failures= 0 errors= 0 skipped= 0 time= 30.633
```

30 例名目：`test_copy_target_refuses_real_database_candidates`、
`test_cli_refuses_real_database_before_opening_any_connection`、
`test_cli_reports_missing_database_instead_of_creating_it`、
`test_cards_land_two_v2_cards_at_hypothesis_with_lineage`、
`test_cards_stage_is_idempotent_and_never_overwrites`、
`test_v1_legacy_cards_are_never_touched`、
`test_evidence_stage_delegates_to_single_gate_source`、
`test_dry_run_evidence_writes_nothing`、
`test_gate_failure_and_mixed_op_pairs_are_never_persisted`、
`test_op_label_drifts_and_illegal_ops_are_refused`、
`test_span_not_matching_registered_text_is_skipped_not_written`、
`test_attach_aligns_ai_side_and_is_idempotent`、
`test_attach_refuses_conflicting_ai_side_for_same_human_side`、
`test_attest_requires_all_six_gates_pass_and_proposed_status`、
`test_ladder_uses_promotion_verdict_single_source`、
`test_hypothesis_cannot_jump_straight_to_verified`、
`test_ladder_walks_one_level_at_a_time_and_derives_work_scope`、
`test_verified_without_two_seat_receipts_writes_nothing`、
`test_two_distinct_seats_admit_both_v2_cards`、
`test_one_non_pass_seat_revokes_admission_of_both_cards[ABSTAIN]`、
`test_one_non_pass_seat_revokes_admission_of_both_cards[BLOCK]`、
`test_one_seat_only_never_admits`、
`test_same_model_twice_is_refused_as_not_independent`、
`test_scope_ids_and_status_readout_is_verbatim`、
`test_versions_two_readout_excludes_hypothesis_cards`、
`test_instance_readout_reports_gates_and_both_sides`、
`test_cli_requires_reviewer_for_ladder_stages`、
`test_cli_requires_injected_ai_side_for_evidence_stages`、
`test_cli_cards_stage_round_trip_on_a_copy_database`、
`test_script_never_touches_the_real_database_path_in_source`

任务单点名的 6 项 ⇒ 全部有用例钉死：混合 op 拒收
（`test_gate_failure_and_mixed_op_pairs_are_never_persisted`）、门0 不过不落库（同例 +
`test_op_label_drifts_and_illegal_ops_are_refused` + `test_span_not_matching_registered_text_is_skipped_not_written`）、
`hypothesis` 不许直写 verified（`test_hypothesis_cannot_jump_straight_to_verified`）、
缺两席收据不许写链接（`test_verified_without_two_seat_receipts_writes_nothing` +
`test_one_seat_only_never_admits` + `test_same_model_twice_is_refused_as_not_independent`）、
副本库路径校验（`test_copy_target_refuses_real_database_candidates` +
`test_cli_refuses_real_database_before_opening_any_connection` +
`test_cli_reports_missing_database_instead_of_creating_it`）、
幂等（`test_cards_stage_is_idempotent_and_never_overwrites` +
`test_dry_run_evidence_writes_nothing` + `test_attach_aligns_ai_side_and_is_idempotent` +
`test_attest_requires_all_six_gates_pass_and_proposed_status`）。

---

## 1. 判据单源（本件不另写一套）

| 环节 | 唯一判据来源 | 本件动作 |
|---|---|---|
| 成对证据门0–门5 | `scripts/k2_contrast_extract.py` 的 `run_contrast` / `GATES` | 只转调 |
| 晋升阶梯判词 | `scripts/k5_promotion_write.py` 的 `evaluate` / `commit_promotion` | 只转调 |
| 两席真收据 | `scripts/k2_receipt_mint.py` + `app.semantic_review_runner.review_snapshot` | 只驱动 |
| 准读数 | `app.semantic_admission.approved_selected`（K4 消费的就是它） | 只调用 |

测试用探针钉死「真的走它」：`test_evidence_stage_delegates_to_single_gate_source` 把
`CX.run_contrast` 换成 spy 断言被调；`test_ladder_uses_promotion_verdict_single_source`
把 `KP.evaluate` 换成 spy 断言被调。**语义审查判词没有第二个产出点**，本件不自填。

---

## 2. 端到端真跑（副本库，四段全绿）

### 2.1 落卡（`--stage cards`）

两张 v2 卡（`status=hypothesis` 起 → 后经阶梯升到 `verified`，见 §2.5）：

| 卡 | id | `legacy_strategy_id`（血缘） | 归并集合（记在 `source`） |
|---|---|---|---|
| S1 `v2:留白摊开` | `ESV2-170daae6c0f6` | `ES-252069e72b43` | `ES-252069e72b43, ES-d2d3e56bc0c3` |
| S2 `v2:节拍注水` | `ESV2-e066d1da0f1a` | `ES-7e3d5b2add03` | `ES-7e3d5b2add03, ES-ec5ad8233e4d, ES-67f8a86edf86, ES-17f795e4341b, ES-785358a815e5` |

- `abstract_operation` / `invariants` / `failure_modes` 逐字取自 `docs/策略卡合并方案_20260923.md` §1；
- **`effect_hypothesis` 未编造**，如实写：
  `未定——v2 合并卡未做效果实验；合并方案 §1 只给「语义意图」（定义内容），不构成效果假设`，
  `effect_status='untested'`；
- 卡 id 由 `strategy_key|version` 派生 ⇒ 幂等的基础（`test_cards_stage_is_idempotent_and_never_overwrites`
  钉死：重跑 `created=[]`、字段漂移即 `card_field_drift` 报错而**不覆盖**）。
- **8 张 v1 legacy 卡一列未动**（副本库实测，`test_v1_legacy_cards_are_never_touched` 钉死）：
  ```
  ESV2-0450910b989f verified WORK ["WK-6c5ea9081547","WK-6e5d2623"] replicated
  ESV2-19bfec4ffc65 verified WORK ["WK-3631b4b3dd44","WK-6e5d2623","WK-8e8e0459284d"] replicated
  ESV2-249d13b06e93 verified WORK ["WK-3631b4b3dd44","WK-6e5d2623"] replicated
  ESV2-564ec4504e80 verified WORK ["WK-3631b4b3dd44","WK-6e5d2623"] replicated
  ESV2-7cee0b354c34 verified WORK ["WK-3631b4b3dd44","WK-6e5d2623","WK-8e8e0459284d"] replicated
  ESV2-a623fa8ad37b verified WORK ["WK-3631b4b3dd44","WK-6e5d2623","WK-8e8e0459284d"] replicated
  ESV2-d014bf02a023 verified WORK ["WK-6e5d2623","WK-8e8e0459284d"] replicated
  ESV2-d5beaf06f0bc verified WORK ["WK-3631b4b3dd44","WK-6e5d2623","WK-8e8e0459284d"] replicated
  ```

### 2.2 成对对照证据（`--stage evidence --live`）

AI 侧**全部由调用方注入**（`--ai-side G:/lg_tmp/k2v2/pairs.json`），本件离线、不联网生成。
8 对里 6 对过门、2 对被门拒；**人类侧逐条来自已登记 `human_fiction` 作品**
（副本库 `work_sources` JOIN 复核，8/8 命中）：

```
WK-6e5d2623 SEG-aafa203c add_psych_narration  human_fiction corpus-v1
WK-6e5d2623 SEG-1d9257c1 add_psych_narration  human_fiction corpus-v1
WK-6c5ea9081547 SEG-48ea4375a631 add_psych_narration human_fiction corpus-v1
WK-6e5d2623 SEG-fcaec970 split_beats         human_fiction corpus-v1
WK-6c5ea9081547 SEG-6c7135eea873 split_beats human_fiction corpus-v1
WK-8e8e0459284d SEG-c1d3cc272f70 dilute_modifiers human_fiction corpus-v1
WK-6e5d2623 SEG-1a287e93 add_psych_narration  human_fiction corpus-v1   （被门拒）
WK-6e5d2623 SEG-542707a1 add_psych_narration  human_fiction corpus-v1   （被门拒）
```

**门0–门3（+门4/门5）通过/拒绝计数**（`--stage evidence` 不带 `--live` 的零写复读，
与落库那轮同参数同结果）：

```json
{"n_pairs": 8, "passed": 6, "rejected": 2,
 "by_op": {"add_interpretation": {"passed": 0, "rejected": 0},
           "add_psych_narration": {"passed": 3, "rejected": 2},
           "split_beats": {"passed": 2, "rejected": 0},
           "dilute_modifiers": {"passed": 1, "rejected": 0}},
 "reject_reason_classes": {"混合操作": 1, "anti-copy 照抄+贴标签": 1,
                           "cross-strategy 互斥破形": 1,
                           "add_psych_narration 构造不符": 1,
                           "场景指称交集为空": 1, "长度比超界": 1},
 "written": 0, "skipped": {}}
```

**两条拒绝理由原文**（`G:/lg_tmp/k2v2/k2_pairs.jsonl` 旁路账本逐字）：

```
REJECT: 混合操作: add_psych_narration 对混入节拍形态（节拍标记数在 AI 侧上升）——拆成两对，每对一个 op
REJECT: anti-copy 照抄+贴标签: ai 侧剔标归一后以 human 全文为前缀（原文 23 字，归一命中片段 20 字，归一口径不设长度容错）——逐字照抄只贴标签，不构成对照
REJECT: cross-strategy 互斥破形: S1 对的 ai 侧同时命中对方 S2 词表 ['慢慢', '然后']（本方命中 ['心里明白']）——同一对不许以两条策略双卡双计
```
```
REJECT: add_psych_narration 构造不符: AI 侧句数未增（1≤1），不构成新增陈述
REJECT: 场景指称交集为空: human=['林玄言'] ai=[]（两侧疑似不是同一场景）
REJECT: 长度比超界: 0.32 ∉ [1.2, 6.0]
```

**6 条已落库实例逐条读数**（op / 门 / span 逐字）：

```
SI-016df8b34758 ESV2-170daae6c0f6 WK-6e5d2623  SEG-1d9257c1 add_psych_narration S1 scene_keys=["叶临渊"] span=[0,41] 六门全 pass
SI-0747abe95d72 ESV2-170daae6c0f6 WK-6e5d2623  SEG-aafa203c add_psych_narration S1 scene_keys=["林玄言"] span=[0,20] 六门全 pass
SI-97edabf6526d ESV2-170daae6c0f6 WK-6c5ea9081547 SEG-48ea4375a631 add_psych_narration S1 scene_keys=["韩立"] span=[0,20] 六门全 pass
SI-5fc25a0259bb ESV2-e066d1da0f1a WK-6c5ea9081547 SEG-6c7135eea873 split_beats S2 scene_keys=["韩立"] span=[0,43] 六门全 pass
SI-797249ecc59b ESV2-e066d1da0f1a WK-6e5d2623  SEG-fcaec970 split_beats S2 scene_keys=["萧忘"] span=[0,27] 六门全 pass
SI-bc48a516a3ad ESV2-e066d1da0f1a WK-8e8e0459284d SEG-c1d3cc272f70 dilute_modifiers S2 scene_keys=["千仞雪"] span=[0,26] 六门全 pass
```

（`status=verified`、`reviewer_version=k2def-v1` 由 `--stage attest` 写入，资格是**可复算的
机械事实**：协议标记 + 六门全 pass + 现 `status=proposed`，缺一即拒，
`test_attest_requires_all_six_gates_pass_and_proposed_status` 钉死。）

### 2.3 `--stage attach`：把对照的另一极并进冻结证据（现场实测必要，非可选）

**这是本轮一个真实的、非理论的问题。** `k2_contrast_extract._persist_one` 只把 AI 侧记成
`ai_side_sha256`，**原文不入库**；而 `build_snapshot` 冻结给两席的 `review_input` 逐字取
`conditions_observed` ⇒ 两席只看到**人类侧一句原文**，对照的另一极从未出现在证据里。

首轮真收据逐字（副本库，`SS-9be272f5d2cd4a0da30dbd9f192f7f84`，S1 卡）：

- `kimi-k3` → **PASS**
- `deepseek-v4.1-flash` → **ABSTAIN**，理由逐字：
  > 三条 admitted 实例（SI-016df8b34758、SI-0747abe95d72、SI-97edabf6526d）均标记为
  > verified、src_ok、corpus-v1、human_fiction，且门控全 pass，策略键一致为 v2:留白摊开，
  > op 均为 add_psych_narration。**但冻结证据只给出各实例的 evidence_text 与
  > observed_content 标签，未提供人类侧对照文本或 AI 侧新增句与人类侧原文的差异比对，
  > 无法独立核实「AI 侧存在人类侧无对应来源的语义增量」这一核心语义意图是否成立**；
  > effect_ref 全为 null，效果假设自述未定。……

这**不是判定席过严，而是证据缺了对照的一极**：本协议的立论就是「同一场景人类留白 vs
AI 摊开」，只给人类侧时该主张在结构上不可核验。`--stage attach` 把**同一批注入对**的
AI 侧原文按 `human_sha256` 对齐并入 `conditions_observed`（对不上/冲突即拒），判据一字
不改、只补**已判过门**的对照另一极。本轮 attach 读数：

```json
{"n_pairs": 8, "n_attached": 6, "unchanged": [],
 "refused": [{"pair": "k2pair-b950442f12339fce", "why": "无对应已落实例（该对未落库或已不在本卡上）"},
             {"pair": "k2pair-ec40bf0df1cc0164", "why": "无对应已落实例（该对未落库或已不在本卡上）"}]}
```

（2 条 refused 正是 §2.2 被门拒的那两对——**门拒过的对不会被 attach 捞回来**。）

### 2.4 晋升链（`hypothesis → replicated → verified`）

一级一事务，每级一条 `promotion_audits` 行，`status` 列只在 `verified` 翻（判定≠升格）：

```
PAUD-5ca7ff8f7b5d94b8e88fdc0c  ESV2-170daae6c0f6 hypothesis→observed  reviewer=Hermes k2-v2-cards  evidence_count=3
PAUD-5b50fb8f09de6590a5a46480  ESV2-170daae6c0f6 observed→replicated reviewer=Hermes k2-v2-cards  evidence_count=3
PAUD-e03c272167ec5c0a396d5339  ESV2-e066d1da0f1a hypothesis→observed  reviewer=Hermes k2-v2-cards  evidence_count=3
PAUD-c76d15906fcc8a7a6abd5027  ESV2-e066d1da0f1a observed→replicated reviewer=Hermes k2-v2-cards  evidence_count=3
PAUD-68db7c263edec01a88aa3577  ESV2-170daae6c0f6 replicated→verified reviewer=Hermes k2-v2-cards  ← 本轮
PAUD-01415e354d8a744789214208  ESV2-e066d1da0f1a replicated→verified reviewer=Hermes k2-v2-cards  ← 本轮
```

`verified` 一步的 `evaluate` 判词原文：

```
PROMOTE: instance=ESV2-170daae6c0f6, from=replicated, to=verified,
status_column=hypothesis->verified, observation=replicated->replicated, scope: WORK -> WORK
[rule=scope-derive-1], evidence_ids=['SI-016df8b34758', 'SI-0747abe95d72', 'SI-97edabf6526d'],
gate_version=k5_promotion_write/v1, reviewer=Hermes k2-v2-cards | evidence_count=3 src_ok=3
reviewed=3 roots=['WK-6c5ea9081547', 'WK-6e5d2623'] stripped=— | blocked_at=gate1_status
```

**禁跳级**：`hypothesis → verified` 直跳被 `evaluate` 自己判 `skip_ladder`（用例
`test_hypothesis_cannot_jump_straight_to_verified`），本件不绕。**缺收据不许写链接**：
`--stage verified` 在没有两席收据时返回
`semantic_review_unverifiable:snapshot_missing` / `two_pass_votes_missing`，
卡行 / 审计行 / `semantic_approval_links` 三处均不变。

### 2.5 两席异模型真收据 + `approved_selected` **admit 成功**（任务单第 1 项证据）

两席：`litellm/deepseek-v4.1-flash` + `litellm/kimi-k3`，上游 `http://127.0.0.1:4000/v1`
（网关**本来就是跑着的**，本件不新起常驻服务；每席的证明头网关是 `k2_receipt_mint` 在
进程内 `port=0` 起的临时实例，`finally` 里 `stop_gateways` 关掉）。
密钥只在进程环境变量里，不落盘、不入审计。

**两席判词分布（每卡每席 verdict + 理由原文）**——这是当前生效轮：

S1 `v2:留白摊开` / `ESV2-170daae6c0f6`，轮 `SS-5d1113bee9a34aecbf08780e4b211f31`
（`content_sha256=8b45b20fe03df22d35e2c06f26078b51c1a96ab49082b2d814c9eeb61269f54d`）：

- `litellm/deepseek-v4.1-flash` → **PASS**（`K2V-a782d786b5fb4975a366c999d0ba99ec`）
  > 三例均为 verified、corpus-v1、human_fiction 来源，符合 policy 的 source_type/version/status
  > 约束；op 均为 add_psych_narration（S1），与策略 v2:留白摊开 的抽象操作一致：人类侧仅保留
  > 动作/状态（如「开始为她拔除心魔」「被白光彻底淹没了」「未曾再多作念想」），AI 侧新增了
  > 人类侧无对应来源的心理旁白与解释句（「他心里明白，这一关若过不去……」「这道白光不是收束，
  > 是把退路一并封在里面」「那点念想一旦说出口，浮屿的旧账就又要被翻出来」），正落在
  > failure_modes 所列「补『他其实/是因为/心里明白』类解释」「添加原文没有的条件、让步、总结」
  > 上。三例门控均 pass，未发现复制、跨策略混用或场景错配。scope_claim 覆盖
  > WK-6c5ea9081547 与 WK-6e5d2623 两部作品，与 admitted_refs 的 roots 一致，证据足以支撑该
  > WORK 级范围。
- `litellm/kimi-k3` → **PASS**（`K2V-479b92631f674e5dba5b88bd19c86ade`）
  > 三个实例（SI-016df8b34758、SI-0747abe95d72、SI-97edabf6526d）逐一核对：状态均为 verified，
  > text_version 均为 corpus-v1，来源 source_type 均为 human_fiction，符合 policy 的
  > allowed_source_types 与 allowed_text_versions；evidence_text 与对应 segment 的 text_sha256
  > 一致，span 与 human_side_chars 吻合（41/20/20）。构造操作均为 add_psych_narration（S1），
  > AI 侧文本均新增人类侧无对应来源的心理陈述……与策略 ESV2-170daae6c0f6 的语义增量定义及
  > failure_modes 描述一致，六项门全 pass。……WK-6e5d2623 的 metadata_status 为 partial
  > （作者留空待补），但 policy 未将 metadata verified 列为准入条件，不构成阻断。

S2 `v2:节拍注水` / `ESV2-e066d1da0f1a`，轮 `SS-077f52e26262424583c40dfeacb580a4`
（`content_sha256=148e6b5bd1e13e1e90943b79138a48e7888235f8a9e8157981df10611cc39b02`）：

- `litellm/deepseek-v4.1-flash` → **PASS**（`K2V-280f4970dfde409cbfc8fc55c2e0a15a`）
  > 三例均为 verified、human_fiction、corpus-v1，来源与版本符合 policy；每例均给出人类侧与
  > AI 侧对照文本，AI 侧相对人类侧在语义不变前提下拆分为多节拍并增加修饰/氛围（如
  > SI-5fc25a0259bb 将「飞过山脉后出现雾海」拆为掠过山脊—压低高度—越过谷口—出现雾海；
  > SI-797249ecc59b 将「巅峰一击凌厉霸道」拆为凝锋—压下；SI-bc48a516a3ad 增加「像水一样
  > 洇开」等比喻），与 strategy_key v2:节拍注水 的抽象操作（拆拍、修饰密度上升、套路收束）
  > 一致，且各门均 pass。scope_claim 覆盖 3 部登记作品，与证据一致。
- `litellm/kimi-k3` → **PASS**（`K2V-229268f1b2494ca291824b688e56226b`）
  > 三个实例均为 verified 状态、text_version=corpus-v1、source_type=human_fiction，符合 policy
  > 的允许来源与版本要求；span 与 segment_text 长度一致（43/27/26 字符），evidence_text 与
  > segment_text 逐字相同，src_ok 均为 true；六项门全部 pass；AI 侧文本相对人类侧均呈现
  > 拆拍/稀释特征（65>43、38>27、57>26 字符），与策略 v2:节拍注水的语义意图一致；
  > scope_claim 声明 3 部作品，证据恰好覆盖同 3 部作品，WORK 级范围有依据。……

**`approved_selected` 判定原文 ⇒ 两张都 admit 成功**（`--stage readout`）：

```json
{"strategy_id": "ESV2-170daae6c0f6", "admitted": true,
 "manifest": [{"strategy_id": "ESV2-170daae6c0f6", "strategy_version": 2,
   "snapshot_id": "SS-5d1113bee9a34aecbf08780e4b211f31",
   "link_id": "SAP-1005dbbcc095153efe18db6c",
   "vote_a_id": "K2V-479b92631f674e5dba5b88bd19c86ade",
   "vote_b_id": "K2V-a782d786b5fb4975a366c999d0ba99ec",
   "verified_audit_id": "PAUD-68db7c263edec01a88aa3577",
   "kind": "pre_promotion",
   "content_sha256": "8b45b20fe03df22d35e2c06f26078b51c1a96ab49082b2d814c9eeb61269f54d"}]}
{"strategy_id": "ESV2-e066d1da0f1a", "admitted": true,
 "manifest": [{"strategy_id": "ESV2-e066d1da0f1a", "strategy_version": 2,
   "snapshot_id": "SS-077f52e26262424583c40dfeacb580a4",
   "link_id": "SAP-1feec35e177f3bd105cbe800",
   "vote_a_id": "K2V-229268f1b2494ca291824b688e56226b",
   "vote_b_id": "K2V-280f4970dfde409cbfc8fc55c2e0a15a",
   "verified_audit_id": "PAUD-01415e354d8a744789214208",
   "kind": "pre_promotion",
   "content_sha256": "148e6b5bd1e13e1e90943b79138a48e7888235f8a9e8157981df10611cc39b02"}]}
```

`semantic_approval_links` 副本库全表（4 行 = 原有 2 条 v1 + 本轮新增 2 条 v2）：

```
SAP-589dd6a5eca79ce77ab28f52  ESV2-19bfec4ffc65 v1  SS-d75d5aadb18a4291860165c99580936b  posthoc_release
SAP-87cbb4b0247116f2849128ba  ESV2-d5beaf06f0bc v1  SS-c4ac5cfa6c58447e8abbec3b529a631a  posthoc_release
SAP-1005dbbcc095153efe18db6c  ESV2-170daae6c0f6 v2  SS-5d1113bee9a34aecbf08780e4b211f31  pre_promotion  ← 本轮
SAP-1feec35e177f3bd105cbe800  ESV2-e066d1da0f1a v2  SS-077f52e26262424583c40dfeacb580a4  pre_promotion  ← 本轮
```

### 2.6 反向验证：把一席判词换成非 PASS ⇒ `approved_selected` **拒**（任务单第 4 项证据）

**做法：零库写、只读。** 同一个副本库里 8 张 v1 卡的**当前轮**本来就各带一席非 PASS
（这正是 K4 起不来的那个阻断），对它们跑**同一个** `approved_selected`：

```
CANDIDATE ESV2-0450910b989f v1 status verified WORK round SS-9d7a1e92a9ce4b39925c6e6963ae9f8d
   votes [('deepseek-v4.1-flash','ABSTAIN'), ('kimi-k3','PASS')]
   admitted= False   error= ApprovalError:non_pass_vote:ABSTAIN
CANDIDATE ESV2-249d13b06e93 v1 status verified WORK round SS-2fbd51dbcff84a9d86d7da9b4136adc4
   votes [('kimi-k3','BLOCK'), ('deepseek-v4.1-flash','PASS')]
   admitted= False   error= ApprovalError:non_pass_vote:BLOCK
CANDIDATE ESV2-564ec4504e80 v1 status verified WORK round SS-118838842f7148aabb40c46138476667
   votes [('kimi-k3','BLOCK'), ('deepseek-v4.1-flash','ABSTAIN')]
   admitted= False   error= ApprovalError:non_pass_vote:BLOCK
CANDIDATE ESV2-7cee0b354c34 v1 status verified WORK round SS-d7090db19a4e40ba92da1ec9b873827a
   votes [('deepseek-v4.1-flash','ABSTAIN'), ('kimi-k3','PASS')]
   admitted= False   error= ApprovalError:non_pass_vote:ABSTAIN
CANDIDATE ESV2-a623fa8ad37b v1 status verified WORK round SS-3a6445a8d4514722960df22e67c39a26
   votes [('deepseek-v4.1-flash','ABSTAIN'), ('kimi-k3','BLOCK')]
   admitted= False   error= ApprovalError:non_pass_vote:ABSTAIN
CANDIDATE ESV2-d014bf02a023 v1 status verified WORK round SS-8e57dcfb7fcf4cd1bc6d92e51a1abcfa
   votes [('deepseek-v4.1-flash','ABSTAIN'), ('kimi-k3','PASS')]
   admitted= False   error= ApprovalError:non_pass_vote:ABSTAIN
```

**ABSTAIN 与 BLOCK 两种非 PASS 都被拒**，`卡 status=verified` 也一样拒——判红的是票不是卡。
回归里还钉死了「已准入之后再来一轮非 PASS 会把准入撤掉」
（`test_one_non_pass_seat_revokes_admission_of_both_cards[ABSTAIN|BLOCK]`：
最新一轮压过旧的 PASS 轮，`ApprovalError` 原文含 `non_pass_vote:<verdict>`）。

**没有用任何一条被禁的手段**：没换宽松判定席（两席固定 `deepseek-v4.1-flash` +
`kimi-k3`）、没缩 `candidate_cap`、没自填判词（`semantic_review_votes` 全部由
`review_snapshot` 真实上游调用产出，`K2C-*` call 收据逐条可查）。

---

## 3. ★ 值班 agent 补充要求的两项读数（K4 那一环）

### 3.1 两张 v2 卡的 `scope_ids` 原文

```json
{"strategy_id": "ESV2-170daae6c0f6", "strategy_key": "v2:留白摊开", "label": "S1", "version": 2,
 "status": "verified", "observation_status": "replicated", "effect_status": "untested",
 "scope": "WORK",
 "scope_ids": ["WK-6c5ea9081547", "WK-6e5d2623"],
 "scope_basis": "scope-derive-1: 证据覆盖 2 部登记作品",
 "legacy_strategy_id": "ES-252069e72b43"}
{"strategy_id": "ESV2-e066d1da0f1a", "strategy_key": "v2:节拍注水", "label": "S2", "version": 2,
 "status": "verified", "observation_status": "replicated", "effect_status": "untested",
 "scope": "WORK",
 "scope_ids": ["WK-6c5ea9081547", "WK-6e5d2623", "WK-8e8e0459284d"],
 "scope_basis": "scope-derive-1: 证据覆盖 3 部登记作品",
 "legacy_strategy_id": "ES-7e3d5b2add03"}
```

### 3.2 A 臂候选收窄读数（**非空**，`k3_status=matched`）

对 `WK-6e5d2623`《琼明神女录》用 `versions={"2"}` 收窄（A 臂 policy 字段照抄
`k4_paired_scenes.build_a_arm_policy`，`limits.context_items=3`）：

```json
{"stage": "readout", "book_id": "WK-6e5d2623", "versions": ["2"],
 "k3_status": "matched",
 "selected_ids": ["ESV2-170daae6c0f6", "ESV2-e066d1da0f1a"],
 "selected": [
   {"strategy_id": "ESV2-170daae6c0f6", "strategy_key": "v2:留白摊开", "version": 2,
    "scope": "WORK", "observation_status": "replicated", "evidence_count": 3,
    "evidence_root_works": ["WK-6c5ea9081547", "WK-6e5d2623"]},
   {"strategy_id": "ESV2-e066d1da0f1a", "strategy_key": "v2:节拍注水", "version": 2,
    "scope": "WORK", "observation_status": "replicated", "evidence_count": 3,
    "evidence_root_works": ["WK-6c5ea9081547", "WK-6e5d2623", "WK-8e8e0459284d"]}],
 "scope_ids": {"ESV2-170daae6c0f6": ["WK-6c5ea9081547", "WK-6e5d2623"],
               "ESV2-e066d1da0f1a": ["WK-6c5ea9081547", "WK-6e5d2623", "WK-8e8e0459284d"]},
 "rejected": [],
 "budget": {"considered": 2, "passed": 2, "selected": 2, "context": 2, "chars": 344},
 "package_sha256": "6db11ca2eb18bb3d3f040275b65e5d6835e285a220f463dfa4dfe5e08b4297b0"}
```

**结论：两张卡的 scope 都覆盖 `WK-6e5d2623`，K4 试点世界不需要改选。**
`WK-8e8e0459284d`、`WK-6c5ea9081547` 也都在 S2 的 scope 里。
不需要动 v1 卡的 scope 来凑。

---

## 4. 复现命令（供值班 agent 对真库执行）

> ⚠️ 下面第 1 条**必须先把真库拷成副本**再对副本跑；`k2_v2_build.py` 的
> `assert_copy_target` 会在打开任何连接之前拒掉真库路径（`REAL_DB_CANDIDATES` 硬编码
> `D:/language-genome-data/language_genome.db`，另加本仓 `data/language_genome.db`）。

```powershell
# 0) 副本库（禁止写 C 盘；本轮实测落 G:）
$PY   = "F:/Hermes/hermes-agent/venv/Scripts/python.exe"
$COPY = "G:/lg_tmp/k2v2/language_genome_copy.db"
$PAIR = "G:/lg_tmp/k2v2/pairs.json"          # 8 对，AI 侧由调用方注入
$REV  = "Hermes k2-v2-cards"

# 1) 验收（本轮 EXIT=0 / 30 passed）
$env:LG_LOCK_DIR = "$env:TEMP/lg_lock"
& $PY -m pytest tests/test_k2_v2_build.py -q

# 2) 落两张 v2 卡（幂等）
& $PY scripts/k2_v2_build.py --db $COPY --stage cards

# 3) 成对证据（先干跑看门计数，再真落）
& $PY scripts/k2_v2_build.py --db $COPY --stage evidence --ai-side $PAIR
& $PY scripts/k2_v2_build.py --db $COPY --stage evidence --ai-side $PAIR --live `
      --pairs-ledger G:/lg_tmp/k2v2/k2_pairs.jsonl

# 4) 把对照另一极（AI 侧原文）并进冻结证据——不做这步，deepseek 会如实 ABSTAIN
& $PY scripts/k2_v2_build.py --db $COPY --stage attach --ai-side $PAIR

# 5) 证据实例 proposed→verified（机械、可复算）
& $PY scripts/k2_v2_build.py --db $COPY --stage attest

# 6) 阶梯 hypothesis→observed→replicated（每级一事务）
& $PY scripts/k2_v2_build.py --db $COPY --stage ladder --reviewer $REV

# 7) 两席异模型真收据（真调 http://127.0.0.1:4000/v1；密钥只在进程环境变量）
$k = (Get-Content F:\Hermes\secrets\litellm_master_key.txt).Trim()
$env:LG_ATTEST_UPSTREAM_BASE_URL   = "http://127.0.0.1:4000/v1"; $env:LG_ATTEST_UPSTREAM_API_KEY = $k
$env:LG_ATTEST_ROUTE_PROVIDER      = "litellm"; $env:LG_ATTEST_ROUTE_MODEL     = "deepseek-v4.1-flash"
$env:LG_ATTEST_ROUTE_CHANNEL_ID    = "lg-k2v2-seat1"
$env:LG_ATTEST_AUDIT_PATH          = "G:/lg_tmp/k2v2/seat1_audit.jsonl"
$env:LG_ATTEST_2_UPSTREAM_BASE_URL = "http://127.0.0.1:4000/v1"; $env:LG_ATTEST_2_UPSTREAM_API_KEY = $k
$env:LG_ATTEST_2_ROUTE_PROVIDER    = "litellm"; $env:LG_ATTEST_2_ROUTE_MODEL   = "kimi-k3"
$env:LG_ATTEST_2_ROUTE_CHANNEL_ID  = "lg-k2v2-seat2"
$env:LG_ATTEST_2_AUDIT_PATH        = "G:/lg_tmp/k2v2/seat2_audit.jsonl"
& $PY scripts/k2_v2_build.py --db $COPY --stage receipts --seats 2

# 8) verified（缺两席收据则 evaluate 自己拒，库里什么都不写）
& $PY scripts/k2_v2_build.py --db $COPY --stage verified --reviewer $REV

# 9) 读数：卡状态 + approved_selected + K3 收窄候选
& $PY scripts/k2_v2_build.py --db $COPY --stage readout --book-id WK-6e5d2623 --versions 2
```

CLI 退出码：`0`=该阶段全过；`2`=拒绝语义（门拒 / 无收据 / 指向真库）；`1`=用法或运行错误。

### AI 侧配方（值班 agent 补充 2 的实测配方，本轮即按此产出 `pairs.json`）

- **不用** `controlled_corruptions`（669 行→167 对→只 6 对过门，3.6%，太薄）；
  **不用** `candidates`（0.51%，且属事后归类，独立审查 REVISE 明令禁止）。
- 按 op **现造**（op 由构造声明，正是协议要求）：
  - S1（`add_interpretation`/`add_psych_narration`）：**整段改写**，不复用原句；长度比 ≥1.2×；
    至少两句含解释词（其实/因为/意味着/说到底）或心理词（心里/心想/暗想/心头）；新增句要能锚定。
  - S2（`split_beats`/`dilute_modifiers`）：保留原句、命题不增，只加节拍词或修饰词；
    **human 段必须先筛掉自带这些词的**，否则门1「双向命中」必拒。
- 证据段必须来自 `work_sources` 里 `source_type='human_fiction'` 的**已登记作品**。
- 池子：`F:/Hermes/team/k2_v2/ai_side_registered_20260930.json`（已登记件）。
  `ai_side_20260930.json`（54 对 work_id 未登记）**本轮未用**。

---

## 5. 口径（判红/判绿怎么说）

| 读数 | 判据 | 本轮实测 |
|---|---|---|
| 卡落库 | `version=2`、`status` 起于 `hypothesis`、`legacy_strategy_id` 指向 v1 血缘、`effect_hypothesis` 写「未定」 | ✅ 2 张 |
| 门判据 | 六道门全 pass 才落 `strategy_instances`（`proposed`）；`op` 必填、混合 op 拒收 | ✅ 6/8 过门，2 条拒 |
| 实例升格 | 协议标记 + 六门全 pass + 现 `proposed`（机械可复算，非判断） | ✅ 6 条 → `verified` / `k2def-v1` |
| 阶梯 | 一级一事务；`hypothesis` 直跳 `verified` = `skip_ladder` | ✅ 6 条审计行 |
| verified | 同 snapshot **恰好 2 票、模型互异、每模型恰好 1 次 call、两票都 PASS** | ✅ 两卡各 2 票 |
| 准入 | `approved_selected` admit 成功并给 `link_id` | ✅ `SAP-1005db…` / `SAP-1feec…` |
| 判红 | 任一非 PASS（含 ABSTAIN）⇒ `ApprovalError:non_pass_vote:<verdict>` | ✅ 6 张 v1 卡实测被拒 |
| A 臂候选 | `versions={"2"}` 收窄后 `k3_status=matched` 且 `selected_ids` 非空 | ✅ 2 张 |
| 8 张 v1 卡 | status/scope/scope_ids/observation_status 逐字节不变 | ✅ 一列未动 |

---

## 6. 已知限制（诚实写）

1. **证据规模小**：每张 v2 卡只有 **3 条**已登记证据实例（S1 3 条 `add_psych_narration`、
   S2 2 条 `split_beats` + 1 条 `dilute_modifiers`），覆盖 3 部作品。6/8 的过门率
   （8 对里 2 对被门拒）说明构造配方还有一半的废品率。**这够撑起「两席能独立核验」，
   不够撑起任何统计性主张。**
2. **只覆盖 2 张合并卡**：`v2:留白摊开` / `v2:节拍注水` 两张。8 张 v1 legacy 卡一行未动，
   它们的阻断（`review_status=verified` 达不到）**没有因为本件而消失**——本件给的是
   合并方案定稿指明的替代路径，不是给 v1 卡开路。
3. **不宣称效果已验**：两张卡的 `effect_status` 恒为 `untested`，`effect_hypothesis` 写的是
   「未定」。两席的 PASS 判词只覆盖**语义意图与构造操作**层面（两席自己在判词里都写明了
   这点：deepseek S1 轮「策略自述 effect_hypothesis 为未定，故仅就语义意图与构造操作层面
   判定，不主张效果结论」）。**本轮没有任何效果实验、没有 A/B 对照、没有读者侧数据。**
4. **`attach` 是对上游一处设计缺陷的补齐，不是放宽**：上游
   `k2_contrast_extract._persist_one` 不把 AI 侧原文入库，导致两席看到的冻结证据缺了
   对照的一极。本件只把**同一批注入对、已判过门**的 AI 侧原文按 `human_sha256` 对齐补入
   `conditions_observed`；六道门在 attach 之前已判完，attach 不重判、不放宽、不碰
   `status`/`reviewer_version`，对不上或冲突即拒。**这一条建议值班 agent 回头在
   `k2_contrast_extract` 上游正式修掉**，本件的 `--stage attach` 只是过渡。
5. **本轮有两轮重投，如实交代**（不是判词造假，但必须写下来）：
   - S2 卡头两轮里 `kimi-k3` 的响应是 **HTTP 200 + 证明头齐备，但响应体不满足 review
     契约**（`_parse_response` 抛 `ReviewResponseError: k2_response_unverifiable`）⇒
     **没有产出任何判词**，`semantic_review_votes` 无该行。旁证：同输入直连上游复测得到
     的是结构完全合法的 PASS JSON。
   - 于是新开一轮重投。**三轮的 `content_sha256` 逐字相同**
     （`148e6b5bd1e13e1e90943b79138a48e7888235f8a9e8157981df10611cc39b02`），
     即**冻结证据一个字节没变**，且把 `max_output_tokens` 从 2048 提到 4096
     （**纯传输预算，判据/契约/席位/门一律未动**）后成功。S1 同样情形下新轮
     `content_sha256=8b45b20fe03df22d35e2c06f26078b51c1a96ab49082b2d814c9eeb61269f54d`
     与其前一轮相同。
   - 判红的是**响应格式**，不是判词内容；没有任何一次是把非 PASS 换成 PASS。
     但「开新轮」在机制上等价于允许重投（栅栏 `seat-<snapshot,model>` 是按轮计的），
     **这一点请值班 agent 独立判断是否接受**。
6. **`WK-6e5d2623` 的 `metadata_status=partial`**（作者/题材留空待补）。两席都自己点出了
   这件事并判「policy 未将 metadata verified 列为准入条件，不构成阻断」。这是**两席的
   判断，不是本件的判断**——若后续 policy 收紧，这两张卡的准入会跟着变。
7. **副产物位置**（不在本 worktree 内）：副本库与旁路账本
   `G:/lg_tmp/k2v2/{language_genome_copy.db, pairs.json, k2_pairs.jsonl, seat1_audit.jsonl, seat2_audit.jsonl, semantic_review_attempts/}`。
   进程内证明网关用 `port=0` 起、`finally` 关停，**无常驻服务**；上游
   `http://127.0.0.1:4000/v1` 本来就在跑，本件不新起、不改其配置。
8. **本机 `-q` 汇总行缺失**：`pytest -q` 单跑时本机终端没刷出 `N passed` 行（退出码 0、
   30 个通过点齐全）。`N passed` 取自同一套件加 `-rA` 的同一轮。别把这个当测试失败。
