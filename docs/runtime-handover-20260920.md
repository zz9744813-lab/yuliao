# 2026-09-20 长篇方案定稿与 Runtime 交接

目标：延续已写入的修订稿，完成复核后交付单场景及连续场景的可验证运行；不重新启动审美评委研究。

## 步骤 1：最终核对完成

- 保留原 1–146 章节；新增的 §0 是状态与证据入口。
- 修复 Dream 必选、策略自报字段、AILeak 名称混用、物理表名、固定进度分母等残留不一致。
- 最新修正：nat / hvai 未超过长度基线；清洗脚本已修、旧导出尚未重导；16 页前端已接通并保留聚合页边界。
- 校验结构化示例、章节引用与本地来源链接；证据：`../../outputs/novel-runtime-20260920/plan-validation.json`、`source-snapshot.json`。

## 步骤 2：长篇关键约束完成

- §15/56/62/129：模拟与草稿不得写入正史，commit 唯一路径。
- §55：Critic 不能自行授权改设定；结构性问题回规划。
- §75/78/86/96：调用、输出、时长、修稿都有上限，恢复不清零；结果未知不盲重试。

## 步骤 3：定稿落盘完成

- 主文件：`AI长篇小说系统_完整工程方案.md`；执行计划：`plan.md`。
- 历史修订过程保留在 `../../outputs/novel-plan-review-20260919/`；09-20 最终核对记录独立保存。
- 后续实现按真实结果追加，不预先勾选功能验收。

## 步骤 4：实现进行中

拟在 LG 内新增独立 Runtime 模块和 CLI，复用现有模型配置与基础设施，避免修改在用的研究数据和页面。真实运行与故障回归完成后在此追加证据、命令及限制。

## 步骤 4a：契约与提交内核完成

- 新增 `app/scene_runtime/contracts.py`、`store.py`、`pipeline.py`、`client.py`，运行库与研究库分离。
- 已验证 26 项新测试：原子回滚、并发重复提交、旧 revision、模拟隔离、跨作品、不可变事实、伪造证据、未授权补丁、修稿/调用限额、未知结果不重派与 outbox 恢复。
- 命令：`python -X utf8 -m pytest tests/test_scene_runtime.py --tb=short --disable-warnings` → 26 passed。
- 模型客户端复用现有 endpoint/凭据配置；原实验网关内部隐式重试且缺 actual_model/finish_reason，故本阶段用单次派发适配，逐次记账，不更改原网关。
- 现有全量基线首次运行出现 Windows 默认 GBK 解码及共享测试次序问题；单独 UTF-8 复跑 RM 24 项通过。最终将以统一 UTF-8 环境复跑并记录，不把环境错误冒称为功能回归。

## 步骤 4b：单场景真实核验通过，恢复验收进行中

- 模型路由：带 vendor 的 `deepseek/deepseek-v4.1-flash` 返回 HTTP 503，未写入世界；按真实 `/models` 的完整 ID 改用 `deepseek-v4.1-flash` 后成功。更换使用独立运行目录，未抹除失败记录。
- `live-pilot-v2`：真实 Writer 与 Kimi 核验共 4 次调用，经历 1 次局部修订后达到 verified；特意停在 commit 前，世界 revision 仍为 0。
- 实际拦截：正文未明确收钱却拟增加收款方钱数；核验器用省略号拼接的非连续引文也未通过证据校验。修订明确收钱后通过。
- 补齐最后一次响应超时/超输出额度也不能放行的检查，并用新测试验证恢复不绕过额度。源库只读适配与三场景状态链测试也已加入，新增测试现为 29 项全过。
- 统一 UTF-8 的全量测试此前已 472 passed（含当时 26 项新测试）；未修改原有业务代码修复测试。最初默认 GBK 运行的失败不计作本次功能回归结论。
- 最终代码哈希已固定到新目录 `../../outputs/novel-runtime-20260920/accepted-pilot/`。先验证并中断，再以相同请求恢复提交，随后扩至三场景。

## 步骤 5：单场景完整闭环与真实恢复通过

- 固定最终实现：`accepted-pilot-final`；Writer `deepseek-v4.1-flash`，Verifier `agnes-3.0-flash`，实际模型回执保留。
- 第一次执行 `--stop-after-verified`：2 次真实调用，status=verified，世界 revision=0，commit=0。
- 相同输入恢复：调用数仍为 2，status=committed，revision=1，commit=1，outbox 清空；未重新生成、未重复扣钱。
- 证据：`verification-checkpoint.json` 与 `single-scene-recovery.json`，均在 `../../outputs/novel-runtime-20260920/accepted-pilot-final/`。
- 真实运行推动的一项修正：证据格式错误只返修 Verifier，不再触发 Writer 重写；新增保护已进入回归。
- 当时全量测试：476 passed，4 个既有警告；其中 Runtime 新测试 30 项。后续增补及最终结果见步骤 6/7；`../../outputs/novel-runtime-20260920/final-tests.txt` 保存最新完整测试日志。
- 下一步顺序执行第二、第三场；先复用第一场收据，读取 revision=1 的钱物、承诺及正文。

## 步骤 6a：第二场从断点恢复并提交完成

- 第一场正文哈希、commit ID、完整收据与断点快照逐项相同；调用数仍为 2，未重跑。
- 对旧核验结果兼容单层 JSON 围栏，只允许唯一匹配的空白差异还原为正文原句；模型原始回执保留，禁止省略号或文字替换。
- Codex 记录两项事实问题：灯内已有油却另取油、增加未确认库存；Writer 第一次修订已删除。
- 第二场核验将第一场石阶钱币的湿印误报为新交易，6 次调用上限正常拦截。Codex 引用第一场已提交的放钱与收钱原文，对这一条误报作有证据的裁定；未新增调用，未修改预算、正文或计划，原始意见和裁定同时保留。此步骤含人工式 Codex 复核，不是无人值守验收。
- 第二场已提交：revision=2，key=林穗、钱=林穗 2 / 沈砚 1、手伤=true、承诺=天亮前还钥匙、灯已点亮、备用绳和未启用整份灯油均为 0。
- 证据：`../../outputs/novel-runtime-20260920/accepted-pilot-final/second-scene-recovery.json`、`second-scene-review-decision.json`、`second-scene-budget-stop.json`；代码升级前后哈希保留在 `implementation-upgrades.jsonl`。
- 新准备的场景向核验器提供冻结的最近同视角正文；旧任务继续使用原请求哈希。后续执行第三场，先 verified 再检查提交。

## 步骤 6b：第三场已核验，提交前复核拦下一处新增事实

- 第三场顺序读取 revision=2 和前两场正文；初轮 Verifier 把未变化的两枚钱误列为补丁且写成字符串，机械契约拒绝。第一次修订后在 4 次调用处达到 verified，仍未提交。
- Codex 复核发现“仓门锁过两回”与第二场仅一次开仓后扣锁不符，已记录带原句的 hard issue，并撤销该草稿的 verified 状态。继续使用第二次（最后一次）修稿机会；调用上限仍为 6。
- 第三场预提交证据：`../../outputs/novel-runtime-20260920/accepted-pilot-final/third-scene-verification.json`。完整测试 508 passed / 4 warnings，其中 Runtime 35 项；日志 `../../outputs/novel-runtime-20260920/final-tests.txt`。

## 步骤 6c：连续三场完成，第一场保留原收据

- 第三场最后一次修订已删除多余锁门次数；最终 Writer 第 2 轮正文经 Codex 逐段复核。Verifier 第 2 轮把四条说明为“并非硬伤 / 符合期限 / 逻辑连贯 / 事实成立”的结果误标为 hard；Codex 逐条绑定本稿、具体意见和既有正史引文裁定，保留原始输出及四条理由，没有自动忽略全部 hard。
- 第三场于 6 次调用处 verified，保存最终断点后提交；恢复提交未增加调用。连续正史 revision=3、commit=3，outbox 无待处理，重放无差异。
- 最终状态：钥匙=沈砚；钱=林穗 2 / 沈砚 1；手伤=true；承诺=已兑现；灯仍亮；新绳已装；备用绳与未启用整份油均为 0。未记录痊愈或退费。
- 三场调用数分别 2 / 6 / 6；第一场 commit ID、正文哈希和完整收据与用户要求保留的断点完全相同。未新增预算、未重置修稿次数。
- 证据：`third-scene-final-checkpoint.json`、`third-scene-review-decisions.json`、`third-scene-budget-stop.json`、`report.json`，均在上述 accepted-pilot-final 目录。计划第 6 项已勾选，开始最终清单与交付复核。

## 步骤 7：交付与最终复查完成

- 正文：`../../outputs/novel-runtime-20260920/accepted-pilot-final/灯下渡口_三场正文.md`，仅添加标题分节，三场正文均对应已提交文本。
- 状态与收据：同目录 `state-receipts.json`，含初始状态、三次补丁、逐场状态及完整收据；钥匙、钱、伤势、承诺逐项核对。
- 成本 / 调用：`call-ledger.json`。当前三场 14 次，输入 30,783 / 输出 13,773 token，累计请求 151.467 秒；较早三个隔离目录合计另 9 次，四目录共 23 次，有 1 次缺少 token 回执。实际费用未知为 null，不把失败调用省略或当作免费。
- 验收：`acceptance.md` / `acceptance.json`；复核：`operator-review-ledger.json`；重复执行：`idempotency-recheck.json`。三场再次执行均 reused=true，完整收据及全部调用行哈希不变，仍为 14 次，outbox=0。
- 代码哈希与 manifest 一致；最终完整仓库测试 508 passed / 4 warnings，Runtime 35 项。日志哈希写入结构化验收。
- 主方案新增 §0.7，仍保留原 1–146 编号；plan.md 第 6、7 项均已勾选，仓库总交接一行更新。
- 可复建交付：`python -X utf8 F:/agi/outputs/novel-runtime-20260920/accepted-pilot-final/build_acceptance.py`，只读调用记录并导出证据，不发模型请求。
- 边界：含 Codex 复核的三场工程试点；核验器仍有漏报 / 误报，未验收无人值守、文学效果、10 / 100 场、长篇规模或设计 HTTP API。下一步先稳定变化抽取与 hard 判定，在新样例上验证复核量，再扩场；微调与大规模 Dream 后置。

最终文档复查：原编号 1–146 完整保留，26 个 JSON / YAML 示例可解析，32 个本地链接全部存在；自有代码无尾随空白，范围内 `git diff --check` 通过。证据 `../../outputs/novel-runtime-20260920/accepted-pilot-final/final-delivery-checks.json`。

## 后续：18:10 起全项目审查

- 用户要求全面审核，以当前工作区 HEAD `799df08` 及未提交修改为基线；发现 8 项 P1 / 4 项 P2，详见 [审查报告](project-audit-20260920-1810.md)。本次未修改业务实现。
- Runtime 新发现：恢复 audit 未校验事件 / 收据；核验器误抽未变事实仍触发 Writer 重写。复现均在临时库及副本，原件保持 3 个提交 / 14 次调用，第一场收据仍与原断点一致。
- 新全量结果：LG 587 passed / 7 failed / 9 errors，6 项主体失败和 9 项 teardown 错误来自在途 bal-v2 fixture 清空登记表键，另 1 项为 FastEmbed 模型下载连接失败；Distiller 214 passed / 2 warnings，额外竞争复现发现租约失效后虚报完成。
- 本轮先隔离了测试对真实 `serve_cursor.json` 的访问，前后哈希一致；正式文件内已有测试批次痕迹，后续须修隔离及按有效备份核对用户游标。
- 数据只读核查：SFT / Rewrite 各 114 行命中当前基准文本，且各含 32 行未校勘源；RM 仍有 172 组冲突分数。训练后置，修代码后须重导而非沿用旧文件。
- 完整证据与可复现脚本在 `../../outputs/project-audit-20260920-181014/`；下一步先修标签与分组可信度、实际文件隔离，再修调用记账及原子运行/提交。

18:26 收尾更新：检测到工作区并行修复 bal-v2 fixture（保留登记表键、补 init_db），立即复跑完整 LG 测试，结果 593 passed / 1 failed / 1 warning；9 项 bal-v2 用例全部通过，剩余为 FastEmbed 模型下载连接失败。A12 关闭，当前未解决共 8 项 P1 / 3 项 P2。首轮日志及 `lg-tests-rerun.txt` 同时保留，报告已按最新状态更新。
