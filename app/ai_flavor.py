"""AI 味检测器（2026-09-18，集霸定目标）。

## 目标变了，工具就得换

集霸原话：

> 「因为文字独有的复杂性其实你很难找到一个定性的标准，我觉得可以适当的降低要求，
>   只要不是出现很明显的 ai 味道就行了」

也就是说：**不做"什么算好"的定性标准，只判"有没有明显的 AI 味"**。
这条比原来的目标可落地得多，因为它是**否定式 + 有对照组**的：
- 劣化数据集（18 类）本来就是"AI 味"的**构造性样本**（过度解释 / 情绪直说 / 微表情模板…）；
- 集霸的 **63 条划词批注**（用词 29 / 解释过度 19 / 其他 14 / 逻辑 1）是人工真值，
  且带 `target`（标的是候选侧还是人类侧）与字符偏移。

## 两层结构（贵的那层只用于边界样本）

1. **规则层**（本模块主体，零成本）：可数的表层痕迹——微表情模板、情绪直说、
   抽象升华、解释性连接词、副词/叠词/明喻密度、四字格堆叠、排比。
2. **模型层**：规则判不了的边界样本再问 LLM（`judge_ai_flavor`）。

## 与之前五次失败的区别（别混）

§⑱ 记的五次失败是"**在自然候选里找出哪边更克制**"——那是没有对照组的糊问题。
这里问的是"**这段里有没有这些具体的痕迹**"，是**检测**不是**比较**，
而且有构造性正样本与人工批注做验证。所以这不是重复那条死路。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── 规则表 ─────────────────────────────────────────────────────
# 每条 = (模式, 名称, 权重, 对应劣化类型/批注类别)
# 权重只区分"这套痕迹有多强的 AI 味"，不是精确标定；由 eval 脚本按真值回收验。
PATTERNS: list[tuple[str, str, float, str]] = [
    # 微表情模板（集霸批注里最典型的一类 AI 味）
    (r"嘴角(微微|轻轻|不自觉地)?(勾起|扬起|上扬|弯起)[^，。]{0,6}(弧度|笑意)?", "微表情·嘴角", 1.0, "MICRO_EXPRESSION_TEMPLATE"),
    (r"(眼中|眸中|眼底|眸光)(闪过|掠过|划过)[^，。]{0,6}(一丝|一抹|一缕|一抹)", "微表情·眼中闪过", 1.0, "MICRO_EXPRESSION_TEMPLATE"),
    (r"瞳孔(骤然|猛地|微微|不自觉地)?(一缩|骤缩|收缩)", "微表情·瞳孔", 0.8, "MICRO_EXPRESSION_TEMPLATE"),
    (r"眉头(微不可察地|几不可察地|微微一?)(皱|蹙|拢)", "微表情·眉头", 0.8, "MICRO_EXPRESSION_TEMPLATE"),
    # 情绪直说（把该由动作表现的情绪直接命名）
    (r"(心中|心里|心头)(不禁|顿时|猛地|蓦地)?(一紧|一沉|一暖|一凛|一颤|一酸)", "情绪·心中一X", 0.9, "EMOTION_LABEL"),
    (r"(感到|觉得|涌起|升起)[^，。]{0,4}(一阵|一股|一丝)[^，。]{0,6}(暖意|凉意|寒意|酸楚|愧疚|愤怒|喜悦)", "情绪·直说", 0.9, "EMOTION_LABEL"),
    (r"(十分|非常|极其|格外|异常)(愤怒|震惊|失望|悲伤|喜悦|紧张|兴奋|难过)", "情绪·程度副词+情绪", 0.8, "EMOTION_LABEL"),
    # 抽象升华 / 点题
    (r"(仿佛|似乎|好像)(在)?(诉说|暗示|预示|宣告|低语|呢喃)", "升华·仿佛在诉说", 1.0, "ABSTRACT_SUMMARY"),
    (r"一切(都)?(仿佛|似乎|好像)?(在)?(悄然|默默)?(改变|苏醒|不同|注定)", "升华·一切都在", 0.9, "ABSTRACT_SUMMARY"),
    (r"(某种|一股|一丝)(说不清|道不明|难以言说|莫名的)[^，。]{0,8}(情绪|感觉|力量|东西)", "升华·莫名情绪", 0.8, "ABSTRACT_SUMMARY"),
    # 解释过度（把因果/动机说破）
    (r"(之所以|正是因为|这是因为|原来如此|可见|显然|无疑|毕竟)", "解释·因果显说", 0.7, "OVER_EXPLAIN"),
    (r"他(这么做|这样做|这样说|如此做)[^，。]{0,4}(是|为了|只因)", "解释·动机补全", 0.9, "OVER_EXPLAIN"),
    (r"(其实|实际上)(他|她|它)?(是|在|想|知道|明白)", "解释·其实", 0.7, "PSYCHOLOGY_LABEL"),
    # 叙述者判断
    (r"(这(显然|无疑|分明)是|不得不说是|可谓是|实在是令人|让人不禁)", "叙述者·下判断", 0.8, "NARRATOR_JUDGMENT"),
    # 明喻套话
    (r"(宛如|犹如|仿佛|好似|如同|恰似)[^，。]{2,10}(一般|似的|一样)", "修辞·明喻套话", 0.6, "LITERARY_OVERWRITE"),
    # 叠词/轻柔副词滥用
    (r"(缓缓|轻轻|静静|悄悄|默默|徐徐|淡淡|微微|渐渐)地?", "修辞·轻柔叠词", 0.5, "ADVERB_INFLATION"),
    # 连接词密集（单条轻，靠密度计）
    (r"(于是|因此|然而|紧接着|随即|不由得|不禁|继而|旋即)", "连接·显式逻辑", 0.35, "LOGIC_CONNECTOR_INFLATION"),
]

_COMPILED = [(re.compile(p), name, w, kind) for p, name, w, kind in PATTERNS]

# 密度类指标（按每 100 字归一）
_DENSITY = {
    "地": (r"地(?=[\u4e00-\u9fff])", 3.0),        # "…地X" 状语
    "明喻": (r"(像|似|如|仿佛|宛如|犹如|好似)", 1.0),
    "四字格": (r"[^\s，。！？；：“”]{4}(?=[，。、；])", 0.0),   # 只记数，不直接计分
    "排比": (r"[^，。]{1,6}着，", 0.8),
}


@dataclass
class FlavorHit:
    pattern: str
    kind: str
    weight: float
    start: int
    end: int
    text: str


@dataclass
class FlavorReport:
    score: float = 0.0                 # 0~1，越高越有 AI 味
    hits: list[FlavorHit] = field(default_factory=list)
    per_100: dict = field(default_factory=dict)
    n_chars: int = 0

    @property
    def kinds(self) -> dict:
        d: dict[str, int] = {}
        for h in self.hits:
            d[h.kind] = d.get(h.kind, 0) + 1
        return d


def analyze(text: str, *, hit_weight: float = 0.11, density_weight: float = 0.05) -> FlavorReport:
    """规则层打分。

    分数 = 命中加权和 / 归一化 + 密度惩罚。**不做"哪边更好"的判断**，
    只回答"这段里有多少可数的 AI 味痕迹"——按集霸定的口径，只拦"很明显的"。
    """
    t = text or ""
    rep = FlavorReport(n_chars=len(t))
    if not t.strip():
        return rep
    total = 0.0
    for rx, name, w, kind in _COMPILED:
        for m in rx.finditer(t):
            rep.hits.append(FlavorHit(name, kind, w, m.start(), m.end(), m.group(0)))
            total += w
    # 长度归一：以 100 字为基准；短文本不因长度吃亏
    per100 = max(1.0, len(t) / 100.0)
    score = total / per100 * hit_weight
    dens = {}
    for label, (rx, w) in _DENSITY.items():
        n = len(re.findall(rx, t))
        rate = n / per100
        dens[label] = round(rate, 2)
        if w:
            score += max(0.0, rate - 1.0) * w * density_weight
    rep.per_100 = dens
    rep.score = round(min(1.0, score), 4)
    return rep


def obvious(text: str, threshold: float = 0.5) -> bool:
    """按集霸"只要不出现很明显的 AI 味就行了"——只拦明显的。"""
    return analyze(text).score >= threshold


# ── 模型层（可选，边界样本用）─────────────────────────────────
PV = "ai_flavor_v1"
SYSTEM = ("你是中文小说的 AI 味检查员。只指出文本里**像 AI 写的**痕迹，"
          "不评价文笔好坏，不改写。判据用下面这张清单，逐条核对。")
PROMPT = """检查这段中文小说有没有下列 AI 味痕迹：

{catalog}

【文本】
{text}

只输出一行 JSON：
{{"hits": [{{"pattern": "上面清单里的名字", "quote": "原文片段"}}],
  "obvious": true|false}}"""


def catalog_text() -> str:
    names = sorted({name for _p, name, _w, _k in PATTERNS})
    return "\n".join(f"- {n}" for n in names)


def judge_ai_flavor(text: str, model: str = "moonshotai/kimi-k3") -> dict | None:
    """规则层判不了的边界样本才调它（有成本，别当默认路径）。"""
    import json as _json
    from .gateway import chat
    from .prompt_render import render
    r = chat(model=model, system=SYSTEM,
             user=render(PROMPT, catalog=catalog_text(), text=text),
             purpose="ai_flavor", prompt_version=PV, temperature=0.0, max_tokens=2000)
    t = (r.text or "").strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        d = _json.loads(t[i:j + 1])
        return d if isinstance(d, dict) else None
    except Exception:
        return None

# ── v2：按集霸 63 条批注的真实结构重写（2026-09-18）──────────────
# 为什么 v1 失败（实测：在他批注上的命中率 33%，低于抛硬币）：
#  v1 盯的是**模板词**（`嘴角勾起`/`心中一紧`），而他标的是两类完全不同的东西：
#   ① 用词类（29 处）：**搭配/语域不对**——`连风都停在半空`（陈词比喻）、
#      `她停了一拍`（借用量词）、`整个人松松的`（口语混进叙述语域）、
#      `立刻无声无息的站立而起`（的地混用）。
#   ② 解释过度类（19 处）：**叙述者替读者解读细节的意义**——
#      `只是肩背微微偏了半寸` + `那半寸不碍礼数，却让她的叩首比旁人更深地落在父亲脚前`。
#      要点是"细节 + 它的意义解释"这个**结构**，不是某个词。
V2_PATTERNS: list[tuple[str, str, float, str]] = [
    # ① 解释过度：具体细节 + 叙述者给出意义/分量/象征
    (r"[^。；]{2,30}(却让|便让|使得|更显得|不碍|意味着|象征着|昭示|印证|读作|像是要说明)[^。；]{2,30}", "解读·细节的意义", 1.0, "OVER_EXPLAIN"),
    (r"(仿佛|似乎|像是)[^。；]{2,20}(在|要)(诉说|暗示|宣告|证明|提醒|告诉)", "解读·仿佛在诉说", 1.0, "OVER_EXPLAIN"),
    (r"(不碍|无碍)[^。；]{0,8}(礼数|规矩|体面|分寸|大局)", "解读·不碍X却", 0.9, "OVER_EXPLAIN"),
    # ② 用词：陈词比喻/套话（这些在中文网文里是"读者一眼看出是凑的"）
    (r"(连|就)?(风|空气|时间|声音)(都|也|仿佛)(停|静|凝固|静止|消失)", "陈词·风都停了", 0.9, "LITERARY_OVERWRITE"),
    (r"(静得|安静得)(能|可以)(听见|听到)(针|心跳|呼吸)", "陈词·静得听见", 0.8, "LITERARY_OVERWRITE"),
    (r"(如同|宛如|仿佛|犹如|好似)(同一?|一)(片|个|座|道)?[^，。]{0,8}(画卷|诗篇|雕塑|丰碑|星辰)", "陈词·比喻套话", 0.8, "LITERARY_OVERWRITE"),
    (r"(凤毛麟角|惊涛骇浪|天崩地裂|电光火石|悄无声息|毫无预兆|难以言喻|五味杂陈|不由自主|不约而同)", "陈词·成语填充", 0.6, "LITERARY_OVERWRITE"),
    # ③ 语域打架：文言虚词密度（之/其/乃/亦/矣/焉/颇/甚）在叙述里扎堆
    (r"(之|其|乃|亦|矣|焉|颇|甚|岂|莫非|竟是)[^，。]{0,6}(之|其|乃|亦|矣|焉|颇|甚)", "语域·文言扎堆", 0.7, "LITERARY_OVERWRITE"),
    # ④ 的地混用（这批语料与 AI 输出都常见，是集霸明确标过的一类）
    (r"[一-鿿]{2,4}的(?=[站坐走说看笑])[一-鿿]", "语病·的/地混用", 0.5, "GRAMMAR"),
    (r"(站立而起|浮现而出|扑面而来|油然而生|油然升起)", "陈词·四字动补", 0.5, "LITERARY_OVERWRITE"),
]

V2_COMPILED = [(re.compile(p), name, w, kind) for p, name, w, kind in V2_PATTERNS]


def analyze_v2(text: str, *, hit_weight: float = 0.16) -> FlavorReport:
    """按集霸批注结构重写的打分（v2）。

    与 v1 的区别：不数"模板词"，抓**结构**——「细节 + 替读者解读它的意义」
    与「陈词/语域打架」。归一化同 v1。
    """
    t = text or ""
    rep = FlavorReport(n_chars=len(t))
    if not t.strip():
        return rep
    total = 0.0
    for rx, name, w, kind in V2_COMPILED:
        for m in rx.finditer(t):
            rep.hits.append(FlavorHit(name, kind, w, m.start(), m.end(), m.group(0)))
            total += w
    per100 = max(1.0, len(t) / 100.0)
    rep.score = round(min(1.0, total / per100 * hit_weight), 4)
    return rep

