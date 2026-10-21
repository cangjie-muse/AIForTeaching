-- 0014: 补齐"任务已通过、题目却没有人工校验标记"的历史数据
--
-- 背景：在「通过」只改工作流状态的那段时间里点过的通过，没有写
-- exercise.human_verified_at，于是总表的可信等级、课程内容页的徽标、
-- 可见范围「仅人工审核」那一档的题量都和实际审核进度对不上。
--
-- 判定口径：这道题最近一条审核任务已经是 approved / published，但题目
-- 没有人工校验标记。只补这两类状态——draft 的题不动，避免把"还没人看"
-- 的题误标成已审。
--
-- 幂等：只处理 human_verified_at IS NULL 的行，重复执行不会再改任何东西。

UPDATE exercise e
   SET human_verified_at = now(),
       human_verified_by = coalesce((
         SELECT rt.assignee
           FROM review_task rt
          WHERE rt.course_id = e.course_id AND rt.target_id = e.id
            AND rt.state IN ('approved', 'published')
          ORDER BY rt.decided_at DESC NULLS LAST, rt.created_at DESC
          LIMIT 1
       ), '历史补录'),
       review_status = CASE
         WHEN e.review_status = 'published' THEN e.review_status
         ELSE 'teacher-reviewed'::review_status
       END
 WHERE e.human_verified_at IS NULL
   AND e.review_status <> 'retired'
   AND EXISTS (
     SELECT 1
       FROM review_task t
      WHERE t.course_id = e.course_id
        AND t.target_id = e.id
        AND t.state IN ('approved', 'published')
   );

COMMENT ON COLUMN exercise.human_verified_at IS
  '人工审核通过时间。审核页点「通过」即写入（发布也会写）；总表与可见范围据此判定 human 档。';
