"""重建全部课程的「知识点 → 问答语料」。

服务器上刚导完库时 `textbook_chunk` 是空的（seed 不带语料，语料由知识点正文现切），
所以问答会一直拒答"暂时没有知识片段"。这个脚本就是那一步的 CLI 版本——
和 `POST /api/v1/admin/qa-corpus/rebuild` 走同一个函数，不需要先登录拿 token。

    python scripts/rebuild_qa_corpus.py            # 全部课程
    python scripts/rebuild_qa_corpus.py --course analysis   # 只重建一门

逐门提交：一门课失败不影响其他课，最后汇总退出码（有失败则 1）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "scripts"))
from _env import ensure_runtime  # noqa: E402

ensure_runtime(Path(__file__).name, ROOT)

from app import db, qa_ingest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="重建问答语料（知识点正文切块）")
    parser.add_argument("--course", default="", help="只重建这一门课程；不填则全部")
    args = parser.parse_args()

    db.pool.open()
    try:
        started = time.time()
        if args.course:
            results = [qa_ingest.rebuild_course(args.course)]
        else:
            results = qa_ingest.rebuild_all()
        elapsed = time.time() - started
    finally:
        db.pool.close()

    failed = 0
    total = 0
    for row in results:
        if row.get("error"):
            failed += 1
            print(f"  ✗ {row['course_id']}：{row['error']}")
            continue
        chunks = row.get("chunks") or row.get("chunk_count") or 0
        total += int(chunks)
        print(f"  ✓ {row['course_id']}：{chunks} 块")

    print(f"\n{len(results)} 门课，共 {total} 块，用时 {elapsed:.1f} 秒")
    if failed:
        print(f"失败 {failed} 门，退出码 1")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
