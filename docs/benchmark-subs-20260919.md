# T5 子基准构建证据（2026-09-19）

| kind | 集合名 | set_id | 条目 | 验证 |
|---|---|---|---|---|
| naturalness_pair | nat-v1 | BS-9fb5d1ac1134 | 201 | 控制臂0混入、ansA=0.54、无空文本 |
| corruption_type | cct-ABSTRACT_SUMMARY-v1 | BS-76d63912130d | 15 | 类型0混入 |
| corruption_type | cct-ADJECTIVE_INFLATION-v1 | BS-6459a0e61173 | 12 | 类型0混入 |
| corruption_type | cct-ADVERB_INFLATION-v1 | BS-c6749db2bba5 | 5 | 类型0混入 |
| corruption_type | cct-DIALOGUE_EXPOSITION-v1 | BS-2de473a715d7 | 6 | 类型0混入 |
| corruption_type | cct-EMOTION_LABEL-v1 | BS-49b9c3f284b5 | 13 | 类型0混入 |
| corruption_type | cct-EXPLICITIZE-v1 | BS-d6a7c0910e50 | 13 | 类型0混入 |
| corruption_type | cct-LITERARY_OVERWRITE-v1 | BS-1fc136aa8db1 | 12 | 类型0混入 |
| corruption_type | cct-LOGIC_CONNECTOR_INFLATION-v1 | BS-7da079ad2ef6 | 13 | 类型0混入 |
| corruption_type | cct-MICRO_EXPRESSION_TEMPLATE-v1 | BS-8aea942e3ea6 | 8 | 类型0混入 |
| corruption_type | cct-NARRATOR_JUDGMENT-v1 | BS-973df0db7b4e | 14 | 类型0混入 |
| corruption_type | cct-NEUTRAL_PARAPHRASE-v1 | BS-ae59dfcae1e5 | 13 | 控制臂类型题（仅身份检测） |
| corruption_type | cct-OVER_EXPLAIN-v1 | BS-82a0508bf17f | 11 | 类型0混入 |
| corruption_type | cct-PARALLELISM_OVERUSE-v1 | BS-282a5f3c4ead | 12 | 类型0混入 |
| corruption_type | cct-POV_DRIFT-v1 | BS-56ce787bf36f | 14 | 类型0混入 |
| corruption_type | cct-PSYCHOLOGY_LABEL-v1 | BS-8324e0a02fe1 | 14 | 类型0混入 |
| corruption_type | cct-REDUNDANCY-v1 | BS-0f206e7a5e2c | 14 | 类型0混入 |
| corruption_type | cct-RHYTHM_FLATTEN-v1 | BS-d1a0f4eafb06 | 5 | 类型0混入 |
| corruption_type | cct-SEMANTIC_OVERCOMPLETION-v1 | BS-71f5ce63dbd9 | 11 | 类型0混入 |
| corruption_type | cct-SUBTEXT_ERASE-v1 | BS-ebe57dc9a747 | 9 | 类型0混入 |

§14 其余子基准状态（**2026-09-22 更正**）：human_vs_ai **已建**（hvai-v1，545 题，BS-95651478c6dd——此前「待解锁：基准段缺自由重建候选」一行已过期）；Implicitness / Rhythm **构题器已就位**（见下方增补节）；Semantic Fidelity / Dialogue / Style 待解锁（需构题器+可验证答案键）；Pragmatics **卡点见增补节**；Human Preference Prediction / Reconstruction Quality（需基准段上的集霸裁定）。

## 2026-09-22 增补（监管指令：Implicitness / Rhythm 构题器离线预置）

| kind | 集合名（真库构建后） | 构题器状态 | 真库 dry-run |
|---|---|---|---|
| implicitness_pair | imp-v1（拍板 --live 后建） | ✅ 构题器+四类回归（闸门一致性/答案键构造性/冻结原文/拒覆盖）；答案键=构造（白名单 9 类「定义即显式化」类型，answer=人类原文侧，零 LLM 判读）；闸门与既有构建器同一套 _eligible_pairs | 126 题 / 28 段 / 9 类（离线构造实测 A/B=72/54；真库集合数 0=零库写实证） |
| rhythm_pair | rhy-v1（同上） | ✅ 同模式（白名单 2 类：RHYTHM_FLATTEN / PARALLELISM_OVERUSE，类型定义即「拉平节奏/句式变化」） | 18 题 / 14 段 / 2 类 |

真库构建命令（拍板后）：`BENCH_ALLOW_LIVE=1 <PY> scripts/benchmark_build.py --kind implicitness_pair --name imp-v1 --live`（rhythm 同理 `--kind rhythm_pair --name rhy-v1`）；默认离线**零库写**（`--out` 落 fixture JSON，目录已存在即拒）。

**Pragmatics 卡点（不可建：构造性答案键来源缺失）**——现有 18 类劣化类型的 variable 定义无一承诺「语用违规」（言外之意/会话含义/合作原则层面的破坏）；最接近的 DIALOGUE_EXPOSITION 定义是**表述方式迁移**（叙述→对话），不承诺语用不当——按 Implicitness 白名单同一条纪律，未承诺改本轴的类型不得进答案键。出路三条（均 集霸 拍板项，只记录不实现）：① 扩劣化类型表——新增 variable 明确定义语用违规的构造器（如同模式即接通）；② 人工构题（语用对错场景对，人工可复核）；③ 放宽「答案键不得来自 LLM 判读」纪律（现行硬纪律，不自行放宽）。

回归：tests/test_benchmark_subs.py 14 项（含 implicitness 4 + rhythm 2）+ 全量 pytest EXIT=0。
