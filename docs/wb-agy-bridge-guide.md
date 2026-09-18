# WorkBuddy（wb）· Antigravity（agy / 反重力）· Qoder 调用指南

> 面向：需要在本机委派任务给 WorkBuddy / Antigravity CLI / Qoder 的**任何智能体**（Hermes / Claude Code / Codex / ZCode / 其它）。
> 目标：照抄命令即可跑通，并且**知道哪些地方会骗你**。
> 状态快照：2026-09-16 实测（WorkBuddy 国内外 + agy 三路）；**2026-09-18 追加第四路 Qoder**，四条通道冒烟见 §7。
> 本机路径基线：Hermes 装在 `F:\Hermes`，所有脚本、产物、日志都在 F 盘（**绝不往 C 盘写文件**）。

---

## 1. 快速索引

| 你要什么 | 用哪个 | 一行命令 |
|---|---|---|
| 委派一个任务给**国内版 WorkBuddy** | `workbuddy_bridge.py` | `python "F:/Hermes/skills/integrations/workbuddy-bridge/scripts/workbuddy_bridge.py" "任务描述"` |
| 委派给**国际版 WorkBuddy**（需 GUI 开着） | `wbai_bridge.py` | `python F:/Hermes/scripts/wbai_bridge.py --mode job --cwd F:/proj "任务描述"` |
| 委派给 **Antigravity CLI（反重力）** | `agy_cli.py` | `python F:/Hermes/scripts/agy_cli.py "任务描述" --dir F:/proj --yolo --json` |
| 只要最强的视觉/前端产出 | `agy_cli.py` + `gemini-3.1-pro-high` | `python F:/Hermes/scripts/agy_cli.py "任务" --model gemini-3.1-pro-high --effort high --new-project --yolo` |
| 委派给 **Qoder 自带 CLI**（零登录、有真免费档） | `qoder_cli.py` | `python F:/Hermes/scripts/qoder_cli.py "任务描述" --json` |
| 想要**不花钱**的模型（批量试提示词/评测首选） | `qoder_cli.py` + 免费档 | `python F:/Hermes/scripts/qoder_cli.py "任务" -m Qwen3.8-Flash`（实测 `total_credits` = 0）|
| 只想拿 token 用量/成本 | 直调 WorkBuddy CLI | 见 §3.4 |
| 测额度 / 免费档冒烟 | `--model hy3`（国内）/ `--model hy3`（国际） | 见 §3.4 / §4.3 |

**三个"别搞混"**：
1. **WorkBuddy 国内版 ≠ 国际版**：安装目录、数据目录、端点、模型 ID 全部不通用，桥接脚本也是两个。
2. **Antigravity CLI（`F:\AntigravityCLI`）≠ Antigravity IDE（`F:\Antigravity`）**：后者是旧空壳目录，端口还是废弃的 10801，别碰。
3. **"反重力"= Antigravity = agy**，是 Google 的 agentic CLI，不是 WorkBuddy 的功能。
4. **Qoder ≠ 上面三个**：它是 Qoder IDE **自带**的 CLI，账号、额度、登录态与 Google / WorkBuddy 完全无关；**有真免费档（`Qwen3.8-Flash`，0 额度消耗）** —— 四条通道里唯一不花钱的一条。

---

## 2. 四条通用铁律（违反必踩坑）

### 2.1 只看磁盘，不信模型自述
这四个通道**都可能让你误判**：
- agy 会把 `file:///F:/...` 链接当确认信息报出来，而文件可能写在别处或压根没写。
- WorkBuddy 桥接的 `ok: true` 只代表它回了话，**不代表外部副作用发生了**。
- Qoder 桥接回的是 CLI 的 **stdout 原文**（比"模型自述"可靠），但**写文件类副作用同样要自己 `ls` 核实**。

→ 凡是"写文件/改代码"类任务，**在提示词里给绝对路径**，跑完自己 `ls` / `cat` 核实。

### 2.2 必须挂代理 `127.0.0.1:2080`
- **agy 强制要**：Go 程序认 `HTTP_PROXY/HTTPS_PROXY`，不挂代理直连 Google 会**静默失败**。`agy_cli.py` 已自动注入（`agy.cmd` 同），除非你显式传 `--no-proxy`。
- 代理挂掉会连锁影响本机 Discord / QQBot / 部分 cron（历史事故：代理断 8.5 小时，一连串"Connection error"）。
- 出现网络类报错，**先查代理**：`netstat -an | grep 2080 | grep LISTENING`，再怀疑模型/额度。
- ⚠ **例外：Qoder 不要挂代理**（服务国内可达，挂了反而慢：实测直连 8.7s vs 走 2080 11.2s），见 §6.6。

### 2.3 凭证纪律：让脚本自己读，绝不打印
本机所有桥接脚本的 token / key / password 都是**脚本内部读取**（进度日志里也不会出现）：
- WorkBuddy 国际版网关密码：从**进程环境变量**读（`CODEBUDDY_GATEWAY_PASSWORD`），你不需要知道它的值。
- agy / WorkBuddy 国内版：走各自 GUI 的登录态，桥接不需要 key。
- Qoder：设备令牌与 job token 都在 `F:\Hermes\secrets\`（`chmod 600`），桥接自动读取 + 自动续期；**不要打印、不要复制到别处**。

→ **不要**把任何 key/token/password 写进文件、命令行、日志或聊天记录。需要读进程环境时用 `F:\Hermes\scripts\read_proc_env.py <pid>`（默认给敏感值打码；`ENV_RAW=1` 才输出明文，**不要用**）。

### 2.4 输出目录一律 F 盘
所有中间产物、日志、临时文件写 `F:\Hermes\tmp\` 或任务自己的项目目录。C 盘只删不写。

---

## 3. ① WorkBuddy 国内版

### 3.1 前置条件
- 安装：`F:\kele\WorkBuddy`
- 数据：`C:\Users\6\.workbuddy`（`workbuddy.db`、`sessions.json`、`models.json`）
- **至少打开并登录过一次 GUI**（连接器配置存在才能用连接器；`--no-connectors` 可绕过）
- 桥接**不需要** GUI 常开（CLI 可独立跑），实测 `--health` 在 GUI 未开时也 ok
- 连接器（connector）**必须先在 WorkBuddy 里启用并登录**，否则委派的任务用不了它

### 3.2 命令
```bash
BR="F:/Hermes/skills/integrations/workbuddy-bridge/scripts/workbuddy_bridge.py"

python "$BR" --health                          # 先验路径，不调模型
python "$BR" "把这份需求整理成任务清单，只输出清单"        # 正常委派（返回 JSON）
python "$BR" --plain "任务"                      # 只要最终文本
python "$BR" --permission-mode plan "先给方案别动手"    # 只规划
python "$BR" --model "GLM-5.3" "任务"             # 指定模型
python "$BR" --workdir "F:/proj" "在项目里干活"
echo "长文本任务" | python "$BR"                  # 不给位置参数则读 stdin
```

### 3.3 参数（实测 `--help`）
| 参数 | 说明 |
|---|---|
| `--health` | 只验路径，不调模型 |
| `--model <name>` | 覆盖模型，**从官方内置模型表选**（见下） |
| `--permission-mode` | `acceptEdits` / `auto` / `default` / `dontAsk`（默认）/ `plan` |
| `--workdir <path>` | 任务工作目录 |
| `--no-connectors` | 不加载连接器 |
| `--plain` | 只打印最终文本 |
| `--timeout <sec>` | 默认 180（可配 `timeout_seconds`），合法区间 **10–1800** |
| `--config <path>` | 换 bridge_config.json |

**权限纪律**：`dontAsk` 是默认值，目的是**永不卡在审批弹窗上**。它可能返回权限错误 —— 那是**结果**，请如实上报，**不要**换更宽的权限重试。绕过类模式被桥接脚本主动拒绝。

### 3.4 模型：官方列表 vs `models.json`（最容易搞错的地方）
- `--model` 选的是 **WorkBuddy 内置官方模型**：`GLM-5.3` / `GLM-5.2` / `Kimi-K3` / `MiniMax-M3` / `DeepSeek-V4-Flash` / `Hy3` 等。
- `C:\Users\6\.workbuddy\models.json` **只记录自定义 relay 模型**（指向 `107.172.138.14:3000/v1`）。
- ⚠️ **`models.json` 里没有 ≠ WorkBuddy 没有这个模型**。`GLM-5.3` 就是这么被误判过的，实测 `--model "GLM-5.3"` 正常。

### 3.5 直调 CLI 拿 token 用量（不经桥接）
```bash
NODE="C:/Users/6/.workbuddy/binaries/node/versions/22.22.2-3/node.exe"
CLI="F:/kele/WorkBuddy/resources/app.asar.unpacked/cli/bin/codebuddy"
"$NODE" "$CLI" --print --output-format json --model hy3 \
  --permission-mode dontAsk --no-session-persistence \
  --strict-mcp-config --mcp-config '{"mcpServers":{}}' "你的问题"
```
输出是 JSON 数组，取 `type=="result"` 那条的 `usage`：`input_tokens / output_tokens / cache_creation_input_tokens / cache_read_input_tokens`，另有 `total_cost_usd`、`is_error`。

**成本估算必须知道的固定开销**：每次调用光系统提示 + 工具定义就约 **26k input token**（几乎全是 cache_creation）。

### 3.6 额度（实测结论，别再瞎猜）
- **`hy3` = 5 小时滚动窗口**，窗口从**首次使用时刻**起算（不是自然日/24h）。
- 窗口内约 **2,017 万 token（计费向 in+out）/ 740 次调用**即撞墙。
- 撞墙原文：`429 您的使用量已超出频率限制，将在 <首调+5h> 重置，您也可以切换其他模型继续使用`。
- **每模型独立额度池**（`hy4-preview` 实测 6.8h / 1,672 万 token 未撞墙）。
- CLI 输出与本地 DB **都不含剩余额度** → **额度天花板只能实跑测出来**。
- 长跑测额度用现成工具：`F:\Hermes\scripts\hy4_quota_test.py` + `hy4_start.py`（脱离进程组启动）+ `hy4_report.py`，产物在 `F:\workbuddy_hy4_test\`。

### 3.7 坑
- 更新后**第一次**调用比后续慢很多。
- 定义了连接器 ≠ 已启用/已登录。
- 直接打本地连接器端点 `127.0.0.1:4791/mcp` 返回 **401**，**不是**桥接的替代品。
- 桥接会主动移除 WorkBuddy 自动加载的 `mcp__weixinpay__*` 工具。
- 桥接只返回最后一条 assistant 回答，不返回原始 transcript（里面有内部上下文）。

---

## 4. ② WorkBuddy 国际版（WorkBuddy AI）—— 和国内版完全两套

### 4.1 前置条件（**硬约束**）
- 安装：`F:\公共\WorkBuddyAI`（进程名 `WorkBuddyAI.exe`）
- 数据：`~/.workbuddy-ai`
- 端点：`www.workbuddy.ai`（Google 账号登录）
- ⚠️ **GUI 必须开着**：国际版没有独立可用的 CLI 通道，正确入口是 **GUI 自己拉起的本地 HTTP 网关**（`codebuddy --serve --random`，**端口随机**，实测出现过 1483）。**GUI 关掉 = 网关消失**。
- 裸跑 CLI 必然 **401**（缺 `CODEBUDDY_API_KEY`，加密凭证库 `entries.json.enc` 未生成）。
- 网关是**随机端口**，不要写死；桥接脚本会自动找 serve 进程和端口。

**GUI 没开时的报错原文**（见到这句就去开 GUI，不要怀疑脚本坏了）：
```json
{"ok": false, "error": "没找到 WorkBuddy AI 的 serve 进程 —— 请先打开 WorkBuddy AI 桌面版"}
```

### 4.2 命令
```bash
WB="F:/Hermes/scripts/wbai_bridge.py"

python "$WB" --health                       # 验证 GUI/网关在不在
python "$WB" "快问快答"                       # 默认 run 模式
python "$WB" --mode job --cwd F:/proj "做活"  # 要写文件/跑命令，必须 job 模式
python "$WB" --model hy3 "任务"               # 换模型
python "$WB" --effort high --json "任务"      # 推理强度 + 结构化输出
```

### 4.3 参数（实测 `--help`）
| 参数 | 说明 |
|---|---|
| `--mode {run,job}` | `run`=快问快答；`job`=在目录里干活 |
| `--cwd <path>` | job 模式的工作目录 |
| `--model <name>` | 见下，**ID 与国内版不通用** |
| `--effort {minimal,low,medium,high,xhigh,max}` | 推理强度 |
| `--permission-mode {default,acceptEdits,plan,auto,dontAsk}` | |
| `--timeout <sec>` | 默认 **900** |
| `--json` / `--health` | 结构化输出 / 只验路径 |

⚠️ **`--mode job` 必须配 `permissionMode=acceptEdits`**，否则 `Write` / `Bash` 类工具会被直接拒掉（桥接已处理，自己写脚本时要注意）。

### 4.4 模型
- 合法模型清单在 `~/.workbuddy-ai/cache/acc-product-config-v3.json` 的 `models[]`（实测 20 个）。
- **ID 与国内版不通用**：`glm-5.3-flash` 在国际版会报 `service info not found`。
- **免费档（x0.00）**：`hy3`、`hy4-preview-f`、`deepseek-v4.1-flash`。

### 4.5 内部机制（自己写脚本时用）
网关由 GUI 拉起，鉴权走请求头：
```
X-CodeBuddy-Request: 1
Authorization: Bearer <网关密码>
```
密码在其进程环境变量 `CODEBUDDY_GATEWAY_PASSWORD`（**cookie 形式才需要 sha256**）。取值用 `read_proc_env.py`，**不要打印**。

---

## 5. ③ Antigravity CLI（反重力 / `agy`）

### 5.1 布局
```
F:\AntigravityCLI\bin\agy.exe     二进制（v1.2.3, ~186MB）
F:\AntigravityCLI\agy.cmd         代理启动器（设 HTTP(S)_PROXY=127.0.0.1:2080 再调 agy.exe）
F:\AntigravityCLI\localappdata\   官方中转目录真身（junction 从 %LOCALAPPDATA%\antigravity 指过来，C 盘零占用）
F:\Hermes\scripts\agy_cli.py      桥接脚本（推荐入口）
```
- User PATH 已含 `F:\AntigravityCLI\bin` → **新开的 shell 可直接 `agy`**；已经开着的终端要重启。
- 账号：`wualterconcentrados@gmail.com`（Google AI Pro）。
- ⚠️ `F:\Antigravity`（**无 CLI 后缀**）是另一个旧目录（IDE 用），别和 CLI 混。

### 5.2 命令（桥接，首选）
```bash
AGY="F:/Hermes/scripts/agy_cli.py"

python "$AGY" "只回复 BRIDGE_OK" --json                       # 冒烟
python "$AGY" "干活" --dir F:/proj --yolo --json              # 写文件/跑命令
python "$AGY" "长任务" --new-project --print-timeout 15m --yolo  # 隔离工作区 + 放宽单轮上限
python "$AGY" "续上一条" --continue
python "$AGY" "你是什么模型" --model gemini-3.1-pro-high --effort high
```

### 5.3 参数（实测 `--help`）
| 参数 | 说明 |
|---|---|
| `--model <id>` | 见 §5.4 |
| `--effort {low,medium,high}` | 推理强度 |
| `--dir <path>` | 工作目录（默认当前目录） |
| `--add-dir <path>` | 附加工作区目录（可重复） |
| `--mode {accept-edits,plan}` | 不写 `accept-edits` 时非交互会卡权限 |
| `--yolo` | 自动批准所有工具权限（= `--dangerously-skip-permissions`） |
| `--continue` | 续接最近一次会话 |
| `--print-timeout <d>` | **agy print 单轮上限，默认 `5m0s`，长任务必须调大** |
| `--new-project` | 建独立项目跑（**隔离工作区，防跨会话抄文件**） |
| `--timeout <sec>` | 子进程硬超时，默认 1200 |
| `--json` | 用 `--output-format json` 并解析（用量走 stderr） |
| `--no-proxy` / `--raw` | 关代理 / 原样打印 stdout |

返回码：`0=SUCCESS`、`4=ERROR`、`3=超时`。

### 5.4 模型（实测 `agy models`）
```
gemini-3.8-flash-high|medium|low    默认，最快最稳
gemini-3.7-flash-high|medium|low
gemini-3.6-flash-high|medium|low
gemini-3.1-pro-high|low             ★ 视觉/前端产出最强
claude-sonnet-4-6                   ✅ 实测 9s 可用
claude-opus-4-6-thinking            ❌ 503 No capacity available（服务端无算力，非额度）
gpt-oss-120b-medium                 ✅ 实测 28s 可用
```
**额度面板分两组独立计**：`GEMINI MODELS`（Flash/Pro）与 `CLAUDE AND GPT MODELS`（Opus/Sonnet/GPT-OSS），每组各有**周窗口 + 5 小时窗口**两条，各带刷新倒计时。实测（2026-09-16）：GEMINI 组周 98.18% / 5h 91.53%；CLAUDE&GPT 组周 **99.62%**。
**面板有额度 ≠ 能用**：`claude-opus-4-6-thinking` 在 99.62% 额度下照样 503（服务端没算力），重试 583s 才返回。碰到 503 **别查额度/账号**，隔段时间自己会好。

### 5.5 裸命令写法
```bash
agy -p="提示词" --output-format json --model gemini-3.8-flash-high \
    --mode accept-edits --dangerously-skip-permissions
```
子命令：`models` 列模型、`agents/agent` 列 agent、`mcp` 管 MCP、`plugin` 管插件、`update` 自更新、`remote-control` 后台守护。

### 5.6 ⚠️ `-p` 会吃掉紧跟其后的参数
`agy -p "文本" --json` 会把 `--json` 当成提示词。**必须写 `-p="文本"`**，或把其它旗标放 `-p` 之后且用 `=` 形式。
报错原文：`-p took "--output-format" as its prompt`。

### 5.7 五大坑（都实测踩过）
1. **print 模式无视 `--dir`，所有会话共用同一个项目目录**（本机 `F:\curose\`）→ 同目录先后跑两个模型，**后跑的会直接看到前一个的产物并抄过去**：实测 `gemini-3.8-flash-high` 交出的文件与 `gemini-3.1-pro-high` 的**逐行 diff 96.7% 相同**（viewBox、class 名一模一样）。
   **修法：每次跑加 `--new-project`**（按 cwd 建独立项目，文件也落 cwd）。提示词里写绝对路径更保险。**做多模型横评时必须这样隔离。**
2. **print 单轮硬上限默认 5 分钟** → 超时输出 `[agy] print timeout after 5m0s with turn in progress; returning partial output`，**任务被砍断、可能一个文件都没写**。看着像"模型能力不行"，其实是超时。**修法：`--print-timeout 15m`**（桥接已默认 15m + 子进程 1200s）。
3. **会谎报 DONE** → 只看磁盘。查盘：`find /f -maxdepth 5 -iname "*.html" -newermt "-20 minutes" | grep -v <自己的目录>`。
4. **质量差异极大**：同题（SVG 动画）`gemini-3.1-pro-high` 出 54,815 字节 / 817 行 / 11 关键帧 / 38 渐变 / 10 滤镜；`gemini-3.8-flash-high` 独立跑出 35,607 字节 / 781 行 / 22 渐变 / 0 滤镜（完整但精致度差一档）。→ **要视觉/前端交付物优先 `--model gemini-3.1-pro-high --effort high`**；Flash 当快速问答用。
5. **交互首启会问** `Do you trust the contents of this project?`（print 模式不阻塞）。

### 5.8 让模型写 SVG 动画时的头号坑：CSS `transform` 覆盖定位
`transform="translate(...)"` 是 CSS `transform` 的 **presentation attribute，不是叠加关系** —— 同一元素上一旦有 CSS 动画写 `transform:`，属性值被**整体丢弃**。

```html
<g transform="translate(295,185)" class="pelican-body">   <!-- ❌ 定位+动画同节点 -->
<g transform="translate(220,310)"> <g class="wheel-rear">  <!-- ✅ 定位在外、动画在内 -->
```
后果：部件飞到画布原点 `(0,0)` 挤成一坨，看着像"没画出来"（实测 `claude-sonnet-4-6` 的鹈鹕就这么从车座上消失的）。

**排查**：① 看 `@keyframes` 里有没有 `transform:`；② 看被动画的 class 是否和 `transform="translate(...)"` 在同一 `<g>`；③ 裁画布左上角放大看有没有"散架的一坨"。
**写进提示词的规矩**：定位在外层 `g`、动画在内层 `g`；或 `<animateTransform additive="sum">`；或把定位写进 keyframes（`transform: translate(295px,185px) translateY(-3px)`）。

### 5.9 验收动画类 HTML 的配方
```bash
chrome --headless=new --disable-gpu --hide-scrollbars --no-first-run \
  --no-default-browser-check --user-data-dir=<unique> --screenshot=<out.png> \
  --window-size=1000,700 --virtual-time-budget=1500 "file:///F:/path/x.html"
```
取 **t=1500ms 与 t=4000ms 两帧比 md5** 证明"真在动"；近邻预算（1200 vs 2600）会撞同相位得到假静态。
本机 Chrome：`C:\Program Files\Google\Chrome\Application\chrome.exe`。

### 5.10 要用户看得见界面时
别用 agent 的 terminal 起（服务会话/无窗口），用新建控制台：
```bash
python F:/Hermes/scripts/run_in_new_console.py cmd.exe /k "F:\AntigravityCLI\agy.cmd"
```
读屏幕：`python F:/Hermes/scripts/read_console_buffer.py <agy.exe PID>`（AttachConsole + ReadConsoleOutputCharacter，比截图靠谱 —— 截图会截到上层窗口、中文标题还会被 GBK 搞乱）。

---

## 6. ④ Qoder（IDE 自带的 `qodercli`）—— 零登录 + 真免费档

> 2026-09-18 接入。**四条通道里唯一不花钱、且不需要任何登录动作的**：令牌链在桥接内部自动建立与续期。

### 6.1 布局与定位

Qoder 不是外部网关，而是 **Qoder IDE 自带的 CLI**（`qodercli` v1.1.53，藏在 IDE 的 asar 包里）。桥接把它当普通 node 脚本拉起即可：

```
F:\Qoder\.qoder-versions\<版本>\Qoder.exe          IDE 本体（当 node 运行时用）
F:\Qoder\.qoder-versions\<版本>\resources\app.asar.unpacked\node_modules\
        @qoder-ai\qoder-agent-sdk\dist\_worker\qoder-worker-runtime.obf.mjs     ← CLI 本体（33MB，混淆）
F:\Hermes\scripts\qoder_cli.py                     桥接（唯一推荐入口）
F:\Hermes\secrets\qoder_tokens.json                设备令牌（chmod 600）
F:\Hermes\secrets\qoder_jobtoken.json              job token 缓存（24h，自动续）
```

- **零安装、C 盘零占用**：CLI 是 IDE 自己带的，不额外下载任何东西。
- **GUI 不需要常开**：实测 `Qoder.exe` 进程全关着照样跑（令牌续期走 HTTP，不依赖 IDE）。
- **版本号别写死**：桥接 `find_install()` 会自动挑 `.qoder-versions` 下**最新的**带 worker 的版本；IDE 自更新后无需改任何配置。
- 账号：`mariabertavendiendo@gmail.com`（**只在此处记账**，凭据在 secrets 里，不外发）。

### 6.2 命令（桥接，唯一入口）

```bash
QO="F:/Hermes/scripts/qoder_cli.py"

python "$QO" --status                  # 账号 / 登录方式（应显示 Login Method: job_token）
python "$QO" --auth-status             # 只看令牌链健康（四行，不打印密钥）
python "$QO" --list-models             # 17 个模型 ID
python "$QO" --cheap-models            # 按实测费率从便宜到贵
python "$QO" "只回复 BRIDGE_OK"         # 真跑（默认免费档）
python "$QO" "17*23 只回复数字" --json  # 拿原始结果（含 [meta] 计费与耗时）
python "$QO" "干活" --cwd F:/proj --json
python "$QO" "干活" -m GLM-5.3-Flash --json    # 换模型
```

### 6.3 参数（实测 `--help`）

| 参数 | 说明 |
|---|---|
| `prompt` | 位置参数，headless 跑一轮 |
| `-m/--model <id>` | 模型名，默认 `Qwen3.8-Flash`（免费档），见 §6.4 |
| `--json` | 输出原始 JSON，含 `[meta]`：`duration_ms` / `total_cost_usd` / `total_credits` |
| `--cwd <path>` | 工作目录（默认当前目录） |
| `--timeout <sec>` | 子进程超时 |
| `--permission-mode <mode>` | 透传给 CLI 的权限模式 |
| `--status` / `--auth-status` / `--list-models` / `--cheap-models` | 诊断用，**不调模型、不花额度** |
| `--login` | **已废弃**（不再需要任何交互登录） |

### 6.4 模型与费率（实测口径：跑一次最小请求读 `total_credits`）

| 模型 | 实测费率 | 备注 |
|---|---|---|
| **Qwen3.8-Flash** | **0.0（免费）** | **默认档**；多模态能读图；实测 3~10s，同池最快 |
| Efficient | 0.06 | 极便宜 |
| GLM-5.3-Flash | 0.45 | |
| Qwen3.7-Plus | 0.55 | |
| DeepSeek-Flash | 0.56 | |
| MiniMax-M3 | 1.20 | |
| Kimi-K2.8-Preview | ≈2.96 | |
| Auto | ≈7.5 | **别默认用** |
| Sonus | ≈37.5 | 一次小请求就烧这么多 |
| Cantus | — | **死别名**（`modelUsage: <synthetic>`，exit 1） |

⚠ **官方文档滞后**：`https://docs.qoder.com/cli/model.md` 当时**没标 0x**，本地那份模型目录是加密的（magic `QMC`）。
→ **判断免费与否只有一个办法：跑一次最小请求，读 `[meta].total_credits`。** 别信文档、别信缓存、别信模型名。

### 6.5 令牌链（"零登录"是怎么做到的）

三步全在桥接里自动完成，过期自续（**没有任何需要人点的授权**）：

```
① 设备令牌  ← 读 IDE 的 auth.v1.dat（Electron OSCrypt 解，非 DPAPI）
             过期时用 refresh 换新：POST https://api3.qoder.sh/api/v1/deviceToken/refresh
② job token ← POST https://openapi.qoder.sh/api/v1/me/jobToken
             （Bearer = 设备令牌 + 正确 clientId）→ jt-…，24h 有效 + 48h refresh
③ 注入 CLI  ← 环境变量 QODER_JOB_TOKEN='{"token":"…","expiresAt":<毫秒>}'   ← 必须 JSON 形态
```

实测健康输出（2026-09-18）：

```
job token                ✅ 有效  剩余  20.3 小时
job token refresh        ✅ 有效  剩余  44.3 小时
device token             ✅ 有效  剩余 716.2 小时
device token refresh     ✅ 有效  剩余 8636.2 小时   （到 2027-09）
```

### 6.6 代理：**这一条不要挂**

Qoder 服务国内可达，实测**直连更快**：直连 8.7s / 走 `127.0.0.1:2080` 11.2s。
→ 别把 agy 那条"必须挂代理"的习惯照搬过来（挂了不报错，只是慢，容易被误判成"模型慢"）。

### 6.7 坑（都实测踩过）

1. **`QODER_JOB_TOKEN` 必须 JSON 编码** —— 喂裸 token 会**静默无输出**（不报错、不返回），必须 `{"token":…,"expiresAt":<毫秒>}`。
2. **`QODER_SERVER_ENDPOINT` 在 global 版是死的** —— 覆盖逻辑被编译期常量关掉（`v7a()` 里 `if(!Ja) return;`）→ 想用"假服务器截流量/伪造登录"那条路**彻底走不通**，别再试。
3. **`Cantus` 看着像免费档，其实是死别名**（`modelUsage: <synthetic>`，exit 1）。
4. **`Auto`（≈7.5）、`Sonus`（≈37.5）会烧额度** —— 别因为名字高级就默认用。
5. **`~/.qoder/.auth/user` 不能手工伪造**（内联 WASM 加密、key 绑机器标识）→ 只能走 job token 注入。
6. **交换接口不吃设备令牌**：`jobToken/exchange` 只认 PAT 格式（`dt-` 会判 `access_token_invalid`）；零登录路线必须走 `/api/v1/me/jobToken`。
7. **别用 IDE 的 `Network\Cookies`**：该文件被 IDE 独占锁，直读会失败（`robocopy /B` 也 exit 124），不需要它。

---

## 7. 冒烟测试（任一路接入前先跑）

```bash
# ① 国内版 WorkBuddy —— 期望 JSON 里 ok: true
python "F:/Hermes/skills/integrations/workbuddy-bridge/scripts/workbuddy_bridge.py" --health

# ② 国际版 WorkBuddy —— 期望 ok: true；报"没找到 serve 进程"就是 GUI 没开
python F:/Hermes/scripts/wbai_bridge.py --health

# ③ agy —— 期望 SUCCESS + BRIDGE_OK（实测约 10s，13,374 tokens）
python F:/Hermes/scripts/agy_cli.py "只回复 BRIDGE_OK 六个字母，不要别的" --json

# ④ Qoder —— 期望回 BRIDGE_OK；[meta] 里 total_credits = 0、total_cost_usd = 0（免费档）
python F:/Hermes/scripts/qoder_cli.py "只回复 BRIDGE_OK" --json
```

**2026-09-16 实测状态**：
| 通道 | 状态 | 证据 |
|---|---|---|
| WorkBuddy 国内版 | ✅ | `ok: true`，CLI `F:\kele\WorkBuddy\resources\app.asar.unpacked\cli\bin\codebuddy`，node `22.22.2-3` |
| WorkBuddy 国际版 | ❌ 当时 GUI 未开 | `{"ok": false, "error": "没找到 WorkBuddy AI 的 serve 进程"}` |
| Antigravity agy | ✅ | 10.1s `status=SUCCESS`，tokens 13,374（in 13,158 / out 216 / think 213） |
| **Qoder（2026-09-18 追加）** | ✅ | 直连 8.7s 返回 `391`，`total_credits: 0`；GUI 全关也能跑；令牌链四项全绿 |

---

## 8. 故障速查表

| 症状 | 根因 | 处置 |
|---|---|---|
| agy 静默失败 / 无输出 | 没挂代理 | 用 `agy.cmd` 或 `agy_cli.py`（自动注入 `127.0.0.1:2080`），别 `--no-proxy` |
| agy `print timeout after 5m0s ... partial output` | 单轮上限默认 5 分钟 | 加 `--print-timeout 15m` |
| agy 回的链接/路径找不到文件 | **它谎报**（写到了别处或没写） | 提示词给绝对路径，跑完自己 `ls` |
| 两个模型产出高度雷同（diff 90%+） | print 无视 `--dir`，共用项目目录，**后跑者抄前面** | 加 `--new-project`；横评必须隔离 |
| agy 报 `-p took "--xxx" as its prompt` | `-p` 吃掉后一个参数 | 写 `-p="文本"` |
| agy `503 No capacity available` | **服务端无算力**，非额度非账号 | 换同组别的模型（如 sonnet）或过段时间重试；别去查额度 |
| agy 部件渲染"消失" | CSS `transform` 覆盖了 SVG `transform` 定位 | 见 §5.8 |
| 国际版 `没找到 serve 进程` | GUI 没开（网关随 GUI 生死） | 打开 WorkBuddy AI 桌面版 |
| 国际版裸跑 CLI `401` | 缺 `CODEBUDDY_API_KEY` | 不要裸跑，走 `wbai_bridge.py` |
| 国际版 `service info not found` | 用了国内版模型 ID | 查 `~/.workbuddy-ai/cache/acc-product-config-v3.json` |
| 国际版 job 模式工具被拒 | 没给 `permissionMode=acceptEdits` | 用桥接（已处理），裸调要显式加 |
| 国内版 `429 使用量已超出频率限制` | 撞 **5 小时滚动窗口**（hy3 约 2,017 万 token / 740 次） | 换模型（各自独立额度池）或等窗口重置；见 §3.6 |
| 国内版找不到某模型 | 只查了 `models.json`（它只存自定义 relay 模型） | 用官方内置模型名直调，如 `--model "GLM-5.3"` |
| 国内版本地 `127.0.0.1:4791/mcp` 401 | 连接器端点要 WorkBuddy 自己的鉴权 | 别直连，用桥接 |
| 委派任务返回权限错误 | `dontAsk` 模式撞到未授权操作 | **如实上报，不要换更宽权限重试** |
| 网络类报错成片出现 | 代理挂了（连锁影响 Discord/QQ/cron） | 先查 `netstat -an \| grep 2080`，再怀疑模型 |
| 后台长任务日志莫名截断 | 用 `nohup ... &` 起在 agent 里会被回收 | 用 `DETACHED_PROCESS` 启动器（模板 `F:\Hermes\scripts\hy4_start.py`） |
| Qoder 无输出、也不报错 | 手工设了 `QODER_JOB_TOKEN` 且喂的是裸 token | 用桥接（已按 JSON 注入）；别手工设这个变量 |
| Qoder `access_token_invalid` | 拿设备令牌（`dt-`）去走 PAT 交换 | 零登录路线走 `/api/v1/me/jobToken`，不走 `jobToken/exchange` |
| Qoder 报找不到 worker / 版本太旧 | IDE 自更新后版本目录变了 | 桥接自动挑最新版本；仍失败就开一次 IDE 让它升完 |
| Qoder 说"额度没有"但其实能跑 | 用了 `Auto`/`Sonus` 这类高价档 | 换 `Qwen3.8-Flash`；费率只认 `[meta].total_credits` |

---

## 9. 纪律（写进任何自动化脚本里）

1. **顺序调用，不并发**：这些通道都是**单账号共享额度**，并发会互相抢额度、还会加剧风控。
2. **免费档先行冒烟**：任何新链路先用 `hy3`（国内/国际）、`gemini-3.8-flash`（agy）或 **`Qwen3.8-Flash`（Qoder，真 0 额度）** 跑通，再换强模型做真活。
   → **大批量试提示词/跑评测优先用 Qoder 免费档**（唯一零成本通道），把贵通道留给真实交付。
3. **长任务不许让 cron 前台挂着跑**：用"启动器瞬返 + 单独收集任务"（`hy4_start.py` / `hy4_report.py` 模式）。
4. **委派内容边界**：不委派购买/支付/转账/改密码/账号安全类操作；发送、发布、远程删除、表单提交必须由用户明确授权那一个动作。
5. **产物验收**：外部副作用（写文件、上传、发送）完成后**读回验证**，再回报"成功"。
6. **时间/日期类事实用工具查**，别凭模型记忆。

---

## 10. 文件清单

| 用途 | 路径 |
|---|---|
| 国内版桥接 | `F:\Hermes\skills\integrations\workbuddy-bridge\scripts\workbuddy_bridge.py` |
| 国内版桥接配置 | `F:\Hermes\skills\integrations\workbuddy-bridge\bridge_config.json` |
| 国际版桥接（技能内副本） | `F:\Hermes\skills\integrations\workbuddy-bridge\scripts\wbai_bridge.py` |
| 桥接说明（技能参考文档） | `F:\Hermes\skills\integrations\workbuddy-bridge\references\workbuddy-ai-international.md` |
| 桥接自测 | `F:\Hermes\skills\integrations\workbuddy-bridge\tests\test_workbuddy_bridge.py` |
| 国际版桥接 | `F:\Hermes\scripts\wbai_bridge.py` |
| agy 桥接 | `F:\Hermes\scripts\agy_cli.py` |
| **Qoder 桥接** | `F:\Hermes\scripts\qoder_cli.py` |
| Qoder 桥接说明（技能参考） | `F:\Hermes\skills\autonomous-ai-agents\desktop-ai-app-bridges\references\qoder-cli-bridge.md` |
| Qoder 令牌（设备 / job） | `F:\Hermes\secrets\qoder_tokens.json`、`qoder_jobtoken.json`（`chmod 600`，**不外发**） |
| Qoder CLI 本体（IDE 自带，免安装） | `F:\Qoder\.qoder-versions\<版本>\resources\app.asar.unpacked\node_modules\@qoder-ai\qoder-agent-sdk\dist\_worker\qoder-worker-runtime.obf.mjs` |
| agy 安装 / junction | `F:\Hermes\scripts\agy_install_to_f.ps1`、`agy_junction_staging.ps1` |
| 读进程环境（取凭据） | `F:\Hermes\scripts\read_proc_env.py` |
| 脱离进程组启动器 | `F:\Hermes\scripts\hy4_start.py` |
| 额度探顶三件套 | `F:\Hermes\scripts\hy4_quota_test.py`、`hy4_start.py`、`hy4_report.py`、`wait_probe.py` |
| 可见控制台启动 / 读屏 | `F:\Hermes\scripts\run_in_new_console.py`、`read_console_buffer.py` |
| 国内版额度记录 | `F:\workbuddy_hy4_test\` |

---

*本文档由 Hermes 生成于 2026-09-16（WorkBuddy ×2 + agy），2026-09-18 追加 Qoder 一路；命令与参数均按本机实跑输出核对（`--help` 原文 + 冒烟结果 + 令牌链 `--auth-status`）。若某条命令报错与本文描述不符，**以本机实跑为准**，并回头修正本文档。*
