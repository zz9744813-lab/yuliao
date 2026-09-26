# PORT_gwhost_userinfo — k4 收据 gateway_host 剥 userinfo + worlds_dir 断言可证伪（孤儿裁定 #13 收口）

- 分支 / worktree：`fix/gwhost-userinfo-2` @ `F:\agi\_scratch\worktrees\gwhost-userinfo-2`
- 日期：2026-09-26
- 改动文件（白名单内）：`scripts/k4_paired_scenes.py`、`tests/test_k4_paired.py`、本文件
- 未 commit / 未 merge / 未 push；未动 `git config`；未动 `data/` 真实库文件。

## 1. 改动内容

### 1.1 gateway_host 剥 userinfo（承重修复）

原 `run_paired` 内收据构造（原 287-291 行形态）：

```python
gw_host = (_cfg.GATEWAY_BASE_URL or "").split("//")[-1].split("/")[0]
```

`LG_GATEWAY_BASE_URL=http://u:p@host:3000/v1` 时收据落 `u:p@host:3000`，
凭据（userinfo）进入产物面。现改为模块级 helper
`gateway_host_from_url(url)`（`scripts/k4_paired_scenes.py`，`run_paired`
定义之前），`run_paired` 内 live 分支调用：

- 口径 = `urllib.parse.urlsplit(url)` 的 `hostname`（含有效端口时
  `host:port`，hostname 天然剥 `@` 前 userinfo 并小写化）；
- 空串 / `None` / 无 netloc（如 `"not a url at all"`）/ 非法端口
  （`parts.port` 抛 `ValueError`）⇒ 按既有口径落空串，不抛异常。

### 1.2 `test_worlds_dir_recorded_in_output` 去恒真兜底

原断言（不可证伪，离线跑恒真）：

```python
assert Path(art["worlds_dir"]).is_dir() or not art["live"], art["worlds_dir"]
```

现改为离线可证伪（monkeypatch 换绑 `k4.tempfile.mkdtemp` 到 `tmp_path` 下
并登记本次自建目录）：

1. `art["worlds_dir"] == Path(created[0])`——产物必须记**本次自建目录**
   （不记、记错目录即红）；
2. `Path(art["worlds_dir"]).is_absolute()`；
3. `not recorded.exists()`——离线收口 main 用后即删，worlds_dir 字段与
   exists 语义一致（清理失效即红）；
4. `assert created`——前提钉死（假 mkdtemp 未被走即红，防空断言）。

补 `seed_knowledge()`：本用例改前依赖前置用例的库状态（单跑 `main()` 因
A 臂失败抛 SystemExit）；补种后**单跑可绿**（已实测，见 §2）。

### 1.3 新增测试（`tests/test_k4_paired.py`）

- `test_gateway_host_forms`：helper 各形态钉（userinfo 剥离、端口保留、
  空/None/不可解析/非法端口落空串不抛）；
- `test_gateway_host_strips_userinfo_in_receipts`：承重钉——monkeypatch
  `_cfg.GATEWAY_BASE_URL = "http://user:secretpw@GW.Example:3000/v1"`，
  live 跑（FxClient、freeze=False、worlds_dir 传 tmp 目录，零真实调用、
  零 LG 库写），断言 6 条收据 `gateway_host == "gw.example:3000"` 且
  `"@" not in`（收据非空前提已钉）。

## 2. 验收门（以下全部为本会话亲手跑出的实测）

命令原文（工作目录 `F:\agi\_scratch\worktrees\gwhost-userinfo-2`）：

```
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_k4_paired.py tests/test_k4_worlds_dir_receipt.py -q -p no:cacheprovider
```

| 轮次 | 结果 | rc |
|---|---|---|
| 基线（改前，git 原始态） | 32 passed（进度行 32 个点，无 F/E） | 0 |
| 终态（改后、变异全部恢复后） | 34 passed（进度行 34 个点，无 F/E） | 0 |

passed 数 32 → 34，只增不减（+2 为 §1.3 新增用例；worlds_dir 用例重写不改计数）。
注：本机 pytest 终端汇总不带 "N passed" 字样，计数按 `-q` 进度行点数核对
（输出快照分别存于会话侧 `/tmp/k4_baseline_out.txt`、`/tmp/k4_final.txt`，
在检出目录之外，未污染树）。

单跑钉（隔离性实测）：

```
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_k4_paired.py -q -p no:cacheprovider -k "worlds_dir_recorded"   # rc=0（补 seed_knowledge 后）
```

## 3. 反向验证（承重变异，两次，均亲手跑）

变异前基线 md5（与终态一致，`md5sum -c` 通过）：

```
a2c3ef88615776791abd3641e9550518 *scripts/k4_paired_scenes.py
9da16eceab295a5f3d8a5e09ea4a0d03 *tests/test_k4_paired.py
```

### 变异 A：urlsplit 换回 `split("//")` 形态

改 `gateway_host_from_url` 首行为 `return (url or "").split("//")[-1].split("/")[0]`，跑：

```
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_k4_paired.py -q -p no:cacheprovider -k "gateway_host"
```

⇒ **rc=1，两条新用例全红**（正是 userinfo 泄进收据的缺陷形态被抓住）：

```
E  AssertionError: assert 'user:secretp....Example:3000' == 'gw.example:3000'
FAILED tests/test_k4_paired.py::test_gateway_host_forms
FAILED tests/test_k4_paired.py::test_gateway_host_strips_userinfo_in_receipts
```

恢复后 `md5sum -c` 两文件 OK，复绿 rc=0。

### 变异 B：产物 worlds_dir 记错目录（证伪新断言）

改 `main()` 产物 `"worlds_dir": str(tmp)` → `str(tmp.parent)`，跑：

```
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_k4_paired.py -q -p no:cacheprovider -k "worlds_dir_recorded"
```

⇒ **rc=1，新断言转红**：

```
E  AssertionError: 产物 worlds_dir 不是本次自建目录：...\test_worlds_dir_recorded_in_ou0 vs ...\k4_worlds_xkv_x_ce
FAILED tests/test_k4_paired.py::test_worlds_dir_recorded_in_output
```

恢复后 `md5sum -c` 两文件 OK（`scripts/k4_paired_scenes.py: OK`、
`tests/test_k4_paired.py: OK`），终态复绿（§2）。

## 4. 口径声明：静态推演 vs 亲手实测

**亲手实测（本文引用的命令与 rc/红绿证据均来自本会话执行）**：
§2 两次验收门、单跑钉、§3 变异 A/B 的红→恢复→md5 一致→复绿全程、
`git status --porcelain`（仅 3 个白名单文件，无未登记新文件残留）。

**静态推演（未自跑，结论仅作说明不作验收依据）**：
- 真实网关 URL（含凭据）下的 live 端到端：修复后收据只落 `host:port`、
  生产 .env 不再可能把凭据写进 `k4_paired.json`——由 §3 变异 A 的注入式
  红证据外推，真实网关环境**未自跑**（402 资金墙双闸未拍板，离线纪律）。
  主控可在配好网关的机器上核验：
  `K4_ALLOW_LIVE=1 python scripts/k4_paired_scenes.py --live --writer-model <m1> --verifier-model <m2> --out <新目录>`
  后检查产物 `receipts[*].gateway_host` 不含 `@`。
- `urlsplit` 对 IPv6 字面量（`http://[::1]:3000/v1` → `[::1]:3000`）的
  行为为文档语义推演（hostname 保留方括号），**未自跑**、未写成断言
  （避免把推演钉成回归）。
- 全量测试套件其余部分不受本改动影响：改动仅 live 分支收据字段构造、
  测试文件内 3 个用例与 `tempfile` 导入——为静态判断，全量套件**未自跑**。
  主控命令原文：
  `F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest -q -p no:cacheprovider`

## 5. 遗留给主控的核验命令汇总

```
cd /f/agi/_scratch/worktrees/gwhost-userinfo-2
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_k4_paired.py tests/test_k4_worlds_dir_receipt.py -q -p no:cacheprovider   # 期望 rc=0、34 点
md5sum scripts/k4_paired_scenes.py tests/test_k4_paired.py   # 期望 = §3 基线两行
git status --porcelain   # 期望仅 M M + PORT_gwhost_userinfo.md
```
