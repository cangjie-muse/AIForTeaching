(function (root) {
  async function readRows(bytes) {
    const excel = typeof module !== 'undefined' ? require('exceljs') : root.ExcelJS;
    const zipLibrary = typeof module !== 'undefined' ? require('jszip') : root.JSZip;
    if (bytes.byteLength > 10 * 1024 * 1024) throw new Error('知识节点 Excel 超过 10 MB，请拆分为单门课程资源包。');
    const zip = await zipLibrary.loadAsync(bytes);
    const parts = Object.keys(zip.files).filter(name => /^xl\/.*\.xml$/.test(name));
    if (parts.length > 200) throw new Error('工作簿包含过多 XML 部件，请使用单工作表模板。');
    for (const name of parts) await normalizePart(zip, name);
    const workbook = new excel.Workbook();
    await workbook.xlsx.load(await zip.generateAsync({ type: 'uint8array' }));
    if (workbook.worksheets.length !== 1) throw new Error('知识节点.xlsx 须只有一个课程工作表。');
    const rows = [];
    workbook.worksheets[0].eachRow(row => {
      if (row.number > 20000) throw new Error('节点行数超过 20,000，请拆分课程。');
      rows.push(Array.from({ length: 8 }, (_, i) => row.getCell(i + 1).value));
    });
    return rows;
  }
  async function normalizePart(zip, name) {
    const xml = await zip.file(name).async('string');
    if (xml.length > 20 * 1024 * 1024) throw new Error('工作簿 XML 过大，请检查文件。');
    const prefixes = [...xml.matchAll(/xmlns:([\w]+)=["']http:\/\/schemas\.openxmlformats\.org\/spreadsheetml\/2006\/main["']/g)];
    let normalized = xml;
    prefixes.forEach(match => { normalized = normalized.replace(new RegExp(`(<\\/?)${match[1]}:`, 'g'), '$1'); });
    if (normalized !== xml) zip.file(name, normalized);
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = { readRows };
  else root.CourseXlsx = { readRows };
})(typeof window === 'undefined' ? globalThis : window);
