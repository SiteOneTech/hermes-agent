ALTER TABLE workflow_runs
    ADD COLUMN IF NOT EXISTS description text NOT NULL DEFAULT '';

UPDATE workflow_runs
SET description = trim(
    concat_ws(
        ' ',
        NULLIF(metadata->>'functional_description', ''),
        NULLIF(metadata->>'objective', ''),
        title
    )
)
WHERE description = '';
