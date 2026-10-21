-- 0011：学生可见范围（验证等级闸）
--
-- 审核不是二元的。一道题的可信程度至少有四档（见 v_exercise_verification）：
--   none       导入进来、没人看过
--   lean       Lean 证明编译通过，且形式化忠实性经人工确认
--   human      人工逐题审核通过（human_verified_at）
--   strongest  两者都有
--
-- 学校在不同阶段要的口径不一样：开发期只能全放，正式上课要"只放人工审核过的"。
-- 这一版把"口径"变成库里的一个开关，而不是散落在各个页面里的 if。
--
-- 与发布闸的关系：review_status = 'published' 决定这道题"能不能出现"，
-- visibility_policy 决定"哪一档可信度才允许出现"。两者是 AND，不是替代。
--
-- 默认 'all'：与引入本表之前的行为完全一致，升级不会让学生端突然空掉。

BEGIN;

CREATE TABLE visibility_policy (
  id            text PRIMARY KEY DEFAULT 'default' CHECK (id = 'default'),
  exercise_tier text NOT NULL DEFAULT 'all' CHECK (exercise_tier IN ('human', 'verified', 'all')),
  qa_tier       text NOT NULL DEFAULT 'all' CHECK (qa_tier IN ('human', 'verified', 'all')),
  note          text,
  updated_at    timestamptz NOT NULL DEFAULT now(),
  updated_by    text
);

COMMENT ON TABLE visibility_policy IS
  '学生可见范围。exercise_tier 管习题与测验，qa_tier 管 AI 问答的完整解答模式；默认 all，与引入本表之前一致。';

COMMENT ON COLUMN visibility_policy.exercise_tier IS
  'human = 仅人工审核通过；verified = Lean 验证或人工审核；all = 不设门槛（含未验证）。';

INSERT INTO visibility_policy (id) VALUES ('default') ON CONFLICT (id) DO NOTHING;

COMMIT;
