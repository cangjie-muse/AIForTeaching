#!/usr/bin/env bash
# 把当前分支推到所有已配置的远端（Gitee 优先），一个失败不影响另一个。
#
#   bash scripts/sync-remotes.sh            # 推 main
#   bash scripts/sync-remotes.sh --tags     # 连标签一起推
#
# 为什么需要它：GitHub 在国内经常连不上，一条 `git push` 失败就会让人以为"推上去了"。
# 这个脚本逐个推、逐个报结果，最后给一句结论。
set -uo pipefail

BRANCH="${GIT_BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
WITH_TAGS=0
for arg in "$@"; do
  case "$arg" in
    --tags) WITH_TAGS=1 ;;
    *) echo "未知参数：$arg"; exit 2 ;;
  esac
done

ok_count=0
fail_count=0
declare -a failed=()

for remote in gitee origin github; do
  if ! git remote get-url "$remote" >/dev/null 2>&1; then
    continue
  fi
  printf '→ %-7s %s\n' "$remote" "$(git remote get-url "$remote")"
  if git push "$remote" "$BRANCH" >/dev/null 2>&1; then
    if [ "$WITH_TAGS" = 1 ]; then
      git push "$remote" --tags >/dev/null 2>&1 || echo "  ！标签推送失败"
    fi
    echo "  ✓ 推送成功"
    ok_count=$((ok_count + 1))
  else
    echo "  ✗ 推送失败（网络或凭据）：git push $remote $BRANCH 单独重试看报错"
    fail_count=$((fail_count + 1))
    failed+=("$remote")
  fi
done

echo
if [ "$ok_count" -eq 0 ]; then
  echo "没有任何远端推送成功。检查：git remote -v、网络、Gitee 令牌是否过期。"
  exit 1
fi
if [ "$fail_count" -gt 0 ]; then
  echo "成功 $ok_count 个，失败 $fail_count 个（${failed[*]}）。"
  echo "只要有一个成功，代码就没丢；补推：git push ${failed[0]} $BRANCH"
  exit 0
fi
echo "全部推送成功（$ok_count 个远端）。"
