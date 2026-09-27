# 交付报告：lg-scope-singlebook-disclose

- 工作树：`F:/agi/_scratch/worktrees/scope-singlebook-disclose`
- 分支：`task/scope-singlebook-disclose`　基点：`bdb2b68`
- 变更文件（**仅白名单三件**）：
  - `app/knowledge_query.py`（改）
  - `tests/test_scope_singlebook_disclose.py`（新增）
  - `DELIVERY_scope_singlebook_disclose.md`（新增，本文件）
- 未 commit / 未 push / 未合并 / 未写库 / 未改其他工作树与源检出。

## 1. 缺口（主控本轮真库只读读数，非推测）

`app/knowledge_query.py:_scope_matches` 对 `scope=="WORK"` 只做
`policy["book_id"] in strategy.scope_ids`，**不检查该策略的合格证据是否真的来自
这本书**。真库实测（`book_id=WK-6e5d2623`、8 条策略全 `scope_verdict=pass`、
`selected=8`）：

- 每条策略 `evidence_root_works` **只含 2 个作品**，且 `evidence_cross_work=True`；
- `scope_ids` 最长 3 本（如 `["WK-3631b4b3dd44","WK-6e5d2623","WK-8e8e0459284d"]`），
  而证据实际只来自 2 本——「声称 3 本、证据只有 2 本」；
- 响应里**没有任何字段**告诉调用方「这条策略在我这本书上到底有几条**同书**证据」
  ⇒ 12 条证据被读成「都在我这本书上」，实际是跨书聚合。

## 2. 交付内容：只加披露，不改判定

`app/knowledge_query.py` 新增模块内私有函数 `_scope_disclosure()`（`app/knowledge_query.py:335`），
在 `query_knowledge` 组装 `selected[]` 条目时调用（`app/knowledge_query.py:489`），
在既有条目上**只增不减**地追加四个**单书披露字段**（只读披露，不参与任何判定）：

| 字段 | 类型 | 口径 |
|---|---|---|
| `evidence_same_book_count` | `int` | 该策略合格证据里 `canonical_work` 等于查询作品 canonical 根的**唯一区间数**（键与 `evidence_count` 同口径 `(canonical_work, span)`）；查询作品缺 `WorkSource` 登记行时为 0 |
| `evidence_scope_ids_declared` | `int` | 该策略 `scope_ids` 的声明条数（原样计数，不去重） |
| `evidence_scope_ids_with_evidence` | `int` | 声明里**真有合格证据根作品**的作品数（`scope=WORK`；恒等式 `with_evidence + len(unbacked) == 去重后声明数`） |
| `evidence_scope_unbacked_ids` | `list[str]` | **声明了却零合格证据**的作品 id（按声明顺序、去重）——正是要消灭的「看起来配了其实没证据」 |

硬约束的落实方式：

- **复用已有 refs，不另跑查询**：函数入参是 `kept` 里已有的 `item["refs"]`（已带
  `canonical_work`）与 `item["roots"]`；`_evidence_for` 一个字未动，过滤顺序
  （来源/版本 → 必需条件/bad_when → 范围 → 去重 → 排序）不变，status 门不变，
  `_scope_matches` 不变。
- **只增不减**：四个字段是**新增键**；`score_components`（含 `evidence_count`）、
  `evidence_count`、`evidence_cross_work`、`evidence_root_works`、`evidence`、
  `uncertain_items`、`for_context`、`evidence_cross_work_note` 全部逐字不变；
  `capabilities().evidence_provenance.package_fields` 也只追加四条说明，既有三条
  说明保留。`package_sha256` 的计算输入（`policy` + `selected_ids` + `snapshot`）
  未触及 ⇒ 包哈希逐字不变。
- **非 WORK 范围不假装**：AUTHOR/GENRE 的 `scope_ids` 是作者/题材 id 而非作品 id，
  作品级 unbacked 名单没有意义 ⇒ `with_evidence` 恒 `0`、`unbacked_ids` 恒 `[]`
  （**不适用**，不是「全部有证据」），`declared` 仍如实报声明条数。该口径同时写进
  `capabilities` 字段说明，调用方不会把 `[]` 读成「声明全有证据」。

判定收紧/放宽（如是否对 WORK 范围强制同书证据）属主控/集霸裁定，本任务**未代劳**。

## 3. 回归钉（`tests/test_scope_singlebook_disclose.py`，8 例）

| 钉 | 用例 | 断言要点 |
|---|---|---|
| ① | `test_disclosure_fields_present_with_right_types` | 四字段在**每个** selected 条目上存在；计数为 `int`（排除 `bool`）、unbacked 为 `list[str]`；非负；WORK 下 `with + len(unbacked) == declared` |
| ② | `test_declared_three_but_evidence_two_names_the_unbacked_one` | 声明 3 本、证据只来自 2 本 ⇒ `declared==3`、`with_evidence==2`、`evidence_scope_unbacked_ids == [缺证据那一本]`（恰好点名），且 `same_book_count==1`、`evidence_count==2`、`cross_work is True` |
| ③ | `test_unbacked_empty_when_declaration_matches_evidence` | 两个方向都钉：声明=证据 ⇒ `unbacked==[]`；反向（声明 1 本、证据全在别书）⇒ `unbacked==[唯一声明项]`、`with_evidence==0`、`same_book_count==0` |
| ④ | `test_disclosure_does_not_change_verdicts` | 同一 policy 下用本仓 `kq._evidence_for` **现算值对拍**：`selected` 集合与**顺序**、`score_components` 逐字相等、`entry["evidence"] == refs`、`evidence_root_works` 等于现算根作品集合、判定字段一个不少 |
| ⑤ | `test_disclosure_survives_same_book_narrowing_switch` | 收窄开关下判定照旧（跨书/异根证据被剔、`rejected` 理由逐条相等），披露随之收窄（`declared=3 / with=1 / unbacked=[beta,gamma]`） |
| ⑥ | `test_non_work_scope_does_not_emit_book_level_unbacked` | AUTHOR/GENRE 不产作品级 unbacked，既有 `evidence_cross_work`/`evidence_root_works` 不变 |
| ⑦ | `test_query_book_without_registry_reports_zero_same_book` | 查询书缺登记行：`cross_work=False` + `note` 仍在，`same_book_count==0` 且声明项被点名 |
| ⑧ | `test_capabilities_documents_the_four_disclosure_fields` | 四字段写进 `capabilities`；既有三字段说明仍在（只增不减） |

**零写入**：夹具在 `tmp_path` 造独立 SQLite，写完即以 `mode=ro` + `PRAGMA
query_only=ON` 打开，收尾断言库文件 **sha256 前后一致**（沿用
`tests/test_evidence_cross_work.py` 的既有纪律）。真库全程未被打开。

## 4. 反向自检

不满足于「新用例自己绿」——把**改动前**的实现（`git show bdb2b68:app/knowledge_query.py`，
以 `app.knowledge_query_base` 身份装载、相对导入仍指向仓内 `app.knowledge`/`app.models`）
与改动后在**同一个只读夹具库、同一批 policy** 上对拍，投影里剔除四个新键：

```
[alpha(default)] 判定投影一致=True selected=5 rejected=1 pkg_sha_same=True
   - sd-partial   scope=WORK   ev_count=2 roots=['WK-sd-alpha', 'WK-sd-beta'] same_book=1 declared=3 with_evidence=2 unbacked=['WK-sd-gamma']
   - sd-author    scope=AUTHOR ev_count=2 roots=['WK-sd-beta', 'WK-sd-gamma'] same_book=0 declared=1 with_evidence=0 unbacked=[]
   - sd-exact     scope=WORK   ev_count=1 roots=['WK-sd-alpha'] same_book=1 declared=1 with_evidence=1 unbacked=[]
   - sd-foreign   scope=WORK   ev_count=1 roots=['WK-sd-beta'] same_book=0 declared=1 with_evidence=0 unbacked=['WK-sd-alpha']
   - sd-genre     scope=GENRE  ev_count=1 roots=['WK-sd-delta'] same_book=0 declared=1 with_evidence=0 unbacked=[]
[alpha(narrow)] 判定投影一致=True selected=2 rejected=4 pkg_sha_same=True
   - sd-exact     scope=WORK   ev_count=1 roots=['WK-sd-alpha'] same_book=1 declared=1 with_evidence=1 unbacked=[]
   - sd-partial   scope=WORK   ev_count=1 roots=['WK-sd-alpha'] same_book=1 declared=3 with_evidence=1 unbacked=['WK-sd-beta', 'WK-sd-gamma']
[noreg(default)] 判定投影一致=True selected=1 rejected=5 pkg_sha_same=True
   - sd-noreg     scope=WORK   ev_count=1 roots=['WK-sd-delta'] same_book=0 declared=1 with_evidence=0 unbacked=['WK-sd-noreg']
[beta(zero-match)] 判定投影一致=True selected=1 rejected=5 pkg_sha_same=True
   - sd-partial   scope=WORK   ev_count=2 roots=['WK-sd-alpha', 'WK-sd-beta'] same_book=1 declared=3 with_evidence=2 unbacked=['WK-sd-gamma']
REVERSE_SELFCHECK_ALL_IDENTICAL= True  DB_SHA_UNCHANGED= True
RC=0
```

投影逐字比较的字段：`status`、`policy_sha256`、`snapshot_fingerprint`、
`package_sha256`、`budget`、`rejected`、剔除四新键后的 `selected` 全量。
四个方向（默认 / 收窄开关 / 缺登记行 / 换书）全部一致 ⇒ 披露是**纯增量**。
本会话已按同一方式复跑并逐行复现上述输出（`REVERSE_SELFCHECK_ALL_IDENTICAL= True
DB_SHA_UNCHANGED= True`，`RC=0`），并确认基点模块 `bdb2b68` 里**不存在**
`_scope_disclosure`（即四个字段确为本次新增）。
（自检脚本写在 `C:\Users\6\AppData\Local\Temp\opencode\` 仓外临时目录，跑完已删；
仓内零临时文件。）

## 5. 验收命令（真跑，输出原文）

```
F:/kelaode/Data/Agents/zqibcc8w9/tools/Python311/python.exe -m pytest tests/test_scope_singlebook_disclose.py tests/test_evidence_cross_work.py -q -p no:warnings
```

输出原文：

```
F:\kelaode\Data\Agents\zqibcc8w9\tools\Python311\Lib\site-packages\pytest_asyncio\plugin.py:208: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default to the loop scope for asynchronous fixtures to function scope. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
..................                                                       [100%]
RC=0
```

`18 passed`（新增 8 + `tests/test_evidence_cross_work.py` 既有 10，参数化 5 道门各计一例），
退出码 **rc=0**；既有跨作品用例**一例不红**。（上面第 2 行 `warnings.warn(...)` 是
pytest-asyncio 自身的告警回显，与本改动无关，`-p no:warnings` 只关告警摘要。）

邻域套件（防外溢，均 rc=0）：

```
F:/kelaode/Data/Agents/zqibcc8w9/tools/Python311/python.exe -m pytest \
  tests/test_knowledge_query_v2.py tests/test_knowledge_freeze.py tests/test_knowledge_query_cards.py \
  tests/test_brief_cost_numbers.py tests/test_k3_preview_reach.py tests/test_docs_code_reconcile.py \
  tests/test_compile_all.py tests/test_strategy_stats_evidence_class.py -q -p no:warnings
→ 120 passed
F:/kelaode/Data/Agents/zqibcc8w9/tools/Python311/python.exe -m pytest \
  tests/test_knowledge_query_cards.py tests/test_k4_registered_world_preflight.py \
  tests/test_k5_promotion_write.py tests/test_k5_promotion_gate_explain.py \
  tests/test_k5_promotion_wire_probe.py -q -p no:warnings
→ 114 passed
```

以上三组读数（18 / 120 / 114）本会话均已用上述解释器真跑复核，退出码与例数一致。

## 6. 未自跑 / 未覆盖事项（如实声明）

- 交付物中**没有**主控那轮真库只读读数的复跑：本次验收全部在 pytest 临时目录的
  独立 SQLite 上完成，真库一次都没打开（本任务零真库读数需求）。§1 的真库数字是
  主控给定的读数，不是本会话实测。
- 判定是否收紧（WORK 范围是否强制同书证据）**未动**，不在本任务范围。
- 未跑全量 pytest（只跑上列 234 例 + 18 例定向）；全量基线由主控统一收口。
- `docs/证据跨作品口径_20260925.md` 未同步新字段（不在白名单内）；字段口径已写进
  `/knowledge/capabilities` 的 `evidence_provenance.package_fields`，并由
  `test_docs_code_reconcile.py` 既有三条锚点保证只增不减。文档同步若需要，请主控
  另派带 docs 授权的任务。
