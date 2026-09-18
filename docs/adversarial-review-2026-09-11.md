# 代码级对抗性审查 — 2026-09-11

对象：`language-genome/` 全模块（gateway / experiments / jobs / judges / leakage / review / report / corpus / api）。
动机：放量跑真实书前，先按"攻击者/审计者"视角过一遍——假设自己要拿这份校准报告做架构否决决策，哪些代码缺陷会让结论不可信。

每项：缺陷 → 危害 → 修复 → 回归测试。

## P0（会直接污染实验结论）

### P0-1 extract 解析失败被记为 ok
`stage_extract_frames` 旧代码仅在 `payload is not None` 时做 schema 校验；模型吐出非 JSON 时
`payload=None` 但 `status="ok"` 原样落库。下游 `stage_reconstruct` 按 `status != failed` 捞取，
会对 `payload=None` 的 frame 调 `build_reconstruct_user(None)`。
**修复**：parse 失败与 schema 失败并入同一条 repair 路径，repair 再败一律 `status="failed"`。
**测试**：`test_extract_parse_failure_marked_failed`。

### P0-2 空 content 最后一发以 ok 落库
`gateway._real_chat` 旧逻辑：`not content and (finish == "length" or attempt < max-1)` 才重试，
若最后一次 finish=stop 且空文本 → **空字符串候选以 status=ok 落库**，judge 会拿空串当真候选评。
**修复**：任何空 content 一律重试；最后一次仍空 → 记 `llm_calls.status=failed` 并抛 `LLMError`。
**测试**：`test_gateway_empty_content_final_raises_and_records_failed`（fake httpx 断言记录）。

### P0-3 匿名标号并发撞号
`stage_reconstruct` 旧代码：`anon_counter = count()` 在 stage 开始快照一次，各线程
`anon_counter + made`（made 是线程局部）→ N 个线程产出 N 个 X0001。
**修复**：`_anon_allocator()`：进程内锁 + DB 当前计数做种子，逐号分配。
**测试**：`test_anon_labels_unique_across_thread_pool`。

### P0-4 pump 跨实验吞任务
`jobs._claim_one` 旧实现按 `created_at` 全局领单，不限实验；handler 发现 experiment_id
不匹配直接 return，pump 却把该 job 标 `completed` —— A 实验的 run 会把 B 实验的 stage
静默标成"已完成"。
**修复**：`pump(..., experiment_id=exp_id)`，claim 带 JSON 过滤。
**测试**：`test_pump_only_takes_own_experiment_jobs`。

### P0-5 judge A/B 随机位不可复现
`stage_judges` 旧代码所有候选共享一个 `random.Random`，取数次序取决于线程调度 →
同一实验重跑 A/B 位置重洗，人肉复核无法对齐。
**修复**：逐候选播种 `random.Random(f"{seed}:{candidate.id}")`，线程调度无关、完全可复现。

### P0-6 review_queue 非幂等
重跑该 stage 会把相同 candidate 再次入队。**修复**：`(experiment_id, subject_id)` 已存在则跳过。
**测试**：`test_review_queue_idempotent`。

### P0-7 中断后的 running job 永久卡死（perpetual stall）
无 reaper：进程重启后 status=running 的 job 不会被任何 pump 再领。
**修复**：`run_experiment` 起始处把本实验的 running stage job 复位为 pending
（单进程单实验假设，实验室阶段成立，文档化）。
**测试**：`test_stale_running_job_recovered`。

### P0-8 failed 是假终态（放量跑时暴露，当场补录）
第一本真书跑完才发现：`extract/propositions/judges/residual_sem/adversarial` 五处的
"已做过"集合把 status=failed 也算进去——一次瞬时 timeout 就永久丢段/丢判定，
且失败集中在 M/L 粒度时直接扭曲粒度对比（首个真书报告 M 仅覆盖 37%）。
**修复**：所有幂等集合只认 status==ok（extract 在 stage 开头删除 failed 行后重建；
其余 stage 过滤 existing；adversarial 按 detail.status 过滤）。另把 HTTP_TIMEOUT_S
90→300（推理模型长思考被误杀）、extract max_tokens 2048→4096（M/L 的 JSON 被截断）。
**测试**：补跑验证（EXP-0911-B82D gap-fill）。

## P1（口径/统计缺陷）

- **P1-1 llm_calls 无实验维度**：报告用量按 `created_at >= exp.created_at` 切窗，背靠背/并行
  实验必然串账。→ `llm_calls.experiment_id` 新列（sqlite 只增列迁移），gateway 线程本地绑定。
- **P1-2 幂等键不含 prompt_version**：reconstruct/judges/propositions/adversarial 改版 prompt 后
  旧产物被静默复用。→ 四类产物的"已做"集合全部加入现行 prompt_version。
- **P1-3 novelty KeyError**：det 指标全空时 `stats[d]` 直接 KeyError（回归测试暴露）。
- **P1-4 add_work 未 flush**：autoflush=False 下同事务后续查询看不到 segments。
- **P1-5 corpus 编码**：utf-8+errors=ignore 对 GBK 网文静默产乱码；BOM 残留首段。
  → `utf-8-sig → gb18030 → ignore` 三级回退（`_read_text_loose`）。
- **P1-6 review.py O(n²)**：`next((c for c in cands ...))` 换成了按 candidate_id 白名单的一次性过滤查询。

## P2（不改代码，写进报告的口径警告）

- **judge 同源**：评委模型若与重建模型同家族则自评偏高。报告头部现列出双方模型并在
  同源时打 ⚠。本次真实跑 judge=kimi，重建=deepseek/muse，无同源。
- **候选短文本 per_k 指标噪声**：报告现计算候选中位长度，<150 字时第六节只作方向参考。
- **adversarial 还原是单上下文 K 连出**：同一次生成内的 K 个版本相关性高于独立采样，
  泄漏分可能低估。v2 改 K 次独立调用。
- **rare 层 IDF 以实验段集为背景**：语料变大后应换成全库背景；当前为实验内近似。
- **API 无鉴权 + import-file 读任意本地路径**：本机工具定位，绑 127.0.0.1；勿暴露到局域网。

## 增补（2026-09-13）

- **jieba 静默降级**：metrics_det/entity_leakage 的 `try import jieba` 在未安装时静默回退，
  entity 词表悄悄返回空集 → 归一化层全部空转、270 行无效分数落库才发现。
  教训：可选依赖降级必须在产物上留痕（detail.backend 标注），否则是隐形单点。
  已装 jieba 并重跑。
- **多版本同表**：segments 表 v1/v2 混存后，一切按 ordinal 的邻接查询必须带版本过滤
  （neighbors 串线上文 bug 的根因）。

## 结论

P0 全部修复并有回归测试（6 条新用例 + 全套 23 条通过）。
风险残余集中在 P2 口径项，已转化为报告正文警告而非代码假设。
