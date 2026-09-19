# 前端优化方案（2026-09-19）

> 决策依据：集霸 2026-09-19 20:15 批准，按推荐选项执行 —— **①A 金棕统一 / ②A 旧外壳重定向 / ③A 盲评台加互链**。
> 边界原则：**后端 console API 一字不动**（shape 被 `tests/test_console.py::test_console_each_module_shapes` 钉住），全部契约对齐在 websrc 侧完成。

## 0. 诊断：三套前端并存，websrc 16 页是死页

| 套 | 文件 | 规模 | 状态 |
|---|---|---|---|
| 第一界面·盲评台 | `app/static/index.html` | 1443 行 | 现役写界面。9-16~18 六轮迭代定型（两层令牌 / 双主题 / diff 高亮），**本次仅在头部加一个入口链接，其余一概不碰** |
| 第二界面·16 模块外壳 | `app/static/console.html` | 273 行 | 2026-09-19 09:26 T3 上线，`/console/ui`，GET-only 仪表 |
| **websrc 16 页** | `websrc/*.html` | 3390 行 | 2026-09-19 19:29 **Hermes** 入库（`b0b02f9`）。**死页**：访问不到、slug 错、结构错 |

### websrc 四层断裂（实测证据）

1. **不被 serve** —— `app/api.py` 只 mount 了 `app/static`，无任何路由指向 `websrc/`，浏览器访问不到。
2. **slug 错位（10/16 页）** —— 页面 fetch 的 slug 与 `app/console.py::MODULE_ORDER` 不一致，直接 404：

   | websrc 页面 fetch | API 实际 slug |
   |---|---|
   | `/console/overview` | `dashboard` |
   | `/console/frames` | `semantic-lab` |
   | `/console/arena` | `reconstruction-arena` |
   | `/console/residual` | `expression-residual` |
   | `/console/strategy` | `strategy-atlas` |
   | `/console/judges` | `judge-arena` |
   | `/console/preference` | `preference-lab` |
   | `/console/hardcase` | `hard-cases` |
   | `/console/benchmark` | `benchmarks` |
   | `/console/training` | `training-data` |

   （`corpus` / `experiments` / `models` / `workflow` / `observability` / `settings` 六页 slug 本就正确。）
3. **包装结构错位** —— API 返回 `{module, title, data}` 嵌套信封，页面直接读 `data.stats`，缺解包层。
4. **数据形状错位** —— 页面期望统一 `{stats:[{value,label}], rows:[...], generated_at, note}`，而 `console.py` 16 个模块**各有各的真实键**（如 `_dashboard` 返回 `human_anchors/works/semantic_frames/...` 平铺标量 + `experiments:{total,by_status}` 嵌套）。即使接通也只会渲染空态。

### 存量技术债（HANDOVER §38 原话）

> "前端没有构建产物也没有类型检查，'某个功能被顺手删掉'没有任何机制能发现。2026-09-16 的 UI 重写就删掉了跨实验取题逻辑（`batchMulti`），后果是实的。"

另：16 页每页重复同一份 CSS/JS（16 份拷贝）、页与页之间**零导航链接**、GitHub Dark 风格与盲评台的冷纸/石墨金棕体系是完全两套语言。

## 1. 五阶段路线

### P1 接线（serve + slug + 解包 + 渲染适配）
- `app/api.py` 新增 `_WEBSRC` 指向项目根 `websrc/`：
  - `/lab/{page}` → `FileResponse(..., Cache-Control: no-store)`（与 `/`、`/console/ui` 缓存策略一致）
  - `/lab/_shared` → `StaticFiles`（CSS/JS 靠 Last-Modified/ETag 自动失效，改完即生效）
  - `/lab` → 302 到研究台首页
  - 目录穿越防护：白名单 + resolve 后确认仍在 `_WEBSRC` 内
- 逐页修正 10 个 slug（见上表）
- 解包 `{module,title,data}` → 渲染器消费 `data`
- 16 个模块渲染适配器，消化各自真实数据形状
- **验收**：16 页全部渲染真实数据，无"数据接口不可用，以下为示例结构" banner

### P2 共享层（16 份重复 → 4 个共享件）
`websrc/_shared/`：
- `tokens.css` —— 两层令牌（原语 `--n-*`/`--gold-*` → 语义 `--bg/--surface/--line/--fg/--accent`），沿用盲评台 9-17 经验
- `base.css` —— 卡片 / 面板 / 表格 / 徽章 / banner / 空态 / 指标卡组件
- `render.js` —— `fmtNum/esc`、指标卡、分组计数、表格、信封解包、错误态
- `nav.js` —— 共享导航（P3）
- 16 页改为外链引用，每页从 ~210 行缩到 ~50 行
- **验收**：改 `tokens.css` 一处色值，16 页全变

### P3 导航与信息架构
- `nav.js` 渲染左侧栏导航（16 模块 + 当前页高亮，沿用 `console.html` 的 200px 侧栏模式，保持肌肉记忆）
- `console.html` 旧外壳降级：`/console/ui` 302 → 研究台首页，**代码保留不删**（免得 Hermes 下次值守困惑）
- `index.html` 头部加入口 `<a>`（批注块 / DOM id / `MARK_KINDS` 一概不碰，其不变量测试须原样全绿）
- **验收**：任意页 2 次点击内可达任意页

### P4 金棕视觉统一 + 16 模块定制渲染（决策 ①A）
- websrc 深色令牌对齐盲评台"石墨底 + 金棕强调"体系，**弃用 GitHub Dark 蓝 `#58a6ff`**
- 通用"键并集表格" → 每模块定制组件：
  dashboard 指标卡组 / corpus 作品段落表 / semantic-lab frame 分布 / reconstruction-arena 候选对照 /
  expression-residual 残差列表 / strategy-atlas 方法分布 / judge-arena 记分板 / preference-lab 批次门禁 /
  hard-cases 严重度榜 / benchmarks 排行榜 / experiments 状态表 / models 注册表 /
  training-data 导出表 / workflow 步骤条 / observability 调用统计 / settings 白名单表
- **验收**：双界面截图同框不违和

### P5 契约测试 + 冒烟（回应 §38 痛点）
- `tests/test_websrc_contract.py`：16 页 fetch slug 白名单对 `console.MODULE_ORDER` 静态校验、共享件引用完整性、导航挂点存在性
- TestClient 冒烟：GET 16 页均 200、GET 16 个 `/console/*` 均 200 且非空
- `render.js` 走 node 抽块单测（沿用 `tests/test_diff_js.py` 模式）
- 全量 pytest 零回归（盲评台不变量测试原样全绿）
- **验收**：新增 ≥10 项全绿；删任一共享件立即红

## 2. 坑点清单（动工逐条落实）

1. **令牌闸自动继承** —— `app/access.py` 是 `@app.middleware("http")` 全路径拦截，`/lab/*` 无需额外配置即受保护；**但** `_GATE_HTML` 的 `onsubmit` 硬编码跳回 `/?t=`，从研究台被拦时会跳去盲评台 —— 改为跳回当前路径（`location.pathname`），首页行为不变。
2. **缓存头** —— HTML 页必须 `no-store`（Round 4 的"改了没变化"假阴性根因之一）；`_shared` 靠 ETag 即可。
3. **localStorage 命名空间** —— 研究台一律 `lg_*`，绝不碰盲评台的 `rv_*`（Round 4 教训：跨标签页持久化会把人钉在旧偏好上）。
4. **动画铁律**（Round 3 事故）—— 入场动画永不碰 `opacity`，只在"坏掉也只是不播"的方向做（`transform` 可以，`opacity` 不可以）。
5. **settings 模块显式白名单** —— 令牌/密钥不进响应，渲染器不得假设字段存在。
6. **不引外部 CDN** —— 服务经公网隧道暴露（9-16 审计标过外部暴露风险），共享层全部自写零依赖。
7. **协作者登记** —— websrc 由 Hermes 入库，本方案须在 HANDOVER 补记，避免重复劳动或覆盖。
8. **调用 `_observability` 的结构** —— 该函数未返回统一 dict（实测抓取为空），适配器需按其真实返回单独处理。

## 3. 不动的东西（硬边界）

- `app/static/index.html` 批注块、DOM id、`MARK_KINDS`、`batchMulti` / `CTX_SCOPE` 逻辑 —— 除头部一个入口 `<a>` 外零改动
- `app/console.py` 全部只读性与 16 模块返回 shape（测试钉住）
- 写操作全部留在第一界面，研究台保持 GET-only
- `tests/` 既有 251 项必须原样全绿

## 4. 复现与验收

```bash
cd F:/agi/language-genome
python -m pytest -q                      # 全量零回归
python scripts/serve_local.py            # 或 uvicorn app.main:app --port 8787
# 研究台入口：http://127.0.0.1:8787/lab/overview.html
# 盲评台：   http://127.0.0.1:8787/
```
浏览器逐页核对 16 页真实读数 + 双主题截图（浅/深），按 Round 2 定下的规矩：**改完就截图立刻看**。

---

## 5. 执行记录（2026-09-19 晚，P1–P5 全部完成）

### 5.1 一处判断修正（开工后实测推翻）

原方案 P4 写的是"通用键并集表格 → 每模块定制组件"。**实测推翻**：websrc 并非通用表格——
Hermes 已在每页写了定制可视化（arena 的候选并排对照、judges 的记分板、preference 的胜率分布、
strategy 的 Wilson 区间、frames 的筛选交互）。

因此改为：**保留每页各自的 renderer，只抽真正的公共部分**（令牌 / 组件样式 / 工具函数 /
导航 / 信封解包）。若照原方案"统一成通用表格"，等于把已有的定制件全部推倒重来。

### 5.2 交付清单

| 项 | 内容 |
|---|---|
| 新增共享层 | `websrc/_shared/`：`tokens.css`（两层令牌，金棕）· `base.css`（组件）· `lg.js`（导航/主题/取数/渲染件）· `selfcheck.html`（16 页运行时自检） |
| 后端接线 | `app/api.py`：`_WEBSRC` + `/lab` 302 + `/lab/{page}`（白名单 + no-store）+ `/lab/_shared` mount |
| 令牌闸 | `app/access.py`：换令牌后**跳回原路径**（此前硬编码跳 `/`），保留原查询参数 |
| 旧外壳降级 | `/console/ui` → 302 `/lab/overview.html`；`console.html` 文件保留，其 4 项静态契约测试原样在跑 |
| 盲评台互链 | `index.html` 头部加一个 `<a id="lab-entry">研究台 ↗</a>` + 一行 `a.tool` 样式；批注块 / DOM id / `MARK_KINDS` 未动 |
| 16 页改造 | 全部改为共享层外链 + 真实数据渲染；每页内联 `<style>` 已清零 |
| 契约测试 | `tests/test_websrc_contract.py` **18 项**（路由 / slug / 共享层 / 纪律 / node 语法 / 冒烟） |

### 5.3 实测抓到的真 bug（这套机制第一次就发挥了作用）

`frames.html` 写了 `lk.over_0.6_by_layer` —— 属性名以数字开头是**非法 JS 语法**，
整块 renderer 静默不执行：导航都建不出来、页面一片空。契约测试与自查都看不出（静态正则查不出语法），
是**浏览器自检页**抓到的。修法：改方括号 `lk['over_0.6_by_layer']`。

据此补了两条防线：
1. `test_page_inline_scripts_are_syntactically_valid` + `test_shared_js_is_syntactically_valid`
   —— 用 `node --check` 对每页内联脚本与 `lg.js` 做真正的语法校验（不引入构建工具的最低成本"编译检查"）；
2. `/lab/_shared/selfcheck.html` —— 在 iframe 里真实加载 16 页，读回导航项数 / 模块声明 /
   指标卡数 / 表格行数 / 错误横幅，一屏给出 16 页体检结论。

### 5.4 未闭合缺口（如实登记，未用占位数据假装）

`/console/*` 是**聚合口径**（T2 的设计目标就是"只读读数"）。有两页的原设计与它不匹配：

| 页面 | 原设计想要 | API 实际提供 | 本次处理 |
|---|---|---|---|
| reconstruction-arena | 对阵记录明细 + 候选正文并排对照 | 只有计数与分布（by_model / by_status / by_temperature / tokens_total） | 改为聚合视图（模型来源 / 扰动类型 / 状态 / 温度 / 提示词版本五张分布卡），页面内写明"对照在盲评台、明细接口未开放"，**不做假明细** |
| semantic-lab | Frame 清单 + 粒度/状态筛选器 | 只有 by_granularity / by_status / by_extractor + leakage_scores 分层 | 改为分布视图（粒度 / 状态 / 模型 / 泄漏层 / 超阈值层），筛选器移除 |

**另**：`test_console_each_module_shapes` 用的是 `need <= set(data)` **子集断言**，
意味着后端 console API **加字段不会破坏测试**。若后续要让上面两页恢复原设计，
扩 API 是安全路径（仍受"只读 + 不泄密钥"约束）—— 这是一个**待集霸拍板**的范围决策，
本次未擅自扩大边界。

### 5.5 实测数据契约（16 模块，供后续改动对照）

```
dashboard           12 标量 + experiments{total,by_status} + best_benchmark_model{model,avg_accuracy}
corpus              works/segments/chars_total/text_cleaned + integrity{checked,src_ok,src_bad,unchecked}
                    + roles{} + seg_versions{} + recent_works[{id,title,author,segments}]
semantic-lab        frames/segments_covered/propositions + by_granularity{}/by_status{}/by_extractor{}
                    + leakage_scores{total,by_layer{},over_0.6_by_layer{}}   ← 键含点，须用方括号访问
reconstruction-arena candidates/by_status{}/by_model{}(含 corrupt:* 前缀)/by_prompt_version{}
                    /by_temperature{}/experiments_with_recon/tokens_total
expression-residual  residuals_det/residuals_sem + by_status{}/by_model{}/sem_payload_keys{}
                    + subtext_preserved{n,mean}
strategy-atlas      strategies/cluster_items_total/by_method{} + top_by_size[{id,name,n_items,success_rate,wilson,method,version}]
judge-arena         judge_runs/by_kind{}/by_status{}/abstain_total + leaderboard[{model,prompt_version,calls,abstain_rate,failed_rate}] + known_bias
preference-lab      review_items/by_status{pending,done}/winner_resolved{batch...}/batches{}/gate
hard-cases          hard_cases/by_kind{}/need_human/need_ontology/need_strategy + top_by_severity[{id,kind,severity,why,need_human,experiment_id}]
benchmarks          sets[{id,name,kind,version,n_items,created_at}]/items/runs + leaderboard[{model,avg_accuracy,best,runs}] + latest_by_type{}/hidden_rule
experiments         total/by_status{}/reports + recent[{id,name,status,n_segments,created_at,updated_at,error}]
models              registry{updated,note,roles{}}/llm_calls_total/serial_discipline + usage_all_time[{model,calls,failed,failed_rate,tokens_in,tokens_out}]
training-data       export_files[{file,bytes}] + corruptions{total,by_status{}}/negative_pool_eligible/sft_note/benchmark_isolation
workflow            workflows[{id,name,steps,engine}] + jobs{by_kind{stage:{completed,pending}},total,pending,failed} + engine_stages[]
observability       window_hours + snapshot{n,ok,failed,success_rate,tokens_total,latency_ms{n,p50,p95,max},
                    by_model{}/by_purpose{}/by_status{}/by_hour[list]/errors_top{items[{error,n,purposes,models}]},n_full_table}
settings            llm_mode/database_backend/data_dir/readonly/secrets_policy + 5 组模型与版本白名单 + modules[]
```

### 5.6 验收结果

| 检查 | 结果 |
|---|---|
| 全量 pytest | **405 项全绿，零失败**（含既有 251+ 与本次新增 18 项） |
| 契约测试 `test_websrc_contract.py` | 18/18 通过 |
| 16 页路由冒烟 | 16/16 → 200 + `no-store` |
| 浏览器运行时自检 | **16/16 通过**（导航完整 / 模块声明正确 / 无错误横幅） |
| 目录穿越与未知页 | `/lab/bogus.html`、`/lab/%2e%2e%2fapp%2fapi.py` 均 404 |
| 旧外壳降级 | `/console/ui` → 302 `/lab/overview.html` |
| 双主题截图 | 深色（overview / judges）、浅色（benchmark）已肉眼核对，金棕体系与盲评台同源 |
| 盲评台未受影响 | 不变量测试原样全绿；`/` 正常；仅新增一个入口链接 |

截图存于 `data/_dbg/lab-shots/`（overview-dark / judges-dark / benchmark-dark / benchmark-light）。
