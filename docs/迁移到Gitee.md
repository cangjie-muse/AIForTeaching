# 从 GitHub 换到 Gitee

GitHub 在校园网里经常连不上，`git push` 卡住或超时。换到 Gitee 后代码托管在国内，直连即可。
这份文档覆盖三件事：建仓库、本地切、服务器切。

> 本文里的 `<用户名>` 换成你自己的 Gitee 用户名。
> **本仓库实际使用 `dr_chan`**：`https://gitee.com/dr_chan/AIForTeaching.git`（私有）。私有仓库的完整地址形如
> `git@gitee.com:<用户名>/AIForTeaching.git`。

---

## 0. 在 Gitee 建仓库

网页操作，三步：

1. 登录 Gitee → 右上角「+」→「新建仓库」；
2. 仓库名 **AIForTeaching**，可见性选 **私有**；
3. **不要勾**「使用 Readme 文件初始化这个仓库」——勾了会多出一个初始提交，第一次 push 就要处理分叉。

建完记下地址（仓库首页「克隆/下载」里有两个）：

| 方式 | 地址 | 适用 |
| --- | --- | --- |
| SSH | `git@gitee.com:<用户名>/AIForTeaching.git` | **推荐**，配一次 key 后不再问密码 |
| HTTPS | `https://gitee.com/<用户名>/AIForTeaching.git` | 用私人令牌当密码，脚本里不好轮换 |

为什么选私有：仓库里有 14 门课的知识点、习题题干、课件 PPTX，按「内部资料」对待。
公开仓库意味着任何人搜到就能 clone 全部课程数据。

**未验证**：Gitee 免费账号建私有仓库是否需要先完成实名认证，以及免费账号的私有仓库数量上限。
如果建仓库时被要求实名，按页面提示走一遍（身份证 + 人脸，通常几分钟）。

---

## 1. 配 SSH key（一次，之后不再问密码）

### 本地（Windows）

```bash
ssh-keygen -t ed25519 -C "gitee" -f ~/.ssh/id_ed25519_gitee
cat ~/.ssh/id_ed25519_gitee.pub
```

把 `cat` 出来的整行（`ssh-ed25519 AAAA...` 开头）贴到
**Gitee → 设置 → SSH 公钥**。

然后让 `gitee.com` 用这把钥匙（避免和 GitHub 的 key 冲突）。新建或编辑 `~/.ssh/config`：

```
Host gitee.com
  HostName gitee.com
  User git
  IdentityFile ~/.ssh/id_ed25519_gitee
  IdentitiesOnly yes
```

测试：

```bash
ssh -T git@gitee.com
```

看到 `Hi <用户名>! You've successfully authenticated...` 就对了。第一次会问 `yes/no`，输 `yes`。

### 服务器（`aiformath` 用户）

同样一套，只是路径不同：

```bash
ssh-keygen -t ed25519 -C "gitee-server" -f ~/.ssh/id_ed25519_gitee
cat ~/.ssh/id_ed25519_gitee.pub
```

把这行**也**加到 Gitee 的 SSH 公钥里（可以和本地那把并存），然后写 `~/.ssh/config` 同上。

服务器不能用 HTTPS + 令牌的替代方案吗？可以，但要把令牌存到
`~/.git-credentials`（`git config credential.helper store`），权限得手动收紧到 600，
而且令牌泄露后要重发。SSH 更省事。

---

## 2. 本地切换（Windows）

先用 `--dry-run` 看一眼，它只检查不推送：

```bash
cd C:\Users\仓颉\Desktop\AIForTeaching
bash deploy/migrate-to-gitee.sh --url git@gitee.com:<用户名>/AIForTeaching.git --dry-run
```

输出会告诉你：仓库 38MB、最大单文件 8MB（`data/courses.js`）、能不能连上目标仓库。
连不上就先解决第 1 步的 key，别往下走。

确认无误再真推：

```bash
bash deploy/migrate-to-gitee.sh --url git@gitee.com:<用户名>/AIForTeaching.git
```

脚本做四件事：

1. 加远程 `gitee`；
2. `git push gitee --all` + `--tags`（分支和标签都过去）；
3. 把原来的 `origin`（GitHub）改名为 `github` 留作备份；
4. 把 `gitee` 改名为 `origin`。

想彻底断掉 GitHub：加 `--no-backup`。

验证：

```bash
git remote -v            # origin 指向 gitee，github 是备份
git log --oneline -1
```

---

## 3. 服务器切换

服务器上的 `/home/aiformath/AIForTeaching` 是**另一个克隆**，它有自己的远程配置，不会跟着变：

```bash
cd ~/AIForTeaching
git remote set-url origin git@gitee.com:<用户名>/AIForTeaching.git
git fetch origin
git status
```

`git status` 显示 `Your branch is up to date with 'origin/main'` 就通了。

两边提交对得上：

```bash
git rev-parse HEAD     # 服务器
git rev-parse HEAD     # 本地
```

两个短哈希一样 → 同一个版本。

如果服务器访问 Gitee 也失败（很少见），把输出贴给我：先看 `ssh -T git@gitee.com` 是不是 key 没配。

---

## 4. 日常怎么用

换了之后没有区别，还是这几条：

```bash
git add -A
git commit -m "改了什么"
git push
```

`git pull` 现在从 Gitee 拉。

想同时留一份 GitHub 备份（不推荐依赖它，但多个备份没坏处）：

```bash
git push github main
```

GitHub 连不上就跳过——这正是换 Gitee 的原因。

---

## 5. 迁移后要知道的几件事

**GitHub Actions 不会在 Gitee 上跑。** `.github/workflows/check.yml` 留在仓库里，但 Gitee
不认这个目录。质量门禁改成在本地跑：

```bash
npm run check          # 现在 818 项
```

**仓库体积。** 当前 38MB（历史全算上），最大单文件 8.24MB。Gitee 对单文件有上限
（常见说法 100MB），我们离得很远。**未验证** Gitee 免费仓库的总容量上限和单次 push 的总体积上限。

**不要提交这些。** 它们会让仓库膨胀或泄露内容，已经在 `.gitignore` 里，别手动加进去：

- `db/seed.sql`（12MB，由 `python db/build_seed.py` 生成）
- 教材 PDF（版权限制，`uploads/` 不入库）
- `secrets/`（模型 API Key）

**bootstrap.sh 还会访问 GitHub。** 它编译 pgvector 时从 GitHub 拉源码，跟代码托管是两件事。
如果那一步失败，见 `docs/服务器部署教程.md` 里 `PGVECTOR_GIT` 的说明。

---

## 6. 排障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `Permission denied (publickey)` | key 没加到 Gitee，或 `~/.ssh/config` 没生效 | `ssh -T git@gitee.com` 先测；确认 `IdentitiesOnly yes` |
| `repository not found` | 地址写错，或用了别人的用户名 | 仓库首页复制地址，别手打 |
| `! [rejected] main -> main (fetch first)` | Gitee 仓库里已有初始提交 | `git pull --rebase origin main` 再 push |
| push 卡在 `Writing objects` | 网络或体积 | 换 SSH；确认没提交大文件 |
| `remote: error: hook declined to update refs/heads/main` 且提示 `Push will publish a hidden email` | Gitee 账号把邮箱设成了「不公开」，而历史提交里带的是真实邮箱 | Gitee → 设置 → 邮箱管理 → 取消「不公开我的邮箱地址」（或把该邮箱设为公开）后重推。不要靠改写历史绕过：改邮箱会让所有提交哈希变化，GitHub 上的历史也跟着分叉 |
| `deploy/update.sh` 输出里某一步整段消失（比如没有 `==> 4`） | 脚本正在运行时被这次 `git pull` 替换，bash 按字节偏移继续读，后半段读到了新文件 | 已修：脚本检测到自身变化会 `exec` 重跑一遍。手动绕过：先 `git fetch gitee && git merge --ff-only gitee/main`，再 `bash deploy/update.sh` |
| 服务器 `git pull` 还去 GitHub | 远程没改 | 第 3 步的 `set-url` 没跑或跑在别的目录 |

---

## 7. 部署脚本自动选远端

服务器上不必手写 `git pull`。`deploy/update.sh` 会按 **gitee → origin → github** 的顺序试，
一个连不上自动换下一个——GitHub 超时不再卡住整次更新：

```bash
bash deploy/update.sh                    # 自动选，优先 gitee
GIT_REMOTE=gitee bash deploy/update.sh   # 强制走 gitee
GIT_BRANCH=main bash deploy/update.sh
bash deploy/update.sh --dry-run          # 只列会拉到的提交，不改任何东西
```

三个远端全连不上时，脚本会明确报"所有远端都拉不下来"，而不是留一个半截状态。

本机要一次推到两边，用：

```bash
bash scripts/sync-remotes.sh --tags
```

它逐个远端推、逐个报结果：Gitee 成功、GitHub 失败时，代码没丢，只提示补推哪一边。
