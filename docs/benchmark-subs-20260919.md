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

§14 其余子基准待解锁：human_vs_ai（基准段缺自由重建候选）、Semantic Fidelity/Implicitness/Pragmatics/Dialogue/Rhythm/Style（需构题器+可验证答案键）、Human Preference Prediction / Reconstruction Quality（需基准段上的集霸裁定）。
回归：tests/test_benchmark_subs.py 6 项 + 全量 pytest EXIT=0。
