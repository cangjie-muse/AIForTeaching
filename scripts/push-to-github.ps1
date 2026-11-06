# 把当前提交推到 GitHub（在你自己的终端里跑，不在我被沙箱限制的环境里）
#
# 为什么需要你在自己的终端执行：推送要读 Windows 凭据管理器里的 GitHub 登录信息，
# 而我这边的沙箱禁止进程访问凭据库（git 报 schannel SEC_E_NO_CREDENTIALS /
# could not read Username）。读取（fetch/ls-remote）是通的，写入（push）必须由你来。
#
# 用法：右键"使用 PowerShell 运行"，或在 PowerShell 里：
#     cd C:\Users\仓颉\Desktop\AIForTeaching
#     .\scripts\push-to-github.ps1
#
# 说明：origin 走 ghproxy 镜像做拉取、推送时由 pushInsteadOf 改写成 github.com 直连。
# 若镜像侧推送失败，脚本会自动改用 GitHub 直连地址重试一次。

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

$branch = 'main'
Write-Host "==> 当前提交" -ForegroundColor Cyan
git --no-pager log --oneline -1

Write-Host "`n==> 与 origin/$branch 的差距" -ForegroundColor Cyan
$counts = git rev-list --left-right --count "origin/$branch...$branch"
Write-Host "  落后 / 领先：$counts"

Write-Host "`n==> 推送到 origin（ghproxy → github.com）" -ForegroundColor Cyan
$env:GIT_TERMINAL_PROMPT = '1'   # 允许弹凭据窗口/提示
git push origin $branch
if ($LASTEXITCODE -eq 0) {
  Write-Host "`n✔ 推送成功。" -ForegroundColor Green
  git --no-pager log --oneline -1 "origin/$branch"
  exit 0
}

Write-Host "`n! 镜像推送失败，改用 GitHub 直连地址重试……" -ForegroundColor Yellow
git push https://github.com/cangjie-muse/AIForTeaching.git $branch
if ($LASTEXITCODE -ne 0) {
  Write-Host @"

✗ 两次都失败。请按报错处理：
  - 需要凭据：弹出窗口里用 GitHub 账号 + Personal Access Token（classic，勾 repo）
  - 拒绝非快进：先 git fetch origin main 再看是否需要 rebase；
    确认本地就是想要的版本后可加 --force-with-lease
  - 网络受限：用 .tmp\mathagent-*.bundle 直接 scp 到服务器（git clone 该文件）
"@ -ForegroundColor Red
  exit 1
}
Write-Host "`n✔ 直连推送成功。" -ForegroundColor Green
