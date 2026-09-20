# 独立复跑全仓测试（2026-09-20）

**结论：全绿。553 用例 / 0 失败 / 0 错误 / 0 跳过 / 34.2 秒 / 退出码 0。**

文档中宣称的「全仓 508 项测试通过」与实际一致（实际规模更大，宣称偏保守，不是虚高）。

## 复现方法（任何人可原样再跑）

```bash
cd F:/agi/language-genome
PYTHONIOENCODING=utf-8 F:/kelaode/Data/Agents/zqibcc8w9/tools/Python311/python.exe \
  -X utf8 -m pytest -q --tb=no -p no:warnings --junitxml=<输出路径>.xml
```

- 解释器：`F:\kelaode\Data\Agents\zqibcc8w9\tools\Python311\python.exe`（含仓库所需依赖）
- 取数方式：**读 junit XML 的 `testsuite` 属性**（`tests/failures/errors/skipped/time`），
  不依赖终端渲染 —— 本仓库 `pyproject.toml` 只设了 `addopts = "-q"`，但警告摘要会把结尾汇总行顶掉，
  人手数点号会被文件名行（`F:\...`）污染，故不采用。
- 证据文件：`F:\Hermes\team\testrun_evidence.json`（本次运行结果快照）。

## 环境观察

- 警告非零但无害（已 `-p no:warnings` 过滤），主要两类：
  1. `app/api.py:441` 字符串里出现 `invalid escape sequence '\_'`（FastAPI 的 `on_event` 用法也是同类旧写法）；
  2. `pytest-asyncio` 未显式设置 `asyncio_default_fixture_loop_scope`。
- 未发现 GBK/UTF-8 解码类失败（UTF-8 模式下全绿）。

## 过程备注（为什么不署执行通道的名字）

本次初版派给免费通道 MiMo 执行，其回合被应用侧限时机制切断（无文字输出 120 秒即 flush、单回合总时长约 360 秒封顶），
跑完 pytest 但**在写文档之前回合就关闭**，仓库里只留下两个临时输出文件（`_tmp_mimo_pytest*.txt`，已移出仓库至 `F:\Hermes\tmp`）。
故改由主控独立复跑并取其机器可读结果 —— 数据全部来自真实执行，无估算、无转述。
