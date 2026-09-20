# 单场景与连续场景试点

2026-09-20。实施范围对应 [总方案](AI长篇小说系统_完整工程方案.md) 的 S1–S3 中最小部分；进度与最终结果见 [交接](runtime-handover-20260920.md)。

## 已有代码入口

| 工件 | 职责 |
|---|---|
| `app/scene_runtime/contracts.py` | 严格场景卡、世界事实、知识包、预算、事件和核验契约 |
| `knowledge.py` | 只读导出指定 LG 策略的条件 / 操作 / 反例边界；不读取参考原文示例 |
| `store.py` | 独立 SQLite、请求去重、调用台账、正文/事件/状态原子提交、outbox、重放核对 |
| `pipeline.py` | 场景卡校验、知识与上下文冻结、生成、独立模型核验、有限修订、提交 |
| `client.py` | 使用现有网关配置，单次 HTTP 派发；记录实际模型和完成原因，显式处理未知结果 |
| `scripts/run_scene.py` | CLI 入口、阶段暂停、恢复、正文和收据导出 |
| `examples/scene_runtime/ferry.json` | 三场原创验收故事的简报、世界与计划 |
| `tests/test_scene_runtime.py` | 故障、并发、隔离、版本、预算、证据与只读来源回归 |

路径均相对仓库根目录 `F:/agi/language-genome`。

## 使用方式

先只校验输入，不发送模型请求：

```powershell
python -X utf8 scripts/run_scene.py --bundle examples/scene_runtime/ferry.json --out F:/agi/outputs/my-scene-pilot --take 1
```

可选接入指定 LG 策略；包创建后固定，恢复时继续使用该包，不从活动库悄悄刷新：

```powershell
python -X utf8 scripts/run_scene.py --bundle examples/scene_runtime/ferry.json --out F:/agi/outputs/my-genome-pilot --genome-db data/language_genome.db --strategy ES-252069e72b43 --take 1
```

真实执行在相同命令末尾添加 `--live --writer-model <网关完整ID> --verifier-model <网关完整ID>`。配置沿用现有 `.env` / 环境变量；日志和导出不包含凭据。

- `--stop-after-verified`：完成正文与核验后停止，不改变世界；去掉参数并重跑同一请求即可提交，不重复调用模型。
- `--take 3`：先复用第一场的已提交收据，再依次生成第二、第三场；下一场读取刚提交的状态与同视角前文。
- 已存在的输出目录固定输入、预算与模型绑定；改变这些内容必须明确作为新任务，不能伪装成原任务恢复或借此重置额度。
- 仅代码修复需要接续已有断点时，使用 `--upgrade-checkpoint "具体原因"`：先核对正史，再把前后实现 manifest 和原因追加到 `implementation-upgrades.jsonl`。旧请求哈希、预算、调用、正文与收据继续保留；不传此参数时拒绝实现哈希变化。该操作不改变已经冻结的模型请求。
- 模型调用失败或结果未知会退出并保留记录。未知结果须先核对，CLI 不会自动重派或切换模型。

样例配置每场最多 6 次调用、2 次局部修订、3,000 输出 token / 次、24,000 输入字符 / 次和累计 600 秒请求时间。输入字符是保守的资源代理，不宣称精确 token 预算；未知价格记为 null，目前没有已核验的货币金额上限。HTTP 超时不证明上游没执行或没计费。

## 产物与核对

输出目录包含 `runtime.sqlite`、冻结知识包、实现 manifest、逐场正文、`连续场景.md` 和 `report.json`。报告同时给出世界状态、提交收据、原文证据、实际模型、调用数、耗时及重放审计。

权威正文保存在提交事务内；Markdown 是可重建的展示副本。删除或修改展示文件不会改变 canon，重新导出即可恢复。模型原始结果与调用状态保留在独立运行库，研究数据库只读消费。

自动测试采用合成夹具，只验工程契约；真实模型产物单独记录。模型给出的引文必须是连续原文，抽取的新值必须匹配批准计划；“能找到引文”仍不等于自动证明全部文学语义。

核验只容忍一个完整外层 JSON 围栏，以及能唯一匹配到原文的纯空白差异；最终引文保存原文片段，模型原始回复不改写。省略号拼接或伪造引文仍被拒绝；每轮最多一次 Verifier 契约返修，计入同一调用上限。

本地操作者可用 `Store.add_confirmed_issue` 指认已有稿件的事实问题；对应已核验稿会退回待修订。`Store.dismiss_review_issue` 只允许逐条裁定已记录的核验误报，必须绑定具体 stage、稿件和 Issue 哈希，并引用同作品、同分支的既有正史原句。相关性由操作者负责，程序不能靠引文匹配自动证明语义。模型没有这项能力；裁定不能编辑正文、计划、状态变化或预算，也不能清除其他硬问题。所有裁定与原始核验结果同时留档。

新建任务的 Verifier 读取冻结的最近两场同视角正文，减少将前文细节误判为新事实；旧断点保留原请求，不能悄悄替换上下文。

## 本次结果

[三场正文](../../outputs/novel-runtime-20260920/accepted-pilot-final/灯下渡口_三场正文.md)及[完整验收记录](../../outputs/novel-runtime-20260920/accepted-pilot-final/acceptance.md)已落盘。三场调用分别为 2 / 6 / 6；世界 revision=3；第一场原收据不变，重复执行三场不新增调用。508 项全仓测试通过，其中 Runtime 35 项，保留 4 个警告。

本次含 Codex 复核：三处正文事实问题经过修订，五条核验误报有逐项裁定。这个结果支持有复核参与的试点，不足以放行无人值守长篇运行。

## 当前边界

- Planner v1 校验明确提供的场景卡，没有自动制定卷级路线、重规划或 Dream。
- 当前是本机 CLI / 单宿主 SQLite 试点，没有上线设计中的 HTTP API、前端作品工作台、多机 worker、租约管理或发布功能。
- 知识适配已消费实际 LG 策略；Distiller 的实时机制桥接仍待接入。现有策略的迁移效果仍是 hypothesis。
- 当前支持已有事实值的变更；新增实体、知识传播、事实可见性变更和多视角复杂叙事还需后续契约。不同 POV 的旧正文不会自动传给新 POV。
- 语义核验由独立角色模型和硬契约共同完成，仍可能漏报或误报。真实示例的人工式复核由本次审查记录补充；没有宣称作者偏好或文学质量已通过。
- 三场景工程试点已完成；10 / 100 场景、百万字容量与长期稳定性尚未验收。

## 测试

```powershell
$env:PYTHONUTF8 = '1'
python -X utf8 -m pytest tests/test_scene_runtime.py --tb=short
python -X utf8 -m pytest --tb=short --disable-warnings
```

Windows 子进程也需要 UTF-8，单独给父进程 `-X utf8` 不一定影响测试内的子进程。
