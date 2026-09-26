# PORT — import-resume-role（工作树 import-resume-role，2026-09-26）

## 状态

白名单内 3 个路径已交付，全部在 `F:/agi/_scratch/worktrees/import-resume-role`；未执行
commit / merge / push，未改 `git config`，未动真实语料与 `data/` 真实库。

| 路径 | 状态 |
|---|---|
| `scripts/import_corpus_v2.py` | 改：+154/-19 行（355 行）；role 落 note 前转义 + `import_state` 严格段解析 + 同源 Work 选取确定化 |
| `tests/test_import_corpus_v2_resume.py` | 改：+177/-3 行（530 行）；用例 12 → 17（新增 5 条） |
| `PORT_import_resume_role.md` | 新增：本交付说明 |

收口对象：孤儿裁定 #3 `lg-review-import-resume`（主控复核确认 main 上两条一般级缺陷仍在）。

## 缺陷 → 收口对照

### 缺陷 1：role 可伪造 `import_state` 段，半本被判 complete

改前两处叠加（`scripts/import_corpus_v2.py:82-83` 与 `:105-109`）：

```python
def _note(role: str, state: str) -> str:
    return f"corpus_role: {role}; segmenter=v2; {STATE_KEY}: {state}"   # role 未白名单/未转义

def import_state(work: Work) -> str:
    for part in _norm_note(work).split(";"):          # 按 `;` 切分
        key, _, val = part.partition(":")
        if key.strip() == STATE_KEY and val.strip():
            return val.strip()                        # 取**首个**匹配 key
```

于是 `--role "a; import_state: complete"` 落库后的 note 字面量就是
`corpus_role: a; import_state: complete; segmenter=v2; import_state: partial`，
解析取**首个** `import_state` 命中 `complete` ⇒ **半本被判完整本**，续跑直接打印
「已导入过」退 0，半本永远补不齐。这条字面量由本次承重变异 M1 的失败输出实测打印
（见下「反向验证」）。

收口（两侧都收，单侧不足以承重）：

1. **转义（写入侧）**：`_note` 是 note 的唯一构造入口，role 过 `_safe_role`：
   `\`→`\\`、`;`→`\x3B`、`:`→`\x3A`、`\n`/`\r`/`\t` → 字面转义串。转义串**本身不含**
   `;` `:`，所以无论解析端怎么切段，role 文本都困在 `corpus_role` 这一个段的值里，
   造不出第二个 `import_state` 段；换行同理（note 不写多行）。转义**可逆**
   （`_role_text` ∘ `_safe_role` = 恒等，由回归钉住），不是静默丢字；role 原文另存
   `anchors["corpus_role"]`（JSON，无结构风险）。
   注：刻意不用 `\;` / `\:`——那种写法只在「切 `;` 之后 key 尾部多出个反斜杠」这条
   偶然路径上侥幸不出事，语义上不成立。
2. **严格段解析（读取侧）**：`import_state` 改走 `_strict_state`，只认本脚本亲手写的
   **末两段**严格格式 `(segmenter, v2) + (import_state, <partial|complete>)`，且
   `import_state` 必须是**最后**一段、值只认 `partial`/`complete` 两个已知字面量。
   注在中间的同 key 段（只能是被注入出来的）定不了状态；不满足就回退旧口径
   （`anchors` 有值=完整本，否则半本）——旧行（`corpus_role: 旧行; segmenter=v2`）
   判定逐字不变。`is_ours` 也从裸子串 `"segmenter=v2" in note` 改成按段解析。

### 缺陷 2：同源多行时选取不确定

改前 `scripts/import_corpus_v2.py:149`：

```python
w = s.query(Work).filter_by(source=src).first()      # 无 ORDER BY
```

同源两行（v1 本与 v2 镜像共享 `file:` 源、历史重复导入）时返回哪一行由查询计划
决定 ⇒ 续跑可能去补错的那一行（或把 v1 段当脏行清掉）。

收口：新增 `_pick_work`，按全序（逐级收紧、无并列）取 min：

1. ours（本脚本的 `segmenter=v2` 行）优先于别人的行；
2. 半本（`import_state == partial`）优先于已完整本——半本正是要补的那本；
3. `created_at` 早的优先（ISO 秒串，字典序即时间序），同秒比 `id`。

## 验收门（自跑）

### 基线（改前，同一命令原文）

```text
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_import_corpus_v2_resume.py -q -p no:cacheprovider
```

退出码 `0`，原始输出首行：

```text
............                                                             [100%]
```

（12 例。注：本仓 `pyproject.toml` 的 `addopts = "-q"` 与命令里的 `-q` 叠加成双重
quiet，pytest 因此不打 `N passed` 汇总行；用例数以点号个数与 `-p no:cacheprovider
--no-header`（单 `-q`）下的汇总行为准。）

### 改后

同一命令原文，退出码 `0`，原始输出首行：

```text
.................                                                        [100%]
```

（17 例 = 12 基线 + 5 新增，无删除。）

另跑一次去掉重复 `-q` 的等价形式取汇总行：

```text
97 passed, 1 warning in 30.45s
```

覆盖范围：`tests/test_import_corpus_v2_resume.py`（17）+ `test_import_v2_caveats.py`
+ `test_import_v2_sentcount.py` + `test_import_v2_textclean.py` + `test_import_guard.py`
+ `test_backfill_v2_sentences_scope.py` + `test_corpus.py` + `test_corpus_v2_isolation.py`
+ `test_compile_all.py`。命令原文：

```text
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_import_v2_caveats.py tests/test_import_v2_sentcount.py tests/test_import_v2_textclean.py tests/test_import_guard.py tests/test_backfill_v2_sentences_scope.py tests/test_corpus.py tests/test_corpus_v2_isolation.py tests/test_import_corpus_v2_resume.py tests/test_compile_all.py -p no:cacheprovider --no-header -W ignore::DeprecationWarning
```

退出码 `0`。

稳定性：验收命令连跑 3 次，3 次均 `RC=0`、17 点（`_pick_work` 的并列键用随机主键
`WK-<hex>`，故专门查了重复运行的抖动）。

新增 5 条用例（`tests/test_import_corpus_v2_resume.py`）：

| 用例 | 钉什么 |
|---|---|
| `test_role_cannot_forge_import_state` | 承重闸门：evil role 导入半本后重跑**仍续跑**（不是「已导入过」），补齐 8 段；且 note 里 `;` 只有 2 个、没有裸的 `import_state: complete` |
| `test_legacy_injected_note_still_resumes` | 解析侧独立钉：存量**已被写坏**的 note（假段注在中间）也按末段续跑 |
| `test_role_escape_is_reversible_and_note_has_three_segments` | 转义可逆不丢字（`_role_text` == 原文 == `anchors`），结构段恒为三段 |
| `test_same_source_picks_ours_row` | 承重闸门：同源两行（先插 v1 本 + 后插 v2 半本）必须续跑 **ours** 那行，v1 段一根汗毛不动 |
| `test_pick_work_order_keys_are_total` | 排序键逐级可验：半本优先 → `created_at` → 同刻比 `id`；重复调用同一输入同一输出 |

## 反向验证

三次承重变异，每次都「改 → 跑 → 精确恢复 → md5 对账」。备份与日志写在仓外临时目录
`C:\Users\6\AppData\Local\Temp\opencode\`，仓内不留任何临时/冒烟/复现脚本。

### M1：摘掉 role 转义 + 严格段解析（同时退回改前写法）

`_note` 退回 f-string 直拼、`import_state` 退回「切 `;` 取首个匹配 key」。

```text
............FFF..                                                        [100%]
E   AssertionError: role 里的 `;` 必须被转义：note 只剩两个结构分隔符
    assert 3 == 2
     +    where 3 = ... .count
     +      where ... = 'corpus_role: a; import_state: complete; segmenter=v2; import_state: partial'.count
E   AssertionError: assert 'complete' == 'partial'
E   AssertionError: 结构段只有脚本自己写的三段
    At index 1 diff: '含分号' != 'segmenter'
FAILED tests/test_import_corpus_v2_resume.py::test_role_cannot_forge_import_state
FAILED tests/test_import_corpus_v2_resume.py::test_legacy_injected_note_still_resumes
FAILED tests/test_import_corpus_v2_resume.py::test_role_escape_is_reversible_and_note_has_three_segments
```

退出码 `1`。那条被 pytest 打印出来的 note 字面量就是缺陷 1 的实证：
`corpus_role: a; import_state: complete; segmenter=v2; import_state: partial` ——
前半段的 `complete` 是 role 注进来的，改前解析取它 ⇒ 半本被判完整本。

### M2：把同源选取退回 `.first()`

`w = _pick_work(s, src)` → `w = s.query(Work).filter_by(source=src).first()`。

```text
...............F.                                                        [100%]
E   AssertionError: 必须续跑 ours 的半本，而不是同源的 v1 本
    assert ('[告警]' in '已导入过: same-src\n')
FAILED tests/test_import_corpus_v2_resume.py::test_same_source_picks_ours_row
```

退出码 `1`。捕获的 stdout `已导入过: same-src` 即缺陷 2 的实证：`.first()` 选中先插的
v1 行（非 ours）⇒ 打印「已导入过」跳过，同源的 v2 半本没被碰。
（`test_pick_work_order_keys_are_total` 在 M2 下仍绿——它直接调 `_pick_work`，不在
调用点；调用点由 M2b 覆盖。）

### M2b：只摘 `_pick_work` 排序键里的 ours 优先

`min(rows, key=lambda w: (not is_ours(w), ...))` → 去掉 `not is_ours(w)` 这一级。

```text
...............FF                                                        [100%]
E   AssertionError: 必须续跑 ours 的半本，而不是同源的 v1 本
    assert ('[告警]' in '已导入过: same-src\n')
E   AssertionError: 半本优先于完整本与别人的行
    assert (... is not None and 'WK-b5283aba67b8' == 'WK-93654b20c301')
FAILED tests/test_import_corpus_v2_resume.py::test_same_source_picks_ours_row
FAILED tests/test_import_corpus_v2_resume.py::test_pick_work_order_keys_are_total
```

退出码 `1`。两条同源用例同时转红 ⇒ ours 优先这一级是承重的。
（发现顺序：M2b 首跑时 `test_same_source_picks_ours_row` 侥幸绿——夹具两行 `created_at`
同秒、随机主键恰好 v2 更小。据此把该夹具的 `created_at` 显式钉成「v1 更早
(2020-01-01) / v2 更晚 (2021-01-01)」，重跑 M2b 两条一起红。这处是本轮唯一的测试改动
（`_seed_work` 增 `created_at` 可选参数），先复绿再重跑变异。）

### 恢复对账

三次变异全部恢复后：

```text
$ md5sum scripts/import_corpus_v2.py
907f9d76281842d9dcae8cae110d6411 *scripts/import_corpus_v2.py
```

与「动变异之前」的指纹逐字节一致（`diff <(md5sum < scripts/...) <(md5sum < 备份)`
无输出，`grep -c MUTATION` = 0，仓内不留变异标记），随后验收命令复跑 `RC=0`、17 点、
同一 md5 `907f9d76281842d9dcae8cae110d6411`。

## 实现核对

- note 段格式只有一个生产者（`_note`）和一个解析入口（`_note_segments` /
  `_strict_state`）；`ROLE_KEY` / `SEG_KEY` / `SEG_VAL` / `STATE_VALUES` 是段格式的
  单一常量源，测试夹具也经 `IC._note(...)` 造 ours 行，不在测试里手抄格式。
- 存量兼容：`_norm_note` 的全角 `；`→`;` 归一保留（存量 note 里带全角分号的 role
  仍能解析）；`anchors` 回退口径未动；旧行（无 `import_state`、有 `anchors`）判
  complete、旧真半本判 partial 并补齐——原有 12 条回归一字未改、全绿。
- 不认的字面量不猜：`import_state: bogus` 不满足严格段，回退 anchors 口径（判不到
  complete 就当半本续跑，方向是保守的）。
- 未新增列、未改 schema、未改 CLI 参数、未改 note 的**可读形状**（role 不含结构性
  字符时与改前逐字相同）。

## 未自跑

- **未自跑**：真库（`F:\agi\language-genome\data\language_genome.db`）上的实跑
  盘点——本次没查过真库里是否已存在被 role 注入污染的 note 行，也**未对真库执行任何
  写入/迁移**。存量脏行的修复口径靠 `test_legacy_injected_note_still_resumes`
  （夹具行）覆盖；真库若有同类行，重跑该书即会按末段续跑补齐，但**真库上此结论属推演，
  未自跑**。要核真库，命令原文（只读）：

  ```text
  F:/Hermes/hermes-agent/venv/Scripts/python.exe -c "import sqlite3;c=sqlite3.connect('file:F:/agi/language-genome/data/language_genome.db?mode=ro',uri=True);print(c.execute(\"select count(*) from works where note like '%import_state%' and note not like '%segmenter=v2; import_state: %'\").fetchone())"
  ```

- **未自跑**：全量 pytest（本轮只跑了验收文件 + 9 个 import/corpus 邻近文件）。
  全量命令原文：

  ```text
  F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest -p no:cacheprovider
  ```

- **未自跑**：`scripts/import_corpus_v2.py --caveats` / `--clean` 两条开关路径的
  CLI 真跑（本轮回归覆盖其 `import_work` 逻辑，未单独起子进程跑带开关的 CLI）。
- **未做**：git 写操作（无 commit / merge / push / `git config` 改动）、网络调用、
  模型调用、真实语料读取。
- 承重变异的红/绿均为**本会话实跑输出**（原文见上）；凡未实跑的一律在「未自跑」列出，
  无推演冒充输出。

## 仓状态

```text
$ git status --short
 M scripts/import_corpus_v2.py
 M tests/test_import_corpus_v2_resume.py
```

（`PORT_import_resume_role.md` 为本文件。）`data/` 未被创建；`__pycache__/` 属
`.gitignore` 既有忽略项。
