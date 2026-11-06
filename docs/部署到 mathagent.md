# 部署到 mathagent 服务器（阶段 A + B 当前状态）

本文只讲**在这台服务器上把项目跑起来、并验证到哪些结论**。与旧文档
[服务器部署与迁移指南](服务器部署与迁移指南.md) 的关系：那份是历史完整部署说明，
目录名与安装清单以本文为准（服务器目录统一为 `mathagent`）。

---

## 1. 服务器需要装什么

| 组件 | 版本 | 用途 | 不装的后果 |
| --- | --- | --- | --- |
| Python | 3.12 | 后端 + 测试 | 起不来 |
| Node.js | 20+ | 前端检查脚本、迁移真引擎检查（PGlite） | 前端与迁移断言无法验证 |
| PostgreSQL | 16 | 业务库 | 起不来 |
| pgvector | 与 PG16 匹配 | `vector(1024)` 列与 hnsw 索引 | 0001 迁移失败 |
| **OR-Tools** | **==9.11.4210** | **约束组卷的 CP-SAT 求解器** | 组卷页运行会显示"这台机器没有装求解器"，不会产出假方案 |
| nginx（可选） | 任意稳定版 | TLS 与反代 | 只能本机访问 |

OR-Tools 是这次新增的唯一硬依赖（`server/requirements-assembly.txt`）：

```bash
.venv/bin/python -m pip install -r server/requirements-assembly.txt
.venv/bin/python -c "from importlib.metadata import version; print(version('ortools'))"
# 期望输出：9.11.4210
```

> 本地开发机没有外网、装不上 ortools，所以 **CP-SAT 的全部结论只能在这台服务器上
> 第一次得到验证**。装完请立刻跑第 5 节里带 ★ 的两条命令。

---

## 2. 取代码与目录

```bash
sudo mkdir -p /srv/mathagent
sudo chown "$USER":"$USER" /srv/mathagent
git clone <仓库地址> /srv/mathagent          # 或从本地 scp 一份干净的工作树
cd /srv/mathagent
git log --oneline -1                          # 记下这个 commit，反馈问题时贴它
```

只从 git 取，不要整盘拷贝工作目录：本地有 `.worktrees/`（1.6 GB）、`.tmp/`、
`e2e-gen-*` 等测试残留，都不该上服务器。

---

## 3. 安装与初始化

```bash
cd /srv/mathagent
python3.12 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -r server/requirements.txt
.venv/bin/python -m pip install -r server/requirements-assembly.txt   # OR-Tools

cp server/.env.example .env && chmod 600 .env
# 必须改的三行：
#   DATABASE_URL=postgresql://<user>:<pass>@127.0.0.1:5432/<db>
#   AUTH_MODE=dev（仅试用）或 cas（学校统一身份，未实现时调用会 501）
#   APP_ENV=dev|prod（prod 下 AUTH_MODE=dev 会被服务拒绝启动）
```

数据库（若还没建）：

```bash
sudo -u postgres psql -c "CREATE DATABASE mathagent;"
sudo -u postgres psql -d mathagent -c "CREATE EXTENSION IF NOT EXISTS vector;"
.venv/bin/python db/migrate.py --status    # 看已应用与待应用
.venv/bin/python db/migrate.py --apply     # 期望：35 个迁移全部应用
```

`--apply` 之后 `--status` 里应显示 **35/35**，其中包含本阶段新增的
`0034_assembly_run.sql`（求解运行表）与 `0035_assembly_job_kind.sql`
（`pipeline_job` 认识 `paper-assembly`）。

---

## 4. 启动

```bash
# API
.venv/bin/uvicorn app.main:app --app-dir server --host 127.0.0.1 --port 8000

# worker（组卷求解、打印渲染、生成任务都在这一条队列里）
.venv/bin/python scripts/run_worker.py
```

常驻用 systemd（模板在 `deploy/whu-teaching.service` 与 `deploy/whu-worker.service`，
把 `WorkingDirectory` / `EnvironmentFile` 改成 `/srv/mathagent/...`）。

前端是纯静态：`index.html` + `js/` + `styles/`，用 nginx 指到仓库根目录即可
（`deploy/nginx.conf.example` 可参考）。

---

## 5. 验收命令（在服务器上跑）

一条命令跑完全部可验证项，并打印可粘贴的汇总：

```bash
bash scripts/verify-on-server.sh            # 全量（含前端）
bash scripts/verify-on-server.sh --quick    # 只跑迁移 + Python 单测
```

必须看到的两条（★ 只有装了 OR-Tools 才会真跑）：

```bash
★ .venv/bin/python server/tests/test_assembly_solver.py      # 13 条 CP-SAT 断言
★ .venv/bin/python scripts/load_assembly.py --slots 50 --candidates 1000
```

期望：

- `test_assembly_solver.py`：`✓ CP-SAT 求解器测试全部通过（13 项）`
  （若打印 `⊘ 未验证：本机未安装 ortools`，说明第 1 节的安装没生效）
- `load_assembly.py`：输出里 `求解：optimal（ortools 9.11.4210，N ms）`，
  并且 JSON 摘要里出现 `solve` 计时

其余应全绿：迁移 35/35（断言 12/12）、阶段 A/B 的 18 个 Python 套件、
四个前端视图测试。

### 在真实库上再确认一次（可选，但建议）

```bash
.venv/bin/python db/migrate.py --status
psql "$DATABASE_URL" -c "select status, count(*) from assessment.assembly_run group by 1;"
psql "$DATABASE_URL" -c "select variant_group_id, variant_label, status
                           from assessment.paper_version
                          where variant_group_id is not null;"
```

---

## 6. 这一版在浏览器上应该能看到什么

1. 课程 → 组卷工作台：多出「约束组卷」入口。
2. 组卷页：选一份**约束组卷**蓝图 → 「开始求解」→ 状态徽章与"一句话结论"
   （最优 / 可行未证明最优 / 无解 / 超时 / 失败）→ 方案表 → 「用这个方案建卷」。
3. 手工蓝图会直接提示"不能跑求解"并给出「去建卷页」入口（服务端同样会拒）。
4. 运行历史里可把另一次已解出的运行「与本页运行配 A/B」，配不上会逐条列出重叠槽位。
5. 缺 OR-Tools 时页面显示安装命令，且**不会**出现任何"看起来可行"的方案。

---

## 7. 已知未验证 / 限制（不要当成已完成）

| 项 | 状态 |
| --- | --- |
| CP-SAT 硬约束、软目标、多方案 | **写好了但一次都没跑过**，到服务器上第一次跑 |
| A/B 的自动生成（一次解出两卷） | 未实现；目前是"分别求解 + 显式配对判定" |
| 不可行诊断的"逐组移除约束定位" | 未实现；目前只做可证的预检 |
| 真实浏览器 E2E | 未做（沙箱内无浏览器） |
| 阶段 C（在线作答）及之后 | 未开始 |

---

## 8. 反馈时请贴这些

- `git log --oneline -1` 的 commit
- `bash scripts/verify-on-server.sh` 的【汇总】整段
- 两条 ★ 命令的完整输出
- 页面截图（尤其组卷页的状态徽章与方案表）
