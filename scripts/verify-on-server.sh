#!/usr/bin/env bash
# 服务器侧验收：一条命令跑完"能在这台机器上真跑"的全部检查，并把结果汇总成一段可粘贴的文字。
#
# 为什么需要它：本地开发机没有外网，CP-SAT（ortools）装不上，所以固定卷阶段 B 的
# 求解器只能到服务器上验。这个脚本把"该跑哪些命令、按什么顺序、失败长什么样"
# 固化下来，避免每次靠记忆拼命令、也避免把没跑的东西写成通过。
#
# 用法：
#   bash scripts/verify-on-server.sh              # 全量
#   bash scripts/verify-on-server.sh --quick      # 只跑迁移 + Python 单测（跳过前端）
#
# 约定：任何一步失败都不中断，全部跑完后统一汇总——一次拿到所有问题，
# 比修一个跑一遍快得多。退出码：0 = 全过；1 = 有失败；2 = 环境缺件。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

QUICK=0
[ "${1:-}" = "--quick" ] && QUICK=1

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || true)"

pass=0; fail=0; skipped=0
declare -a results

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
run() {
  local name="$1"; shift
  printf '  %-46s' "$name"
  if "$@" >/tmp/verify-step.log 2>&1; then
    printf '\033[32mPASS\033[0m\n'; pass=$((pass+1)); results+=("PASS  $name")
  else
    printf '\033[31mFAIL\033[0m\n'; fail=$((fail+1)); results+=("FAIL  $name")
    sed -n '1,40p' /tmp/verify-step.log | sed 's/^/        /'
  fi
}
skip() { printf '  %-46s\033[33mSKIP\033[0m  %s\n' "$1" "$2"; skipped=$((skipped+1)); results+=("SKIP  $1（$2）"); }

if [ -z "$PY" ]; then
  echo "找不到 python3：先装 Python 3.12 并建好 .venv（见 docs/部署到 mathagent.md）"
  exit 2
fi

say "0. 环境"
"$PY" -V | sed 's/^/  /'
printf '  %-46s' "ortools 可用性"
if "$PY" -c 'import ortools' >/dev/null 2>&1; then
  printf '\033[32m已安装\033[0m %s\n' "$("$PY" -c 'from importlib.metadata import version;print(version("ortools"))' 2>/dev/null)"
else
  printf '\033[33m未安装（CP-SAT 相关检查会被跳过，不会被算作通过）\033[0m\n'
fi
printf '  %-46s' "DATABASE_URL"
if [ -n "${DATABASE_URL:-}" ]; then printf '已设置\n'; else printf '\033[33m未设置（迁移与集成检查会被跳过）\033[0m\n'; fi

say "1. 数据库迁移（真引擎）"
if command -v node >/dev/null; then
  run "迁移 + 行为断言（PGlite 真引擎）" node scripts/verify-migrations.cjs
else
  skip "迁移真引擎检查" "没装 node"
fi

say "2. Python 单测（固定卷阶段 A + B）"
SUITES=(
  server/tests/test_blueprints.py
  server/tests/test_blueprint_course_api.py
  server/tests/test_papers.py
  server/tests/test_paper_validation.py
  server/tests/test_paper_review.py
  server/tests/test_rendering.py
  server/tests/test_assignments.py
  server/tests/test_paper_acceptance.py
  server/tests/test_assembly_blueprint.py
  server/tests/test_assembly_candidate.py
  server/tests/test_assembly_diagnostics.py
  server/tests/test_assembly_comparator.py
  server/tests/test_assembly_verification.py
  server/tests/test_assembly_ab.py
  server/tests/test_assembly_pipeline.py
  server/tests/test_assembly_api.py
  server/tests/test_assembly_worker.py
  server/tests/test_load_assembly.py
)
for suite in "${SUITES[@]}"; do
  run "$(basename "$suite")" "$PY" "$suite"
done

say "3. CP-SAT 求解器（只有装了 ortools 才会真跑）"
if "$PY" -c 'import ortools' >/dev/null 2>&1; then
  run "求解器 13 条断言" "$PY" server/tests/test_assembly_solver.py
  run "1,000 候选 / 50 槽位（含求解计时）" "$PY" scripts/load_assembly.py --candidates 1000 --slots 50
else
  skip "test_assembly_solver.py" "未安装 ortools"
  skip "load_assembly 求解计时" "未安装 ortools"
fi

if [ "$QUICK" -eq 0 ]; then
  say "4. 前端与视图测试"
  if command -v node >/dev/null; then
    for view in workbench build assembly editor ops; do
      file="scripts/test-view-paper-${view}.cjs"
      [ -f "$file" ] && run "test-view-paper-${view}" node "$file"
    done
    run "脚本顺序与 App 依赖" node scripts/test-script-order.cjs
  else
    skip "前端视图测试" "没装 node"
  fi
fi

say "汇总"
for line in "${results[@]}"; do echo "  $line"; done
printf '\n  通过 %d / 失败 %d / 跳过 %d\n' "$pass" "$fail" "$skipped"
echo
echo "把上面【汇总】整段贴回对话即可；失败项的前 40 行日志已打印在对应位置。"
[ "$fail" -eq 0 ] || exit 1
