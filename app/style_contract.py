"""语感契约与"干瘪体检"（single source of truth）。

## 为什么长这样（先看这节，别重复踩）

2026-09-20 的 A/B 对照实验（F:\\Hermes\\team\\reports\\style_ab.html）：
同一模型、同一场景卡、只替换写作提示词 —— 现状产出 555 字、交易流水式的场景；
加语感契约后 900 字，物象、潜台词、节奏同时出现，且**事实漂移被验证器抓回**。
结论：**产线语感差是提示词口径问题，不是模型的锅**。

同一天两次失败的尝试（留档，别再走）：
1. 自制词表"语感评分门"给 A/B 两份文本 **都打了满分 1.0** —— 词表抓不到"干瘪"，
   因为干瘪的特征是**缺席**（没有细节），不是**出现**（有坏词）。该门已废弃。
2. 拿项目自己的 `ai_flavor`（集霸 63 条批注训出的 AI 味检测）打真实产物《灯下渡口》：
   **0.003 分，不判为 AI 味**。所以用户的意见方向是"信息交付式的干瘪"，
   与 AI 味是**相反的失败模式**——本模块的检查项必须覆盖两侧，不能只防 AI 味。

因此本模块只做两件事：
- 提供**契约文本**（供写手提示词引用，单一真源，避免口径漂移）；
- 提供**可数的代理指标**（干瘪体检），明确标注"这是代理不是裁判"：
  只用于触发修稿回环里的一条具体指令，不产出"写得好不好"的结论。
"""

from .ai_flavor import analyze_v2, obvious

# 供写手提示词引用的契约正文。硬约束（计划事件/状态变化必须成立）不在本文件，
# 由 verifier 与 validate_review 负责，本文件不得越权改设定语义。
STYLE_CONTRACT = """【本场明确授权：非持久细节不仅要写，而且必须写足】
环境的光线与温度、手上的触感、声音的远近、气味、呼吸与停顿的长度、人物注意到但没有说出口的东西。
这些**不算**"新造设定"：只要不改变计划中的事实键、不新增可被后续场景引用的持久状态，就一律允许。
把每个情节节点落到可感之物上；抽象概括不能单独成句。

【语感要求】
1. 具体落地：每个计划事件都要有承载它的具体细节，不能只写"谁做了什么"。
2. 句长起伏：30 字以上的长句与 8 字以内的短句交替，不要连续三句长度相近。
3. 对话潜台词：不说破动机，用中断、改口、答非所问、动作代替解释；一段对话至少一处"该说而未说"。
4. 篇幅与节奏：按 min_chars 写足，段落 4~9 句，叙述与对话交错，不要通篇对白。

【同时禁止另一侧的过度】（项目 ai_flavor v2 检测的痕迹，命中即为返工）
- 解释过度：给出细节后替读者解读它的意义（"这半寸不碍礼数，却让…"）；
- 陈词比喻：连风都停了 / 静得能听见针落 / 宛如一幅画卷；
- 语域打架：叙述里文言虚词扎堆，或口语混进叙述；
- 微表情模板与情绪直说：嘴角勾起弧度、心中不禁一紧、感到一阵暖意。

【表达策略的正确用法】
克制类手法（留白、间接表态、动作一笔带过）只在"该收的地方"用，**不得当成全文基调**：
该给的具体细节必须给足，否则写出来是干瘪的电报体。

【人类文风锚点（只学语感，不要抄内容）】
"从未离开不代表不知道如何离开。"小鬼沉默了片刻："等到客人拯救了雪国。我自然会送客人离开。"
—— 短句、间接表态、沉默落地、不解释过程。
"""

# 代理指标阈值。刻意粗：只拦"明显"的干瘪与明显的 AI 味（与集霸定的口径一致）。
SENSORY_FLOOR = 12.0        # 感官词/千字（实测：干瘪版 18.0‰、契约版 23.3‰；门槛取低值只拦极端）
SENTENCE_FLOOR = 12.0       # 平均句长（低于此多为电报体）
FLAVOR_CEILING = 0.5        # ai_flavor.obvious 的默认阈值

SENSORY_WORDS = ("光", "影", "声", "响", "气味", "味道", "冷", "暖", "湿", "烫", "凉", "热",
                 "风", "雾", "雨", "雪", "寂", "静", "哑", "闷", "触", "抚", "压", "痛", "麻",
                 "痒", "颤", "抖", "沉重", "柔软", "粗糙", "冰凉", "温热", "喘", "呼吸", "咽",
                 "咳", "汗", "泪", "血", "缩", "紧", "松")

_SENT_END = "。！？…"


def sentences(text: str) -> list:
    """按句末标点切句；收尾的引号/括号并入上一句。"""
    out, cur = [], ""
    for ch in text:
        cur += ch
        if ch in _SENT_END:
            out.append(cur)
            cur = ""
    if cur.strip():
        if out and cur.strip() in ("\u201d", "\u300d", "\u300f", "\uff09", ")"):
            out[-1] += cur
        else:
            out.append(cur)
    return [s.strip() for s in out if s.strip()]


def probe(text: str, *, min_chars: int = 0) -> dict:
    """干瘪体检：只报可数代理指标 + 项目 AI 味检测分，不做文笔裁判。"""
    sents = sentences(text)
    lens = [len(s) for s in sents] or [0]
    mean = sum(lens) / len(lens)
    var = sum((x - mean) ** 2 for x in lens) / len(lens)
    chars = max(1, len(text))
    sensory = sum(text.count(w) for w in SENSORY_WORDS)
    flavor = analyze_v2(text)
    return {
        "chars": len(text),
        "min_chars": min_chars,
        "sentences": len(sents),
        "sent_mean": round(mean, 1),
        "sent_sd": round(var ** 0.5, 2),
        "sensory_per_1k": round(sensory / chars * 1000, 1),
        "paragraphs": len([p for p in text.split("\n") if p.strip()]),
        "flavor_score": flavor.score,
        "flavor_obvious": obvious(text, FLAVOR_CEILING),
        "flavor_kinds": dict(flavor.kinds),
    }


def issues(text: str, *, min_chars: int = 0) -> list:
    """把体检结果变成修稿指令（与 Review.issues 同形：kind/quote/instruction）。

    仅在明显越界时给出，且 kind 恒为 "style" —— 绝不产生 hard 结论，不改变事实校验语义。
    """
    p = probe(text, min_chars=min_chars)
    quote = (sentences(text) or [text[:40]])[0][:60]
    out = []
    if min_chars and p["chars"] < min_chars * 0.9:
        out.append({"kind": "style", "quote": quote,
                    "instruction": f"正文 {p['chars']} 字，低于计划下限 {min_chars} 字："
                                   "把【非持久细节授权】允许的环境、触碰、声音、停顿写足，而不是拉长句子。"})
    if p["sensory_per_1k"] < SENSORY_FLOOR:
        out.append({"kind": "style", "quote": quote,
                    "instruction": f"感官密度 {p['sensory_per_1k']}‰ 低于代理门槛 {SENSORY_FLOOR}‰："
                                   "每个计划事件都要有一个可感的承载物（物、声、光、温度、手上的触感）。"})
    if p["sent_mean"] < SENTENCE_FLOOR:
        out.append({"kind": "style", "quote": quote,
                    "instruction": f"平均句长 {p['sent_mean']} 字偏电报体：把动作与感知展开成完整句，"
                                   "长短句交替（30 字以上与 8 字以内）。"})
    if p["flavor_obvious"]:
        out.append({"kind": "style", "quote": quote,
                    "instruction": "项目 AI 味检测判为明显（分数 " + str(p["flavor_score"]) + "，痕迹：" +
                                   "、".join(p["flavor_kinds"]) + "）：删掉替读者解读细节意义的句子与陈词比喻，"
                                   "只留动作、物件与身体反应。"})
    return out
