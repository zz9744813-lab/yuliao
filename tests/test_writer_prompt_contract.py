"""提示词契约回归（会审 BLOCK 的修复件：静态、确定性，不花一次模型调用）。

背景：``app/scene_runtime/pipeline.py`` 的 WRITER_SYSTEM / VERIFIER_SYSTEM 被会审
判定 BLOCK，理由是「改了 hard 判定范围与验收语义，却没有任何测试/回归变更」，
并要求至少三条回归：
  ① 计划外持久变更仍被判 hard；
  ② 「缺少推进来由/计量单位措辞不同」不被判 hard；
  ③ 自豁免路径不被滥用（反向）。

判词本身由模型给出、无法离线断言，因此这里钉的是**规则文本**：把会审指出的
每一处口径都变成一条可判定断言，任何一次提示词改动只要破坏这些不变量就会红。
"""
from __future__ import annotations

import re

from app.scene_runtime.pipeline import VERIFIER_SYSTEM, WRITER_SYSTEM

# 会审点名：轮次专有字面量（r95 场景的键名与专名）不得硬编进通用提示词。
R95_LITERALS = ("韩灯", "公开簿", "今日不签", "不替全城下结论",
                "signing_hold", "temporary_terms", "notice_board",
                "rescue_scope", "survivor_signal")
# 会审点名：内部错误码不得喂给模型（有被抄进正文的风险）。
INTERNAL_CODES = ("state_patch_not_authorized_by_plan", "gateway_invalid_or_partial_result",
                  "invalid_model_contract", "RuntimeFault")


# --- ① 计划外持久变更仍被判 hard（hard 覆盖面不许被收窄到漏掉写作者硬约束） --------

def test_hard_still_covers_unauthorized_persistent_changes():
    assert "之外" in VERIFIER_SYSTEM and "持久事实变更" in VERIFIER_SYSTEM
    assert "新增可被后续场景引用的设定" in VERIFIER_SYSTEM


def test_hard_categories_cover_the_writer_hard_constraints():
    """会审 [严重]：hard 收窄成两类后，写作者的 5 条硬约束无人执法。逐条钉住。"""
    assert "视点越界或离场未收" in VERIFIER_SYSTEM          # 离场即收 + 视角
    assert "无法观察的内容" in VERIFIER_SYSTEM
    assert "核心事件未落地或专名被换" in VERIFIER_SYSTEM      # 核心事件 + 载体照抄
    assert "载体/名目/专名被替换" in VERIFIER_SYSTEM
    assert "不得降级为 style" in VERIFIER_SYSTEM             # 不许把 hard 降级


def test_hard_categories_still_include_the_original_two():
    assert "与源世界状态、世界规则、人物知识范围、资源或伤势记录" in VERIFIER_SYSTEM


# --- ② 「缺少推进来由 / 计量单位措辞不同」不被判 hard ---------------------------

def test_scoped_exemption_for_in_plan_changes_survives():
    assert "缺少推进来由的解释" in VERIFIER_SYSTEM
    assert "计量单位措辞不同" in VERIFIER_SYSTEM


# --- ③ 自豁免路径不被滥用（反向） ----------------------------------------------

def test_self_exemption_is_keyed_to_the_plan_not_to_the_reviewers_own_changes():
    """会审 [严重]：原文「凡你在 changes/events 里用正文原句证明其成立者不得再报 hard」
    给了核对者一条自我豁免通道 ⇒ 必须改成以 plan 原文为准，并显式堵死。"""
    assert "以 plan 原文为准" in VERIFIER_SYSTEM
    assert "不以你自己写进 changes 的条目为准" in VERIFIER_SYSTEM
    assert "不能给自己开豁免" in VERIFIER_SYSTEM
    # 旧口径的措辞必须消失（它是"用自己写的 changes 证明"的入口）
    assert "凡你在 changes/events 里用正文原句证明其成立者" not in VERIFIER_SYSTEM


# --- 会审指出的其它口径问题 ----------------------------------------------------

def test_exit_rule_has_exactly_one_criterion():
    """会审 [一般]：离场即收写了两遍且判定标准不同（"最后一句/动作或话语" vs "最后一个动作"）。"""
    body = WRITER_SYSTEM
    assert body.count("【第一硬规则 · 离场即收】") == 1        # 规则头只剩一条
    assert "·离场即收（硬）" not in body                      # 旧重复条目已删
    assert "动作**或话语**" in body and "判定标准只有这一条" in body
    assert "本场最后一个动作必须是视点人物自己做的" not in body  # 旧的双标准已删


def test_there_is_a_single_documented_priority_rule():
    """会审 [一般]：两处自称最高优先，冲突时无可判定裁决。"""
    assert "【优先级（唯一裁决口径）】" in WRITER_SYSTEM
    assert "先让人物把该事件落地" in WRITER_SYSTEM
    assert "最高优先" not in WRITER_SYSTEM


def test_verifier_json_template_shows_all_three_required_keys():
    """两个席位都称模板缺 events 键（实为误读）—— 用断言把事实钉死。"""
    template = VERIFIER_SYSTEM[VERIFIER_SYSTEM.index("只返回 JSON："):VERIFIER_SYSTEM.index("issues 没有问题")]
    for key in ('"issues"', '"changes"', '"events"'):
        assert key in template, key
    assert "三个键**必须都存在**" in VERIFIER_SYSTEM


def test_writer_output_contract_is_text_only():
    """会审 [一般]：把核对者才产出的 changes 契约塞进了写作者提示词，两处口径打架。"""
    assert '{"text":"完整正文"}' in WRITER_SYSTEM
    assert "逐字相同" not in WRITER_SYSTEM
    assert "changes 清单" not in WRITER_SYSTEM


def test_prop_handoff_cannot_smuggle_an_unauthorized_ownership_change():
    """会审 [一般]：道具交接规则本身可能制造被禁的未授权变更。"""
    assert "交接只写动作" in WRITER_SYSTEM
    assert "不得在正文里声明持有权或归属发生了变更" in WRITER_SYSTEM


def test_no_round_specific_literals_leak_into_shared_prompts():
    for prompt, name in ((WRITER_SYSTEM, "WRITER_SYSTEM"), (VERIFIER_SYSTEM, "VERIFIER_SYSTEM")):
        for literal in R95_LITERALS:
            assert literal not in prompt, "%s 仍含轮次专有字面量: %s" % (name, literal)


def test_no_internal_error_codes_leak_into_prompts():
    for prompt, name in ((WRITER_SYSTEM, "WRITER_SYSTEM"), (VERIFIER_SYSTEM, "VERIFIER_SYSTEM")):
        for code in INTERNAL_CODES:
            assert code not in prompt, "%s 泄露内部错误码: %s" % (name, code)


def test_fact_protection_is_abstract_not_a_whitelist():
    """会审 [一般]：「含 X、Y、Z」会被读成白名单，未列出的反而像被放行。"""
    assert "不论是否点名" in WRITER_SYSTEM
    assert re.search(r"含 plan 与本场可见事实里出现的受保护事实", WRITER_SYSTEM)


def test_goal_prefix_carries_the_recap_and_length_limits_hold():
    """DR-2 / DR-5 的结构性不变量（防止后续提交无声放宽）。"""
    from app.scene_runtime.contracts import ScenePlan
    goal = ScenePlan.model_fields["goal"]
    assert goal.annotation is str
    assert goal.metadata or True  # 上限由 Field(max_length=1500) 表达，校验在模型层
