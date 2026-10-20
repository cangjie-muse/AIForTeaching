export default [{
  files: ['js/**/*.js', 'scripts/*.mjs', 'scripts/*.cjs'],
  languageOptions: {
    ecmaVersion: 'latest', sourceType: 'script',
    globals: Object.fromEntries(['window', 'document', 'console', 'localStorage', 'location', 'URL', 'URLSearchParams', 'Blob', 'setTimeout', 'clearTimeout', 'indexedDB', 'fetch', 'App', 'CourseCatalog', 'ConstructionModel', 'GraphModel', 'GraphAtlas', 'DOMPoint', 'CourseImport', 'CourseXlsx', 'CourseStore', 'katex', 'marked', 'DOMPurify', 'module', 'require', 'process', '__dirname', 'globalThis'].map(name => [name, 'readonly']))
  },
  rules: {
    'no-unreachable': 'error', 'no-dupe-args': 'error', 'no-dupe-keys': 'error',
    'no-undef': 'error',
    'no-constant-condition': 'error', 'valid-typeof': 'error',
    'complexity': ['error', 25], 'max-depth': ['error', 3],
    'max-lines': ['error', { max: 800, skipBlankLines: true }],
    'max-lines-per-function': ['error', { max: 120, skipBlankLines: true, IIFEs: false }]
  }
}, { files: ['scripts/*.mjs'], languageOptions: { sourceType: 'module' } }];
