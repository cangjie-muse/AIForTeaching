(function (root) {
  'use strict';
  const groups = [
    { id: 'phase-1', label: '基础课程（一期核心课程）', shortLabel: '基础课程', stage: '一期核心课程', number: '01' },
    { id: 'phase-2', label: '专业课程（二期重点课程）', shortLabel: '专业课程', stage: '二期重点课程', number: '02' }
  ];
  const rows = [
    ['algebra', '高等代数', 'algebra'],
    ['analysis', '数学分析I', 'analysis'],
    ['analysis-ii', '数学分析II', 'analysis'],
    ['analysis-iii', '数学分析III', 'analysis'],
    ['abstract-algebra', '抽象代数', 'algebra'],
    ['analytic-geometry', '解析几何', 'geometry'],
    ['point-set-topology', '点集拓扑', 'geometry'],
    ['ordinary-differential-equations', '常微分方程', 'pde'],
    ['complex-analysis', '复变函数', 'analysis'],
    ['probability', '概率论', 'probability'],
    ['real-analysis', '实变函数', 'analysis'],
    ['functional-analysis', '泛函分析', 'analysis'],
    ['pde', '偏微分方程', 'pde'],
    ['harmonic-analysis', '调和分析', 'pde'],
    ['course-symplectic-math-physics', '辛几何与数学物理', 'symplectic'],
    ['homology', '同调论', 'geometry'],
    ['geometry', '微分几何', 'geometry'],
    ['riemannian-geometry', '黎曼几何', 'geometry'],
    ['differentiable-manifolds', '微分流形', 'geometry'],
    ['commutative-algebra', '交换代数', 'algebra'],
    ['number-theory', '数论基础', 'algebra'],
    ['algebraic-geometry', '代数几何', 'geometry'],
    ['homological-algebra', '同调代数', 'algebra'],
    ['groups-representations', '群与表示', 'algebra'],
    ['mathematical-modeling', '数学模型', 'pde'],
    ['financial-mathematics', '金融数学', 'probability'],
    ['operations-research', '运筹学', 'algebra'],
    ['stochastic-processes', '随机过程', 'probability'],
    ['mathematical-statistics', '数理统计', 'probability'],
    ['risk-management', '风险管理', 'probability'],
    ['stochastic-analysis', '随机分析', 'probability'],
    ['high-dimensional-probability', '高维概率', 'probability'],
    ['numerical-linear-algebra', '数值线代', 'algebra', ['数值线性代数']],
    ['numerical-analysis', '数值分析', 'analysis'],
    ['machine-learning', '机器学习', 'algebra'],
    ['neural-networks', '神经网络', 'pde', ['神经网络与深度学习']]
  ];
  const normalize = title => String(title || '').normalize('NFKC').replace(/[\s()（）]/g, '');
  const entries = rows.map(([id, title, theme, aliases = []], index) => {
    const group = groups[index < 10 ? 0 : 1];
    return { id, catalogId: id, title, aliases, theme, groupId: group.id, subject: group.label, categoryLabel: group.shortLabel, stage: group.stage, order: index + 1, catalogPlanned: true, audience: '本科课程', status: 'planned', description: '课程框架已建立，待接入知识图谱、知识内容与题库。' };
  });
  function merge(imported) {
    const used = new Set();
    const planned = entries.map(entry => {
      const titles = [entry.title, ...entry.aliases].map(normalize);
      const course = imported.find(item => item.id === entry.id || titles.includes(normalize(item.title)));
      if (!course) return { ...entry };
      used.add(course.id);
      return { ...entry, ...course, catalogId: entry.id, catalogPlanned: true, order: entry.order, groupId: entry.groupId, subject: entry.subject, categoryLabel: entry.categoryLabel, stage: entry.stage, theme: entry.theme };
    });
    const additional = imported.filter(course => !used.has(course.id)).map((course, index) => ({ ...course, catalogPlanned: false, groupId: 'imported', categoryLabel: '新导入课程', stage: '待归类', order: entries.length + index + 1 }));
    return [...planned, ...additional];
  }
  const api = { groups, entries, normalize, merge };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.CourseCatalog = api;
})(typeof window === 'undefined' ? globalThis : window);
