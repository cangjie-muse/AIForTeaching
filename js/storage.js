'use strict';
const CourseStore = window.CourseStore = {};
CourseStore.open = () => new Promise((resolve, reject) => {
  if (!window.indexedDB) { reject(new Error('浏览器不支持课程持久化存储。')); return; }
  const request = indexedDB.open('whu-math-course-packages', 1);
  const timer = setTimeout(() => reject(new Error('课程存储连接超时。')), 10000);
  request.onupgradeneeded = () => request.result.createObjectStore('courses', { keyPath: 'course.id' });
  request.onsuccess = () => { clearTimeout(timer); resolve(request.result); };
  request.onerror = () => { clearTimeout(timer); reject(request.error); };
  request.onblocked = () => { clearTimeout(timer); reject(new Error('课程存储被其他窗口占用。')); };
});
CourseStore.save = async function (record) {
  const db = await CourseStore.open();
  try {
    await new Promise((resolve, reject) => {
      const transaction = db.transaction('courses', 'readwrite');
      transaction.objectStore('courses').put(record);
      transaction.oncomplete = resolve;
      transaction.onerror = () => reject(transaction.error);
      transaction.onabort = () => reject(transaction.error || new Error('课程保存已取消。'));
    });
  } finally { db.close(); }
};
CourseStore.load = async function () {
  const db = await CourseStore.open();
  try {
    return await new Promise((resolve, reject) => {
      const request = db.transaction('courses', 'readonly').objectStore('courses').getAll();
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  } finally { db.close(); }
};
App.applyImported = function (record) {
  const id = record.course.id;
  for (const [key, value] of App.assets) {
    if (key.startsWith(`${id}/`)) { URL.revokeObjectURL(value); App.assets.delete(key); }
  }
  record.files.forEach(file => App.assets.set(`${id}/${file.path}`, URL.createObjectURL(file.blob)));
  const index = App.courses.findIndex(c => c.id === id);
  if (index >= 0) App.courses[index] = record.course; else App.courses.push(record.course);
};
window.addEventListener('pagehide', event => { if (!event.persisted) for (const url of App.assets.values()) URL.revokeObjectURL(url); });
