#!/bin/zsh
cd -- "$(dirname -- "$0")" || exit 1
python3 scripts/launch.py
launch_status=$?
if [ "$launch_status" -ne 0 ] && [ "$launch_status" -ne 130 ]; then
  printf '启动未完成，请查看上方错误信息。按回车关闭。\n'
  read -r
fi
