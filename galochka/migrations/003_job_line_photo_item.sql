-- Snapshot of template_lines.photo_item_id on the job line: the reference photos of a
-- job line must not change when the template is edited afterwards (the line may even be
-- deleted from the template). NULL = show the photos of item_id.
ALTER TABLE job_lines ADD COLUMN photo_item_id TEXT REFERENCES items(id);
