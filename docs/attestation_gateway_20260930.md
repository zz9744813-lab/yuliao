# K2 可信证明网关（lg-k2-attestation-gateway）设计说明 — 2026-09-30（加固版）

模块：`tools/attestation_gateway.py`　测试：`tests/test_attestation_gateway.py`
本版依据会审双席（glm-5.3 PASS / qwen3.8-flash BLOCK，原始记录
`F:\Hermes\team\reviews\language-genome-b0a394b686.md`）逐条加固；**§10 是逐条对账表**
（改了什么 / 为什么不改 / 用例名）。

## 0. 这块补齐的是什么断点

K2 语义审查的**消费侧**是 `app/semantic_review_runner.py`。它在
`_post_once`（`semantic_review_runner.py:144-186` 一带）里只*读取*四个证明头
`x-lg-upstream-provider / -model / -channel-id / -request-id`，并在
`_parse_response`（`semantic_review_runner.py:189-237`）里把它们与
「**预先核准路由**」和「**上游响应体的 `id` / `model`**」三方对账：

```
headers == {provider: route.upstream_provider,
            model:    route.upstream_model,
            channel-id: route.upstream_channel_id,
            request-id: data["id"]}          # request-id 必须恒等响应体 id
且 data["model"] == route.upstream_model     # 体 model 必须等于核准路由 model
且 data["id"] 是非空字符串
```

任一条不满足 ⇒ `ReviewResponseError("k2_response_unverifiable")`
（`semantic_review_runner.py:236`）⇒ **即使正文写 PASS 也不生成语义票**。

改造前全仓 `x-lg-upstream` 只有消费侧命中、无任何模块*生成*这些头——这正是收据链
`calls/votes/links` 长期为 0、`GATE_K2_evidence` 一直 FAIL 的最后一环。本任务只做
**生成侧**：一个最小、可审计的 OpenAI 兼容网关，把请求原样转发到单条核准上游、按上游
响应体生成证明头、并逐条落 append-only 审计。**不写真实库、不调真实模型、不改判据。**

> 行号锚点已按本仓库当前 `b0a394b` 重新核过（`_post_once` 144、httpx.Client 147、
> `_parse_response` 189、抛点 236、`MAX_RESPONSE_BYTES` 45、来源硬门 435-442）。

## 1. 数据流

```
semantic_review_runner._post_once  --(POST /chat/completions, Bearer, identity)-->
        tools/attestation_gateway  (stdlib ThreadingHTTPServer + attest())
                 |  ① 入站鉴权（Bearer，配置了令牌时）② 协议硬门（Content-Length 等）
                 |  forward(request_body)  # 原样字节，经 httpx 打到核准上游
                 v
        单条预先核准上游路由 (base_url/api_key + provider/model/channel_id 由环境变量给出)
                 |  UpstreamReply(status, body, body_complete)  # 受上限约束，body 原样回传
                 v
        attest(): 捕获 body 的 id/model → 字符白名单校验 → 生成 4 个头 → 写 1 条审计
```

`attest()` 是纯函数（`request_body, route, forward, audit` 全注入），便于零网络单测；
生产入口 `main()` 用 `make_httpx_forwarder()` 作转发器、`route_from_env()` 读核准路由。

## 2. 发放 / 拒发判据（与消费侧对齐）

**只有全部满足才发放四个头**（`_decide`，`tools/attestation_gateway.py:294-325`）：

1. 上游 HTTP 状态 == 200；
2. 上游响应体**完整读到**（未因超 `MAX_UPSTREAM_RESPONSE_BYTES` 截断）；
3. 上游响应体是 JSON 对象；
4. 体含非空字符串 `id`；
5. 体 `id` 过证明头字符白名单 `^[A-Za-z0-9._:-]{1,200}$`；
6. 体含非空字符串 `model`；
7. 体 `model` 过同一白名单；
8. 体 `model` == 核准路由 `model`。

**deny_reason 取第一个命中的**（优先级即上表顺序）。白名单排在"回写响应头"之前、
且排在 model 相等之前：脏 `id` 即便 model 正确也绝不出话。

发放时四个头由**本网关生成**（不是转手上游的头）：

| 头 | 值来源 | 硬约束 |
|----|--------|--------|
| `x-lg-upstream-provider` | 核准路由 `provider` | 与路由一致，且过白名单 |
| `x-lg-upstream-model` | **捕获自上游响应体 `model`** | 恒等体 `model`，== 路由 `model`，且过白名单 |
| `x-lg-upstream-channel-id` | 核准路由 `channel_id` | 与路由一致，且过白名单 |
| `x-lg-upstream-request-id` | **捕获自上游响应体 `id`** | 恒等体 `id`，且过白名单 |

`http.server.send_header` **不**清洗值中的 `\r\n`（会被写成响应头注入/响应拆分），
所以白名单是这一层的硬门，不是"锦上添花"。字符集刻意只放行
`A-Za-z0-9 . _ : -`（`:`/`-` 是既有 `model`/`id` 形态所需），空格、制表、非 ASCII、
控制字符、>200 字符一律**拒发**（不生成任何证明头、回 502、审计记拒发）。

**完整 deny_reason 清单**（新增项加粗）：

| deny_reason | 触发条件 | 对外状态 |
|-------------|---------|---------|
| `upstream_status_<code>` | 上游非 200 | 502 |
| **`upstream_response_too_large`** | **上游体超 512KiB、未完整读到** | **502** |
| `upstream_body_not_json` | 体不是合法 UTF-8 JSON | 502 |
| `upstream_body_not_object` | 合法 JSON 但不是对象（`[]`/`"s"`/`null`/数字…） | 502 |
| `upstream_missing_id` | `id` 缺失/非字符串/全空白 | 502 |
| **`upstream_id_invalid_chars`** | **`id` 过不了字符白名单（CR/LF/空格/非 ASCII/超长）** | **502** |
| `upstream_missing_model` | `model` 缺失/非字符串/全空白 | 502 |
| **`upstream_model_invalid_chars`** | **`model` 过不了字符白名单** | **502** |
| `upstream_model_route_mismatch` | 体 model ≠ 核准路由 model | 502 |
| **`route_identity_invalid_chars`** | **路由 provider/model/channel_id 自身过不了白名单（转发前就停）** | **502** |
| **`forward_exception`** | **转发器抛异常（超时/连接/DNS/…），`detail` 记异常类名** | **502** |
| **`inbound_unauthorized`** | **入站 `Authorization: Bearer` 不匹配** | **401** |
| **`path_not_found`** | **POST 路径不是 `/chat/completions`** | **404** |
| **`transfer_encoding_unsupported`** | **请求带 `Transfer-Encoding`（含 chunked）** | **400** |
| **`content_length_missing`** | **无 `Content-Length`** | **400** |
| **`content_length_invalid`** | **`Content-Length` 非纯十进制/负数/多个/逗号并列** | **400** |
| **`request_too_large`** | **`Content-Length` > `MAX_FORWARD_BYTES`(8MiB)** | **413** |
| **`request_body_truncated`** | **声明长度与实读不符（客户端提前断）** | **400** |
| **`request_body_read_timeout`** | **读请求体超时（socket 30s）** | **408** |
| **`request_body_read_error`** | **读请求体其它 I/O 错误** | **400** |

> 注意"拒发回 502"与"上游本就 5xx"：网关把**任何**拒发情形（含上游 5xx）统一映射为
> 502，绝不把上游错误连同证明头透传——透传即伪造资格。
> 协议层/鉴权层的 400/401/404/408/413 **不**是 502：它们没到上游、没转发，回 502 会
> 让消费侧把"入站协议错"误读成"上游坏了"。

## 3. 信任模型：为什么这四个头是「可审计」的

**谁能生成证明**：只有同时持有下列四项的进程——(a) 单条核准上游路由（`base_url` +
`api_key`），(b) 该路由的**身份三元组**（`provider`/`model`/`channel_id`），(c) 审计
落盘权，(d) **入站令牌**（非回环监听时，见 §4）。四者由环境变量注入、部署方独占，
模型/上游**无法**凭空造出与本地路由身份 + 审计账本三方自洽的头。

**头与账本的绑定关系**（可审计性的核心）：`attest()` 对每次调用都先
`forward(request_body)` 拿到**上游原始响应体字节**，再
`response_sha256 = SHA256(上游体字节)`，并把
`{seq, at, decision, route{provider,model,channel_id}, upstream_id, upstream_model,
request_sha256, response_sha256[, deny_reason][, detail]}`
追加进 append-only JSONL。于是：

- 发放头的 `request-id` **恒等于**上游体 `id`，`model` **恒等于**上游体 `model`——
  头不是自由文本，是上游体的**投影**；
- 审计行的 `response_sha256` 锁死了"哪一字节序列被当作上游体"，
  `upstream_id`/`upstream_model` 锁死了从中捕获的身份；
- **消费侧收据（`semantic_review_calls`）落的是 `upstream_request_id`（=头的
  request-id=体 id）与 `response_sha256`**，据此回账本复核（判定方法见下）。

### 3.1 审计唯一键与「一次真实往返一行」的判定方法

**唯一键是 `seq`**（不是 `(response_sha256, upstream_request_id)`——同一上游体被重复
观察到时那对字段会重复出现，那不是重复记账，是真的发生了 N 次往返）。

`seq` 的三条性质（`AuditLog._reserve_seq`，`tools/attestation_gateway.py:263-292`）：

1. **进程内不重号**：`threading.Lock` + 模块级**按路径共享**的锁（多实例写同一账本
   也串行）；
2. **跨进程不重号**：`os` 级 advisory 文件锁（Windows `msvcrt.locking` / POSIX
   `fcntl.flock`）覆盖"扫账本尾定号 + 追加落盘"整个临界区；
3. **重启后不回退**：每次追加前从账本尾部读出已存在的最大 `seq` 再 +1（append-only ⇒
   历史行不变 ⇒ 只需扫上次记账之后新追加的字节）。

`seq` 与 `request_sha256` 一起把「这一次入站请求 ↔ 这一组证明头 ↔ 这一条账本行」
绑成一对一（`test_issued_row_binds_seq_request_and_proof_headers`）。

**判定方法（复核一张收据是不是真往返后签的）**：

1. 取收据的 `(upstream_request_id, response_sha256)`，在账本里筛出
   `decision == "issued" and upstream_id == upstream_request_id and
   response_sha256 == <收据的值>` 的全部行，按 `seq` 升序，记为 `R`；
2. **`|R| == 0` ⇒ 判红**：这张收据没有对应的真实上游往返（伪造或账本被截断）；
3. **`|R| == N` ⇒ 恰好发生过 N 次真实往返**（一次往返一行，`seq` 不重号 ⇒ 不存在
   "同一次往返被记了两行"）；
4. 按时间单调配对：收据 `at = T` 的那张，必须匹配 `R` 中**尚未被更早的同键收据占用**
   的、`at <= T` 的行（K 4 对 K 1 的顺序匹配）。首行 `at` 晚于 `T` ⇒ 该收据是补写/
   重放 ⇒ 判红；
5. 头侧可再复核一次：四头的取值必须等于该行 `route` / `upstream_model` /
   `upstream_id`（谁给哪个字段供值是机械可对的，见 `test_issued_row_binds_seq_
   request_and_proof_headers`）。

**读取侧口径**：`write` 与 `flush` 之间被硬 kill 会留下**无换行的半行 JSONL**。本模块
的处理是：下一条追加前**只在文件尾补一个换行**把半行封口（不改写任何既有字节，
`test_audit_heals_torn_last_line_without_rewriting_history`），因此账本从下一行起恢复
逐行可解析；**但那条半行永久留在文件里**，复核脚本必须**跳过不可解析行**。加固前
落下的历史行**没有 `seq` 字段**，无法参与上述配对，需人工确认或另行迁移。

**append-only 保证**：审计文件以 `"a+b"`（`O_APPEND` 只追加 + 可读）打开，只 `fsync`
追加、绝不 `truncate` / 回写历史行（seek 只用于读账本尾）。测试
`test_audit_is_append_only` 连发两次后断言行数=2 且**首行逐字节未变**。

**它不证明什么**（诚实边界）：账本证明"网关在该路由上看到该上游体并据此签发"；它不
独立证明上游体内容语义正确——语义正确性仍由 `verdict/cited_instance_ids/concerns`
的判据在 `_parse_response` 里把关（本模块不碰判据）。若上游本身撒谎，那是上游信任域
的事，非本证明层可覆盖。同样地，`issued` **不等于**"消费侧已接受"：消费侧还有
512KiB 硬上限与自身超时/网络失败分类（§9），那属于消费侧口径，不在账本。

## 4. 入站鉴权与监听约束（fail-closed）

默认 `127.0.0.1` 只是**缓解**，不是鉴权：任何能触达监听端口的进程都能借网关手里的
上游 `api_key` 免费出话、并对任意正文拿到合法证明头（开放代理 / confused deputy）。
故本版把约束写进代码而不只写进文档：

| 监听主机 | 入站令牌 | 行为 |
|---------|---------|------|
| 回环（`127.0.0.0/8`、`::1`、字面量 `localhost`） | 未配 | 允许（只放行回环，不出机器） |
| 回环 | 已配 | **每次 POST 校验 `Authorization: Bearer <token>`** |
| **非回环** | **未配** | **拒绝启动**：`GatewayStartupError` ⇒ `main()` 退出码 2 + 单行原因 |
| 非回环 | 已配 | 允许启动，且**校验** Bearer |

- 不匹配/缺头/scheme 不对 ⇒ **401 + `WWW-Authenticate: Bearer`，零证明头，绝不转发**，
  并留一行 `denied`（`deny_reason=inbound_unauthorized`）。
- 比对用 `secrets.compare_digest`（恒定时间）。
- 回环判定只认**字面量**（`is_loopback_host`）：不解析 DNS，因此 `localhost.localdomain`
  这类**无法证明是回环**的名字一律按非回环处理（fail-closed）。`::`（全零地址）**不是**
  回环。
- 密钥与令牌都**刻意不 strip**（`api_key` 显式保留首尾空白，否则上游 401 且难定位；
  判定"空"用 `strip()` 只作存在性检查）。

## 5. TLS 落地与证书信任锚（不允许关校验）

消费侧 `_post_once` 用 `httpx.Client(timeout=..., follow_redirects=False)`，**校验走
httpx 默认 `verify=True`**（`semantic_review_runner.py:147-148`），且本任务**禁止**改
runner 去传 `verify=False` 或自定义 `verify=` 路径。约束与落地：

1. **生产必须 https**。`review_snapshot` 有来源硬门
   （`semantic_review_runner.py:435-442`）：scheme 非 https 时**仅**放行
   `localhost/127.0.0.1/::1` 回环。真实部署 `LG_GATEWAY_BASE_URL` 必是 https。
   （本仓测试走回环 http + 直接调 `_post_once`，属合成测试，不代表生产放宽。）

   **生成侧镜像同一把锁**（`_check_base_url`，`tools/attestation_gateway.py:628-649`）：
   scheme 必须是 http/https、禁 userinfo（否则 `api_key` 会随 URL 进日志/异常）、
   必须有 hostname、禁 query/fragment，且**非回环 host 只许 https**——生产静默以
   `http://` 跑、把 `api_key` 明文送上网络的路被封死在启动期。

2. **自签 ≠ 关校验**。httpx 默认 `verify=True` 用 `certifi` 内置 CA 束建
   `ssl` 默认上下文，**不读** `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`，也不接受
   裸自签叶子证书。正确做法是**建一个内网 CA**，用该 CA 给网关签服务端证书
   （SAN 含监听主机名），再把**该 CA 根证书**装进运行 venv 所信任的锚集里——即
   httpx 实际使用的那份 CA bundle（`python -c "import certifi;print(certifi.where())"`
   定位；生产镜像内以运维方式把内网 CA 追加进 bundle，或用打包进镜像的私有
   `certifi` 根）。校验链完整即放行；**任何情况下都不得 `verify=False`**。

3. **主机名匹配**：`base_url` 的 host 必须落在证书 SAN 内，否则 httpx 校验失败——
   这是**预期的 fail-closed**，不是要绕开的障碍。

4. **凭据边界**：
   - 上游 `api_key` 只出现在网关→上游的转发头 `Authorization: Bearer`
     （`make_httpx_forwarder`），**绝不**出现在证明头、审计记录
     或对消费侧的回包中（`test_api_key_never_leaves_gateway` 遍历发放/5 类拒发/转发
     异常全路径断言回包与账本都不含密钥；`test_normal_attestation_headers_match_
     upstream_body` 断言审计 JSON 里不含 `api_key`）。
   - `Route.api_key` 用 `field(repr=False)`：**repr 会被异常/日志/traceback 局部变量与
     pytest 断言展开带出去**，刻意脱敏的 `identity()` 会被 repr 绕过
     （`test_route_repr_hides_api_key`）。
   - 转发器**不跟随重定向**（`follow_redirects=False`）：绝不带着 `Authorization`
     跟着 302 跳去别处（`test_httpx_forwarder_sends_bearer_and_identity`）。
   - 消费侧→网关的凭据即 §4 的入站令牌。

## 6. 审计 JSONL 字段

每行一条 JSON（`sort_keys`，UTF-8，行尾 `\n`）：

| 字段 | 含义 |
|------|------|
| `seq` | **单调序号**（进程内/跨进程/重启后都不重号不回退），见 §3.1 |
| `at` | UTC ISO8601（微秒，`Z` 结尾） |
| `decision` | `issued`（发放）/ `denied`（拒发） |
| `route` | `{provider, model, channel_id}`（核准路由身份，**不含** api_key/base_url） |
| `upstream_id` | 捕获的上游体 `id`（非字符串/未捕获时为 `null`） |
| `upstream_model` | 捕获的上游体 `model` |
| `request_sha256` | 入站请求体的 SHA256；协议层拒发记**实际读到的字节**（鉴权/路径/长度类多为 `SHA256("")`） |
| `response_sha256` | 上游响应体字节的 SHA256；**没拿到完整上游体时为 `null`**（未转发、状态非 200 之外的截断等） |
| `deny_reason` | 仅拒发时出现，见 §2 |
| `detail` | 仅部分拒发出现：转发异常=异常**类名**（不记消息，消息里可能带 URL/凭据片段）；路由身份非法=非法字段名列表 |

## 7. 环境变量与运行

```
LG_ATTEST_UPSTREAM_BASE_URL   单条核准上游 base_url（拼 /chat/completions）；https 或回环 http
LG_ATTEST_UPSTREAM_API_KEY    上游密钥（原样使用，不 strip；仅入转发头，绝不落审计）
LG_ATTEST_ROUTE_PROVIDER      路由身份：provider（须过证明头字符白名单）
LG_ATTEST_ROUTE_MODEL         路由身份：核准 model（体 model 必须等于它才发放）
LG_ATTEST_ROUTE_CHANNEL_ID    路由身份：channel_id（须过白名单）
LG_ATTEST_AUDIT_PATH          append-only JSONL 落盘路径
LG_ATTEST_INBOUND_TOKEN       入站 Bearer 令牌；**非回环监听时必填**，否则拒启动
LG_ATTEST_LISTEN_HOST/PORT    监听地址（默认 127.0.0.1:8080；端口非法 ⇒ 退出码 2）
```

`route_from_env` / `listen_from_env` / `make_server` 的任一校验不通过 ⇒
`GatewayStartupError`（`RuntimeError` 子类），`main()` 打印**单行**
`attestation_gateway_startup_failed:<原因>` 到 stderr 并返回 **2**——绝不裸 traceback
（与 `route_from_env` 原有的 fail-closed 风格一致）。运行：
`python -m tools.attestation_gateway`（读环境变量起 `serve_forever`）。

## 8. 边界（越界即判红）

- **不写真实库**：本模块与测试全程不触碰
  `data/language_genome.db`；端到端测试只调 `_post_once` + `_parse_response`，
  不走 `review_snapshot` 的 DB 事务/ACL 探针。
- **不调真实模型**：转发目标与"上游响应体"均由**合成上游**（`tests/` 内 stdlib HTTP
  服务）产生，零外呼。
- **不改判据**：`app/semantic_review_runner.py` 一行未动；本模块只按其既有契约
  生成它能核验的头。
- **不 push / 不合并 / 不改主仓工作区**。
- **不起常驻服务**：`main()` 的常驻路径未在测试里真跑（成功路径用假 server 走完
  生命周期，见 `test_main_success_path_serves_and_closes`）。

## 9. 两侧口径的已知差异（如实记录）

1. **请求体上限不同**：`MAX_FORWARD_BYTES = 8MiB`（网关侧防御性护栏）vs 消费侧
   **不限制**请求体。刻意不改：K2 审查请求体是冻结证据 dossier，装 8MiB 很正常，
   而消费侧没有请求侧上限，收紧到 512KiB 会凭空拒掉合法审查。这是唯一"有意不一致"
   的一处。
2. **响应体上限已对齐**：`MAX_UPSTREAM_RESPONSE_BYTES = 512KiB` ==
   `semantic_review_runner.MAX_RESPONSE_BYTES`。超限即**拒发**（`upstream_response_
   too_large`）且**流式截断不缓冲超限部分**（内存有界）。不改的后果是"账本记
   `issued`、收据根本不存在"（消费侧硬拒收）⇒ 复核按 `(response_sha256,
   upstream_request_id)` 找 `issued` 行时永远找不到对应收据。
3. **502 的分类后果（未改，任务书要求 502）**：转发器网络故障（超时/连接错）映射为
   502，消费侧因此抛 `ReviewResponseError("k2_gateway_http_502")`，而**网络层**失败
   在消费侧走的是可重试的 `ReviewOutcomeUnknown("k2_gateway_outcome_unknown")`
   （`semantic_review_runner.py:185-186`）。即：一次上游抖动在上层可能被记成"网关已
   定论"而非"结果未知、可重试"。任务书明确要求"转发器异常 ⇒ 502 + 审计拒发行"，
   故按任务书实现；**运维区分办法**：回查账本 `deny_reason=forward_exception` +
   `detail`（`ConnectTimeout`/`ConnectError`/…）即知是网络层抖动，应按可重试处理。
   该分类是否要改属消费侧/上游预算口径（仓库有 `fix/infra-fault-classify` 前例），
   **不在本模块边界内**，留待调用方裁定。
4. **deny_reason 对消费侧不可见**：消费侧对非 200 **从不读 body**
   （`semantic_review_runner.py:190-191` 直接抛 `k2_gateway_http_<status>`），所以
   §2 的 deny_reason 只出现在回包 body（给人/运维看）与账本（判定依据）里。

## 10. 会审逐条对账（glm-5.3 / qwen3.8-flash）

| # | 会审意见（原文要点） | 处理 | 用例 / 证据 |
|---|--------------------|------|------------|
| glm-1 | 转发器抛异常无捕获：handler 炸线程、连接被断、**审计零记录** | **改**：`attest()` 兜 `except Exception` ⇒ 502 + `denied/forward_exception`（`detail`=异常类名）；`do_POST` 再兜一层（异常 ⇒ 500 + stderr 单行） | `test_forward_exception_returns_502_and_audits_denial`（4 例）、`test_forward_exception_over_http_returns_502_not_dropped_connection` |
| glm-2 | 证明头值来自上游体、未禁控制字符，可注入任意头 | **改**：`HEADER_VALUE_RE` 白名单，脏值拒发（新增 `upstream_id_invalid_chars`/`upstream_model_invalid_chars`/`route_identity_invalid_chars`） | `test_proof_header_value_charset_whitelist_denies`（8 例）、`test_crlf_in_upstream_id_cannot_inject_response_headers`、`test_unsafe_route_identity_denies_before_forwarding` |
| glm-3 | 8MiB vs 512KiB 两限不一致、413 分支无测试 | **改**（响应侧）：`MAX_UPSTREAM_RESPONSE_BYTES=512KiB` 对齐消费侧并流式截断；请求侧 8MiB **有意保留**并写明理由（§9.1）；413 补测 | `test_response_cap_matches_consumer_cap`、`test_upstream_response_cap_bounds_buffer_and_marks_incomplete`、`test_oversize_upstream_response_denied_end_to_end`、`test_request_too_large_returns_413_without_forwarding` |
| glm-4 | 入站无鉴权，非回环即裸奔，建议强制回环或要求 Bearer | **改**：非回环无令牌**拒启动**；配令牌则校验 Bearer，不匹配 401 + 零证明头 + 审计行 | `test_non_loopback_listen_without_token_refuses_to_start`、`test_inbound_authorization_matrix`（13 例）、`test_inbound_token_rejects_missing_and_wrong_bearer`、`test_main_refuses_non_loopback_listen_without_token` |
| glm-5 | `api_key` 被 `.strip()` 静默改写 | **改**：不 strip，值原样使用；空值（含全空白）仍 fail-closed | `test_route_from_env_keeps_api_key_verbatim`、`test_route_from_env_blank_value_is_fail_closed` |
| glm-6 | 忽略 chunked，`length=0` 把空体原样转发还记账 | **改**：任何 `Transfer-Encoding` ⇒ 400 `transfer_encoding_unsupported`，不转发不读体 | `test_chunked_transfer_encoding_returns_400_without_forwarding`、`test_missing_content_length_returns_400_without_forwarding`、`test_invalid_content_length_returns_400_without_forwarding`（5 例，含多个/逗号/下划线 CL） |
| glm-7 | 测试内直写 `sys.__stdout__` 绕过捕获 | **改**：改用 `print()`（走 pytest 捕获，`-s` 仍见真 stdout） | `test_reverse_selfcheck_missing_header_rejected_by_runner`（`-s -k reverse` 输出见交付文档 §4） |
| glm-8 | 缺口：`route_from_env` 成功路径/`gateway_from_env`/`main()`/413/404/400/forward 异常分支 | **改**：全部补真用例 | `test_route_from_env_success_path`、`test_gateway_from_env_success_and_missing_audit`、`test_listen_from_env_success_path`、`test_main_success_path_serves_and_closes`、`test_path_not_found_returns_404_and_audits`、`test_truncated_request_body_returns_400_without_forwarding`、`test_slow_client_read_timeout_returns_408_without_forwarding` |
| qwen-1（截断段） | 转发异常 502 与消费侧 `k2_gateway_outcome_unknown` 分类不一致；`deny_reason` 只进 body 而消费侧从不读 body | **不改（分类）**，按任务书"异常⇒502"实现，并写明区分办法；文档化 body 不可见 | `docs §9.3 / §9.4`（任务书边界：消费侧分类口径不在本模块内） |
| qwen-2 | `route_from_env` 不校验 scheme、不拒 userinfo，可静默 `http://` 跑 | **改**：镜像消费侧来源硬门（§5.1） | `test_route_from_env_rejects_untrusted_base_url`（5 例）、`test_route_from_env_accepts_https_and_loopback_http` |
| qwen-3 | `Route` frozen dataclass 的 `api_key` 进默认 `__repr__` | **改**：`field(repr=False)` | `test_route_repr_hides_api_key` |
| qwen-4 | chunked / 缺 `Content-Length` ⇒ 空体转发；实读短于声明则静默截断后转发记账 | **改**（同 glm-6）+ 新增 `request_body_truncated`（实读前缀记账、不转发） | `test_chunked_...`、`test_missing_content_length_...`、`test_truncated_request_body_returns_400_without_forwarding` |
| qwen-5 | 缺口：`upstream_body_not_object`、id 空串/非字符串、413/404/400、`route_from_env` 成功路径 | **改**：全部补真用例 | `test_denial_upstream_body_not_object`（6 例）、`test_denial_upstream_id_empty_or_non_string`（7 例）、`test_request_too_large_...`、`test_path_not_found_...`、`test_route_from_env_success_path` |
| qwen-6 | `MAX_FORWARD_BYTES` 只拦入站；上游响应全量缓冲无上限 ⇒ "记 issued 却无收据" | **改**：见 glm-3 | `test_upstream_response_cap_bounds_buffer_and_marks_incomplete`、`test_oversize_upstream_response_denied_end_to_end` |
| qwen-7 | append-only 语义本身正确；但硬 kill 会留半行 JSONL、读取侧无容错；每条持全局锁 fsync 串行化 | **改**（半行）：下一次追加前只补一个换行封口、读取侧跳过不可解析行（§3.1）；全局锁**保留**：审计写入是热路径串行点，但去掉它会换来序号重号/交错两行，比慢更糟 | `test_audit_heals_torn_last_line_without_rewriting_history`（锁的必要性：`test_audit_seq_never_reused_across_processes`） |
| qwen-8 | `_reply` 发 `Connection: close` 但 404/413/400 未设 `close_connection`；handler 无 socket 读超时 | **改**：`close_connection = True` 提到 `_reply` 最前（全分支一致）；handler `timeout = 30s`（`StreamRequestHandler.setup` 生效） | `test_slow_client_read_timeout_returns_408_without_forwarding`（408 分支）、`test_path_not_found_returns_404_and_audits`（404 分支回包完整可读） |
| qwen-9 | 测试内直写 `sys.__stdout__`（同 glm-7） | **改** | 同 glm-7 |
| qwen-10 | 需补输入：`tools/` 无 `__init__.py`，若 packaging 用显式 packages 列表可能不进包 | **不改**（并已核实）：`pyproject.toml` 只有 `[project]` + `[tool.pytest.ini_options]`，**没有 `[build-system]`、没有显式 packages 列表** ⇒ 本仓是"仓库根在 `sys.path`、命名空间包导入 + `python -m tools.attestation_gateway` 直跑"用法，不存在被打包排除的问题。`test_compile_all` 也会编译该文件 | 仓库现状；`python -m pytest` 实跑通过（`tests/test_attestation_gateway.py` 与跨进程子进程两处都成功 import） |
| qwen-11 | 建议整体复核文档行号锚点 | **改**：`§0` 行号重新核准（`_post_once` 144 / `httpx.Client` 147 / `_parse_response` 189 / 抛点 236 / `MAX_RESPONSE_BYTES` 45 / 来源硬门 435-442）。**会审原文"189 应为 190"一条不成立**：`rg -n "^def _parse_response" app/semantic_review_runner.py` = **189** | 文档 §0 末注 |
| qwen-12 | 审计唯一性：`(response_sha256, upstream_request_id)` 不是唯一键，"一次往返一行"不可判定 | **改**（任务书第 3 条）：单调 `seq` + 请求侧标识绑定 + §3.1 判定方法 | `test_issued_row_binds_seq_request_and_proof_headers`、`test_audit_seq_is_monotonic_across_calls`、`test_audit_seq_continues_after_restart_without_rollback`、`test_audit_seq_is_monotonic_under_concurrency`、`test_audit_seq_never_reused_across_processes` |
| qwen-13 | 建议项：每请求新建 `httpx.Client`（无连接复用） | **改**：client 建一次跨请求复用（`httpx.Client` 官方声明线程安全），`follow_redirects=False`，`gateway.close()` 释放 | `test_httpx_forwarder_reuses_one_client`、`test_httpx_forwarder_sends_bearer_and_identity` |
| qwen-14 | 建议项：任务书第 7 条 `main()` 端口 `int()` 未捕获 `ValueError` | **改**：`listen_from_env` 捕获并给单行原因 + 退出码 2；越界也拒 | `test_listen_from_env_rejects_bad_port`（4 例）、`test_main_startup_failures_return_nonzero_without_traceback`（含 `eighty`/`70000`） |

## 11. 测试如何逐条对齐任务书

| 任务书必做 | 测试 |
|------------|------|
| 正常路径：四头与体 id/model 一致 | `test_normal_attestation_headers_match_upstream_body` |
| 缺 id / model 不符 / 上游 5xx ⇒ 无头+502+记拒发 | `test_denial_paths_return_502_without_headers`（参数化 5 例） |
| append-only：两次⇒2 行且首行逐字节未变 | `test_audit_is_append_only` |
| 端到端：起网关后 runner 产出合格 review | `test_end_to_end_runner_produces_qualifying_review` |
| 反向自检：少发一个头 ⇒ `ReviewResponseError` | `test_reverse_selfcheck_missing_header_rejected_by_runner`（核心） |
| 拒发透传到消费侧 | `test_denial_upstream_end_to_end_runner_sees_502_no_headers` |
| ① 头值白名单 | `test_proof_header_value_charset_whitelist_denies`、`test_crlf_in_upstream_id_cannot_inject_response_headers`、`test_unsafe_route_identity_denies_before_forwarding` |
| ② 入站鉴权 fail-closed | `test_non_loopback_listen_without_token_refuses_to_start`、`test_inbound_authorization_matrix`、`test_inbound_token_rejects_missing_and_wrong_bearer`、`test_main_refuses_non_loopback_listen_without_token` |
| ③ 审计单调序号 + 唯一键 | `test_issued_row_binds_seq_request_and_proof_headers`、`test_audit_seq_is_monotonic_across_calls`、`test_audit_seq_continues_after_restart_without_rollback`、`test_audit_seq_never_reused_across_processes`、`test_audit_seq_is_monotonic_under_concurrency`、`test_audit_heals_torn_last_line_without_rewriting_history` |
| ④ do_POST 全路径 try/except | `test_forward_exception_returns_502_and_audits_denial`、`test_forward_exception_over_http_returns_502_not_dropped_connection` |
| ⑤ 缺/非法 `Content-Length` ⇒ 400 | `test_missing_content_length_returns_400_without_forwarding`、`test_invalid_content_length_returns_400_without_forwarding` |
| ⑥ `api_key` 不入 repr | `test_route_repr_hides_api_key`、`test_api_key_never_leaves_gateway` |
| ⑦ `main()` 端口解析 | `test_listen_from_env_rejects_bad_port`、`test_main_startup_failures_return_nonzero_without_traceback` |
| ⑧ 会审点名的测试缺口 | `test_denial_upstream_body_not_object`、`test_denial_upstream_id_empty_or_non_string`、`test_path_not_found_returns_404_and_audits`、`test_request_too_large_returns_413_without_forwarding`、`test_truncated_request_body_returns_400_without_forwarding`、`test_route_from_env_success_path`、`test_gateway_from_env_success_and_missing_audit`、`test_main_success_path_serves_and_closes` |
| ⑨ 建议项（client 复用 / 上限口径） | `test_httpx_forwarder_reuses_one_client`、`test_response_cap_matches_consumer_cap`、`test_upstream_response_cap_bounds_buffer_and_marks_incomplete`、`test_oversize_upstream_response_denied_end_to_end` |

真实运行输出（rc、逐条 `-v` 清单、反向自检原文、启动面拒启动原文）见
`DELIVERY_k2_gateway_harden.md`。
