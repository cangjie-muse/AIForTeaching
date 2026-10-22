(function (root) {
  /**
   * 读、写、下载 .xlsx——账号、班级、名单三处共用。
   *
   * 为什么不用 CSV：中文 Windows 上 Excel 默认按 GBK 存 CSV，用 UTF-8 读就是一片
   * 乱码，而老师多数是用 Excel 存一遍再传上来。xlsx 不存在这个问题。
   *
   * 读的时候留了一条退路：有些导出工具会给 XML 标签加命名空间前缀，ExcelJS 直接
   * 读会报错。这条路先按原样读，失败再剥掉前缀重读一次——而不是让人家去改文件。
   */
  const MAX_BYTES = 10 * 1024 * 1024;
  const MAX_ROWS = 5000;
  const MAX_COLS = 30;

  function libraries() {
    const isNode = typeof module !== 'undefined' && module.exports;
    return {
      Excel: isNode ? require('exceljs') : root.ExcelJS,
      Zip: isNode ? require('jszip') : root.JSZip
    };
  }

  function cellText(value) {
    if (value === null || value === undefined) return '';
    if (value instanceof Date) return value.toISOString().slice(0, 10);
    if (typeof value === 'object') {
      if (value.text !== undefined && value.text !== null) return String(value.text);
      if (value.result !== undefined && value.result !== null) return String(value.result);
      if (Array.isArray(value.richText)) return value.richText.map(part => part.text || '').join('');
      return String(value);
    }
    return String(value);
  }

  async function normalizePrefixes(zip) {
    const parts = Object.keys(zip.files).filter(name => /^xl\/.*\.xml$/.test(name));
    if (parts.length > 200) throw new Error('工作簿包含过多 XML 部件，请只保留一个工作表。');
    for (const name of parts) {
      const xml = await zip.file(name).async('string');
      if (xml.length > 20 * 1024 * 1024) throw new Error('工作簿 XML 过大，请检查文件。');
      const prefixes = [...xml.matchAll(
        /xmlns:([\w]+)=["']http:\/\/schemas\.openxmlformats\.org\/spreadsheetml\/2006\/main["']/g)];
      let normalized = xml;
      prefixes.forEach(match => {
        normalized = normalized.replace(new RegExp(`(<\\/?)${match[1]}:`, 'g'), '$1');
      });
      if (normalized !== xml) zip.file(name, normalized);
    }
    return zip.generateAsync({ type: 'uint8array' });
  }

  async function loadWorkbook(bytes) {
    const { Excel, Zip } = libraries();
    if (!bytes || !bytes.byteLength) throw new Error('文件是空的');
    if (bytes.byteLength > MAX_BYTES) throw new Error('文件超过 10 MB，请拆成几个小表。');
    const workbook = new Excel.Workbook();
    try {
      await workbook.xlsx.load(bytes);
      return workbook;
    } catch (error) {
      const zip = await Zip.loadAsync(bytes);
      const repaired = await normalizePrefixes(zip);
      const retry = new Excel.Workbook();
      await retry.xlsx.load(repaired);
      return retry;
    }
  }

  /** 读成字符串二维表：第一行是表头，行号按 Excel 的真实行号（表头是第 1 行）。 */
  async function readGrid(bytes) {
    const workbook = await loadWorkbook(bytes);
    const sheet = workbook.worksheets[0];
    if (!sheet) throw new Error('这个工作簿里没有工作表');
    const rows = [];
    sheet.eachRow({ includeEmpty: false }, row => {
      const cells = [];
      const width = Math.min(Math.max(row.cellCount || 0, 1), MAX_COLS);
      for (let index = 1; index <= width; index += 1) {
        cells.push(cellText(row.getCell(index).value).replace(/\u3000/g, ' ').trim());
      }
      rows.push(cells);
    });
    if (rows.length > MAX_ROWS) throw new Error(`行数超过 ${MAX_ROWS}，请拆开导入。`);
    return { sheetName: sheet.name, rows };
  }

  function columnWidths(header, rows, widths) {
    return header.map((title, index) => {
      if (widths && widths[index]) return widths[index];
      const longest = rows.reduce((max, row) => {
        const text = String(row[index] || '');
        // 中文按两个字符宽算，不然中文列总是窄
        const width = text.replace(/[^\x00-\xff]/g, 'aa').length;
        return Math.max(max, width);
      }, title.replace(/[^\x00-\xff]/g, 'aa').length);
      return Math.min(Math.max(longest + 4, 12), 42);
    });
  }

  /** 生成一个表头加粗、首行冻结、带筛选按钮的工作表，返回字节。 */
  async function buildSheet(options) {
    const { Excel } = libraries();
    const header = options.header;
    const rows = options.rows || [];
    const workbook = new Excel.Workbook();
    workbook.creator = options.creator || '智慧武大数学课程平台';
    const sheet = workbook.addWorksheet((options.name || 'Sheet1').slice(0, 31));
    sheet.columns = columnWidths(header, rows, options.widths).map(width => ({ width }));
    const head = sheet.addRow(header);
    head.font = { bold: true, color: { argb: 'FFFFFFFF' } };
    head.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FF1F3864' } };
    head.alignment = { vertical: 'middle' };
    head.height = 22;
    rows.forEach(row => sheet.addRow(row));
    if (options.notes && options.notes.length) {
      const note = workbook.addWorksheet('填写说明');
      note.columns = [{ width: 96 }];
      options.notes.forEach(line => {
        const row = note.addRow([line]);
        row.alignment = { wrapText: true, vertical: 'top' };
      });
    }
    sheet.views = [{ state: 'frozen', ySplit: 1 }];
    sheet.autoFilter = { from: { row: 1, column: 1 }, to: { row: 1, column: header.length } };
    sheet.eachRow((row, number) => {
      if (number === 1) return;
      row.alignment = { vertical: 'top', wrapText: false };
    });
    const buffer = await workbook.xlsx.writeBuffer();
    return new Uint8Array(buffer);
  }

  /** 浏览器里把字节变成下载。 */
  function download(filename, bytes, mime) {
    const type = mime || 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
    const blob = new Blob([bytes], { type });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }

  async function downloadSheet(filename, options) {
    download(filename, await buildSheet(options));
  }

  const api = { readGrid, buildSheet, download, downloadSheet, cellText };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.AppXlsx = api;
})(typeof window === 'undefined' ? globalThis : window);
