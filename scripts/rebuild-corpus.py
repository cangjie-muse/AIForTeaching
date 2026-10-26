#!/usr/bin/env python3
"""查看并补齐问答语料（textbook_chunk）。

为什么要单独一个脚本：

    「片段」不是导入产生的。资源包导入只写知识点的标题和正文
    （knowledge_node）；问答检索读的是切成块的语料（textbook_chunk）。
    两者之间隔着一个切块动作，历史上只有管理员设置页里的「全部重建」。
    于是会出现最难解释的状态：内容明明导进去了、知识点页面能看，
    但问答一律拒答，检索范围里显示「0 片段」。

    现在导入时会自动切（content._rebuild_corpus），这个脚本用来
    兜住两类情况：导入时切块失败但被吞掉了，以及修复之前导入的旧课。

用法（在仓库根目录执行）：

    .venv/bin/python scripts/rebuild-corpus.py            # 先看，只补缺的
    .venv/bin/python scripts/rebuild-corpus.py --dry-run  # 只看，不动库
    .venv/bin/python scripts/rebuild-corpus.py --all      # 全部重建（先删再建）
    .venv/bin/python scripts/rebuild-corpus.py --course analysis-iii

默认行为是「只补缺语料的课」（有正文但 0 片段）。全部重建会先删旧块再插，
正在被引用的答案会短暂失去依据，所以不做默认。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description="查看/补齐问答语料")
    parser.add_argument("--course", default=None, help="只处理这一门课（课程 id）")
    parser.add_argument("--all", action="store_true", help="全部重建（会先删旧块）")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不改库")
    args = parser.parse_args()

    from app import db, qa_ingest

    db.pool.open()

    try:
        status = qa_ingest.status()
    except Exception as error:  # noqa: BLE001 —— 连不上库时把原因说清楚
        print(f"✗ 读不到语料状态：{error}")
        print(f"  连接串来自 {db.envfile.ENV_FILE}；对照 systemctl show whu-teaching -p EnvironmentFile")
        return 1

    items = status["items"]
    print(f"共 {len(items)} 门课，{status['courses_ready']} 门有语料，"
          f"{status['total_chunks']} 块。\n")
    print(f"{'课程 id':<34}{'有正文':>8}{'片段':>8}  状态")
    for row in items:
        chunks = int(row.get("chunks") or 0)
        if chunks:
            state = "有语料"
        elif int(row.get("nodes_with_text") or 0) > 0:
            state = "缺语料（导入时切失败了，或早于自动切块上线）"
        else:
            state = "没有正文，不用切"
        print(f"{str(row['course_id']):<34}{int(row.get('nodes_with_text') or 0):>8}"
              f"{chunks:>8}  {state}")
        if row.get("title"):
            print(f"{'':<34}{row['title']}")

    missing = qa_ingest.missing_courses()
    if args.course:
        targets = [row for row in items if row["course_id"] == args.course]
        if not targets:
            print(f"\n✗ 没有这门课：{args.course}")
            return 1
    elif args.all:
        targets = items
    else:
        targets = missing

    if not targets:
        print("\n没有需要处理的课。")
        return 0

    names = "、".join(str(row.get("title") or row["course_id"]) for row in targets)
    if args.dry_run:
        print(f"\n--dry-run：会给 {len(targets)} 门课切块（{names}），没有动库。")
        return 0

    print(f"\n开始切块：{len(targets)} 门课（{names}）")
    total = 0
    failed: list[str] = []
    for row in targets:
        try:
            result = qa_ingest.rebuild_course(row["course_id"])
            chunks = int(result.get("chunks") or 0)
            total += chunks
            print(f"  ✓ {row.get('title') or row['course_id']}：{chunks} 块")
            if chunks == 0:
                failed.append(f"{row['course_id']}（切出 0 块，知识点正文可能是空的）")
        except Exception as error:  # noqa: BLE001 —— 一门失败不影响其他课
            failed.append(f"{row['course_id']}（{error}）")
            print(f"  ✗ {row.get('title') or row['course_id']}：{error}")

    print(f"\n合计 {total} 块。")
    if failed:
        print("以下课程没切出内容：")
        for line in failed:
            print(f"  · {line}")
        print("  先确认这些课的知识点正文（content_md）不是空的；"
              "正文为空时切不出块，这是预期行为。")
        return 1
    print("完成。刷新网页「AI 问答」的检索范围，片段数应当已经变了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
