"""主工作流 B —— Controlled Corruption（总方案 §7 / §4.6 / §8 / 任务清单第 6 项）。

## 为什么这是目前最大的空缺

已有数据全是「人类原文 vs 模型重建」：两边差**很多变量**，无论谁赢都不知道是哪个变量
造成的。集霸给的批注（「解释过度」17 处全在候选侧、标了它的题候选胜率 0/17）指向
**一个**语言变量——克制/铺陈——但**五次自动量化全部失败**（词表 AUC 0.427、LLM 全局
0.24、few-shot 0.17、span 级低于随机、帧约束违规 0.273 vs 0.262）。

那些失败有一个共同点：**比较的两边本来就差着无数变量**，想从混杂里干净地切出
"解释过度"这一项，信噪比太低。受控劣化把这个变量**单独拧出来**：

    人类原文 → 只改「过度解释」这一个变量 → 劣化版

于是：
1. **训练数据**（§42）：human=chosen / corruption=rejected 的 DPO pair，
   且**知道 reject 的理由是哪一个变量**——这是"让 AI 会写"最直接的教材。
2. **仪器的可判别性标尺**：连单变量劣化都分不出来的评委，不足以支撑任何 κ 结论。
   已有结论「评委与集霸方向相反（评委选候选 53–74%，集霸 26.5%）」到底
   是"评委口味不同"还是"评委根本读不出这类差异"，用劣化对照可以直接分开。
3. **反例**（§7 要求 5）：某些劣化在特定语境下未必是劣化。**评委反而偏好劣化版**
   的那几条，是信息量最大的样本（既有硬例库 §10 的进入条件）。

## 口径

- **单变量**：每个变体只在 prompt 里指定一个可改的变量，其余一律冻结（事实/事件/
  人物/动作/结果/时间顺序/信息量/大致长度）。
- **自动验证漂移**（§7 要求 3）：校验器是**另一个模型**（不做自校验），
  按 §11.1 SemanticVerifier 口径**只判信息**（事实一致性 / 增加 / 丢失 / 漂移），
  **禁止评价文采**。确定性长度比一并硬卡（>1.8 或 <0.55 直接拒）。
- 落库：`controlled_corruptions` + 一条 `candidates`（prompt_version='corrupt_v1'）。
  候选进的是**独立实验**，且 `corrupt_v1` 不在 `BLIND_REVIEW_PROMPT_VERSIONS` 里
  → 随机抽样池永远捞不到（回归测试锁定）。

用法：
    python scripts/controlled_corruption.py --dry-run
    python scripts/controlled_corruption.py --n-seg 12 --types EXPLICITIZE,OVER_EXPLAIN
    python scripts/controlled_corruption.py --report
    python scripts/controlled_corruption.py --build-batch corr16 --n 16   # 给集霸端一批
"""
from __future__ import annotations

import argparse
import json
import random
import re
import secrets
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import config, db  # noqa: E402
from app.gateway import bind_experiment, chat  # noqa: E402
from app.models import (Candidate, ControlledCorruption, Experiment, Frame,  # noqa: E402
                        ReviewItem, Segment, Work, exclude_corpus_v2_segments)
from app.prompt_render import render  # noqa: E402
import preflight_models as pf  # noqa: E402  # 批量防呆①：开跑前校验模型名在网关池内

sys.path.insert(0, str(ROOT / "scripts"))
from make_random_batch import extras_start, looks_watermarked  # noqa: E402

# v2（2026-09-18 生成侧）：加"标点照抄 + 不许病句"两条冻结约束。
# 起因：v1 的 PARALLELISM_OVERUSE 产出「黑夜越来越寒冷着」这种病句、
# RHYTHM_FLATTEN 顺带把 " " 换成 「 」——**变量被污染**：
# 读者分辨的是"通不通顺"，不是"排比有没有过度"。这类对照数据等于作废。
GEN_PV = "corrupt_v2"
# v2：把「与原文明说的事实矛盾」与「增补解说」拆开（见 VERIFY_PROMPT）
# v3：加 grammatical 字段（只查病句/标点错乱，不评文风）——用于**审计**生成器有没有
#     把"表达方式劣化"做成"语法劣化"。不做硬拒（有些类型天然读起来别扭）。
VERIFY_PV = "corrupt_verify_v3"
DEFAULT_GEN = config.DEFAULT_LLM_MODEL
DEFAULT_VERIFY = "z-ai/glm-5.3"

# 长度硬卡：单变量改写不该把长度翻倍或砍半（超了就说明改了不止一个变量）
RATIO_HI, RATIO_LO = 1.8, 0.55
DRIFT_MAX = 0.35          # 语义漂移阈值（校验器 0~1）

# ── bal-v2 长度方向规格（docs/proposal-length-balanced-regen-20260920.md）──
# L = variant 更短（human 更长——长度混淆的正交层，全库只有 ~19 对，必须重生成）；
# S = variant 更长；None = any（沿用全局 0.55~1.8 卡）。接受窗为**闭区间**
# （端点接受、±0.01 外拒收）——规格先于实现，tests/test_len_spec.py 按此写死。
LEN_WINDOW_L = (0.60, 0.92)
LEN_WINDOW_S = (1.08, 1.80)
# pilot 放宽档（提案 §1.5 预注册协议：round-1 接受率 <30% 的类型放宽
# ±0.05 再 pilot 一轮）。pilot round-1 实测（EXP-BAL2-L1，18 条）：
#   SUBTEXT_ERASE 2/3、LITERARY_OVERWRITE 2/3（67%，保持标准窗）；
#   RHYTHM_FLATTEN 0/3 但两发 ratio=0.96 在标准窗外沿——放宽后可收；
#   ABSTRACT_SUMMARY/EMOTION_LABEL/DIALOGUE_EXPOSITION 0/3 且
#   ratio 1.00~1.41（模型不压缩，放宽大概率救不活，round-2 见分晓）。
# LEN_WINDOW_L_WIDE：**保留**（无产线引用，非死常量）——它是 pilot round-2
# 放宽档的历史规格记录（提案 §1.5 协议执行的证据），tests/test_len_spec.py
# 的边界用例钉住它。删除会抹掉协议执行的可复核痕迹。
LEN_WINDOW_L_WIDE = (0.60, 0.97)
assert (LEN_WINDOW_L[1] < LEN_WINDOW_L_WIDE[1] < LEN_WINDOW_S[0]),     "窗链必须单调：标准 L 上界 < 放宽上界 < S 下界（0.98~1.02 重叠会失效）"
COMPRESSION_TYPES = ("SUBTEXT_ERASE", "ABSTRACT_SUMMARY", "RHYTHM_FLATTEN",
                     "LITERARY_OVERWRITE", "EMOTION_LABEL", "DIALOGUE_EXPOSITION")
# 膨胀型与控制臂保持 any：膨胀型天然产 S（S 侧既有来源），控制臂是中性改写
# 不许进方向窗（§7.5 纪律 2 的镜像）。S 窗已定义、边界用例已测，供显式指派用。
# per-type 分派按 pilot 两轮 + kimi 诊断的定论（提案 §1.5 协议上限两轮）：
# · SUBTEXT_ERASE / LITERARY_OVERWRITE：deepseek 下 2/3=67%，产线保留标准窗；
# · 其余 4 类两轮全灭（round-1 0/3×4；round-2 放宽 +0.05 后 0/4×4，ratio
#   1.00~1.46；换 kimi 当生成器同样越窗 1.05~1.26 或直接生成失败，
#   EXP-BAL2-DIAG）→ **类型问题，不是单模型问题**，记「本代模型不可产出」。
#
# 语义三分（会审 qwen [严重] 修复——None 是「无窗=any」，不是「排除」）：
#   · window 窗口（TYPE_LEN_SPEC）：校验侧的长度闸。**6 类全部保留 L 窗**
#     ——不可产出类的 ratio 1.0~1.46 样本必须被校验拒收，删键会让它们
#     以压缩类型标签混过全局卡（0.55~1.8）——数据污染且入库不可逆；
#   · UNPRODUCTIVE_TYPES：调度侧的排除。产线分派跳过这 4 类（不烧预算），
#     显式请求也跳过并打印——「不再生成」与「校验仍拒」是两个不同的层。
TYPE_LEN_SPEC: dict[str, tuple[float, float] | None] = {
    "SUBTEXT_ERASE": LEN_WINDOW_L,
    "LITERARY_OVERWRITE": LEN_WINDOW_L,
    # 以下 4 类已判「本代模型不可产出」：显式写 None（区别于漏填——
    # 覆盖断言保证 6 类都有键）。调度侧由 UNPRODUCTIVE_TYPES 排除，
    # 校验侧由 window_for 返回的 EXCLUDED_WINDOW 哨兵显式拒收——
    # 三层各司其职，None 不再承担「排除」语义。
    "RHYTHM_FLATTEN": None,
    "ABSTRACT_SUMMARY": None,
    "EMOTION_LABEL": None,
    "DIALOGUE_EXPOSITION": None,
}
UNPRODUCTIVE_TYPES: frozenset[str] = frozenset({
    "RHYTHM_FLATTEN", "ABSTRACT_SUMMARY", "EMOTION_LABEL", "DIALOGUE_EXPOSITION",
})
# 压缩型的 spec 必须全覆盖：新增压缩型漏填会静默降级 any（同一条污染路径），
# 本断言让它当场炸而不是静默放行。
assert all(t in TYPE_LEN_SPEC for t in COMPRESSION_TYPES),     "COMPRESSION_TYPES 必须全部有窗——漏填=静默 any=污染"


def dispatchable(ctypes: list[str]) -> list[str]:
    """调度侧排除：不可产出类跳过（显式请求也跳过），打印被跳过清单。"""
    keep = [t for t in ctypes if t not in UNPRODUCTIVE_TYPES]
    skipped = [t for t in ctypes if t in UNPRODUCTIVE_TYPES]
    if skipped:
        print(f"[不可产出] 跳过 {len(skipped)} 类（pilot 两轮+kimi 诊断全灭，"
              f"见 UNPRODUCTIVE_TYPES 注释）：{','.join(sorted(skipped))}")
    return keep


# 「排除」哨兵（会审整改：None=any 是「不约束」，不是「排除」——两个语义
# 必须显式区分，不得静默当 any）。window_for 对不可产出类返回它；
# window_accepts / judge_verify 都有显式分支接住。
EXCLUDED_WINDOW = "excluded"


def window_for(ctype: str):
    """排除态优先于窗值：不可产出类返回 EXCLUDED_WINDOW（显式排除），
    其余查表（窗 or None=any）。全仓无 TYPE_LEN_SPEC[t] 直接下标——
    只有这里读（监督方核对结论，2026-09-20）。"""
    if ctype in UNPRODUCTIVE_TYPES:
        return EXCLUDED_WINDOW
    return TYPE_LEN_SPEC.get(ctype)


def window_accepts(ratio: float, win) -> bool:
    """闭区间判定：win=None 不约束（any）；EXCLUDED 一律不接受（显式分支，
    不是 any 的静默直通）。"""
    if win is EXCLUDED_WINDOW:
        return False
    if win is None:
        return True
    return win[0] <= ratio <= win[1]


def len_directive_for(ctype: str) -> str:
    """压缩型的生成 prompt 附加行（膨胀型/控制臂返回空——不加长度指令）。"""
    if window_for(ctype) == LEN_WINDOW_L:
        return ("\n- 改写后的总字数必须压缩到原文的 60%~90%（这是本变量的"
                "组成部分，越界视为没做这个变量）")
    return ""


def reject_status(why: str) -> str:
    """verify 拒收原因 → 落库状态：越窗走 rejected_length，
    排除态（excluded_type）走 rejected_drift（类型级排除不是长度问题），
    其余 rejected_drift。"""
    if (why or "").startswith("len_window"):
        return "rejected_length"
    return "rejected_drift"

# ── 劣化类型表（§4.6 的 17 类 + NARRATOR_JUDGMENT，共 18）────────────────
# variable  = 本变体**唯一**允许改变的变量
# directive = 给生成器的具体做法
TYPES: dict[str, dict[str, str]] = {
    "EXPLICITIZE": {
        "zh": "显式化",
        "variable": "把原文**一处**留给读者推断的暗含之意（关系/意图/态度）直接写出来",
        "directive": "挑原文里**最要紧的那一处**潜台词，把它写成明明白白的一句话。"
                     "其余各处保持原样，只改这一处。",
    },
    "OVER_EXPLAIN": {
        "zh": "过度解释",
        "variable": "给已有动作**追加**「他为什么这么做」的动机/因果解释",
        "directive": "原句的动作本身不要动，在它后面补上动机或因果解释（他这样做，是为了…／"
                     "因为他知道…）。只补解释，不加新事件。",
    },
    "EMOTION_LABEL": {
        "zh": "情绪直说",
        "variable": "把原本靠动作/生理反应表现的内心情绪**直接命名**",
        "directive": "原文大概是用动作或身体反应暗示情绪的，改成直接说出情绪名称"
                     "（他感到愤怒／心里很紧张／松了口气）。动作可以保留，但情绪必须被点名。",
    },
    "PSYCHOLOGY_LABEL": {
        "zh": "心理解说",
        "variable": "插入对人物内心活动的**解说性**句子（他其实是在…）",
        "directive": "加一到两句心理层面的解说，解释人物此刻的内在状态与动机结构。"
                     "不要写成第一人称独白，要像叙述者在旁边分析。",
    },
    "LITERARY_OVERWRITE": {
        "zh": "文学腔",
        "variable": "把大白话换成**书面化修辞**（四字句、比喻、文言腔）",
        "directive": "意思一个字不改，只把用词换成更「雅」的书面语，可加比喻。"
                     "目标是让句子显得更「文学」，而不是更清楚。",
    },
    "ADJECTIVE_INFLATION": {
        "zh": "形容词膨胀",
        "variable": "给原文的名词/动作**堆上修饰性形容词**",
        "directive": "在名词和动作前加形容词（很/极其/难以言喻的/说不出的…），"
                     "每个关键名词都要带上一到两个修饰语。",
    },
    "ADVERB_INFLATION": {
        "zh": "副词膨胀",
        "variable": "给原文的动词**加「地」字状语**（最多 3~4 处，且每处都要读得通顺）",
        # v2（2026-09-18）：初版写的是"尽量让每个动作都被副词修饰"，结果生成出一片
        # 「地」汤：`不应该地扫描不到` / `不禁地有种` / `稍微地安心` / `大范围地地进行`。
        # 那是**病句**，不是"副词偏多"——集霸当场炸了（"你就叫我吃一坨这种狗屎吗"）。
        # 变量被污染：读者分辨的是"通不通顺"，不是"副词多不多"。故加密度上限 + 通顺硬约束。
        "directive": "给原文的动词加上状语副词（缓缓地／轻轻地／不动声色地…），"
                     "**最多加 3~4 处**。加完必须逐句读一遍：凡是加了不通顺的"
                     "（`不应该地扫描不到`、`不禁地有种`、`稍微地安心`、`大范围地地进行`"
                     "这种）**一律不许加**，宁可少加。原文本来就有副词的位置不要重复加。",
    },
    "LOGIC_CONNECTOR_INFLATION": {
        "zh": "连接词膨胀",
        "variable": "在句间**显式补上逻辑连接词**（于是/因此/然而/紧接着）",
        "directive": "给原本靠语序自然衔接的句子之间插入逻辑连接词，把因果、转折、"
                     "先后关系全部写明。事件的先后顺序不变。",
    },
    "REDUNDANCY": {
        "zh": "冗余重复",
        "variable": "把同一信息用**同义说法再说一遍**",
        "directive": "挑出原文里两到三处关键信息，各用换词的方式再复述一次"
                     "（往前走着，一步一步向前；冷，那股寒意）。",
    },
    "PARALLELISM_OVERUSE": {
        "zh": "强行排比",
        "variable": "把并列/递进改写成**整齐排比或对仗**",
        "directive": "把原文里并列或递进的成分改写成结构完全一致的排比句，使句式过于整齐。"
                     "**每一句都必须语法正确**：只在本来就能带的词后面加「着」，"
                     "形容词不能加「着」（「黑夜寒冷着」是病句，不许出现）；"
                     "宁可少排比，也不许造病句。内容不变，只让句式变整齐。",
    },
    "ABSTRACT_SUMMARY": {
        "zh": "抽象总结",
        "variable": "在段首或段末**追加一句抽象概括/点题**",
        "directive": "原文的具体描写一字不动，只在开头或结尾加一句抽象层面的总结"
                     "（这就是…的道理／一切都在暗示着…／某种东西已经改变了）。",
    },
    "MICRO_EXPRESSION_TEMPLATE": {
        "zh": "微表情模板",
        "variable": "用**套路化微表情描写**替换原文的具体动作细节",
        "directive": "把原文里具体的动作/神态细节，换成人人都在用的套话："
                     "嘴角勾起一抹弧度／眼中闪过一丝复杂／眉头微不可察地皱起／"
                     "瞳孔骤然收缩。内容层面仍指向同一情绪。",
    },
    "DIALOGUE_EXPOSITION": {
        "zh": "对话交代",
        "variable": "把叙述里的信息**塞进人物对话**，让人物自己说出来",
        "directive": "把原文中由叙述交代的某条信息，改写成人物的一句台词说出来。"
                     "说话人必须是原文在场的人；信息内容不变。",
    },
    "POV_DRIFT": {
        "zh": "视点漂移",
        "variable": "插入**原文视角看不到**的另一人物内心/全知视角所知",
        "directive": "在原文的视角之外，补一句只有换到另一个人身上（或上帝视角）才看得见的东西"
                     "（而他没有察觉，身后的那人已经…／他并不知道…）。事实层面不新增事件，"
                     "只换到另一个视点去看同一件事。",
    },
    "SEMANTIC_OVERCOMPLETION": {
        "zh": "语义补完",
        "variable": "把原文**没写的结果**补完（把因果链末端说尽）",
        "directive": "原文停在悬念处或结果未言明，把它补完：说明这件事最终会导致什么／"
                     "那个人接下来一定会…。**不得新增具体事件**，只把原文悬置的结论说出来。",
    },
    "RHYTHM_FLATTEN": {
        "zh": "节奏拉平",
        "variable": "把长短交错的节奏**拉成匀速长句**（去掉短句与停顿）",
        "directive": "原文的短句、断句、停顿全部合并成结构相近、长度接近的陈述长句，"
                     "删除单独的短促句。内容与顺序完全不变。",
    },
    "SUBTEXT_ERASE": {
        "zh": "潜台词抹除",
        "variable": "把整段**所有**暗含之意一并摊到明面",
        "directive": "逐处检查原文每一句：凡是有言外之意、态度、关系、评断的地方，"
                     "全部补一句把它说破。允许改多处，但改的都是同一件事（含蓄→直白）。",
    },
    "NARRATOR_JUDGMENT": {
        "zh": "叙述者判断",
        "variable": "让叙述者**直接对人物或事件下评价判断**",
        "directive": "加一到两句叙述者的评断（这显然是愚蠢的／他到底还是太嫩了／"
                     "这样的沉默比争吵更可怕）。不要写成人物观点，而是叙述者的口吻。",
    },
}

# ── 控制臂（不是劣化类型）─────────────────────────────────────
# 为什么必须有：如果只看"评委认出人类原文"的比例，90% 这个数**有两种解释**——
#   ① 评委读得出被拧出来的那个变量；
#   ② 评委只是能认出"哪边是 AI 改的"（生成文本自带某种可辨认的痕迹）。
# 加一条**中性改写**控制臂就能分开：中性改写同样由 AI 产出、同样经过同一个校验器，
# 但**不要求变差**。若中性臂上评委选人类的比例也远高于 50%，那 ① 的证据就塌了，
# 高判别率只能说明"能认出 AI 手笔"。（§8 Minimal Pair 的对照思路。）
CONTROLS: dict[str, dict[str, str]] = {
    "NEUTRAL_PARAPHRASE": {
        "zh": "中性改写（控制臂）",
        "variable": "不改质量，只换一种说法（同义改写）",
        "directive": "把这段文字换一种说法写出来：同义替换、调整语序、改换句式，"
                     "但**不要求写得更好，也不许写得更差**——保持原文的表达浓度、"
                     "含蓄程度、节奏和篇幅基本不变。这是一条对照臂。",
    },
}
ALL_TYPES: dict[str, dict[str, str]] = {**TYPES, **CONTROLS}

GEN_SYSTEM = ("你是中文小说的受控改写器。你的工作是在**只改变指定变量**的前提下，"
              "把人类作者的段落改写成一个更差的版本。你只做实验需要的改写，不做任何解释。")
GEN_PROMPT = """把一个人类作者写的中文小说段落，改写成**受控劣化版本**。

【人类原文】
{human}

【上文（仅供理解语境，不要改写，不要复述）】
{ctx}

【本次唯一允许改变的变量】
{variable}

【具体做法】
{directive}

【必须冻结、一个字都不能变的东西】
- 事件、人物、动作、结果、时间先后顺序
- 信息的**有**与**无**：原文没写的事实不要补进来，原文写了的事实不要删掉
- 叙述人称与视角（除本变量明确要求改视角时）
- 大致篇幅：改写后在原文的 0.7~1.4 倍之间
- **标点形式**：引号、括号、省略号照抄原文（原文用 " " 就不要换成 「 」）
- **语法合法性**：改写结果必须是通顺合法的中文，不许出现病句
  （词性误用、搭配不成立、给不该加"着/地/的"的词硬加）。

  ⚠ 这条是实验的命门：劣化必须劣在**表达方式**上。如果改出来的是病句，
  读者/评委分辨的是"通不通顺"，而不是"这个变量有没有过度"——
  变量就被污染了，这一条对照数据也就作废了。

段末不要加任何解释、标注或点评。只输出一行 JSON：
{{"text": "改写后的段落", "changed": "你改了哪一处（一句话，具体）", "frozen": "你怎么保证事实层没变（一句话）"}}"""

VERIFY_SYSTEM = ("你是语义校核器。只核对**事件层**是否一致、增补了哪类信息、漂移多大。"
                 "**禁止评价文采、禁止判断好坏**——即使你觉得某一边更好看也不要写进结论。")
# v2（2026-09-18）：v1 把「加了一句对意图的解说」也判成 fact_consistent=false，
# 于是把**本来该放行的那一类**（EMOTION_LABEL / PSYCHOLOGY_LABEL / EXPLICITIZE
# 的增补）大批拒收 —— 而"加这句解说"正是受控劣化要制造的**那一个变量**。
# §7 要的是"只改一个变量"，不是"什么都不许改"。所以 v2 把两件事拆开：
#   · 矛盾（contradicts_source）：变体与原文明说的事实冲突 → 唯一硬拒条件
#   · 增补（added_events / added_non_event）：事件层的增补要单独记，
#     解释/情绪/判断层的增补是**预期之内**的，只记录不拒收
VERIFY_PROMPT = """对照两个版本，只回答事件层的问题。

【原文（人类作者）】
{human}

【变体】
{variant}

1. 矛盾：变体有没有与原文明说的事实**冲突**？
   （例：施动者与受动者对调、结果反转（摔出→稳稳站住）、时空或人数被改、时序被打乱。
   只是"多了一句解释/情绪/评价"**不算**矛盾。）
2. 事件增补：变体**新增了原文没有发生的事件**吗？（"某人做了某事"这类，逐条列出）
3. 非事件增补：变体加了哪些原文没说出口的信息，但**不是新事件**？
   （意图、情绪、心理、判断、评价、因果解释）逐条列出。
4. 信息丢失：原文说了而变体没说的？（逐条列出）
5. 漂移：0~1 的一个数，**只针对事件层**——0=同一件事的另一种说法，
   1=已经是另一件事。

6. 语法：变体里有没有**病句**（词性误用、搭配不成立、硬加的"着/地/的"、
   标点错乱或引号形式被改动）？只查语法，**不评文风好坏**。
   `ungrammatical` 为 true 时，`grammar_issue` 里写出具体那一处。

只输出一行 JSON：
{{"contradicts_source": true|false,
  "contradiction_note": "一句话（无矛盾则留空）",
  "added_events": ["..."],
  "added_non_event": ["..."],
  "lost": ["..."],
  "drift": 0.0,
  "ungrammatical": true|false,
  "grammar_issue": "一句话（无则留空）",
  "notes": "一句话"}}"""

_lock = threading.Lock()
_stat = {"gen_ok": 0, "gen_failed": 0, "verified": 0, "rejected": 0, "skip": 0}


# ── 工具 ──────────────────────────────────────────────────────

def parse_json(text: str) -> dict | None:
    """从模型输出里抠出第一个 JSON 对象（容忍 ```json 围栏与前后废话）。"""
    if not text:
        return None
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        d = json.loads(t[i:j + 1])
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def primary_l_frame(s, segment_id: str) -> Frame | None:
    """段的 L 主帧。

    ⚠ 状态只排除 `failed`，**不能只认 `ok`**：`repaired`（抽取结果被修复器补全过）
    是全项目通用的有效状态——`export_training.py` 等处都写成 `status != "failed"`。
    本轮实测：只认 ok 时，3 个 repaired 段的 54 条变体全部以 `no_frame_id` 收场，
    而帧明明在库里。
    """
    return (s.query(Frame)
            .filter_by(segment_id=segment_id, granularity="L", is_primary=True)
            .filter(Frame.status != "failed")
            .order_by(Frame.created_at.desc()).first())


def frame_brief(frame: Frame | None) -> str:
    """帧的**约束侧**（不许说 / 应留推断 / 表达约束）——给生成器当"别越界"的护栏。"""
    if not frame or not frame.payload:
        return "（无）"
    p = frame.payload
    parts = []
    if p.get("must_not_state"):
        parts.append("不许直接写出：" + "；".join(map(str, p["must_not_state"])))
    if p.get("reader_should_infer"):
        parts.append("应留给读者推断：" + "；".join(map(str, p["reader_should_infer"])))
    if p.get("expression_constraints"):
        parts.append("表达约束：" + json.dumps(p["expression_constraints"], ensure_ascii=False))
    if p.get("pov"):
        parts.append("视角：" + str(p["pov"]))
    if p.get("rhythm"):
        parts.append("节奏：" + str(p["rhythm"]))
    return "\n".join(parts) if parts else "（无）"


def _src_ok(seg) -> bool:
    """源文本完整性：`integrity.src_ok` 必须显式为 true（没查过=不可用）。"""
    import json as _json
    try:
        d = _json.loads(seg.integrity or "{}")
    except Exception:
        d = {}
    return d.get("src_ok") is True


def pick_segments(s, n: int, works: list[str] | None, seed: int,
                  exclude_extras: bool = True, exclude_dirty: bool = True,
                  skip_done: bool = False,
                  min_chars: int = 60) -> list[tuple[Segment, Frame]]:
    """挑有 L 主帧的人类段落。默认排除番外与带水印伪影的段（同随机批口径）。

    min_chars 默认 60：太短的段（语料里最短 2 字）没有可供"单变量劣化"的结构，
    改一处就等于改全段，对照实验的"其余全同"前提不成立。实测四个语料里
    ≥60 字的 L 帧段共 125 个，够用。

    fixture_* 一律排除：那是测试夹具（10 段），进了数据集会污染统计且不可外推。
    """
    q = (s.query(Frame, Segment)
         .join(Segment, Segment.id == Frame.segment_id)
         .filter(Frame.granularity == "L", Frame.is_primary == True,  # noqa: E712
                 Frame.status == "ok"))
    rows = q.all()
    starts: dict[str, int | None] = {}
    if works:
        rows = [(f, g) for f, g in rows if g.work_id in set(works)]
    titles = {w.id: (w.title or "") for w in s.query(Work).all()}
    done = set()
    if skip_done:
        done = {c.segment_id for c in
                s.query(ControlledCorruption).filter(ControlledCorruption.status == "ok").all()}
    out = []
    for f, seg in rows:
        if seg.id in done or not seg.text or len(seg.text.strip()) < min_chars:
            continue
        if titles.get(seg.work_id, "").startswith("fixture"):
            continue
        # 源文本本身缺字/截断的段一律不用（见 scripts/source_check.py）：
        # 已经查过且判坏的 → 跳过；**没查过的也跳过**（宁可少用，不许把
        # 「逃走的五名强。」这种端给集霸——他 2026-09-18 为此发过火）。
        if not _src_ok(seg):
            continue
        if exclude_dirty and looks_watermarked(seg.text):
            continue
        if exclude_extras:
            if seg.work_id not in starts:
                starts[seg.work_id] = extras_start(s, seg.work_id, seg.seg_version)
            st = starts[seg.work_id]
            if st is not None and seg.ordinal >= st:
                continue
        out.append((seg, f))
    out.sort(key=lambda x: x[0].id)
    rng = random.Random(seed)
    per_work: dict[str, list] = {}
    for seg, f in out:
        per_work.setdefault(seg.work_id, []).append((seg, f))
    # 按作品轮流取，尽量覆盖多个语料（单一作品会让结论绑在一种文风上）
    picked, keys = [], sorted(per_work)
    while len(picked) < min(n, len(out)):
        progressed = False
        for k in keys:
            if len(picked) >= min(n, len(out)):
                break
            if per_work[k]:
                picked.append(per_work[k].pop(rng.randrange(len(per_work[k]))))
                progressed = True
        if not progressed:
            break
    return picked


# ── 生成 + 校验 ────────────────────────────────────────────────

def generate_variant(*, human: str, ctx: str, ctype: str, model: str,
                     temperature: float = 0.8) -> dict | None:
    t = ALL_TYPES[ctype]
    user = render(GEN_PROMPT, human=human, ctx=ctx or "（无）",
                  variable=t["variable"],
                  directive=t["directive"] + len_directive_for(ctype))
    # max_tokens 给足（同 verify_variant）：推理系模型会把预算烧在思考上再返回空 content，
    # 网关加倍重试到顶仍是空 → 记 failed。实测 1200 起手时约 15% 生成以 empty_text 收场。
    r = chat(model=model, system=GEN_SYSTEM, user=user,
             purpose=f"corrupt:{ctype}", prompt_version=GEN_PV,
             temperature=temperature, max_tokens=2500)
    return parse_json(r.text)


def verify_variant(*, human: str, variant: str, model: str) -> dict | None:
    user = render(VERIFY_PROMPT, human=human, variant=variant)
    # max_tokens 给足：推理系模型（muse-spark）会把预算烧在思考上，然后返回**空 content**，
    # 网关的加倍重试（800→1600→3200）到顶后仍空 → 最终记 failed。
    # 实测：800 起手时 14% 的校验调用以 "empty content (finish=length)" 收场。
    r = chat(model=model, system=VERIFY_SYSTEM, user=user,
             purpose="corrupt_verify", prompt_version=VERIFY_PV,
             temperature=0.0, max_tokens=3000)
    return parse_json(r.text)


# 「地」汤阈值：变体比原文多出这么多「地」就直接判机械劣化。
# 实测分布（2026-09-18，全部 ok 变体）：Δ=0 有 433 条，Δ≥5 只有 11 条——后者就是
# 「最令他深深地感到失望…悄悄地留下的注解…再也没有缓缓地出现过」这种。
# LLM 校验器会漏判（它认为"语法上说得过去"），所以这里用**确定性**规则兜底：
# 一个类型的机械执行痕迹，用计数就能看出来，不必赌模型。
DE_DELTA_MAX = 5


def mechanical_defect(human: str, variant: str) -> str:
    """确定性机械劣化检测；命中则返回原因，否则空串。"""
    if not human or not variant:
        return ""
    d = variant.count("地") - human.count("地")
    if d >= DE_DELTA_MAX:
        return f"de_delta={d}"
    return ""


def judge_verify(v: dict | None, ratio: float,
                window: tuple[float, float] | None = None) -> tuple[bool, float, str]:
    """把校验器输出折成 (drift_ok, drift_score, 拒绝原因)。

    拒收条件（§7：**只**改一个变量，不是什么都不许改）：
      1. 与原文明说的事实**矛盾**（v2 口径；v1 的 `fact_consistent` 也认，兼容旧数据）
      2. **病句**（`ungrammatical`）——硬拒，见下
      3. 事件层漂移超阈
      4. 长度比越界——长度翻倍说明改的已经不止一个变量
    解释/情绪/判断层的增补**不拒收**：那正是多数劣化类型要制造的那一个变量。

    为什么病句必须硬拒（而不是记个标记）：病句会**污染变量**。读者看到
    「不应该地扫描不到」时分辨的是"通不通顺"，而不是"副词有没有过度"，
    这条对照数据就作废了——集霸 2026-09-18 看到 ADVERB_INFLATION 的初版产物
    当场问"你就叫我吃一坨这种狗屎吗"。宁可少一条数据。
    """
    if v is None:
        return False, 1.0, "verify_parse_failed"
    try:
        drift = float(v.get("drift", 1.0))
    except Exception:
        drift = 1.0
    if "contradicts_source" in v:
        bad = bool(v.get("contradicts_source"))
    else:                                   # v1 旧输出兼容
        bad = not bool(v.get("fact_consistent"))
    if bad:
        return False, drift, "contradicts_source"
    if v.get("ungrammatical"):
        return False, drift, "ungrammatical"
    if drift > DRIFT_MAX:
        return False, drift, f"drift={drift:.2f}"
    if window is EXCLUDED_WINDOW:
        # 排除态显式分支（会审整改）：不可产出类的任何新样本一律拒收，
        # 理由带 excluded_type 前缀——与长度越窗（len_window）分诊可区分。
        return False, drift, "excluded_type"
    if window is not None:
        # bal-v2 方向窗（闭区间）：窗界在全局界内，设窗时窗检查即全覆盖。
        # len_window 前缀是状态分诊键——reject_status 靠它落 rejected_length。
        if not window_accepts(ratio, window):
            return False, drift, f"len_window={ratio:.2f}"
    elif ratio > RATIO_HI or ratio < RATIO_LO:
        return False, drift, f"len_ratio={ratio:.2f}"
    return True, drift, ""


def ensure_experiment(s, exp_id: str, name: str) -> str:
    e = s.get(Experiment, exp_id)
    if e is None:
        s.add(Experiment(id=exp_id, name=name, status="created",
                         config={"kind": "controlled_corruption", "gen_pv": GEN_PV,
                                 "verify_pv": VERIFY_PV}))
        s.commit()
    return exp_id


def run_one(s_pair: tuple, ctypes: list[str], *, exp_id: str, gen_model: str,
            verify_model: str, ctx_limit: int, dry_run: bool) -> list[dict]:
    from app.context_ablation import scene_context
    seg, frame = s_pair
    if frame is None:
        # 纯基准路径选段时不查帧（那些段本来就没抽过帧），但 candidates.frame_id 非空——
        # 不在这里补查，整批会以 no_frame_id 静默失败（2026-09-18 实测 162 条白跑）。
        with db.session() as s0:
            frame = primary_l_frame(s0, seg.id)
    with db.session() as s:
        ctx_texts, _ = scene_context(s, seg, limit=ctx_limit)
        ctx = "\n\n".join(ctx_texts[-2:])          # 与盲评 near 口径一致（最近上文）
    out = []
    for ctype in ctypes:
        with db.session() as s:
            # 只把**已有文本**的记录当作重复（ok / rejected_drift）；
            # failed（empty_text 之类）允许重试，否则一次空返回就永久锁死这一格。
            # prompt_version 进 key：换了生成口径（v1→v2）时**允许重生成**同一格，
            # 否则修好的 prompt 永远覆盖不到旧格，数据集里会永久留着已知有缺陷的样本。
            # generator_model 双形兼容：生成模型改名不该让"这格已做过"失效
            # （否则断点续跑会把同一 (段,类型) 再劣化一遍，数据集里出现双份）。
            # filter_by 只支持等值——in_ 必须走 filter()（filter_by(X__in=...)
            # 抛 InvalidRequestError，实测，pilot 首跑即撞）
            dup = (s.query(ControlledCorruption)
                   .filter_by(segment_id=seg.id, corruption_type=ctype,
                              prompt_version=GEN_PV)
                   .filter(ControlledCorruption.generator_model
                            .in_(config.model_any(gen_model)),
                            ControlledCorruption.status != "failed").first())
        if dup is not None:
            with _lock:
                _stat["skip"] += 1
            continue
        rec = {"segment_id": seg.id, "frame_id": frame.id if frame else None,
               "corruption_type": ctype, "gen_model": gen_model,
               "verify_model": verify_model, "human": seg.text, "exp": exp_id}
        if dry_run:
            rec["status"] = "dry"
            out.append(rec)
            continue
        try:
            g = generate_variant(human=seg.text, ctx=ctx, ctype=ctype, model=gen_model)
        except Exception as e:                      # noqa: BLE001
            g = None
            rec["error"] = f"gen: {e}"
        text = ((g or {}).get("text") or "").strip()
        if not text:
            with _lock:
                _stat["gen_failed"] += 1
            rec.update(status="failed", error=rec.get("error", "empty_text"))
            _save(rec, human_len=len(seg.text))
            out.append(rec)
            continue
        with _lock:
            _stat["gen_ok"] += 1
        ratio = len(text) / max(1, len(seg.text))
        try:
            v = verify_variant(human=seg.text, variant=text, model=verify_model)
        except Exception as e:                      # noqa: BLE001
            v = None
            rec["error"] = f"verify: {e}"
        with _lock:
            _stat["verified"] += 1
        ok, drift, why = judge_verify(v, ratio, window=window_for(ctype))
        if ok:
            mech = mechanical_defect(seg.text, text)     # 确定性兜底（「地」汤）
            if mech:
                ok, why = False, mech
        if not ok:
            with _lock:
                _stat["rejected"] += 1
        rec.update(text=text, gen_note=(g or {}).get("changed", ""),
                   drift=v or {}, drift_score=drift, drift_ok=ok,
                   fact_consistent=bool((v or {}).get("fact_consistent")),
                   len_ratio=ratio,
                   status="ok" if ok else reject_status(why),
                   reject_reason=why)
        _save(rec, human_len=len(seg.text))
        out.append(rec)
    return out


def _save(rec: dict, *, human_len: int) -> None:
    """落库：受控劣化记录 + （通过校验的）一条 candidate。

    两条纪律：
    · frame_id 必须是**真实帧 id**（candidates.frame_id 有外键，conn 上开了
      `PRAGMA foreign_keys=ON`，塞空串会直接报错，而不是静默写下脏值）。
    · anon_label 必须**不含类型信息**——它是给评审台看的短标签，
      写成 `CEXP`（EXPLICITIZE）等于把谜底印在题面上。类型只存在 DB 侧的 model 字段。
    """
    ctype = rec["corruption_type"]
    with db.session() as s:
        cc = ControlledCorruption(
            experiment_id=rec["exp"], segment_id=rec["segment_id"],
            frame_id=rec.get("frame_id"), corruption_type=ctype,
            variable=ALL_TYPES[ctype]["variable"][:200],
            generator_model=rec["gen_model"], prompt_version=GEN_PV,
            text=rec.get("text", ""), n_chars=len(rec.get("text", "")),
            gen_note=rec.get("gen_note", ""),
            len_ratio=rec.get("len_ratio", 0.0), drift=rec.get("drift") or {},
            drift_score=rec.get("drift_score", 0.0),
            fact_consistent=rec.get("fact_consistent", False),
            drift_ok=rec.get("drift_ok", False),
            verify_model=rec["verify_model"], verify_pv=VERIFY_PV,
            status=rec["status"], error=rec.get("error") or rec.get("reject_reason") or None)
        s.add(cc)
        s.flush()
        if rec["status"] == "ok":
            if not rec.get("frame_id"):
                cc.status = "failed"
                cc.error = "no_frame_id"
                s.commit()
                return
            cand = Candidate(
                experiment_id=rec["exp"], frame_id=rec["frame_id"],
                segment_id=rec["segment_id"], anon_label=_neutral_label(),
                model=f"corrupt:{ctype}", temperature=0.0,
                prompt_version=GEN_PV, text=rec["text"], status="ok")
            s.add(cand)
            s.flush()
            cc.candidate_id = cand.id
        s.commit()


def _neutral_label() -> str:
    """与类型无关的匿名短标签，形如 X7F（不泄漏 corruption 类型）。"""
    return "X" + secrets.token_hex(1).upper()


# ── 纯基准段（从未被任何实验用过的段，§14）────────────────────

def pick_fresh_segments(s, n: int, seed: int, min_chars: int = 60,
                        works: list[str] | None = None) -> list[tuple[Segment, None]]:
    """挑**从未被任何实验用过**的人类段落，专门做 §14 隐藏基准。

    为什么不用 `pick_segments`：现有 120 个"有 L 帧且够长"的段里，只有 9 个没被
    别的实验用过（L 帧本来就是*为了*那些实验抽的）。拿已用过的段当基准 =
    基准题泄漏（它的正文早进了 SFT 导出）。

    为什么不需要 SemanticFrame：**corruption 检测不需要帧**——基准题就是
    「两版里哪版是人类原文」，比的是文本本身。所以基准段可以完全独立于帧抽取，
    从 29 万段里挑，不受"哪段抽过帧"的约束。

    corpus v2 镜像段（role=None，与 v1 同文）一律排除：挑中它就是"同文双份入基准"，
    会把 U0c 刚收口的 v2 污染重新写回来（会审①复发路径）。
    """
    segs = (s.query(Segment).filter(Segment.role.is_(None))
            .filter(exclude_corpus_v2_segments())
            .filter(Segment.n_chars >= min_chars).all())
    titles = {w.id: (w.title or "") for w in s.query(Work).all()}
    cand_segs = {r[0] for r in s.query(Candidate.segment_id).distinct()}
    cc_segs = {r[0] for r in s.query(ControlledCorruption.segment_id).distinct()}
    starts: dict[tuple, int | None] = {}
    out = []
    for seg in segs:
        if seg.id in cand_segs or seg.id in cc_segs:
            continue
        if titles.get(seg.work_id, "").startswith("fixture"):
            continue
        if not seg.text or len(seg.text.strip()) < min_chars:
            continue
        if looks_watermarked(seg.text):
            continue
        key = (seg.work_id, seg.seg_version)
        if key not in starts:
            starts[key] = extras_start(s, seg.work_id, seg.seg_version)
        st = starts[key]
        if st is not None and seg.ordinal >= st:      # 番外
            continue
        out.append(seg)
    out.sort(key=lambda x: x.id)
    rng = random.Random(seed)
    per_work: dict[str, list] = {}
    for seg in out:
        per_work.setdefault(seg.work_id, []).append(seg)
    picked: list[Segment] = []
    keys = sorted(per_work)
    while len(picked) < min(n, len(out)):
        progressed = False
        for k in keys:
            if len(picked) >= min(n, len(out)):
                break
            if per_work[k]:
                picked.append(per_work[k].pop(rng.randrange(len(per_work[k]))))
                progressed = True
        if not progressed:
            break
    return [(seg, None) for seg in picked]


# ── 基准隔离（§14 Hidden Benchmark）───────────────────────────

def split_benchmark(n: int | None = None, ratio: float = 0.25, seed: int = 99118,
                    dry_run: bool = False, exp: str | None = None) -> dict:
    """把劣化数据集里的一部分段落划为**基准段**（Segment.role='benchmark'），
    训练导出从此不再读它们（§14：Hidden Benchmark 不得被训练/调参读取）。

    §14 明确把 **Controlled Corruption Detection** 列为基准的必含项——本数据集
    天生带标签（"哪一边是被劣化的"），所以它既是最便宜的训练数据，也是最干净的
    基准题：判对率不依赖集霸再判一次。

    两条选择纪律：
    · **优先挑没被其他实验用过的段**：段落在别的实验里已经产出过候选、且已进过
      SFT 导出（`export_training.py`），拿它当基准等于基准题泄漏。
    · 划定后**只增不减**（role 只从 None 变成 benchmark），且是确定性的（按 seed
      排序取样），否则两次跑会得到两套基准、历史数字不可比。

    corpus v2 镜像段永不入基准（会审①复发路径：本函数是 role='benchmark' 的写入侧）。
    """
    with db.session() as s:
        q = s.query(ControlledCorruption.segment_id)
        if exp:                              # 限定实验：避免跨实验互相牵动（测试与多批共用一库）
            q = q.filter(ControlledCorruption.experiment_id == exp)
        all_ids = sorted({r[0] for r in q.distinct()})
        if not all_ids:
            return {"marked": 0, "pool": 0, "reason": "劣化数据集为空"}
        # v2 段与 v1 同文，双份入基准=基准被污染；只挡新划，不回改历史 role
        seg_ids = sorted({r[0] for r in s.query(Segment.id).filter(
            Segment.id.in_(all_ids), exclude_corpus_v2_segments()).all()})
        v2_excluded = len(all_ids) - len(seg_ids)
        if not seg_ids:
            return {"marked": 0, "pool": 0, "v2_excluded": v2_excluded,
                    "reason": "劣化数据集只剩 corpus v2 镜像段"}
        used_elsewhere = {r[0] for r in s.query(Candidate.segment_id)
                          .filter(Candidate.prompt_version.in_(("reconstruct_v1", "recon_ctx_v1")))
                          .distinct()}
        segs = {x.id: x for x in s.query(Segment).filter(Segment.id.in_(seg_ids)).all()}
        fresh = [i for i in seg_ids if i not in used_elsewhere
                 and (segs.get(i) is not None and segs[i].role is None)]
        already = [i for i in seg_ids if (segs.get(i) is not None and segs[i].role == "benchmark")]
        want = n if n is not None else max(1, round(len(seg_ids) * ratio))
        want = max(0, want - len(already))
        pool = sorted(fresh)
        picked = random.Random(seed).sample(pool, min(want, len(pool)))
        if not dry_run:
            for i in picked:
                segs[i].role = "benchmark"
            s.commit()
        return {"marked": len(picked), "already": len(already), "pool_fresh": len(pool),
                "pool_total": len(seg_ids), "used_elsewhere": len(seg_ids) - len(fresh) - len(already),
                "v2_excluded": v2_excluded,
                "dry_run": dry_run}


def _ensure_frames(seg_ids: list[str], exp_id: str) -> int:
    """给基准段补抽 L 帧（缺了就落不了 candidates）。幂等：已有的不重抽。"""
    from app.experiments import stage_extract_frames
    from app.models import Frame
    if not seg_ids:
        return 0
    helper = f"{exp_id}-FR"
    with db.session() as s:
        have = {f.segment_id for f in
                s.query(Frame).filter(Frame.segment_id.in_(seg_ids),
                                      Frame.granularity == "L",
                                      Frame.status != "failed").all()}
        need = [i for i in seg_ids if i not in have]
        if not need:
            print(f"L 帧齐备（{len(have)} 段），跳过抽取")
            return 0
        e = s.get(Experiment, helper)
        if e is None:
            e = Experiment(id=helper, name="benchmark-frame-extract", status="created",
                           config={}, )
            s.add(e)
        # ⚠ config["segment_ids"] 必须**每次都更新**：辅助实验是复用同一个 id 的，
        # 只建一次的话新段根本不在抽取范围里 → 一个调用都不发、静默"0/5"
        # （2026-09-18 实测：5 个基准段因此永远补不上帧）。
        e.config = {**(e.config or {}), "segment_ids": sorted(set(need)),
                    "granularities": ["L"],
                    # canonical_model：存量配置里的死 id 归一到在册名——
                    # 「源校勘 0/300 → 0 帧」事故的残留入口就是这里只补空不洗存量
                    "extractors": [config.canonical_model(x)
                                   for x in ((e.config or {}).get("extractors") or [])
                                   if config.canonical_model(x)] or [config.DEFAULT_LLM_MODEL],
                    "concurrency": 4}
        s.commit()
        print(f"补抽 L 帧：{len(need)} 段（基准段本来没抽过帧）")
        stage_extract_frames(s, e)
        # ⚠ 用**新 session** 数：抽取是在工作线程各自的 session 里提交的，
        # 外层这个 session 的快照看不到（实测打印 "抽到 0/8" 而库里其实有帧）。
        with db.session() as s2:
            got = s2.query(Frame).filter(Frame.segment_id.in_(need),
                                         Frame.granularity == "L",
                                         Frame.status != "failed").count()
        print(f"  L 帧就绪 {got}/{len(need)}")
        return got


def benchmark_segments() -> set[str]:
    with db.session() as s:
        return {x.id for x in s.query(Segment).filter(Segment.role == "benchmark").all()}


# ── 用新校验口径重判已生成的变体（--reverify）─────────────────

def recheck(*, verify_model: str, conc: int, batch: str = "", dry_run: bool = False,
            all_variants: bool = False) -> dict:
    """按**当前**校验口径重查已有变体，并**剔除病句**（删候选、置 rejected_grammar）。

    为什么要单独一步：`ungrammatical` 字段是后来才加的，早先生成的变体没这个标记；
    而集霸已经看到了「不应该地扫描不到」「大范围地地进行」这种病句。
    重查就是拿同一把尺子把旧数据也量一遍，量出病句的**不进数据集**。

    batch="corr24" 时只查该批次的候选（优先处理集民正在看的题）。
    """
    import heldout_eval as he
    con = sqlite3.connect(he.DB)
    con.row_factory = sqlite3.Row
    if all_variants:
        rows = con.execute("""select cc.id ccid, cc.candidate_id cid, cc.segment_id seg,
                 cc.corruption_type ct, cc.text v, s.text_clean hc, s.text h
                 from controlled_corruptions cc join segments s on s.id=cc.segment_id
                 where cc.status='ok' and cc.candidate_id is not null""").fetchall()
    else:
        like = f"%batch_{batch}%" if batch else "%"
        rows = con.execute("""select distinct cc.id ccid, cc.candidate_id cid, cc.segment_id seg,
                 cc.corruption_type ct, cc.text v, s.text_clean hc, s.text h
                 from review_items ri join controlled_corruptions cc on cc.candidate_id=ri.subject_id
                 join segments s on s.id=cc.segment_id
                 where ri.reasons like ?""", (like,)).fetchall()
    con.close()
    todo = [dict(r) for r in rows]
    print(f"重查 {len(todo)} 条变体（口径 {VERIFY_PV}）")
    if dry_run:
        return {"would_check": len(todo)}

    def one(r):
        human = r["hc"] or r["h"] or ""
        try:
            v = verify_variant(human=human, variant=r["v"], model=verify_model)
        except Exception:                            # noqa: BLE001
            with _lock:
                _stat["verified"] += 0
            return
        ratio = len(r["v"]) / max(1, len(human))
        ok, drift, why = judge_verify(v, ratio)
        if ok:
            mech = mechanical_defect(human, r["v"])
            if mech:
                ok, why = False, mech
        with db.session() as s:
            cc = s.get(ControlledCorruption, r["ccid"])
            if cc is None:
                return
            cc.drift = v or {}
            cc.drift_score = drift
            cc.drift_ok = ok
            cc.verify_model = verify_model
            cc.verify_pv = VERIFY_PV
            if not ok:
                cc.status = ("rejected_grammar" if why == "ungrammatical"
                             else "rejected_drift")
                cc.error = why
                # 从数据集里摘掉：删候选 + 删评审项，否则病句还会被端出来
                if r["cid"]:
                    s.query(ReviewItem).filter_by(subject_id=r["cid"]).delete()
                    s.query(Candidate).filter_by(id=r["cid"]).delete()
                    cc.candidate_id = None
                with _lock:
                    _stat["rejected"] += 1
            s.commit()

    with ThreadPoolExecutor(max_workers=max(1, conc)) as ex:
        list(ex.map(one, todo))
    print(f"重查完成：剔除 {_stat['rejected']} 条")
    return dict(_stat)


def reverify(*, verify_model: str, conc: int, dry_run: bool = False,
             only_types: list[str] | None = None) -> dict:
    """对**已有文本但被拒**的变体换新校验器/新 prompt 重判，通过的补建 candidate。

    为什么要有这一步：校验口径本身会演进（v1 把"加了一句意图解说"误判成事实不符，
    把 EMOTION_LABEL / PSYCHOLOGY_LABEL 的大批本该放行的样本拒了）。变体文本还在库里，
    重判只花校验的钱，不必重新生成（生成更贵、且换了文本就不是同一批对照了）。
    """
    out = {"checked": 0, "rescued": 0, "still_rejected": 0, "skipped": 0}
    with db.session() as s:
        q = (s.query(ControlledCorruption)
             .filter(ControlledCorruption.status == "rejected_drift")
             .filter(ControlledCorruption.text != ""))
        if only_types:
            q = q.filter(ControlledCorruption.corruption_type.in_(only_types))
        rows = [(c.id, c.segment_id, c.corruption_type, c.text, c.experiment_id,
                 c.generator_model, c.frame_id) for c in q.all()]
        humans = {seg.id: seg.text for seg in
                  s.query(Segment).filter(Segment.id.in_({r[1] for r in rows})).all()}
    print(f"待重判 {len(rows)} 条（口径 {VERIFY_PV}）")
    if dry_run:
        return out | {"dry": True}
    def _one(row) -> str:
        cc_id, seg_id, ctype, text, exp, _gmodel, frame_id = row
        human = humans.get(seg_id) or ""
        if not human:
            return "skipped"
        try:
            v = verify_variant(human=human, variant=text, model=verify_model)
        except Exception as e:                       # noqa: BLE001
            print(f"  校验异常 {cc_id}: {e}")
            return "skipped"
        ratio = len(text) / max(1, len(human))
        ok, drift, why = judge_verify(v, ratio)
        with db.session() as s:
            cc = s.get(ControlledCorruption, cc_id)
            cc.drift = v or {}
            cc.drift_score = drift
            cc.drift_ok = ok
            cc.fact_consistent = bool((v or {}).get("contradicts_source") is False)
            cc.verify_model = verify_model
            cc.verify_pv = VERIFY_PV
            if ok:
                cand = Candidate(experiment_id=exp, frame_id=frame_id,
                                 segment_id=seg_id, anon_label=_neutral_label(),
                                 model=f"corrupt:{ctype}", temperature=0.0,
                                 prompt_version=GEN_PV, text=text, status="ok")
                s.add(cand)
                s.flush()
                cc.status = "ok"
                cc.candidate_id = cand.id
                cc.error = None
            else:
                cc.status = "rejected_drift"
                cc.error = why
            s.commit()
        with _lock:
            out["checked"] += 1
            out["rescued" if ok else "still_rejected"] += 1
            if out["checked"] % 20 == 0:
                print(f"  ...{out['checked']}/{len(rows)}（救回 {out['rescued']}）")
        return "ok"

    with ThreadPoolExecutor(max_workers=max(1, conc)) as ex:
        list(ex.map(_one, rows))
    return out


# ── 评委判定（可判别性标尺）───────────────────────────────────

# 评委池（2026-09-18 集霸指令：停用 meta/muse-spark-1.3，换成 glm-5.3 与本机
# agy 的 Gemini 3.8）。
# ⚠ agy 是**单账号共享额度**：必须顺序调用（见 app/gateway.is_serial_model），
#   混在并发池里会互相挤掉。判分函数已按此把 agy 模型单独串行跑。
DEFAULT_JUDGES = ("moonshotai/kimi-k3", config.DEFAULT_LLM_MODEL,
                  "z-ai/glm-5.3", "agy/gemini-3.8-flash-high")
NEW1_SUFFIX = "_near1"


def judge_corruptions(*, models: list[str], variant: str = "v4", conc: int = 6,
                      limit: int = 0, dry_run: bool = False) -> None:
    """让评委判「人类原文 vs 劣化版」，全用**用户所见的那份上下文**（near1）。

    为什么不用整场景上下文（既有 `heldout_eval` 默认）：§6⑥ 要求评委与用户吃同一份
    上下文，而评审台默认 `ctx=near`（只给最近一段，集霸的阅读成本 97% 在上文）。
    这里按同一个口径喂，且 pv 记 `_near1` 后缀，**与既有 κ 表可分**——两者上下文不同，
    不可直接混入同一张表比较。
    """
    import heldout_eval as he
    from app.context_ablation import scene_context
    exp = _current_exp()
    con = sqlite3.connect(config.DATABASE_URL.replace("sqlite:///", ""))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """select cc.candidate_id cid, cc.corruption_type ct from controlled_corruptions cc
            where cc.status='ok' and cc.candidate_id is not null""" + 
        (f" limit {int(limit)}" if limit else "")).fetchall()
    con.close()
    items = [dict(r) for r in rows]
    print(f"劣化候选 {len(items)} 条 × {len(models)} 评委 × 1 口径 = "
          f"{len(items) * len(models)} 次调用（幂等，已判过会跳过）")
    ctx_by_cid: dict[str, str] = {}
    with db.session() as s:
        for x in items:
            cand = s.get(Candidate, x["cid"])
            human = s.get(Segment, cand.segment_id)
            texts, _ = scene_context(s, human)
            ctx_by_cid[x["cid"]] = (chr(10) * 2).join(texts[-1:])   # near1：与评审台默认一致
    if dry_run:
        print("dry-run：未发起")
        return
    from app.gateway import split_models
    par, serial = split_models(models)
    if serial:
        print(f"串行模型（单账号共享额度，禁止并发）：{serial}")
    jobs = [(x["cid"], ctx_by_cid[x["cid"]], m, variant) for m in par for x in items]
    if jobs:
        with ThreadPoolExecutor(max_workers=max(1, conc)) as pool:
            for _ in pool.map(lambda j: he.run_one(*j, pv_suffix=NEW1_SUFFIX, exp=exp), jobs):
                pass
    for m in serial:                     # agy 等：严格顺序，逐个跑完再下一个
        for x in items:
            he.run_one(x["cid"], ctx_by_cid[x["cid"]], m, variant,
                       pv_suffix=NEW1_SUFFIX, exp=exp)
    print(f"完成：ok={he._counter['ok']} failed={he._counter['failed']} "
          f"skip={he._counter['skip']}")


def _current_exp() -> str:
    """最近一个劣化实验。在 Python 里过滤 config，不写 JSON 方言表达式——
    同一段 SQL 在 SQLite（json_extract）与 Postgres（->>）上写法不同，写死一边
    会在切库时静默失败。"""
    with db.session() as s:
        rows = s.query(Experiment).order_by(Experiment.created_at.desc()).limit(50).all()
        for e in rows:
            if (e.config or {}).get("kind") == "controlled_corruption":
                return e.id
    return f"EXP-{time.strftime('%m%d')}-CORR"


# ── 报告 ──────────────────────────────────────────────────────

def report(db_path: Path | str | None = None) -> None:
    con = sqlite3.connect(db_path or config.DATABASE_URL.replace("sqlite:///", ""))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """select corruption_type, status, prompt_version, count(*) n,
                  avg(drift_score) d, avg(len_ratio) r,
                  sum(case when json_extract(drift,'$.ungrammatical') then 1 else 0 end) ungram
           from controlled_corruptions group by corruption_type, status, prompt_version
           order by corruption_type, status""").fetchall()
    print(f"{'类型':<28}{'状态':<18}{'口径':<12}{'n':>4}{'漂移':>7}{'长度比':>8}{'病句':>6}")
    for r in rows:
        print(f'{TYPES.get(r["corruption_type"], {}).get("zh", r["corruption_type"]):<28}'
              f'{r["status"]:<18}{r["prompt_version"]:<12}{r["n"]:>4}'
              f'{r["d"] or 0:>7.2f}{r["r"] or 0:>8.2f}{r["ungram"] or 0:>6}')
    tot = con.execute("select count(*) n, sum(status='ok') ok from controlled_corruptions").fetchone()
    print(f'\n合计 {tot["n"]} 条，通过校验 {tot["ok"] or 0} 条')

    con.close()
    detect_report()


def detect_report(db_path: Path | str | None = None) -> dict:
    """**可判别性标尺**：评委能不能认出"这是被劣化过的那一版"。

    这是本数据集最有价值的一次读数。做法：每一对都**自带答案**（哪边是人类原文），
    所以可以脱离集霸的判定，直接量出每个评委、每一类劣化的判别率。

    它回答的是此前无法区分的一件事：
    · 判别率显著高于 50% → 评委读得出这个变量，"评委与集霸方向相反"只能是**口味差**；
    · 判别率贴着 50%（甚至更低）→ 评委对单变量劣化是**盲的**，
      那么所有基于评委的 κ / agreement 数字都建立在噪声上，必须整体降级解读。

    同时打出**位置基线**（"永远选 A"撞对答案的比例）：A/B 是随机的，判别率若不
    高于它，说明评委的判断与内容无关。
    """
    import heldout_eval as he
    sys.path.insert(0, str(ROOT / "scripts"))
    from select_harvest_batch import wilson

    con = sqlite3.connect(db_path or he.DB)
    con.row_factory = sqlite3.Row
    PV = he.PROMPT_VARIANTS["v4"][1] + NEW1_SUFFIX
    rows = con.execute(
        """select cc.corruption_type ct, cc.candidate_id cid, cc.text vtext, cc.variable var,
                  cc.drift_score ds, s.text htext, jr.model model, jr.verdict v
             from controlled_corruptions cc
             join segments s on s.id = cc.segment_id
             join judge_runs jr on jr.subject_id = cc.candidate_id
                               and jr.judge_kind='preference' and jr.status='ok'
                               and jr.prompt_version=?
            where cc.status='ok'""", (PV,)).fetchall()
    con.close()
    if not rows:
        print(f"\n（还没有评委判定，跳过可判别性读数；口径 {PV}）")
        return {}
    by_model: dict[str, dict] = {}
    by_type: dict[str, dict] = {}
    counter = []
    for r in rows:
        v = r["v"]
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:
                continue
        if not isinstance(v, dict):
            continue
        h, pick = v.get("human_was_a"), v.get("winner")
        if h is None or pick not in ("A", "B"):
            continue
        human_picked = (pick == "A") == bool(h)      # 评委选的是不是人类原文
        m = by_model.setdefault(r["model"], {"n": 0, "human": 0, "a": 0, "a_hit": 0})
        m["n"] += 1
        m["human"] += human_picked
        m["a"] += (pick == "A")
        # 「永远选A」的命中率 = A 位**恰好是人类原文**的比例（= bool(h)），
        # 与评委怎么选无关。⚠ 别写成 `(pick=="A")==bool(h)`——那与人类判别率是同一个量，
        # 会让基线栏变成判别率的复制品，看起来"判别率完全被位置解释"，实为乌龙。
        m["a_hit"] += bool(h)
        t = by_type.setdefault(r["ct"], {"n": 0, "human": 0})
        t["n"] += 1
        t["human"] += human_picked
        if not human_picked:
            counter.append({"type": r["ct"], "model": r["model"], "cid": r["cid"],
                            "human": r["htext"][:70], "corrupt": r["vtext"][:70],
                            "variable": (r["var"] or "")[:60], "drift": r["ds"]})

    print("\n" + "=" * 96)
    print("可判别性标尺：评委能不能认出被劣化的那一版（自带答案，不依赖集霸判定）")
    print(f"{'模型':<34}{'判定数':>6}{'认出人类':>9}{'判别率':>8}{'95%CI':>16}{'永远选A撞对':>12}")
    for m, d in sorted(by_model.items(), key=lambda x: -x[1]["n"]):
        n, k = d["n"], d["human"]
        lo, hi = wilson(k, n)
        print(f'{m[:33]:<34}{n:>6}{k:>9}{k / n:>8.3f}   [{lo:.3f},{hi:.3f}]{d["a_hit"] / n:>12.3f}')
    n = sum(d["n"] for d in by_model.values())
    k = sum(d["human"] for d in by_model.values())
    lo, hi = wilson(k, n)
    print(f'{"（合计，按判定计）":<34}{n:>6}{k:>9}{k / n:>8.3f}   [{lo:.3f},{hi:.3f}]')

    print(f"\n{'劣化类型':<28}{'判定数':>7}{'认出人类':>9}{'判别率':>8}{'95%CI':>16}")
    for t, d in sorted(by_type.items(), key=lambda x: x[1]["human"] / max(1, x[1]["n"])):
        n2, k2 = d["n"], d["human"]
        lo, hi = wilson(k2, n2)
        print(f'{TYPES.get(t, {}).get("zh", t):<28}{n2:>7}{k2:>9}{k2 / n2:>8.3f}   [{lo:.3f},{hi:.3f}]')

    if counter:
        print(f"\n§7.5 反例（评委选了**劣化版**，共 {len(counter)} 条）——信息量最大，优先给集霸看：")
        for c in counter[:20]:
            print(f'  · [{TYPES.get(c["type"], {}).get("zh", c["type"])}] {c["model"].split("/")[-1]}')
            print(f'      人: {c["human"]}')
            print(f'      劣: {c["corrupt"]}')
    con = sqlite3.connect(db_path or he.DB)
    con.row_factory = sqlite3.Row
    con.execute("""create table if not exists cc_detect_reports (
                     id integer primary key autoincrement, created_at text, pv text,
                     payload text)""")
    con.execute("insert into cc_detect_reports (created_at, pv, payload) values (?,?,?)",
                (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), PV,
                 json.dumps({"by_model": by_model, "by_type": by_type,
                             "counter_examples": counter}, ensure_ascii=False)))
    con.commit()
    con.close()
    return {"by_model": by_model, "by_type": by_type, "n_counter": len(counter)}


# ── 给集霸端一批（--build-batch）──────────────────────────────

def build_batch(tag: str, n: int, seed: int, *, db_path: Path | str | None = None,
                include_counter: bool = True, dry_run: bool = False) -> dict:
    """挑对照做成盲评批次，**补足到 n 条**（已有同批次的题不重复计）。

    选题口径（不是随机抽）：
      1. **反例优先**：评委已经偏好劣化版的那几条——这是唯一能直接检验
         "评委口味与集霸相反" 的样本（§7.5）。
      2. 其余按**类型轮转**取（每类尽量均摊），避免整批都是同一种劣化。
      3. 优先短段（他明说过"太折磨了"）：段短 → 读得快。
    """
    con = sqlite3.connect(db_path or config.DATABASE_URL.replace("sqlite:///", ""))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """select cc.id ccid, cc.candidate_id cid, cc.corruption_type ct, cc.experiment_id exp,
                  cc.drift, s.id seg, s.text htext, s.ordinal,
                  (select count(*) from judge_runs jr where jr.subject_id=cc.candidate_id
                     and jr.judge_kind='preference' and jr.status='ok') nj,
                  (select count(*) from judge_runs jr where jr.subject_id=cc.candidate_id
                     and jr.judge_kind='preference' and jr.status='ok'
                     and json_extract(jr.verdict,'$.winner_resolved')='candidate') nw
             from controlled_corruptions cc join segments s on s.id = cc.segment_id
            where cc.status='ok' and cc.candidate_id is not null""").fetchall()
    con.close()
    have = set()
    with db.session() as s:
        for r in s.query(ReviewItem).all():
            if f"batch_{tag}" in (r.reasons or []):
                have.add(r.subject_id)
    pool = [r for r in rows if r["cid"] not in have]
    # 只端"源文本完好 + 变体非病句"的题（两条都是集霸 2026-09-18 当场骂过的）
    import json as _json
    seg_rows = {r["seg"]: r for r in pool}
    with db.session() as s:
        src = {x.id: x.integrity for x in
               s.query(Segment).filter(Segment.id.in_(list(seg_rows))).all()}
    kept = []
    for r in pool:
        try:
            d = _json.loads(src.get(r["seg"]) or "{}")
        except Exception:
            d = {}
        if d.get("src_ok") is not True:
            continue
        try:
            dr = _json.loads(r["drift"]) if isinstance(r["drift"], str) else (r["drift"] or {})
        except Exception:
            dr = {}
        if dr.get("ungrammatical"):
            continue
        kept.append(r)
    print(f"建批过滤：池 {len(pool)} → {len(kept)}（源文本或变体不合格的已剔除）")
    pool = kept
    # n 是**批次目标总数**，不是"本次新增数"：否则每次重跑都会往同一批里再塞 n 条
    # （2026-09-18 实测：本意 16 条的批被跑成 31 条）。
    n = max(0, n - len(have))
    if not pool or n == 0:
        return {"picked": 0, "already_in_batch": len(have), "reason": "已达标或池空"}
    rng = random.Random(seed)
    # 控制臂**先摘出来**再分反例/其余：控制臂 77% 都会被评委判"劣化版更好"，
    # 若先按 nw 排序取反例，控制臂会把反例名额吃光，18 类劣化只剩同一种
    # （2026-09-18 实测：24 条批里 7 条文学腔、控制臂只挤进 2 条）。
    ctrl_pool = [r for r in pool if r["ct"] in CONTROLS]
    rest_all = [r for r in pool if r["ct"] not in CONTROLS]
    counter = [r for r in rest_all if (r["nw"] or 0) > 0]
    rest = [r for r in rest_all if not ((r["nw"] or 0) > 0)]
    counter.sort(key=lambda r: (-(r["nw"] or 0), len(r["htext"] or "")))
    # 短段优先：集霸明说过"太折磨了"，而这一批的价值不依赖长段
    rest.sort(key=lambda r: (len(r["htext"] or ""), r["ct"], r["ccid"]))
    sub: dict[str, list] = {}
    for r in rest:
        sub.setdefault(r["ct"], []).append(r)
    picked: list = []
    want_ctrl = max(0, n // 3)
    ctrl_sorted = sorted(ctrl_pool, key=lambda r: len(r["htext"] or ""))
    picked += [ctrl_sorted.pop(0) for _ in range(min(want_ctrl, len(ctrl_sorted)))]
    # 反例：同一类型最多取 2 条，保证"评委在哪些类型上翻车"这件事被多样本覆盖
    if include_counter:
        seen_t: dict[str, int] = {}
        for r in counter:
            if len(picked) >= 2 * n // 3:
                break
            if seen_t.get(r["ct"], 0) >= 2:
                continue
            seen_t[r["ct"]] = seen_t.get(r["ct"], 0) + 1
            picked.append(r)
    keys = sorted(sub)
    while len(picked) < n and any(sub[k] for k in keys):
        for k in keys:
            if len(picked) >= n:
                break
            if sub[k]:
                picked.append(sub[k].pop(rng.randrange(len(sub[k]))))
    picked = picked[:n]
    if not dry_run:
        with db.session() as s:
            for r in picked:
                s.add(ReviewItem(experiment_id=r["exp"], subject_type="candidate",
                                 subject_id=r["cid"], priority=0.0,
                                 reasons=["controlled_corruption", f"cctype:{r['ct']}",
                                          f"batch_{tag}", "stratum:CORRUPT", "w:1.0"],
                                 status="pending"))
            s.commit()
    return {"picked": len(picked), "counter": len([r for r in picked if (r["nw"] or 0) > 0]),
            "types": sorted({r["ct"] for r in picked}),
            "median_human_chars": sorted(len(r["htext"] or "") for r in picked)[len(picked) // 2]
            if picked else 0}


def _llm_models(args) -> list[str]:
    """这条命令**真正会调到**的模型名——预检只问这些。

    多问会误拦：`--recheck/--reverify` 只用校验模型，把生成模型一起校验等于
    让一个挂掉的生成 id 挡住"救回已生成数据"的通路。分支判定顺序与 main() 一致。
    """
    if args.report or args.build_batch or args.split_benchmark >= 0:
        return []                       # 纯确定性分支：不碰网关
    if args.judge:
        return [m.strip() for m in args.judge_models.split(",") if m.strip()]
    if args.recheck or args.reverify:
        return [args.verify_model]
    return [args.gen_model, args.verify_model]


def _first_error(exp_id: str) -> str:
    """本实验首条**异常**原文（压成一行，可直接贴进汇报）。

    为什么必须打出来：死模型 id 的表现是整批 status='failed'，而"完成"行只有计数——
    2026-09-20 那次就把 100% 的 `503 model_not_found` 误判成"池子耗尽"。
    只取 gen:/verify: 前缀的行：rejected_* 的 error 存的是语义拒收理由，不是故障。
    """
    with db.session() as s:
        row = (s.query(ControlledCorruption.error)
               .filter(ControlledCorruption.experiment_id == exp_id,
                       ControlledCorruption.error.isnot(None),
                       ControlledCorruption.error.like("gen:%")
                       | ControlledCorruption.error.like("verify:%"))
               .order_by(ControlledCorruption.created_at).first())
    return pf.redact(row[0]) if row and row[0] else ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-seg", type=int, default=12, help="取多少个人类段落")
    ap.add_argument("--types", default="", help="逗号分隔；空=全部 18 类")
    ap.add_argument("--works", default="", help="逗号分隔作品标题关键词；空=全部")
    ap.add_argument("--gen-model", default=DEFAULT_GEN)
    ap.add_argument("--verify-model", default=DEFAULT_VERIFY)
    ap.add_argument("--conc", type=int, default=4)
    ap.add_argument("--seed", type=int, default=20260918)
    ap.add_argument("--ctx-limit", type=int, default=2)
    ap.add_argument("--min-chars", type=int, default=60)
    ap.add_argument("--exp", default="", help="复用已有劣化实验 id；空=新建")
    ap.add_argument("--skip-done", action="store_true", help="跳过已生成过的段落")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--build-batch", default="", help="批次 tag（如 corr16）")
    ap.add_argument("--batch-n", type=int, default=16)
    ap.add_argument("--recheck", action="store_true",
                    help="按当前口径重查已有变体并剔除病句（batch= 限定批次）")
    ap.add_argument("--batch", default="", help="--recheck 的批次标签")
    ap.add_argument("--all-variants", action="store_true", help="--recheck 全量")
    ap.add_argument("--reverify", action="store_true",
                    help="用当前校验口径重判已生成但被拒的变体，通过的补建 candidate")
    ap.add_argument("--split-benchmark", type=int, default=-1,
                    help="把 n 个段落划为基准段（§14）；-1=按 25%% 比例，传 0 只看不划")
    ap.add_argument("--fresh-benchmark", type=int, default=-1,
                    help="在**从未被任何实验用过**的段上生成纯基准集（n 个段 × 全部类型）")
    ap.add_argument("--judge", action="store_true", help="让评委判「人 vs 劣化版」")
    ap.add_argument("--judge-models", default=",".join(DEFAULT_JUDGES))
    ap.add_argument("--judge-limit", type=int, default=0)
    ap.add_argument("--variant", default="v4")
    args = ap.parse_args()

    # 批量防呆①（P0 死 id 事故）：会发 LLM 调用的分支，开跑前先把模型名问一遍网关。
    if not args.dry_run:
        pf.require_models(_llm_models(args), source="controlled_corruption")

    if args.report:
        report()
        return

    db.init_db()
    if args.fresh_benchmark > 0:
        ctypes2 = dispatchable([x.strip() for x in args.types.split(",") if x.strip()] or list(ALL_TYPES))
        exp_id = args.exp or f"EXP-{time.strftime('%m%d')}-BENCH"
        with db.session() as s:
            # 断点续跑必须**复用本实验已有的基准段**，不能重新挑：
            # pick_fresh_segments 会把"已经有劣化行的段"排除掉，重新挑等于每次都换一批新段
            # → 上一次因缺帧/空返回失败的格子永远没人回头补，池子还会无限膨胀
            # （2026-09-18 实测：连跑三次攒出 14 个段，只有 7 个有帧，0 条成功）。
            used = sorted({r[0] for r in s.query(ControlledCorruption.segment_id)
                           .filter(ControlledCorruption.experiment_id == exp_id).distinct()})
            # 续用的一律过 corpus v2 血缘闸（role='benchmark' 在本循环下方写入，
            # 让历史脏行经断点续跑复活=会审①指出的复发路径）
            held = [x for x in s.query(Segment).filter(
                Segment.id.in_(used), exclude_corpus_v2_segments()).all()] if used else []
            dropped_v2 = len(used) - len(held)
            if dropped_v2:
                print(f"[v2 闸] 断点续跑丢弃 corpus v2 镜像段 {dropped_v2} 个（不划基准）")
            picked = [(seg, None) for seg in held]
            if len(picked) < args.fresh_benchmark:
                more = pick_fresh_segments(s, args.fresh_benchmark - len(picked),
                                           args.seed, min_chars=args.min_chars)
                print(f"续用已有基准段 {len(picked)} 个，另挑新段 {len(more)} 个")
                picked += more
            print(f"纯基准段 {len(picked)} 个（从未被任何实验用过）；类型 {len(ctypes2)} 类；"
                  f"计划 {len(picked) * len(ctypes2)} 条")
            for seg, _ in picked[:10]:
                print(f'   {seg.id} {len(seg.text)}字 :: {seg.text[:36]}')
            if args.dry_run:
                print("（dry-run，不发调用）")
                return
            ensure_experiment(s, exp_id, f"hidden_benchmark {time.strftime('%Y-%m-%d')}")
            for seg, _ in picked:              # 生成前先划基准：漏划即泄漏
                seg.role = "benchmark"
            seg_ids = [seg.id for seg, _ in picked]
            s.commit()
        # 基准段是**没抽过帧**的段（这正是它"干净"的原因），但 candidates.frame_id
        # 是外键、非空 → 不先抽帧，65 条变体会全部以 no_frame_id 收场
        # （2026-09-18 实测踩到）。所以这里补一步 L 帧抽取，幂等。
        _ensure_frames(seg_ids, exp_id)
        bind_experiment(exp_id)
        with ThreadPoolExecutor(max_workers=max(1, args.conc)) as ex:
            list(ex.map(lambda sp: run_one(sp, ctypes2, exp_id=exp_id,
                                           gen_model=args.gen_model,
                                           verify_model=args.verify_model,
                                           ctx_limit=args.ctx_limit, dry_run=False), picked))
        print(f"\n完成：生成 ok={_stat['gen_ok']} failed={_stat['gen_failed']} "
              f"skip={_stat['skip']}；拒收 {_stat['rejected']}")
        if _stat["gen_failed"]:
            print(f"       首条错误原文：{_first_error(exp_id) or '（一条异常文本都没存进 DB）'}")
        report()
        return
    if args.split_benchmark >= 0:
        # 0 与 None 必须分开（监督整改）：0 = 标零段（校验/干跑语义），
        # 默认 -1 = 不进本分支。旧写法 `or None` 把 0 吃成全库默认比例。
        n = args.split_benchmark if args.split_benchmark >= 0 else None
        # --exp 透传（监督方实测缺口）：不限定会混入旧实验的无窗样本，
        # 破坏 bal-v2 的强制长度方向性质；空=全库（旧行为，供对照）。
        out = split_benchmark(n=n, seed=args.seed, dry_run=args.dry_run,
                              exp=args.exp or None)
        print(json.dumps(out, ensure_ascii=False))
        # 划零段且此前也没划过 = 大概率 exp 打错 / 池空——静默成功是谎报
        if not args.dry_run and out.get("marked", 0) == 0 and out.get("already", 0) == 0:
            raise SystemExit(f"[split-benchmark] 一段都没标成（pool_total="
                            f"{out.get('pool_total', 0)}）——exp 打错或池空，非零退出")
        return
    if args.recheck:
        out = recheck(verify_model=args.verify_model, conc=args.conc,
                      batch=args.batch, dry_run=args.dry_run,
                      all_variants=args.all_variants)
        print(json.dumps(out, ensure_ascii=False))
        report()
        return
    if args.reverify:
        out = reverify(verify_model=args.verify_model, conc=args.conc,
                       dry_run=args.dry_run,
                       only_types=[x.strip() for x in args.types.split(",") if x.strip()] or None)
        print(f"重判 {out['checked']} 条：救回 {out['rescued']}，仍拒 {out['still_rejected']}，"
              f"跳过 {out['skipped']}")
        report()
        return
    if args.judge:
        judge_corruptions(models=[m.strip() for m in args.judge_models.split(",") if m.strip()],
                          variant=args.variant, conc=args.conc,
                          limit=args.judge_limit, dry_run=args.dry_run)
        report()
        return
    if args.build_batch:
        out = build_batch(args.build_batch, args.batch_n, args.seed, dry_run=args.dry_run)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return

    ctypes = dispatchable([x.strip() for x in args.types.split(",") if x.strip()]
                          or list(ALL_TYPES))
    bad = [c for c in ctypes if c not in ALL_TYPES]
    if bad:
        raise SystemExit(f"未知类型 {bad}；可选 {list(ALL_TYPES)}")
    works = [x.strip() for x in args.works.split(",") if x.strip()] or None

    with db.session() as s:
        picked = pick_segments(s, args.n_seg, works, args.seed, skip_done=args.skip_done,
                               min_chars=args.min_chars)
        exp_id = args.exp
        if not exp_id:
            exp_id = f"EXP-{time.strftime('%m%d')}-CORR"
        print(f"取得人类段落 {len(picked)} 个；劣化类型 {len(ctypes)} 类；"
              f"计划生成 {len(picked) * len(ctypes)} 条")
        print(f"生成模型 {args.gen_model} / 校验模型 {args.verify_model}"
              f"（异源，不做自校验）")
        print(f"实验 {exp_id}；长度硬卡 {RATIO_LO}~{RATIO_HI}×，漂移阈值 {DRIFT_MAX}")
        if args.dry_run:
            for seg, f in picked[:20]:
                print(f'   {seg.id} {seg.work_id} ordinal={seg.ordinal} '
                      f'{len(seg.text)}字 frame={f.id if f else "-"} :: {seg.text[:40]}')
            print("（dry-run，不发调用）")
            return
        ensure_experiment(s, exp_id, f"controlled_corruption {time.strftime('%Y-%m-%d')}")

    bind_experiment(exp_id)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.conc)) as ex:
        list(ex.map(lambda sp: run_one(sp, ctypes, exp_id=exp_id,
                                       gen_model=args.gen_model,
                                       verify_model=args.verify_model,
                                       ctx_limit=args.ctx_limit, dry_run=False), picked))
    dt = time.time() - t0
    print(f"\n完成：生成 ok={_stat['gen_ok']} failed={_stat['gen_failed']} "
          f"skip={_stat['skip']}；校验 {_stat['verified']} 条，拒收 {_stat['rejected']} 条"
          f"（{dt / 60:.1f} 分钟）")
    if _stat["gen_failed"]:
        print(f"       首条错误原文：{_first_error(exp_id) or '（一条异常文本都没存进 DB）'}")
    report()


if __name__ == "__main__":
    main()
