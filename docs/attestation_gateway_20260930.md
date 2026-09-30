# K2 可信证明网关（lg-k2-attestation-gateway）设计说明 — 2026-09-30

模块：`tools/attestation_gateway.py`　测试：`tests/test_attestation_gateway.py`

## 0. 这块补齐的是什么断点

K2 语义审查的**消费侧**是 `app/semantic_review_runner.py`。它在
`_post_once`（`semantic_review_runner.py:176-208` 一带）里只*读取*四个证明头
`x-lg-upstream-provider / -model / -channel-id / -request-id`，并在
`_parse_response`（`semantic_review_runner.py:189-238`）里把它们与
「**预先核准路由**」和「**上游响应体的 `id` / `model`**」三方对账：

```
headers == {provider: route.upstream_provider,
            model:    route.upstream_model,
            channel-id: route.upstream_channel_id,
            request-id: data["id"]}          # request-id 必须恒等响应体 id
且 data["model"] == route.upstream_model     # 体 model 必须等于核准路由 model
且 data["id"] 是非空字符串
```

任一条不满足 ⇒ `ReviewResponseError("k2_response_unverifiable")`（
`semantic_review_runner.py:236`）⇒ **即使正文写 PASS 也不生成语义票**。

改造前全仓 `x-lg-upstream` 只有消费侧命中、无任何模块*生成*这些头——这正是收据链
`calls/votes/links` 长期为 0、`GATE_K2_evidence` 一直 FAIL 的最后一环。本任务只做
**生成侧**：一个最小、可审计的 OpenAI 兼容网关，把请求原样转发到单条核准上游、按上游
响应体生成证明头、并逐条落 append-only 审计。**不写真实库、不调真实模型、不改判据。**

## 1. 数据流

```
semantic_review_runner._post_once  --(POST /chat/completions, Bearer, identity)-->
        tools/attestation_gateway  (stdlib ThreadingHTTPServer + attest())
                 |  forward(request_body)  # 原样字节，经 httpx 打到核准上游
                 v
        单条预先核准上游路由 (base_url/api_key + provider/model/channel_id 由环境变量给出)
                 |  UpstreamReply(status, body)  # 上游响应体原样回传
                 v
        attest(): 捕获 body 的 id/model → 生成 4 个头 → 写 1 条审计（发放/拒发）
```

`attest()` 是纯函数（`request_body, route, forward, audit` 全注入），便于零网络单测；
生产入口 `main()` 用 `make_httpx_forwarder()` 作转发器、`route_from_env()` 读核准路由。

## 2. 发放 / 拒发判据（与消费侧对齐）

**只有全部满足才发放四个头**（`_decide`）：

1. 上游 HTTP 状态 == 200；
2. 上游响应体是 JSON 对象；
3. 体含非空字符串 `id`；
4. 体含字符串 `model`；
5. 体 `model` == 核准路由 `model`。

发放时四个头由**本网关生成**（不是转手上游的头）：

| 头 | 值来源 | 硬约束 |
|----|--------|--------|
| `x-lg-upstream-provider` | 核准路由 `provider` | 与路由一致 |
| `x-lg-upstream-model` | **捕获自上游响应体 `model`** | 恒等体 `model`，且 == 路由 `model` |
| `x-lg-upstream-channel-id` | 核准路由 `channel_id` | 与路由一致 |
| `x-lg-upstream-request-id` | **捕获自上游响应体 `id`** | 恒等体 `id` |

任一不满足 ⇒ **零证明头**、统一返回 **502**、审计记「拒发」（`denied`），并带
`deny_reason`（`upstream_status_*` / `upstream_missing_id` / `upstream_missing_model` /
`upstream_model_route_mismatch` / `upstream_body_not_json` / `upstream_body_not_object`）。
消费侧据此在 `_parse_response` 先撞 `k2_gateway_http_502`，拿不到任何可核验证明。

> 注意"拒发回 502"与"上游本就 5xx"：网关把**任何**拒发情形（含上游 5xx）统一映射为
> 502，绝不把上游错误连同证明头透传——透传即伪造资格。

## 3. 信任模型：为什么这四个头是「可审计」的

**谁能生成证明**：只有持有下列三项的进程——(a) 单条核准上游路由（`base_url` +
`api_key`），(b) 该路由的**身份三元组**（`provider`/`model`/`channel_id`），(c) 审计
落盘权。三者由环境变量注入、部署方独占，模型/上游**无法**凭空造出与本地路由身份 +
审计账本三方自洽的头。

**头与账本的绑定关系**（可审计性的核心）：`attest()` 对每次调用都先
`forward(request_body)` 拿到**上游原始响应体字节**，再
`response_sha256 = SHA256(上游体字节)`，并把 `{decision, route{provider,model,
channel_id}, upstream_id, upstream_model, request_sha256, response_sha256, at}`
追加进 append-only JSONL。于是：

- 发放头的 `request-id` **恒等于**上游体 `id`，`model` **恒等于**上游体 `model`——
  头不是自由文本，是上游体的**投影**；
- 审计行的 `response_sha256` 锁死了"哪一字节序列被当作上游体"，
  `upstream_id`/`upstream_model` 锁死了从中捕获的身份；
- 消费侧收据（`semantic_review_calls`）落的是 `upstream_request_id`（=头的
  request-id=体 id）与 `response_sha256`。审计者拿收据的
  `(response_sha256, upstream_request_id)` 去账本里找同键的 `issued` 行即可复核：
  **这条证明确由核准路由、在一次真实上游往返后签发**，而非事后补写。

**append-only 保证**：审计文件以 `"ab"`（二进制追加）打开，只 `fsync` 追加、
绝不 `truncate` / `seek` 回写历史行（`AuditLog.record`）。测试
`test_audit_is_append_only` 连发两次后断言行数=2 且**首行逐字节未变**。

**它不证明什么**（诚实边界）：账本证明"网关在该路由上看到该上游体并据此签发"；它不
独立证明上游体内容语义正确——语义正确性仍由 `verdict/cited_instance_ids/concerns`
的判据在 `_parse_response` 里把关（本模块不碰判据）。若上游本身撒谎，那是上游信任域
的事，非本证明层可覆盖。

## 4. TLS 落地与证书信任锚（不允许关校验）

消费侧 `_post_once` 用 `httpx.Client(timeout=..., follow_redirects=False)`，**校验走
httpx 默认 `verify=True`**（`semantic_review_runner.py:147-148`），且本任务**禁止**改
runner 去传 `verify=False` 或自定义 `verify=` 路径。约束与落地：

1. **生产必须 https**。`review_snapshot` 有来源硬门
   （`semantic_review_runner.py:435-442`）：scheme 非 https 时**仅**放行
   `localhost/127.0.0.1/::1` 回环。真实部署 `LG_GATEWAY_BASE_URL` 必是 https。
   （本仓测试走回环 http + 直接调 `_post_once`，属合成测试，不代表生产放宽。）

2. **自签 ≠ 关校验**。httpx 默认 `verify=True` 用 `certifi` 内置 CA 束建
   `ssl` 默认上下文，**不读** `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`，也不接受
   裸自签叶子证书。正确做法是**建一个内网 CA**，用该 CA 给网关签服务端证书
   （SAN 含监听主机名），再把**该 CA 根证书**装进运行 venv 所信任的锚集里——即
   httpx 实际使用的那份 CA bundle（`python -c "import certifi;print(certifi.where())"`
   定位；生产镜像内以运维方式把内网 CA 追加进 bundle，或用打包进镜像的私有
   `certifi` 根）。校验链完整即放行；**任何情况下都不得 `verify=False`**。

3. **主机名匹配**：`base_url` 的 host 必须落在证书 SAN 内，否则 httpx 校验失败——
   这是**预期的 fail-closed**，不是要绕开的障碍。

4. **凭据边界**：上游 `api_key` 只出现在网关→上游的转发头
   `Authorization: Bearer`（`make_httpx_forwarder`），**绝不**出现在证明头、审计记录
   或对消费侧的回包中（测试 `test_normal_attestation_headers_match_upstream_body`
   断言审计 JSON 里不含 `api_key`）。消费侧→网关的 `Bearer` 由部署方在网关侧策略另行
   校验，本最小网关对入站凭据不做额外鉴权（边界见 §6）。

## 5. 审计 JSONL 字段

每行一条 JSON（`sort_keys`，UTF-8，行尾 `\n`）：

| 字段 | 含义 |
|------|------|
| `at` | UTC ISO8601（微秒，`Z` 结尾） |
| `decision` | `issued`（发放）/ `denied`（拒发） |
| `route` | `{provider, model, channel_id}`（核准路由身份，**不含** api_key/base_url） |
| `upstream_id` | 捕获的上游体 `id`（拒发缺体身份时为 `null`） |
| `upstream_model` | 捕获的上游体 `model` |
| `request_sha256` | 转发请求体的 SHA256 |
| `response_sha256` | 上游响应体字节的 SHA256（发放=回传体；拒发=上游原体） |
| `deny_reason` | 仅拒发时出现，见 §2 |

## 6. 环境变量与运行（生产入口，本任务不实跑）

```
LG_ATTEST_UPSTREAM_BASE_URL   单条核准上游 base_url（拼 /chat/completions）
LG_ATTEST_UPSTREAM_API_KEY    上游密钥（仅入转发头，绝不落审计）
LG_ATTEST_ROUTE_PROVIDER      路由身份：provider
LG_ATTEST_ROUTE_MODEL         路由身份：核准 model（体 model 必须等于它才发放）
LG_ATTEST_ROUTE_CHANNEL_ID    路由身份：channel_id
LG_ATTEST_AUDIT_PATH          append-only JSONL 落盘路径
LG_ATTEST_LISTEN_HOST/PORT    监听地址（默认 127.0.0.1:8080）
```

`route_from_env` 缺任一必需项即 `RuntimeError`（fail-closed，绝不以默认路由静默放行）。
运行：`python -m tools.attestation_gateway`（读环境变量起 `serve_forever`）。

## 7. 边界（越界即判红）

- **不写真实库**：本模块与测试全程不触碰
  `data/language_genome.db`；端到端测试只调 `_post_once` + `_parse_response`，
  不走 `review_snapshot` 的 DB 事务/ACL 探针。
- **不调真实模型**：转发目标与"上游响应体"均由**合成上游**（`tests/` 内 stdlib HTTP
  服务）产生，零外呼。
- **不改判据**：`app/semantic_review_runner.py` 一行未动；本模块只按其既有契约
  生成它能核验的头。
- **不 push / 不合并 / 不改主仓工作区**。

## 8. 测试如何逐条对齐任务书

| 任务书必做 | 测试 |
|------------|------|
| 正常路径：四头与体 id/model 一致 | `test_normal_attestation_headers_match_upstream_body` |
| 缺 id / model 不符 / 上游 5xx ⇒ 无头+502+记拒发 | `test_denial_paths_return_502_without_headers`（参数化含缺 id/缺 model/model 不符/5xx/非 JSON） |
| append-only：两次⇒2 行且首行逐字节未变 | `test_audit_is_append_only` |
| 端到端：起网关后 runner 产出合格 review | `test_end_to_end_runner_produces_qualifying_review` |
| 反向自检：少发一个头 ⇒ `ReviewResponseError` | `test_reverse_selfcheck_missing_header_rejected_by_runner`（核心） |
| 拒发透传到消费侧 | `test_denial_upstream_end_to_end_runner_sees_502_no_headers` |

真实运行输出（rc、反向自检原文）见 `DELIVERY_k2_attestation_gateway.md`。
