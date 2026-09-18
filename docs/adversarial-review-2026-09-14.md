# 对抗性审查记录 — 2026-09-14（额度窗口夜跑）

审查人：主会话（集霸纪律：交付前代码级对抗审查）。本轮为并行推进期间的**过程审查**，
覆盖今晚新增代码与产出；非全量复审。

## 审查对象
- `scripts/gen_cand_one.py`（今晚新增，4 进程并行候选生成驱动）
- `scripts/factorial_blind_multicorpus.py` 的 prepare/commit（今晚判 5 批 232 项实际使用）
- `scripts/corpus_matrix_candidates.py` / `corpus_matrix_full.py`（与并行驱动共存运行）
- 盲评判定流程（今晚 232 项判定 + commit + 解盲统计）

## 发现

### P1-1 顶层副作用脚本可被 import 误触发
`corpus_matrix_candidates.py` 与 `corpus_matrix_full.py` 均无 `if __name__ == "__main__"` 保护，
模块体直接跑生成循环。任何脚本 `import` 它们（如 `from corpus_matrix_candidates import MODELS`）
都会触发全量生成。今晚 `corpus_matrix_full.py` 自身就 import 了前者——幸而其执行点在
gap-fill 完成之后，且幂等键挡住了重复，属"侥幸正确"。
**状态（08:50 复核）**：`factorial_d.py`、`corpus_matrix_candidates.py` 已由并行会话补上 `__main__`
保护（记忆/代码双确认）；`corpus_matrix_full.py` 仍无 guard——它本身是直接运行的入口，
但 import 它仍会触发执行，建议后续也拆分常量。
**回归测试**：`import` 两个模块后断言 `candidates` 表无新增行（需 mock chat）。

### P1-2 拒答文本混入候选池污染胜率口径
muse/deepseek 对胁迫/未成年情节返回"抱歉，我无法续写…"（今晚 BA19 HD 1 例、琼明 2 例）。
此类文本 status=ok（非空即 ok），会以正常候选身份进入盲评与胜率。
**建议**：入库前或评审层加 refusal 检测（前缀匹配"抱歉/无法续写/I cannot"），
单列 `refused` 状态；报告侧 refusal 不计入胜率分母。
**回归测试**：构造含拒答前缀的候选文本，断言被标记而非计 ok。

### P1-3 判定数组人工转录错位（已发生两次，被拦截一次）
手抄 116 元素 verdict 数组两次出错（一次 117≠116 被 commit 长度校验拦截——校验有效；
一次长度对但元素错位，commit 成功后才被抽查发现）。
**已采取**：改为 item_id 显式字典 → 按批次取值，附带 comp 分组计数校验。
**教训入规**：判定文件**禁止按位置手抄**，必须按 item_id 映射构建，commit 前先跑分组计数对账。
**回归测试**：commit 对长度不匹配 raise（现有行为，补测固化）。

### P2-1 主进程旧快照可致同键候选重复生成
并行驱动的幂等键逐条重查，但主脚本 `gen_group` 的 `existing` 集在每组开始时快照一次；
若并行代理在快照后写入同键候选，主进程会重复调用 API 并落重复行。
下游 `_groups_by_model` 以 dict 覆盖去重 → 数据不脏，但 API 额度浪费且行数虚高。
**建议**：candidates 表加部分唯一索引（experiment_id,frame_id,model,prompt_version WHERE status='ok'），
或主脚本也改逐条重查。**实测（08:50）**：C812 上主进程旧快照与反向代理相撞，
dup 键组 14 组、多余行 14 行（status 均 ok，下游 dict 覆盖无害，但 API 额度浪费 14 次调用）。

### P2-2 anon_label 跨进程撞号（已预防，记录在案）
原方案 `BX{n:03d}` 按进程内计数，多进程必然同号，judge prompt 里会指称歧义。
今晚驱动已加 4 位 run_tag（`BX{tag}{n:03d}`）。遗留：主脚本仍是旧方案，与并行驱动并存期间
同实验内可能新旧混用；builder 不依赖 anon_label join，风险止于 judge 可读性。

### 通过项（证据）
- WAL 多进程写：4 驱动 + 主进程 + 会话查询并发 ~1.5h，0 条 lock 报错、0 条丢提交（逐条 commit 粒度）。
- 失败路径：chat 异常 → status=failed + error 截断落库，不吞帧；空文本 → failed 而非 ok。
- 盲评纪律：批次与 .map 分文件；今晚评委（会话）判完前未读任何 map/result/DB 对照信息；
  commit 校验 verdict 合法值与长度；结果合并按 item_id 幂等替换，不串批。
- llm_calls 记账与 candidates 落库同 try 块，无"烧了额度没落库"窗口（极端断电除外）。

## 回归测试
`tests/test_factorial_commit_guards.py`（今晚新增）：commit 长度不匹配/非法 verdict 值两项守卫。
