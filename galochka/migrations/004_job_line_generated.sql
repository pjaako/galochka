-- 1 = created by generate_outgoing ("what came in goes out"). A worker may not delete such a
-- line from the return list, even though its source can be 'worker'.
ALTER TABLE job_lines ADD COLUMN generated INTEGER NOT NULL DEFAULT 0;
