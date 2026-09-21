"""A04 回归：评审游标路径必须从 config.DATA_DIR 派生（审查 20260920-1810）。

事故：_CURSOR_FILE 旧硬编码指向仓库 data/serve_cursor.json——测试只隔离
LG_DATA_DIR 与数据库，管不住这条路径。test_review_batch 的 unlink、取题
测试的写入全打在真实文件上：正式游标被测试批次键（c41/rj*/pr*）反复
污染、多轮全量测试反复销毁历史内容（18:10 审查证据：9 键中 3 个确认
测试键；次日实测 15 键全为已知测试批次名，真实用户键已不可恢复——
无 git 追踪、无内容备份）。

影响评估（如实）：游标只是**轮换起点**，真实进度在 review_items
（status=done，完好无损）；_pick_next 固定整批列表 + 跳过已判——
游标丢失=下次从 0 开始扫、跳过已判、从首个待判继续，无判定数据损失。

锁死：
1. _CURSOR_FILE == config.DATA_DIR / "serve_cursor.json"——测试
   （LG_DATA_DIR→临时目录）与生产各写各的文件，永不串线；
2. 仓库 data/serve_cursor.json 若再出现，必是有人把硬编码改回来了。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import api as api_mod                    # noqa: E402
from app import config                            # noqa: E402


def test_cursor_file_derives_from_data_dir():
    """路径从 config.DATA_DIR 派生——测试环境的 LG_DATA_DIR 临时目录
    自动接住全部游标写入，正式文件永不被测试触碰。"""
    assert api_mod._CURSOR_FILE == config.DATA_DIR / "serve_cursor.json", \
        "_CURSOR_FILE 被改回硬编码路径了？——A04：正式游标会被测试污染"


def test_cursor_file_not_the_repo_hardcoded_path():
    """测试环境里游标文件绝不能落在仓库 data/ 下（旧硬编码的事故路径）。"""
    repo_data_path = (ROOT / "data" / "serve_cursor.json").resolve()
    assert api_mod._CURSOR_FILE.resolve() != repo_data_path, \
        "游标又指回仓库 data/serve_cursor.json——测试将污染正式游标（A04）"
