"""队列诊断：一条命令说清"点了没反应"卡在哪。

用法（服务器上）：

    cd /home/aiformath/AIForTeaching && .venv/bin/python scripts/diagnose-queue.py

它只读不写：查最近的任务、算队列积压、看依赖装没装、看 worker 服务活没活。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

print("=" * 66)
print("队列诊断")
print("=" * 66)

from app import db, llm  # noqa: E402

# 先用一次性连接探活，连不上就直接退出。
# 不要一上来就 open 连接池：池初始化失败会留下重连线程，
# 解释器退出时 psycopg_pool 会抱怨 "couldn't stop thread ... within 5.0 seconds"，
# 把真正的结论淹在噪音里。
import psycopg  # noqa: E402

try:
    with psycopg.connect(db.DATABASE_URL, connect_timeout=5):
        pass
except Exception as exc:
    print(f"\n✗ 连不上数据库：{exc}")
    print(f"  连接串来自 {db.envfile.ENV_FILE}（不存在则用代码默认值）")
    print("  • 密码认证失败：.env 里的口令与数据库实际口令不一致，"
          "对照 sudo -u postgres psql -c '\\du'")
    print("  • 连接被拒/超时：postgresql 没起，或地址端口不对："
          "sudo systemctl status postgresql")
    raise SystemExit(1)

db.pool.open(wait=True, timeout=10)

KIND_LABEL = {
    "generate": "教材生成", "generate-book": "整本教材",
    "grade-homework": "作业批改",
    "courseware-generate": "课件初版", "courseware-revise": "课件修订",
}

connected = False
try:
    summary = db.query_one(
        """
        SELECT
          (SELECT count(*) FROM pipeline_job WHERE status = 'queued')  AS queued,
          (SELECT count(*) FROM pipeline_job WHERE status = 'running') AS running,
          (SELECT max(started_at) FROM pipeline_job) AS last_claim,
          (SELECT extract(epoch FROM (now() - max(started_at))) FROM pipeline_job) AS since_claim,
          (SELECT extract(epoch FROM (now() - min(created_at)))
             FROM pipeline_job WHERE status = 'queued') AS oldest_age
        """
    ) or {}
    connected = True
except Exception as exc:  # 连库都失败时，先把这个原因说清楚
    print(f"\n✗ 数据库连不上：{exc}")
    print(f"  连接串来自 {db.envfile.ENV_FILE}（不存在则用代码默认值）")
    print("  对照：systemctl show whu-teaching -p EnvironmentFile；sudo systemctl status postgresql")
    raise SystemExit(1)

queued = int(summary.get("queued") or 0)
running = int(summary.get("running") or 0)
since = summary.get("since_claim")
oldest = summary.get("oldest_age")

print(f"\n排队 {queued} 个，执行中 {running} 个")
print(f"最近一次取任务：{summary.get('last_claim') or '从未有过'}"
      + (f"（{int(since)} 秒前）" if since is not None else ""))
if oldest is not None:
    print(f"最老的排队任务已等：{int(oldest // 60)} 分 {int(oldest % 60)} 秒")

print("\n最近 12 个任务：")
rows = db.query(
    """SELECT id, kind, status, attempts, created_at, started_at, finished_at,
              left(coalesce(error, ''), 70) AS error
         FROM pipeline_job ORDER BY id DESC LIMIT 12"""
)
if not rows:
    print("  （一条都没有：任务根本没进队列，问题在接口那一侧）")
for row in rows:
    print(f"  #{row['id']:<5} {(KIND_LABEL.get(row['kind']) or row['kind']):<8} "
          f"{row['status']:<9} 尝试{row['attempts']} 建于 {row['created_at']}"
          + (f"\n         错误：{row['error']}" if row.get("error") else ""))

print("\n依赖与配置：")
try:
    import pptx  # noqa: F401
    print("  ✓ python-pptx 已装（课件渲染需要）")
except ImportError:
    print("  ✗ python-pptx 没装：课件任务会在渲染那一步失败 "
          "（pip install -r server/requirements.txt）")

models = llm.describe()
chat = models.get("chat") or {}
print(f"  文本模型：{chat.get('name')} / {chat.get('model')}")
vision = models.get("vision") or {}
print(f"  视觉模型：{vision.get('name')} / {vision.get('model')}")
if not chat.get("model"):
    print("  ！没有可用的文本模型：生成类任务会直接失败")

print("\nworker 服务：")
for command in (["systemctl", "is-active", "whu-worker"],
                ["systemctl", "show", "whu-worker", "-p", "ActiveState,ExecMainStatus"]):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        print(f"  $ {' '.join(command)}\n    {(result.stdout or result.stderr).strip()}")
    except Exception as exc:
        print(f"  $ {' '.join(command)}\n    取不到：{exc}")
try:
    logs = subprocess.run(["journalctl", "-u", "whu-worker", "-n", "12", "--no-pager"],
                          capture_output=True, text=True, timeout=15)
    print("  最近日志：")
    for line in (logs.stdout or logs.stderr or "（取不到日志，可能需要 sudo）").strip().splitlines():
        print(f"    {line}")
except Exception as exc:
    print(f"  日志取不到：{exc}")

print("\n判读：")
if queued and (since is None or since > 600):
    print("  ✗ 任务在排队但 10 分钟内没人取 —— worker 没在跑或卡住了：")
    print("    sudo systemctl restart whu-worker && sudo journalctl -u whu-worker -n 40")
elif queued and oldest is not None and oldest > 120:
    print("  ！worker 在取任务但队列一直不消化 —— 单个任务可能卡在模型调用上：")
    print("    看上面 running 那条的 kind，再对照日志。")
elif not queued and not running:
    print("  ✓ 队列是空的。如果你刚点过按钮却没看到任务，问题在「任务入队」之前，"
          "即接口返回了什么错误。")
else:
    print("  ✓ 队列正常在动。")
print("=" * 66)

if connected:
    try:
        db.pool.close()
    except Exception:
        pass
