// 用 PGlite（WASM PostgreSQL 16）真实执行 db/schema.sql 与 db/seed.sql。
// 目的：验证 DDL 语法、外键插入顺序、以及现有数据能否原样入库。
// 局限：PGlite 没有 pgvector，所以向量列降级为 jsonb、hnsw 索引跳过。
//       这里验证的是"结构与数据"，不是"向量检索"。

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { PGlite } from '@electric-sql/pglite';

const root = fileURLToPath(new URL('..', import.meta.url));
const schemaRaw = readFileSync(`${root}/db/schema.sql`, 'utf8');
const seedRaw = readFileSync(`${root}/db/seed.sql`, 'utf8');

const schema = schemaRaw
  .replace(/CREATE EXTENSION IF NOT EXISTS pgcrypto;/g, '')
  .replace(/CREATE EXTENSION IF NOT EXISTS vector;[^\n]*/g, '')
  .replace(/vector\(1024\)/g, 'jsonb')
  .replace(/CREATE INDEX textbook_chunk_embedding_idx[\s\S]*?WHERE embedding IS NOT NULL;/g, '');

if (/vector\(/.test(schema)) {
  console.error('仍有未降级的 vector 类型');
  process.exit(1);
}

const db = new PGlite();
console.log('执行 schema.sql …');
await db.exec(schema);
console.log('schema 执行成功');

console.log('执行 seed.sql（约 12 MB）…');
const t0 = Date.now();
await db.exec(seedRaw);
console.log(`seed 执行成功，耗时 ${((Date.now() - t0) / 1000).toFixed(1)}s`);

const checks = [
  ['course', 14],
  ['chapter', 173],
  ['knowledge_node', 4485],
  ['exercise', 4807],
  ['exercise_node', 5287],
  ['course_asset', 169],
];

let failed = 0;
for (const [table, expected] of checks) {
  const { rows } = await db.query(`SELECT count(*)::int AS n FROM ${table}`);
  const actual = rows[0].n;
  const ok = actual === expected;
  if (!ok) failed += 1;
  console.log(`  ${ok ? '✓' : '✗'} ${table.padEnd(16)} ${String(actual).padStart(5)} / 期望 ${expected}`);
}

// 抽查三件事：复合主键真的隔离了跨课程重名、视图能算、溯源列有值
const { rows: dup } = await db.query(
  "SELECT count(*)::int AS n FROM knowledge_node WHERE id = 'group-ch01'"
);
console.log(`  跨课程同名节点 group-ch01 独立行数：${dup[0].n}（应 >1，证明复合主键生效）`);

const { rows: view } = await db.query('SELECT count(*)::int AS n FROM v_contains');
console.log(`  v_contains 视图行数：${view[0].n}`);

const { rows: prov } = await db.query(
  `SELECT provenance, count(*)::int AS n FROM exercise GROUP BY provenance ORDER BY n DESC`
);
console.log('  题目来源分布：', prov.map((r) => `${r.provenance}=${r.n}`).join(', '));

const { rows: pages } = await db.query(
  'SELECT count(*)::int AS n FROM exercise WHERE source_page IS NOT NULL'
);
console.log(`  解析出精确书页的题目：${pages[0].n}`);

const { rows: status } = await db.query(
  `SELECT review_status, count(*)::int AS n FROM exercise GROUP BY review_status`
);
console.log('  审核状态分布：', status.map((r) => `${r.review_status}=${r.n}`).join(', '));

process.exit(failed === 0 ? 0 : 1);
