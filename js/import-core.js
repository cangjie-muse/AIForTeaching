(function (root) {
  'use strict';
  const headers = ['1级节点', '2级节点', '3级节点', '4级节点', '5级节点', '6级节点', '知识说明', '知识点标签'];
  function assert(ok, message) { if (!ok) throw new Error(message); }
  function sections(markdown) {
    const matches = [...markdown.matchAll(/<a\s+id=["']([^"']+)["']\s*>\s*<\/a>\s*\n(#{1,6})\s+([^\n]+)\n/g)];
    const result = new Map();
    matches.forEach((match, index) => {
      assert(!result.has(match[1]), `Markdown 出现重复锚点：${match[1]}`);
      let body = markdown.slice(match.index + match[0].length, matches[index + 1]?.index ?? markdown.length);
      const nextHeading = new RegExp(`^#{1,${match[2].length}}\\s+`, 'm').exec(body);
      if (nextHeading) body = body.slice(0, nextHeading.index);
      result.set(match[1], { title: match[3].trim(), body: body.trim() });
    });
    assert(result.size > 0, 'Markdown 中未发现 <a id="…"></a> 知识点锚点。');
    return result;
  }
  function textValue(value) {
    if (value == null) return '';
    if (typeof value === 'object') {
      assert(!value.formula && !value.sharedFormula, '知识表须使用文本值，不能包含公式。');
      if (value.richText) return value.richText.map(item => item.text).join('');
      return String(value.text ?? value.result ?? '');
    }
    return String(value).trim();
  }
  function parseNodes(rows, knowledge, exerciseSections) {
    assert(headers.every((name, i) => textValue(rows[0]?.[i]) === name), `Excel 表头须为：${headers.join('、')}`);
    const nodes = [], paths = new Map(), ids = new Set(), repeatedPaths = new Set();
    rows.slice(1).forEach((raw, index) => {
      const row = raw.map(textValue);
      if (!row.some(Boolean)) return;
      const path = row.slice(0, 6).filter(Boolean), level = path.length;
      assert(path.length && row.slice(0, level).every(Boolean), `Excel 第 ${index + 2} 行的节点层级不连续。`);
      const refs = [...(row[7] || '').matchAll(/([^\s：:]+\.md)#([^\s]+)/g)];
      const kref = refs.find(item => item[1] === '知识点.md');
      const eref = refs.find(item => item[1] === '习题.md');
      assert(kref && eref, `Excel 第 ${index + 2} 行缺少知识内容或习题的 Markdown 引用。`);
      const id = kref[2];
      assert(!ids.has(id), `知识节点 ID 重复：${id}`);
      assert(knowledge.has(id), `知识点.md 缺少锚点：${id}`);
      assert(exerciseSections.has(eref[2]), `习题.md 缺少锚点：${eref[2]}`);
      const pathKey = JSON.stringify(path);
      if (paths.has(pathKey)) repeatedPaths.add(pathKey);
      else paths.set(pathKey, id);
      ids.add(id);
      const body = knowledge.get(id).body.replace(/^\*\*(?:图谱路径|教材范围|习题|知识内容)：\*\*.*\n?/gm, '').trim();
      nodes.push({ id, title: path.at(-1), path, level, source: row[6], exerciseAnchor: eref[2], content: body });
    });
    repeatedPaths.forEach(key => {
      const path = JSON.parse(key);
      assert(!nodes.some(node => node.level > path.length && path.every((part, index) => node.path[index] === part)), `同名父节点无法确定归属：${path.join(' / ')}`);
    });
    nodes.forEach(node => {
      node.parentId = node.level > 1 ? paths.get(JSON.stringify(node.path.slice(0, -1))) : null;
      assert(node.level === 1 || node.parentId, `缺少父节点：${node.path.join(' / ')}`);
      node.children = nodes.filter(child => child.level === node.level + 1 && child.path.slice(0, -1).join('\0') === node.path.join('\0')).map(child => child.id);
      node.kind = node.children.length ? 'container' : 'knowledge';
      node.chapterId = node.level >= 2 ? paths.get(JSON.stringify(node.path.slice(0, 2))) : null;
      node.exerciseIds = [];
    });
    assert(nodes.filter(node => node.level === 1).length === 1, '每个资源包须且只能包含一个一级课程节点。');
    return nodes;
  }
  function parseExercise(raw, id) {
    const body = raw.trim().replace(/^\d+\.\s*/, '').replace(/^ {3}/gm, '');
    const answer = /\*\*答案：\*\*/.exec(body);
    const meta = /\*\*难度：(\d)\/5；出处：\*\*/.exec(body);
    const head = (body.split('\n').find(line => line.trim()) || '').trim().slice(0, 40);
    const missing = [];
    if (!answer) missing.push('缺少「**答案：**」这一行');
    if (!meta) missing.push('缺少「**难度：n/5；出处：**」这一行（全角「：」「；」，冒号后面用「；」接到「出处：」）');
    assert(!missing.length, `习题 ${id} ${missing.join('，')}。这道题开头是「${head}」。`
      + '三段顺序必须是：题干 → **答案：**… → **难度：3/5；出处：**…');
    assert(meta.index > answer.index, `习题 ${id} 的「**难度：n/5；出处：**」写在了「**答案：**」前面，两者需要对调。`
      + `这道题开头是「${head}」。`);
    const difficulty = Number(meta[1]);
    assert(difficulty >= 1 && difficulty <= 5, `习题 ${id} 难度须在 1–5 之间。`);
    return {
      id, prompt: body.slice(0, answer.index).trim(),
      answer: body.slice(answer.index + answer[0].length, meta.index).trim(),
      difficulty, source: body.slice(meta.index + meta[0].length).trim(), nodeIds: []
    };
  }
  function sameExercise(a, b) {
    return ['prompt', 'answer', 'source', 'difficulty'].every(key => a[key] === b[key]);
  }
  /* 行首的题号才算一道新题。答案里缩进的 "   1. " 是解题步骤，
     用 ^\d+\. 匹配会把它也当成题，一道题被拆成三道。 */
  const QUESTION_START = /^(\d+)\.\s/gm;

  /* 内容哈希当稳定 ID。为什么要哈希而不是序号：老师重跑一次生成、
     题序变了但题没变，序号会全错位，哈希不会。 */
  function stableQuestionId(text) {
    let first = 2166136261, second = 2246822519;
    for (const character of text.normalize('NFKC')) {
      const code = character.codePointAt(0);
      first = Math.imul(first ^ code, 16777619) >>> 0;
      second = Math.imul(second ^ code, 3266489917) >>> 0;
    }
    return `q-${first.toString(16).padStart(8, '0')}${second.toString(16).padStart(8, '0')}`;
  }

  function splitQuestions(section) {
    const starts = [...section.matchAll(QUESTION_START)];
    return starts.map((match, index) => section.slice(match.index, starts[index + 1]?.index ?? section.length));
  }

  function parseExercises(nodes, source) {
    const unique = new Map();
    const synthesizedIds = new Set();
    let associationCount = 0;

    // 逐题收集问题再一起抛：一个资源包常有几道题格式不对，
    // 一次只报第一道的话，老师得改一轮导一轮，来回好几趟。
    const problems = [];
    const guard = (node, id, raw) => {
      try { ingest(node, id, raw); } catch (error) { problems.push(error.message); }
    };

    const ingest = (node, id, raw) => {
      const parsed = parseExercise(raw, id), old = unique.get(id);
      if (!old) unique.set(id, { ...parsed, nodeIds: [], _variants: [{ ...parsed, nodeIds: [] }] });
      const target = unique.get(id);
      let variant = target._variants.find(item => sameExercise(item, parsed));
      if (!variant) { variant = { ...parsed, nodeIds: [] }; target._variants.push(variant); }
      if (!variant.nodeIds.includes(node.id)) variant.nodeIds.push(node.id);
      if (!target.nodeIds.includes(node.id)) { target.nodeIds.push(node.id); associationCount += 1; }
      if (!node.exerciseIds.includes(id)) node.exerciseIds.push(id);
    };

    nodes.forEach(node => {
      const section = source.get(node.exerciseAnchor).body;
      const marks = [...section.matchAll(/<!--\s*source-exercise:([^\s]+)\s*-->/g)];
      if (marks.length) {
        marks.forEach((mark, index) => {
          const raw = section.slice(mark.index + mark[0].length, marks[index + 1]?.index ?? section.length);
          guard(node, mark[1], raw);
        });
        return;
      }

      // 没有 source-exercise 标记的老资源包：按题号拆开，用内容哈希补 ID。
      // 不能因为少一行注释就让整包导不进来——二十几本教材会全部卡在这一句上。
      // 但补出来的 ID 以 q- 开头且计数上报，老师能看出哪些不是原始题号。
      const blocks = splitQuestions(section);
      assert(blocks.length || !/\*\*答案：\*\*/.test(section),
        `知识点 ${node.id} 的题目既没有 source-exercise 标记，也找不到题号，无法拆分。`);
      blocks.forEach(raw => {
        const id = stableQuestionId(raw);
        synthesizedIds.add(id);
        guard(node, id, raw);
      });
    });

    if (problems.length) {
      const detail = problems.length === 1
        ? problems[0]
        : `有 ${problems.length} 道习题格式不对：\n· ${problems.join('\n· ')}`;
      assert(false, detail);
    }

    let variantGroups = 0, variantCopies = 0;
    const exercises = [...unique.values()].map(exercise => {
      const variants = exercise._variants; delete exercise._variants;
      if (variants.length > 1) { exercise.variants = variants; variantGroups += 1; variantCopies += variants.length - 1; }
      return exercise;
    });
    return { exercises, associationCount, variantGroups, variantCopies, synthesized: synthesizedIds.size };
  }

  function courseId(rootNode, metadata) {
    if (metadata.id) {
      assert(/^[a-z][a-z0-9_-]*$/.test(metadata.id), 'course.json 的课程 ID 只能使用小写英文字母、数字、连字符或下划线。');
      return metadata.id;
    }
    if (rootNode.id !== 'course-overview') return rootNode.id;
    const catalog = typeof module !== 'undefined' && module.exports ? require('./course-catalog.js') : root.CourseCatalog;
    const title = catalog.normalize(rootNode.title);
    const planned = catalog.entries.find(entry => [entry.title, ...entry.aliases].some(name => catalog.normalize(name) === title));
    if (planned) return planned.id;
    let first = 2166136261, second = 2246822519;
    for (const character of rootNode.title.normalize('NFKC')) {
      const code = character.codePointAt(0);
      first = Math.imul(first ^ code, 16777619) >>> 0;
      second = Math.imul(second ^ code, 3266489917) >>> 0;
    }
    return `course-${first.toString(16).padStart(8, '0')}${second.toString(16).padStart(8, '0')}`;
  }
  function referencedImages(...texts) {
    const paths = new Set();
    texts.forEach(text => {
      const raw = [...String(text || '').matchAll(/!\[[^\]]*\]\(([^)]+)\)/g)].map(match => match[1]);
      raw.push(...[...String(text || '').matchAll(/<img\b[^>]*src=["']([^"']+)["']/gi)].map(match => match[1]));
      raw.forEach(value => {
        let filename;
        try { filename = decodeURIComponent(value.split('#')[0]).replace(/^\.\//, ''); }
        catch { throw new Error(`插图路径无法解码：${value}`); }
        assert(filename && !/^(?:[a-z]+:|\/|\\)/i.test(filename) && !filename.split('/').includes('..'), `插图须使用资源包内的相对路径：${value}`);
        paths.add(filename);
      });
    });
    return [...paths].sort();
  }
  function parsePackage(rows, knowledgeText, exerciseText, metadata = {}) {
    const knowledge = sections(knowledgeText), exerciseSections = sections(exerciseText);
    const nodes = parseNodes(rows, knowledge, exerciseSections);
    const { exercises, associationCount, variantGroups, variantCopies, synthesized } =
      parseExercises(nodes, exerciseSections);
    const rootNode = nodes.find(node => node.level === 1);
    const leaves = nodes.filter(node => node.kind === 'knowledge');
    const warnings = [];
    const noExercises = leaves.filter(node => !node.exerciseIds.length).length;
    if (noExercises) warnings.push(`${noExercises} 个知识点未关联习题，已保留空状态。`);
    if (synthesized) warnings.push(`${synthesized} 道题没有 source-exercise 标记，已按题目内容补了稳定 ID：同一份文件重复导入仍是同一批题，但改过内容的题会被当成新题。`);
    if (variantGroups) warnings.push(`${variantGroups} 道源题在不同位置有 ${variantCopies} 份其他表述或解答，均已保留；请教师后续核对。`);
    const repeatedNames = nodes.length - new Set(nodes.map(node => JSON.stringify(node.path))).size;
    if (repeatedNames) warnings.push(`${repeatedNames} 个同名不同来源知识点已按各自锚点保留。`);
    warnings.push('当前文件提供章节隶属关系；尚未提供先修、推导等跨知识点关系。');
    return {
      schemaVersion: '1.0', id: courseId(rootNode, metadata), title: rootNode.title,
      audience: metadata.audience || '本科高年级', status: 'ready',
      description: metadata.description || '按章节组织知识内容与习题，连接概念、方法和应用。',
      subject: metadata.subject || '专业课程', textbook: metadata.textbook || '',
      importedAt: new Date().toISOString(), resourceBase: '',
      nodes, exercises, relationships: nodes.filter(node => node.parentId).map(node => ({ source: node.parentId, target: node.id, type: 'contains' })),
      stats: { nodes: nodes.length, knowledge: leaves.length, sections: nodes.filter(node => node.level === 2).length, exercises: exercises.length, associations: associationCount, variantGroups, synthesized },
      warnings, files: metadata.files || [], reviewStatus: '待教师审核'
    };
  }
  const api = { parsePackage, sections, headers, textValue, referencedImages };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.CourseImport = api;
})(typeof window === 'undefined' ? globalThis : window);
