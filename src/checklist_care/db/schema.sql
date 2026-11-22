-- checklist-care schema, version 1 (plan §3.3).
--
-- db/connection.py applies this file in one transaction when PRAGMA user_version
-- is 0, then sets user_version to connection.SCHEMA_VERSION.
-- The CHECK enums mirror src/checklist_care/limits.py; tests/unit/test_limits.py
-- fails if they drift.

CREATE TABLE bucket (
  id         INTEGER PRIMARY KEY CHECK (id BETWEEN 1 AND 4),
  title      TEXT NOT NULL,
  rubric     TEXT NOT NULL,          -- editable; the skill reads it via `context`
  updated_at TEXT NOT NULL
);

CREATE TABLE source (
  id          INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('youtube','wiki','text','manual')),
  ref         TEXT NOT NULL UNIQUE,
  title       TEXT,
  url         TEXT,
  revision    TEXT,                  -- wiki lastrevid | youtube '<generated|manual>:<lang>'
  mode        TEXT NOT NULL DEFAULT 'extract' CHECK (mode IN ('extract','investigate','manual')),
  fetched_at  TEXT,
  reviewed_at TEXT                   -- set when the proposal is imported ("stored as reviewed")
);

CREATE TABLE tip (
  id                    INTEGER PRIMARY KEY,
  name                  TEXT NOT NULL,
  action                TEXT NOT NULL, -- the printed "what to do"
  reason                TEXT,          -- one plain sentence; NULL only for manual tips
  bucket                INTEGER NOT NULL REFERENCES bucket(id),
  audience              TEXT NOT NULL CHECK (audience IN ('kids+','adults')),
  status                TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved')),
  source_id             INTEGER NOT NULL REFERENCES source(id),
  start_s               INTEGER,       -- whole seconds into the video; NULL for wiki/text/manual
  locator               TEXT,          -- original chapter title | wiki section heading
  evidence_url          TEXT,          -- page the tip came from (required in investigate mode)
  duplicate_of          INTEGER REFERENCES tip(id) ON DELETE SET NULL,
  possible_duplicate_of INTEGER REFERENCES tip(id) ON DELETE SET NULL,
  note                  TEXT,          -- why this bucket, e.g. 'good duplicate of #6: adds a brain/mood reason'
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL,
  CHECK (duplicate_of IS NULL OR bucket = 4)
);

CREATE TABLE list (
  id         INTEGER PRIMARY KEY,
  place      TEXT NOT NULL,
  audience   TEXT NOT NULL CHECK (audience IN ('kids','adults')),
  cue        TEXT NOT NULL DEFAULT '', -- editable "when you are here" rule; guides the skill's placement
  sort_order INTEGER NOT NULL DEFAULT 0,
  UNIQUE (place, audience)
);

CREATE TABLE list_item (
  list_id  INTEGER NOT NULL REFERENCES list(id) ON DELETE CASCADE,
  tip_id   INTEGER NOT NULL REFERENCES tip(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  PRIMARY KEY (list_id, tip_id)
);

CREATE INDEX tip_by_status_bucket ON tip(status, bucket);
CREATE INDEX tip_by_source ON tip(source_id);

-- Placement invariants (plan §3.3, §4.1). placement.py enforces them with
-- field-level errors; these triggers back them up by aborting any violating
-- write. A missing tip or list is left to the foreign keys.
--
--   1. A list_item may only point at a bucket-1 tip.
--   2. A kids list accepts only kids+ tips; an adults list accepts both.
--
-- A tip that leaves bucket 1 or becomes adults, and a list that becomes kids,
-- must have its incompatible placements deleted first, in the same
-- transaction. The tip and list triggers abort if any remain.

CREATE TRIGGER list_item_insert_requires_bucket_1
BEFORE INSERT ON list_item
FOR EACH ROW
WHEN EXISTS (SELECT 1 FROM tip WHERE tip.id = NEW.tip_id AND tip.bucket <> 1)
BEGIN
  SELECT RAISE(ABORT, 'placement: a list item must be a bucket-1 (daily) tip');
END;

CREATE TRIGGER list_item_insert_requires_kids_tip_on_kids_list
BEFORE INSERT ON list_item
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM list, tip
  WHERE list.id = NEW.list_id AND tip.id = NEW.tip_id
    AND list.audience = 'kids' AND tip.audience <> 'kids+'
)
BEGIN
  SELECT RAISE(ABORT, 'placement: a kids list accepts only kids+ tips');
END;

CREATE TRIGGER list_item_update_requires_bucket_1
BEFORE UPDATE OF list_id, tip_id ON list_item
FOR EACH ROW
WHEN EXISTS (SELECT 1 FROM tip WHERE tip.id = NEW.tip_id AND tip.bucket <> 1)
BEGIN
  SELECT RAISE(ABORT, 'placement: a list item must be a bucket-1 (daily) tip');
END;

CREATE TRIGGER list_item_update_requires_kids_tip_on_kids_list
BEFORE UPDATE OF list_id, tip_id ON list_item
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM list, tip
  WHERE list.id = NEW.list_id AND tip.id = NEW.tip_id
    AND list.audience = 'kids' AND tip.audience <> 'kids+'
)
BEGIN
  SELECT RAISE(ABORT, 'placement: a kids list accepts only kids+ tips');
END;

CREATE TRIGGER tip_leaving_bucket_1_must_be_unplaced
BEFORE UPDATE OF bucket ON tip
FOR EACH ROW
WHEN NEW.bucket <> 1 AND EXISTS (SELECT 1 FROM list_item WHERE list_item.tip_id = NEW.id)
BEGIN
  SELECT RAISE(ABORT, 'placement: remove the list items of a tip before it leaves bucket 1');
END;

CREATE TRIGGER tip_becoming_adults_must_leave_kids_lists
BEFORE UPDATE OF audience ON tip
FOR EACH ROW
WHEN NEW.audience <> 'kids+' AND EXISTS (
  SELECT 1 FROM list_item, list
  WHERE list_item.tip_id = NEW.id AND list.id = list_item.list_id AND list.audience = 'kids'
)
BEGIN
  SELECT RAISE(ABORT, 'placement: remove a tip from kids lists before it becomes adults');
END;

CREATE TRIGGER list_becoming_kids_must_hold_only_kids_tips
BEFORE UPDATE OF audience ON list
FOR EACH ROW
WHEN NEW.audience = 'kids' AND EXISTS (
  SELECT 1 FROM list_item, tip
  WHERE list_item.list_id = NEW.id AND tip.id = list_item.tip_id AND tip.audience <> 'kids+'
)
BEGIN
  SELECT RAISE(ABORT, 'placement: remove adults tips from a list before it becomes kids');
END;
