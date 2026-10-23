-- 课程名称与学科方向可以改。
--
-- 原来这两件事都没有入口：
--   · 名称只能靠重新导入改（会重写整门课、把审核打回 draft，为三个字赔掉几百道题的审核记录）；
--   · 学科方向数据库里根本没有这一列，课程中心的分组靠前端静态表 js/course-catalog.js，
--     新导入的课一律进"待归类"，要归位只能改代码再部署。
--
-- direction 存的是课程中心那六个方向的 id（analysis/algebra/geometry/pde/probability/computing），
-- 留空表示待归类；前端遇到不认识的 id 也按待归类处理，不会让课凭空消失。
ALTER TABLE course ADD COLUMN IF NOT EXISTS direction text;

-- 谁导入的。用于"教师改自己导入的课"这条规则；
-- 早期导入的课没有这一列的值，视为无主，教师可改。
ALTER TABLE course ADD COLUMN IF NOT EXISTS created_by text;
