(function (root) {
  'use strict';
  /* 课程中心按学科方向组织，不按建设批次。
   *
   * 一期/二期是项目管理的口径：学生找课时它不提供任何信息，而且"二期重点课程"
   * 这种说法会让一门已经上线的课看起来像半成品。主流慕课平台与 LMS 的做法也是
   * 按学科门类分组（并可选按学期），批次不出现在目录里。
   *
   * 方向定在课程上，不靠顺序推断：原来按数组下标前 10 门归一组，加一门课就会
   * 把分组整体挤错位。
   */
  const groups = [
    { id: 'analysis', label: '分析', number: '01', theme: 'analysis',
      description: '极限、连续、微分与积分，以及测度与泛函' },
    { id: 'algebra', label: '代数与数论', number: '02', theme: 'algebra',
      description: '线性结构、群环域与数论' },
    { id: 'geometry', label: '几何与拓扑', number: '03', theme: 'geometry',
      description: '空间、流形与不变量' },
    { id: 'pde', label: '微分方程', number: '04', theme: 'pde',
      description: '常微分方程、偏微分方程与数学建模' },
    { id: 'probability', label: '概率统计', number: '05', theme: 'probability',
      description: '随机现象、统计推断与金融应用' },
    { id: 'computing', label: '计算与应用', number: '06', theme: 'algebra',
      description: '数值方法、机器学习与运筹' }
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
    ['harmonic-analysis', '调和分析', 'analysis'],
    ['course-symplectic-math-physics', '辛几何与数学物理', 'geometry'],
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
    ['operations-research', '运筹学', 'computing'],
    ['stochastic-processes', '随机过程', 'probability'],
    ['mathematical-statistics', '数理统计', 'probability'],
    ['risk-management', '风险管理', 'probability'],
    ['stochastic-analysis', '随机分析', 'probability'],
    ['high-dimensional-probability', '高维概率', 'probability'],
    ['numerical-linear-algebra', '数值线代', 'computing', ['数值线性代数']],
    ['numerical-analysis', '数值分析', 'computing'],
    ['machine-learning', '机器学习', 'computing'],
    ['neural-networks', '神经网络', 'computing', ['神经网络与深度学习']]
  ];
  const byId = new Map(groups.map(group => [group.id, group]));
  const normalize = title => String(title || '').normalize('NFKC').replace(/[\s()（）]/g, '');
  const entries = rows.map(([id, title, groupId, aliases = []], index) => {
    const group = byId.get(groupId) || groups[0];
    return {
      id, catalogId: id, title, aliases, groupId: group.id,
      subject: group.label, categoryLabel: group.label, theme: group.theme,
      direction: group.label, order: index + 1, catalogPlanned: true,
      audience: '本科课程', status: 'planned',
      description: '课程框架已建立，待接入知识图谱、知识内容与题库。'
    };
  });
  /* 库里记了方向就用库里的：课程中心的分组不该只存在于这张静态表里，
     否则老师给一门课归位，还得改代码再部署。不认识的 id 一律当待归类，
     不让课因为一个拼错的值凭空消失。 */
  const groupOf = direction => (direction ? byId.get(direction) : null);
  function merge(imported) {
    const used = new Set();
    const planned = entries.map(entry => {
      const titles = [entry.title, ...entry.aliases].map(normalize);
      const course = imported.find(item => item.id === entry.id || titles.includes(normalize(item.title)));
      if (!course) return { ...entry };
      used.add(course.id);
      const group = groupOf(course.direction) || byId.get(entry.groupId) || groups[0];
      return { ...entry, ...course, catalogId: entry.id, catalogPlanned: true, order: entry.order,
        groupId: group.id, directionId: group.id, subject: group.label,
        categoryLabel: group.label, direction: group.label, theme: group.theme };
    });
    const additional = imported.filter(course => !used.has(course.id)).map((course, index) => {
      const group = groupOf(course.direction);
      return {
        ...course, catalogPlanned: false,
        groupId: group ? group.id : 'imported',
        directionId: group ? group.id : '',
        categoryLabel: group ? group.label : '新导入课程',
        direction: group ? group.label : '待归类',
        order: entries.length + index + 1
      };
    });
    return [...planned, ...additional];
  }
  /* 分类清单以数据库为准（/api/v1/directions）。后端连不上、或迁移没跑时，
     上面这六个就是兜底——离线包里本来也只有这六组。
     原地替换而不是重新赋值：外部拿到的是 groups 这个数组本身的引用。 */
  const setGroups = list => {
    if (!Array.isArray(list) || !list.length) return;
    groups.splice(0, groups.length, ...list.map((group, index) => ({
      id: String(group.id),
      label: String(group.label),
      number: String(index + 1).padStart(2, '0'),
      theme: group.theme || 'analysis',
      description: group.description || ''
    })));
    byId.clear();
    groups.forEach(group => byId.set(group.id, group));
  };
  const api = { groups, entries, normalize, merge, setGroups };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.CourseCatalog = api;
})(typeof window === 'undefined' ? globalThis : window);
