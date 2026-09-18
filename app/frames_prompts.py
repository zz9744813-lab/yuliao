"""抽取 prompt：版本化登记，改动必须新增版本号。

所有抽取模板共享的硬约束（防泄漏的关键）：
1. 输出**只**允许 JSON，不允许任何解释文字
2. Frame 描述"意思"，禁止复用原文措辞（任何超过 6 字的连续片段）
3. 拿不准的字段留空 / null，禁止编造
"""

EXTRACT_V1 = {
    "S": """你是一名中文小说语义分析员。把下面这段小说压缩成"粗粒度语义骨架" Frame-S。

只抽取三样东西：
- event：这段文字客观发生了什么（一句话，中性，不加心理分析）
- intention：人物此刻的意图（从行为/对白推，不要求作者说了）
- reader_effect：希望读者读到这里时感受到什么

硬约束：
- 禁止引用原文超过 6 字的连续片段
- 不允许使用原文的比喻、修饰语
- 拿不准就填 null，不要编造
- 只输出 JSON，不要任何解释

输出 schema：
{"event": "...", "intention": "...", "reader_effect": "..."}

原文：
«{text}»""",

    "M": """你是一名中文小说语义分析员。把下面这段小说压缩成"中粒度语义骨架" Frame-M。

你需要提取：
- event：客观主线（一句话）
- intention：人物此刻意图
- reader_effect：读者应获得的效果
- facts：关键事实列表，每条标注 certainty（certain/likely/speculative）和 source（谁知道的）
- character_state：visible_emotion（外显）、hidden_emotion（内隐）、goal、intention、knows（知道什么）、does_not_know（不知道什么）
- reader_should_infer：读者应该能推断出什么（不许作者直说）
- must_not_state：哪些信息绝不能直接写出来
- expression_constraints：
  - explicitness: low|mid|high（信息显式化程度）
  - psychological_explanation_allowed: 是否允许直接解释心理
  - dialogue_allowed: 是否允许对话
  - rhythm_target: 节奏要求（可选）

硬约束：
- 禁止引用原文超过 6 字的连续片段
- 不允许使用原文的比喻、修饰语
- 信息以"谁可以知道"为准：原文只暗示的，你就要标 must_not_state
- 拿不准就留空 / null，不要编造
- 只输出 JSON，不要任何解释

原文：
«{text}»""",

    "L": """你是一名中文小说语义分析员。把下面这段小说压缩成"细粒度语义骨架" Frame-L。

在 Frame-M 全部字段的基础上，再补这些：
- beats：把这段拆成有顺序的微事件/动作节拍（每条一句话）
- pauses：哪些节拍之间有明显停顿、空白
- emotion_intensity：情绪强度 0~1
- observation_focus：视线/感知先后聚焦在什么上
- information_focus：信息焦点落在哪个细节
- dialogue_intent：对话在这段的目的是什么（不出现对话就 null）
- pov：叙述视角贴在谁身上
- rhythm：节奏提示（用描述，如"快-慢-停"，不要写句子）

硬约束：
- 禁止引用原文超过 6 字的连续片段
- 不允许使用原文的比喻、修饰语
- beats 描述顺序，禁止照抄原文句子
- 拿不准就留空 / null，不要编造
- 只输出 JSON，不要任何解释

输出 schema（在 Frame-M 的 schema 基础上追加）：
{
  "event": "...",
  "intention": "...",
  "reader_effect": "...",
  "facts": [{"statement": "...", "certainty": "certain|likely|speculative", "source": "..."}],
  "character_state": {"visible_emotion": "...", "hidden_emotion": "...", "goal": "...", "intention": "...", "knows": [], "does_not_know": []},
  "reader_should_infer": [],
  "must_not_state": [],
  "expression_constraints": {"explicitness": "low|mid|high", "psychological_explanation_allowed": true|false, "dialogue_allowed": true|false, "rhythm_target": "..."},
  "beats": ["..."],
  "pauses": ["..."],
  "emotion_intensity": 0.0,
  "observation_focus": "...",
  "information_focus": "...",
  "dialogue_intent": "...",
  "pov": "...",
  "rhythm": "..."
}

原文：
«{text}»""",
}

# extract_v2（2026-09-14，M-gap 根因修复）：
# 根因 = V1 的 M 模板没有「输出 schema」JSON 块（S/L 都有），模型只能按文字描述
# 自造字段名，把 facts[].statement 写成 content → 18/20 失败 M 是字段改名，
# 内容本身合格。V2 补 M 的 schema 块 + 两处钉死字段名；L 加同名约束行。
_EXTRACT_V2_M = EXTRACT_V1["M"].replace(
    "- 只输出 JSON，不要任何解释\n\n原文：",
    "- 只输出 JSON，不要任何解释\n"
    "- 字段名必须与 schema 逐字一致（facts 里每条用 \"statement\"，不要写 content 等别名）\n"
    "- 列表类字段（facts/reader_should_infer/must_not_state/knows/does_not_know）"
    "必须输出 JSON 数组，不要用字符串、分号拼接或 null 代替\n"
    "- character_state 是单一角色的状态（贴 POV 角色），不要按角色拆成数组\n"
    "- 拿不准的**字符串字段**才留 null；列表字段拿不准就输出 []\n\n"
    "输出 schema：\n"
    "{\"event\": \"...\", \"intention\": \"...\", \"reader_effect\": \"...\",\n"
    " \"facts\": [{\"statement\": \"...\", \"certainty\": \"certain|likely|speculative\", \"source\": \"...\"}],\n"
    " \"character_state\": {\"visible_emotion\": \"...\", \"hidden_emotion\": \"...\", \"goal\": \"...\", \"intention\": \"...\", \"knows\": [], \"does_not_know\": []},\n"
    " \"reader_should_infer\": [], \"must_not_state\": [],\n"
    " \"expression_constraints\": {\"explicitness\": \"low|mid|high\", \"psychological_explanation_allowed\": false, \"dialogue_allowed\": true, \"rhythm_target\": \"...\"}}\n\n"
    "原文：",
)
assert _EXTRACT_V2_M != EXTRACT_V1["M"], "M 模板 replace 未命中"
_EXTRACT_V2_L = EXTRACT_V1["L"].replace(
    "- 只输出 JSON，不要任何解释\n\n输出 schema",
    "- 只输出 JSON，不要任何解释\n"
    "- 字段名必须与 schema 逐字一致：facts 里每条用 \"statement\"（不要写成 content 等别名）\n"
    "- 列表类字段必须输出 JSON 数组，不要用字符串、分号拼接或 null 代替\n"
    "- character_state 是单一角色的状态（贴 POV 角色），不要按角色拆成数组\n\n输出 schema",
)
assert _EXTRACT_V2_L != EXTRACT_V1["L"], "L 模板 replace 未命中"

EXTRACT_V2 = {**EXTRACT_V1, "M": _EXTRACT_V2_M, "L": _EXTRACT_V2_L}

PROMPT_VERSION = "extract_v2"
