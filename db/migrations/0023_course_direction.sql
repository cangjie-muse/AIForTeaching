-- 学科分类从代码里搬进数据库。
--
-- 原来六个方向硬编码在 js/course-catalog.js 与 server/app/course_meta.py 两处：
-- 想多加一个「数理逻辑」，得同时改前端、改后端、重新部署。分类属于运营数据，
-- 不是程序结构，管理员在界面上加一条就应该生效。
--
-- 课程表上的 course.direction 存的就是这里的 id；没有外键约束：
-- 方向被删掉时课不该跟着消失，读不到 id 就按「待归类」显示。
CREATE TABLE IF NOT EXISTS course_direction (
    id          text PRIMARY KEY,
    label       text NOT NULL UNIQUE,
    description text,
    ord         int  NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  text
);

INSERT INTO course_direction (id, label, description, ord) VALUES
    ('analysis',    '分析',       '极限、连续、微分与积分，以及测度与泛函', 1),
    ('algebra',     '代数与数论', '线性结构、群环域与数论',               2),
    ('geometry',    '几何与拓扑', '空间、流形与不变量',                   3),
    ('pde',         '微分方程',   '常微分方程、偏微分方程与数学建模',     4),
    ('probability', '概率统计',   '随机现象、统计推断与金融应用',         5),
    ('computing',   '计算与应用', '数值方法、机器学习与运筹',             6)
ON CONFLICT (id) DO NOTHING;
