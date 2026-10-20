# 后端 API

FastAPI + psycopg3 + PostgreSQL(pgvector)。不引入 ORM：表结构手写 SQL，查询也手写 SQL。

## 本地启动

```bash
# 1. 起数据库（根目录）
docker compose up -d db

# 2. 建表 + 灌数据（迁移制，非破坏性，可重复执行）
DATABASE_URL="postgresql://whu:whu_dev_password@127.0.0.1:5432/whu_teaching" python db/migrate.py --apply
python db/build_seed.py
psql "postgresql://whu:whu_dev_password@127.0.0.1:5432/whu_teaching" -f db/seed.sql

# 3. 起 API
cd server
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

打开 http://127.0.0.1:8000/docs 可以看到自动生成的接口文档。

## 已实现端点（最小骨架）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/health` | 探活 + 数据库连通性 |
| GET | `/api/v1/courses` | 课程列表（含知识点/题目数） |
| GET | `/api/v1/courses/{course_id}/graph` | 节点 + 层级边 + 共题边 |

`/graph` 的结构刻意对齐前端 `js/graph-model.js` 的预期，便于把 `window.COURSE_DATA` 换成 fetch 时不动渲染代码。

## 后续端点

按 `config/api-contract.json` 逐个实现，顺序建议：

1. `/courses/{id}/knowledge`、`/courses/{id}/exercises`（只读，替换现有全量 JSON）
2. `/ai/chat`（RAG 问答，带 citations）
3. `/classes/*`、`/assignments/*`、`submissions`（批改）

## 约定

- **所有 ID 都带 `course_id` 作用域**，路由里显式出现，不做全局 ID 猜测。
- **模型密钥只在服务端**。前端不持有任何 provider 凭据。
- **未经教师审核的内容不得标记为 published**。AI 产出走 `draft` → `machine-checked` → `teacher-reviewed`。
- 每次模型调用写 `model_call`，包含 tokens 与成本，否则无法回答"这个东西一个月花多少钱"。
