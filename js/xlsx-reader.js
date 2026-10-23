(function (root) {
  const HEADER_CELLS = 8;
  const MAX_ROWS = 20000;
  const MAX_BYTES = 10 * 1024 * 1024;

  function libraries() {
    const excel = typeof module !== 'undefined' ? require('exceljs') : root.ExcelJS;
    const zipLibrary = typeof module !== 'undefined' ? require('jszip') : root.JSZip;
    return { excel, zipLibrary };
  }

  async function readRows(bytes) {
    const { excel, zipLibrary } = libraries();
    if (bytes.byteLength > MAX_BYTES) {
      throw new Error('知识节点 Excel 超过 10 MB，请拆分为单门课程资源包。');
    }
    const zip = await zipLibrary.loadAsync(bytes);
    const parts = Object.keys(zip.files).filter(name => /^xl\/.*\.xml$/.test(name));
    if (parts.length > 200) {
      throw new Error('工作簿包含过多 XML 部件，请使用单工作表模板。');
    }
    for (const name of parts) await normalizePart(zip, name);
    const workbook = new excel.Workbook();
    await workbook.xlsx.load(await zip.generateAsync({ type: 'uint8array' }));
    if (workbook.worksheets.length !== 1) {
      throw new Error('知识节点.xlsx 须只有一个课程工作表。');
    }
    const rows = [];
    workbook.worksheets[0].eachRow(row => {
      if (row.number > MAX_ROWS) throw new Error('节点行数超过 20,000，请拆分课程。');
      rows.push(Array.from({ length: HEADER_CELLS }, (_, i) => row.getCell(i + 1).value));
    });
    return rows;
  }

  /* 直接读 OOXML 的兜底解析。
   *
   * 为什么需要它：exceljs 对"不完全标准"的工作簿很挑——WPS 另存的文件、
   * 程序生成的 xlsx 都可能让它抛一句 Cannot read properties of undefined，
   * 而文件本身用 Excel 打开完全正常。这里只认单元格文本，够导入器用，
   * 也不依赖任何库的内部结构。 */
  async function readRowsRaw(bytes) {
    const { zipLibrary } = libraries();
    if (bytes.byteLength > MAX_BYTES) {
      throw new Error('知识节点 Excel 超过 10 MB，请拆分为单门课程资源包。');
    }
    const zip = await zipLibrary.loadAsync(bytes);
    const shared = await sharedStrings(zip);
    const sheetPath = await firstSheetPath(zip);
    const xml = stripPrefixes(await zip.file(sheetPath).async('string'));
    const rows = [];
    for (const rowMatch of xml.matchAll(/<row\b[^>]*>([\s\S]*?)<\/row>/g)) {
      if (rows.length >= MAX_ROWS) throw new Error('节点行数超过 20,000，请拆分课程。');
      const cells = new Array(HEADER_CELLS).fill('');
      for (const cellMatch of rowMatch[1].matchAll(/<c\b([^>]*?)(?:\/>|>([\s\S]*?)<\/c>)/g)) {
        const attrs = cellMatch[1] || '';
        const inner = cellMatch[2] || '';
        const ref = /\br="([A-Z]+)\d+"/.exec(attrs);
        if (!ref) continue;
        const index = columnIndex(ref[1]);
        if (index >= HEADER_CELLS) continue;
        cells[index] = cellText(attrs, inner, shared);
      }
      rows.push(cells);
    }
    if (!rows.length) throw new Error('工作表里没有读到任何行。');
    return rows;
  }

  /* 有些生成器（以及这个仓库自己的资源包）会给元素加命名空间前缀：
     <x:row>、<x:c>。exceljs 那条路上是 normalizePart 把前缀剥掉的，
     兜底路径直接读原文件，所以这里也要剥一次——否则一行都读不到。 */
  function stripPrefixes(xml) {
    return xml.replace(/<(\/?)([\w.-]+):/g, '<$1');
  }

  function columnIndex(letters) {
    let index = 0;
    for (const character of letters) index = index * 26 + (character.charCodeAt(0) - 64);
    return index - 1;
  }

  function cellText(attrs, inner, shared) {
    const type = /\bt="([^"]+)"/.exec(attrs)?.[1] || 'n';
    if (type === 's') {
      const value = /<v>\s*(\d+)\s*<\/v>/.exec(inner);
      return value ? (shared[Number(value[1])] ?? '') : '';
    }
    if (type === 'inlineStr') return [...inner.matchAll(/<t\b[^>]*>([\s\S]*?)<\/t>/g)]
      .map(match => match[1]).join('');
    const value = /<v\b[^>]*>([\s\S]*?)<\/v>/.exec(inner);
    if (!value) return '';
    return decodeEntities(value[1]);
  }

  async function sharedStrings(zip) {
    const file = zip.file('xl/sharedStrings.xml');
    if (!file) return [];
    const xml = stripPrefixes(await file.async('string'));
    return [...xml.matchAll(/<si\b[^>]*>([\s\S]*?)<\/si>/g)]
      .map(match => [...match[1].matchAll(/<t\b[^>]*>([\s\S]*?)<\/t>/g)]
        .map(item => decodeEntities(item[1])).join(''));
  }

  /* 第一张工作表的真实路径：workbook.xml + rels 决定，不能假定是 sheet1.xml */
  async function firstSheetPath(zip) {
    const declared = await declaredSheetPath(zip);
    if (declared) return declared;
    const guess = Object.keys(zip.files)
      .filter(name => /^xl\/worksheets\/sheet\d+\.xml$/.test(name)).sort()[0];
    if (!guess) throw new Error('工作簿里没有工作表。');
    return guess;
  }

  async function declaredSheetPath(zip) {
    const workbookFile = zip.file('xl/workbook.xml');
    const relsFile = zip.file('xl/_rels/workbook.xml.rels');
    if (!workbookFile || !relsFile) return null;
    const workbook = stripPrefixes(await workbookFile.async('string'));
    const sheet = /<sheet\b[^>]*\br:id="([^"]+)"[^>]*\/?>/.exec(workbook);
    if (!sheet) return null;
    const rels = stripPrefixes(await relsFile.async('string'));
    const rel = new RegExp(`<Relationship\\b[^>]*\\bId="${sheet[1]}"[^>]*/?>`, 'i').exec(rels);
    const target = rel && /\bTarget="([^"]+)"/.exec(rel[0]);
    if (!target) return null;
    const path = target[1].replace(/^\/?xl\//, '').replace(/^\.\//, '');
    return `xl/${path}`;
  }

  function decodeEntities(text) {
    return text
      .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"')
      .replace(/&apos;/g, "'").replace(/&#(\d+);/g, (_, code) => String.fromCodePoint(Number(code)))
      .replace(/&#x([0-9a-f]+);/gi, (_, code) => String.fromCodePoint(parseInt(code, 16)))
      .replace(/&amp;/g, '&');
  }

  async function normalizePart(zip, name) {
    const xml = await zip.file(name).async('string');
    if (xml.length > 20 * 1024 * 1024) throw new Error('工作簿 XML 过大，请检查文件。');
    const prefixes = [...xml.matchAll(/xmlns:([\w]+)=["']http:\/\/schemas\.openxmlformats\.org\/spreadsheetml\/2006\/main["']/g)];
    let normalized = xml;
    prefixes.forEach(match => { normalized = normalized.replace(new RegExp(`(<\\/?)${match[1]}:`, 'g'), '$1'); });
    if (normalized !== xml) zip.file(name, normalized);
  }

  /* 先用 exceljs（保留数字、公式值等类型）；它抛错就退回直接读 XML。
     两者都失败时抛原始错误——那句话至少指向真正的原因。 */
  async function readRowsWithFallback(bytes) {
    try {
      return await readRows(bytes);
    } catch (error) {
      // 我们自己抛的校验（中文信息）要原样往上抛：多工作表、行数超限这些是
      // 有意拒绝，退回兜底就变成"悄悄只读第一张表"了。
      if (/[\u4e00-\u9fa5]/.test(error.message || '')) throw error;
      try {
        return await readRowsRaw(bytes);
      } catch {
        throw error;
      }
    }
  }

  const api = { readRows: readRowsWithFallback, readRowsStrict: readRows, readRowsRaw };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.CourseXlsx = api;
})(typeof window === 'undefined' ? globalThis : window);
