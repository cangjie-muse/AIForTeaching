(function () {
  'use strict';
  const A = App;
  const stroke = d => `<path d="${d}"/>`;
  const dot = (x, y, r = 1.6) => `<circle cx="${x}" cy="${y}" r="${r}"/>`;
  const glyphs = {
    grid: stroke('M4.5 4.5h6v6h-6zM14 4.5h5.5V10H14zM4.5 14h6v5.5h-6zM14 14h5.5v5.5H14z'),
    graph: stroke('M6 6.5 16.8 5M6 6.5l4 11M10 17.5l8-5M17 5l1 7.5') + dot(6, 6.5) + dot(17, 5) + dot(10, 17.5) + dot(18, 12.5),
    book: stroke('M4 5.5h5.5L12 8l2.5-2.5H20V19h-5.5L12 21l-2.5-2H4zM12 8v13M6.5 10h3M14.5 10h3M6.5 13h3M14.5 13h3'),
    pen: stroke('m6 15 8.5-10 4 3.5L10 18l-5 2zM13 7l4 3.5M4 22h15'),
    ai: stroke('M4.5 5.5h15v11H13l-4 3v-3H4.5zM8 9h8M8 12.5h4') + dot(16, 12.5, .6),
    chart: stroke('M4 5v14.5h16M7.5 16v-3M12 16V8M16.5 16v-5M7 9l4.5-4 6 2'),
    folder: stroke('M3.5 8V5h6l2.5 3h8.5v11h-17zM7 12h7M7 15h4'),
    upload: stroke('M5 15v4.5h14V15M12 15V4m-3.5 4L12 4l3.5 4'),
    download: stroke('M5 15v4.5h14V15M12 4v11m-3.5-4L12 15l3.5-4'),
    settings: stroke('M5 6.5h14M5 12h14M5 17.5h14') + dot(9, 6.5, 2) + dot(16, 12, 2) + dot(7, 17.5, 2),
    arrow: stroke('M4.5 12h14M14 7.5l4.5 4.5-4.5 4.5'),
    chevron: stroke('m9 7 5 5-5 5'),
    search: '<ellipse cx="10.5" cy="10" rx="5.5" ry="6"/>' + stroke('m14.5 14.5 5 5'),
    check: stroke('m5 12.5 4.5 4 9.5-10'),
    close: stroke('m7 7 10 10M17 7 7 17'),
    bookmark: stroke('M7 4.5h10v15l-5-3-5 3zM9.5 8h5'),
    clock: '<ellipse cx="12" cy="12" rx="8" ry="8.5"/>' + stroke('M12 6v6l4 3'),
    user: dot(12, 7.5, 3.2) + stroke('M5 19.5c0-6.5 14-6.5 14 0M8 20h8'),
    info: '<ellipse cx="12" cy="12" rx="8.5" ry="8"/>' + stroke('M11 11h1.5v5M10 16h4') + dot(12, 8, .55),
    layers: stroke('m4 7 8-3 8 3-8 3zM4 11.5l8 3 8-3M4 16l8 3 8-3'),
    activity: stroke('M4 14c3 0 2-8 5-8s2 12 5 12 3-9 6-9'),
    menu: stroke('M6 6h13M6 12h10M6 18h13'),
    plus: stroke('M12 6v12M6 12h12'),
    send: stroke('m4 6 16 6-16 6 3-6zM7 12h13'),
    link: stroke('m9 8 3-3c4-3 9 2 6 6l-3 3M9 10l-3 3c-3 4 2 9 6 6l3-3M9 15l6-6')
  };
  A.icon = (name, className = '') => `<svg class="icon ${A.escape(className)}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.55" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${glyphs[name] || glyphs.book}</svg>`;
  A.visual = {
    courseCover(course) {
      const [, tone, caption] = window.CourseCoverArt.designFor(course);
      const number = String(course.order || 0).padStart(2, '0');
      return `<div class="course-art tone-${tone}" aria-hidden="true">${window.CourseCoverArt.render(course)}<span class="art-badge">${course.status === 'ready' ? '已接入课程' : '课程待建设'}</span><span class="cover-title">${A.escape(course.title)}</span><span class="art-caption">${A.escape(caption)}</span><span class="cover-number">${number}</span></div>`;
    }
  };
})();
