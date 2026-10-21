#!/usr/bin/env node
/**
 * 导航外壳的"样式契约"测试：吸顶、层级、课程中心回链。
 *
 * 为什么 CSS 也要测：课程条吸顶依赖三件事同时成立——顶栏 sticky、课程条 sticky、
 * 手机端 top 偏移量跟着顶栏高度（76 → 64）改。少任何一条，表现都是"滚动后被盖住或跳一下"，
 * 而这种问题在单元测试里不会报错，只有真人滚动才会发现。
 */
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');

const base = read('styles/base.css');
const views = read('styles/views.css');

const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};
const rule = (sheet, selector) => {
  const start = sheet.indexOf(selector + '{');
  if (start < 0) return '';
  return sheet.slice(start, sheet.indexOf('}', start));
};

console.log('顶栏与课程条的吸顶：');
const topbar = rule(base, '.topbar');
check('顶栏 sticky', topbar.includes('position:sticky'), topbar.slice(0, 60));
check('顶栏贴住顶部', topbar.includes('top:0'));
check('顶栏层级 30', topbar.includes('z-index:30'), topbar.includes('z-index:30') ? '30' : '缺失');

const bar = rule(views, '.course-bar');
check('课程条 sticky', bar.includes('position:sticky'));
check('课程条紧贴顶栏（top:76px）', bar.includes('top:76px'), bar.includes('top:76px') ? '76px' : '缺失');
check('课程条在顶栏之下（z-index 25）', bar.includes('z-index:25'));
check('课程条不透明背景（滚动时不被内容透出）', /background:#f7f9fd/.test(bar));

const mobile = views.slice(views.indexOf('@media (max-width:720px)'));
const mobileBlock = mobile.slice(0, mobile.indexOf('\n', mobile.lastIndexOf('course-bar-switch')));
check('手机端课程条 top 跟顶栏 64px 对齐', mobileBlock.includes('top:64px'), mobileBlock.slice(0, 120));
check('手机端单行不换行', mobileBlock.includes('flex-wrap:nowrap'));
check('手机端可横向滚动', mobileBlock.includes('overflow-x:auto'));
check('手机端隐藏规模文字给链接让位', mobileBlock.includes('.course-bar-main .muted.small{display:none}'));

console.log('\n课程条内容：');
const core = read('js/core.js');
const barFn = core.slice(core.indexOf('App.courseBar = function'), core.indexOf('App.url = function'));
check('有回课程中心的链接', barFn.includes('href="#/catalog"') && barFn.includes('课程中心'));
check('课程名可点回概览', barFn.includes("App.url('overview', course.id)"));
check('换课程回选课页并带当前功能', barFn.includes("App.url(page === 'pick' ? 'graph' : page, null)"));
check('本课页面链接带课程 id', barFn.includes("App.url(target, course.id)"));
check('规模来自课程统计', barFn.includes('stats.sections') && barFn.includes('stats.exercises'));
check('待接入课程有说明文案', barFn.includes('内容待接入'));

console.log('\n按课程记忆（换课再换回不重开）：');
check('有按课程的内存记忆容器', core.includes('App.courseMemory = function'));
check('记忆不写 localStorage（刷新回默认）',
  !barFn.includes('localStorage') && core.slice(core.indexOf('App.courseMemory'), core.indexOf('App.courseBar')).indexOf('localStorage') < 0);
const graph = read('js/graph-view.js');
check('图谱范围按课程恢复', graph.includes('memory.graphFocus'));
check('图谱显示方式按课程恢复', graph.includes('memory.graphDisplay'));
check('共用习题开关按课程恢复', graph.includes('memory.graphShowShared'));
check('展开层级时就地写记忆', graph.includes('A.courseMemory().graphFocus = focusId'));
const learning = read('js/learning.js');
check('习题筛选写进课程记忆', learning.includes('memory.exerciseQuery'));
check('习题页码写进课程记忆', learning.includes('memory.exercisePage'));
check('习题页渲染时回填筛选', learning.includes('value="${e(memory.exerciseQuery || \'\')}"'));

console.log('');
if (failures.length) {
  console.log(`失败 ${failures.length} 项：${failures.join(', ')}`);
  process.exit(1);
}
console.log('导航外壳样式契约测试全部通过。');
