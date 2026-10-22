-- 回答的"依据"来源。课程材料里找不到依据时，允许改用模型的通用知识，
-- 但必须留下痕迹：前端会显示「通用知识」标记，回答正文开头也有一段免责说明。
ALTER TABLE ai_message ADD COLUMN basis text NOT NULL DEFAULT 'course'
  CHECK (basis IN ('course', 'model'));

COMMENT ON COLUMN ai_message.basis IS
  'course=依据课程材料（有引用）；model=知识库无依据，模型通用知识（正文已带未验证标注）';
