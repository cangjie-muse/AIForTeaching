#!/usr/bin/env node
/**
 * 跑 Python 侧不依赖数据库的单元测试。
 *
 * 为什么需要这层壳：`npm run check` 要能在 Windows 和 Linux 上都跑通，
 * 而虚拟环境里的解释器路径两边不同（.venv/Scripts/python.exe 与 .venv/bin/python）。
 * 在这里解析一次路径，脚本里就不必到处写平台判断。
 */
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const python = process.platform === 'win32'
  ? path.join(root, '.venv', 'Scripts', 'python.exe')
  : path.join(root, '.venv', 'bin', 'python');

if (!fs.existsSync(python)) {
  console.error(`找不到虚拟环境解释器：${python}\n先运行：python -m venv .venv && pip install -r server/requirements.txt`);
  process.exit(1);
}

const suites = ['server/tests/test_policy.py', 'server/tests/test_source_parse.py', 'server/tests/test_generation.py', 'server/tests/test_chapters.py', 'server/tests/test_content.py', 'server/tests/test_catalog.py', 'server/tests/test_visibility.py', 'server/tests/test_lean_service.py', 'server/tests/test_auth.py', 'server/tests/test_exercise_tags.py', 'server/tests/test_admin_bootstrap.py', 'server/tests/test_cookie_secure.py', 'server/tests/test_reset_password.py', 'server/tests/test_publish_courses.py', 'server/tests/test_script_env.py', 'server/tests/test_llm_endpoint_resolution.py', 'server/tests/test_qa_fallback.py', 'server/tests/test_settings.py', 'server/tests/test_repo_hosting.py', 'server/tests/test_approved_hosts.py',
  'server/tests/test_pack_merge.py',
  'server/tests/test_import_course.py'];
let failed = 0;

for (const suite of suites) {
  const result = spawnSync(python, [suite], {
    cwd: root,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
    stdio: 'inherit'
  });
  if (result.status !== 0) {
    failed += 1;
    console.error(`\n${suite} 失败（退出码 ${result.status}）`);
  }
}

if (failed) {
  console.error(`\n${failed} 个 Python 测试套件未通过`);
  process.exit(1);
}
console.log('\nPython 单元测试全部通过。');
