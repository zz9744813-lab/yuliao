# K5-A 离线前置评估：扩到 10 场的判据与成本模型（草案）

版本：v1/2（2026-09-22 定稿；A3/A5/A6 + 会审二轮 B1/B2/B4/B8 修正并入）· 零配额（未跑任何真实模型调用；K4 三场真跑仍
blocked，见文末）· 对应方案 §8 K5 行 + §9 K5-A 任务单。

本文件把「扩到 10 场 / 补源 / 停某策略 / 停扩张」从主观判断改成
**机械化核验**：判据在 K4 真跑产出后逐条打分，任一不过 → 不扩，报告停止
（方案 K5：「未证明收益不扩成全局规则」）。核验由接手 agent 按本表执行，
结果逐项落台账；判据本身改动须重新预注册（同 bal-v2 Freeze 纪律）。

## 1. 前提（Gate 前）：K4 真跑完成且六臂全 committed

| # | 前提 | 核验口径 |
|---|---|---|
| P0 | 402 资金墙已拍板拆墙（充值或授权换通道）**〔2026-09-23 失效标注：主控实测 deepseek 探针 200，402 墙不成立；P0 改判「通道健康」（中转对长文生成 120s 超时）——见 docs/K4_首轮真跑_证据_20260923.md；本表正式改版随新派工统一更新〕** | 拍板记录 + 单发探针通过 |
| P1 | K4 三场真跑 6/6 臂 committed（failures=0） | k4_paired.json 的 failures 数组为空 |
| P2 | A 臂包非空率 ≥2/3 场（知识查得到才谈收益） | packages[].n_techniques ≥1 的场数 |
| P3 | 双闸纪律未被绕过（K4_ALLOW_LIVE=1 显式、LG 只读隔离有效） | 跑分日志含环境哨兵记录 |

## 2. 扩 10 场判据（全部满足才扩；任一未达 → 停止扩张）

| # | 判据 | 阈值/口径 | 未达时的处置 |
|---|---|---|---|
| C1 | 方向一致差异：A 臂（v2 冻结包）对 B 臂（空包）出现**至少 1 处经人工复核确认方向**的硬差异 | 3 场内 ≥1 处（正文或核验工件），差异非预算/超时/通道噪声 | 报告停止——「读得出、偏好反」旧教训不重蹈 |
| C2 | 零失败臂 | failures=0（含 rollback_failed=0） | 修故障后重跑 K4，不扩 |
| C3 | A 臂非空包率 ≥2/3 | packages 记录核验 | <2/3 → 先补 K2-A 实例（查得空=知识不足，不是 Writer 问题） |
| C4 | 复核负担可承受 | 每场差异人工复核 ≤20 分钟（集霸口径） | 超限 → 缩差异清单再判 |
| C5 | 通道一致性 | 10 场用与 K4 相同的 writer/verifier 通道；若换通道须在收据标 channel_changed 并对 C1 复核一遍 | **未标 channel_changed 时，C1 与 C5 均判不过**（A6：写死，不再含糊） |

## 3. 停某策略的机械条件（在 K4/K5 真跑数据上核）

- 任一策略版本满足任一条 → 该策略 retired（不删除，状态机走 K1-B 契约）：
  ① **跨作品复现失败**：`strategy_stats.unique_source_intervals < 2`
  （独立源区间 = canonical 根作品聚合后的 (根作品, span) 区间数，
  镜像/重切段/重复抽取聚合后）；② **bad_when 命中率 > 50%**（对该策略
  的 condition 求值统计）；③ K4 配对中携带该策略的场 C1 未过。
- **样本量下限（A5：防小样误判 retired）**：①② 两条都只在
  `strategy_stats.valid >= 8`（verified 实例数 N≥8）时才可判 retired；
  N < 8 → 只标 `insufficient_sample`，不得 retired（2 实例 1 命中 = 50%
  正是小样误判的形状）。① 还要求 `strategy_stats.attempts >= 2`
  （至少 2 个独立根作品试过复现），否则同样只标 insufficient_sample。
- 「补源」触发条件：C3 过但 `strategy_stats.root_works < 2` → 按方案
  先最多 48 段来源合格试点口径补实例，不直接全库放量。
  （术语统一：独立源区间/独立根作品数分别取
  unique_source_intervals / root_works 两字段，不再混用。）

## 4. 成本模型（每场每臂 → 10 场上限）

- 实测基线（离线 FixtureClient 口径，调用次数结构）：每臂 2 次调用
  （writer 1 + verifier 1）；含改稿上限 3 轮，**结构性上限 8 调用/臂**；
  运行期预算闸 `Budget.max_calls=6`（契约默认值，runner 按
  `count >= budget.max_calls` 触顶）先于结构上限生效。
  **机械推导：最坏/臂 = min(结构 8, 预算 6) = 6**
  （A10 修复后核验工件缺陷走有上限返修，不再烧 Writer 额度）。
- 10 场 × 2 臂 = **正常 40 调用；最坏 10 × 2 × 6 = 120 调用**。
- Token 口径（A3 修正：口径=**每次调用**约 4~6 万 input token——
  单场全上下文每次都整段送入；此前把「每臂」当基数多乘了轮数，高 4~5 倍）：
  - 推导式（机械复核）：`总token = 调用数 × per_call_input`；
    `per_call_input ≈ 4~6 万`（校准史实测口径）
  - 正常路径：10 场 × 2 臂 × 2 调用 = 40 次 → **160 ~ 240 万 token**
  - 最坏路径：10 场 × 2 臂 × 6 调用 = 120 次
    → **480 ~ 720 万 token**（output ≤0.3 万/次，含 10% 余量 ≤800 万）
- **总量止损线：800 万 token**，高于合法最坏上限 720 万——合法路径
  不击穿，命中即成本模型失真信号 → 停扩张，报告实耗（此前 1,600 万
  由膨胀值推得，防护实际失效偏晚，A3 已废）。
- 超预算防护（自洽约束，机械可核验）：k4_paired_scenes 逐场跑，
  **运行口径 Budget.max_calls 固定 6，不许上调**——上调到 7 即
  最坏 140 次 × 6 万 = 840 万 > 800 万止损线，自洽破坏；契约字段
  允许域 le=20 只是合法取值域，不作预算口径。任一场超预算 →
  failures 记 budget（不静默）；总量超 **800 万** → 停扩张，报告实耗。
- 拆仓评估：**10 场判据过了才启动**（方案 K5：拆仓后置）。

## 5. blocked 注记（待集霸拍板）

K4 三场真跑与 10 场扩展双双 blocked 于 **deepseek 通道 HTTP 402 资金耗尽**
（2026-09-21 --run 60 实测：源校勘 59/59 ok 但抽帧 45 尝试全 402、0 新帧；
kimi 探针正常=402 仅 deepseek 单通道）。两个选项：

1. **deepseek 通道充值**——延续 extractor 溯源不变（改写/评委会历史口径连续）；
2. **授权换通道**（如 kimi-k3，探针可用）——换通道改变 Writer/Verifier
   模型基线，K4/10 场收据须标 channel_changed，C1 判据须复核。

拍板前不发起任何真实模型调用（现行纪律）；拍板后执行顺序：
K2-A 实例放量（逐次记账+输出门+证据门已就位）→ K4 三场真跑（一条
命令已写死于任务单《知识化调整方案_20260920》§9 末「待拍板清单」段）
→ 本表判据逐项核验 → 扩/停报告。

## 6. 判据核验命令（B 定稿：凡可机械核验的给命令与期望输出）

设 10 场真跑产物在 `out_k4_10/k4_paired.json`（P3 前提下生成；3 场真跑
同结构验于 `out_k4_3/k4_paired.json`）。本机无 jq——核验命令统一用
项目解释器（Git Bash 口径，先设
`PY=F:/kelaode/Data/Agents/zqibcc8w9/tools/Python311/python.exe`）：

| 判据 | 核验命令 | 期望输出 |
|---|---|---|
| P1 | `$PY -c "import json;print(len(json.load(open('out_k4_10/k4_paired.json',encoding='utf-8'))['artifacts']['failures']))"` | `0` |
| P2 | `$PY -c "import json;d=json.load(open('out_k4_10/k4_paired.json',encoding='utf-8'));print(sum(1 for p in d['artifacts']['packages'] if p.get('n_techniques',0)>=1))"` | `>= 2` |
| C2 | 同 P1（failures 总数含 rollback_failed=true 条目） | `0` |
| ①（strategies） | `$PY -c "import sqlite3;con=sqlite3.connect('file:data/language_genome.db?mode=ro',uri=True);[print(r) for r in con.execute('SELECT strategy_id,unique_source_intervals,root_works,valid FROM strategy_stats ORDER BY strategy_id')]"`（mode=ro 只读纪律） | 逐条按 §3 表核（停策略先看 valid=N≥8） |
| 总量止损 | `$PY -c "import json;d=json.load(open('out_k4_10/k4_paired.json',encoding='utf-8'));print(sum((r.get('usage') or {}).get('tokens') or 0 for r in d['artifacts']['receipts']))"` | `<= 8000000`（token） |

核验由接手 agent 跑（不自证）；逐项结果落台账，任一不过 → 停止扩张报告。

### 6.1 离线预演对照（2026-09-22，零配额，命令机械可跑性实跑证据）

`--scenes 10`（FixtureClient，离线；预演产物取证后**已删**，`out_k4_10/`
真跑落点仍为空）。期望列为**真跑口径**；离线数值只证命令与结构，不用于判定：

| 判据 | 实跑输出 | 期望（真跑口径） |
|---|---|---|
| P1 | `0` | `0` |
| P2 | `0`（真库现无匹配场景知识→空包；真跑前置=K2-A 放量先行，§5 顺序） | `>= 2` |
| C2 | `0` | `0` |
| ① | `0 行`（表/列已在：`strategy_stats` 含 `unique_source_intervals`/`root_works`/`valid`；行数随 K2-A 放量增长） | 按 §3 表逐条核 |
| 总量止损 | `0`（fixture 零真实消耗；收据 `usage.tokens` 恒在、缺记=0） | `<= 8000000` |

结构证据：场景数 **10**（s1–s10）、正文/收据各 **20**、A 臂包 **10**、
分析行 10；收据 `usage` 三键恒在（calls/duration_ms/tokens）、包全含
`n_techniques`；复跑同 `--out` → **拒**（exit 1「已存在」）。回归钉死：
tests/test_k4_paired.py（scenes_for 派生前置全真 / 10 场 e2e / 预算闸
烧穿必抛不静默 / main --scenes 10 拒覆盖 / Budget 默认 6）。

## 7. 产物落点（B 定稿）

- **K4 三场原始收据（不可覆盖）**：首跑 `--out out_k4_3/`（首拍板后）。
- **10 场扩展产物**：`--out out_k4_10/`（独立目录）。
- **严禁覆盖 K4 三场原始收据**（脚本 `--out` 已拒已存在目录；这是第二道
  保险的书面契约）。

## 8. 任务单进度图例（B 定稿）

- `[~]` = 零配额离线部分已落地过会审，真跑/放量 blocked 于 402 资金墙
  待拍板（代码+评估就位，拍板后一条命令接通，无额外开发）。
