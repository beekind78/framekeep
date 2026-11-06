"""Durable receipts for one resumable job in the local catalog."""

import json

SCHEMA = """
CREATE TABLE IF NOT EXISTS saved_job (
 slot INTEGER PRIMARY KEY CHECK(slot=1), id TEXT UNIQUE NOT NULL,
 operation TEXT NOT NULL, label TEXT NOT NULL, arguments TEXT NOT NULL,
 total INTEGER NOT NULL DEFAULT 0, scope TEXT
);
CREATE TABLE IF NOT EXISTS job_receipts (
 job_id TEXT NOT NULL REFERENCES saved_job(id) ON DELETE CASCADE,
 item TEXT NOT NULL, revision TEXT NOT NULL, outcome TEXT NOT NULL,
 PRIMARY KEY(job_id, item)
);
"""


class Checkpoint:
    def __init__(self, connect, job_id):
        self.connect, self.job_id = connect, job_id

    def receipts(self):
        with self.connect() as db:
            return {row["item"]: (row["revision"], json.loads(row["outcome"])) for row in db.execute(
                "SELECT item,revision,outcome FROM job_receipts WHERE job_id=?", (self.job_id,)
            )}

    def record(self, item, revision, outcome):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO job_receipts VALUES (?,?,?,?)",
                       (self.job_id, item, revision, json.dumps(outcome)))

    def remove_receipts(self, items):
        with self.connect() as db:
            db.executemany("DELETE FROM job_receipts WHERE job_id=? AND item=?",
                           ((self.job_id, item) for item in items))

    def scope(self):
        with self.connect() as db:
            return db.execute("SELECT scope FROM saved_job WHERE id=?", (self.job_id,)).fetchone()[0]

    def save_scope(self, scope):
        with self.connect() as db:
            db.execute("UPDATE saved_job SET scope=? WHERE id=?", (json.dumps(scope), self.job_id))


def run_items(items, identify, process, progress, cancelled, checkpoint=None, *, check_sources=None):
    """Skip matching receipts and record each outcome after its work commits."""
    receipts = checkpoint.receipts() if checkpoint else {}
    pending, outcomes, obsolete = [], [], []
    for item in items:
        if cancelled():
            return outcomes
        key, revision, name = identify(item)
        saved = receipts.pop(key, None)
        if saved and saved[0] == revision:
            outcomes.append(saved[1])
        else:
            pending.append((item, key, revision, name))
            if saved:
                obsolete.append(key)
    if checkpoint and (receipts or obsolete):
        checkpoint.remove_receipts([*receipts, *obsolete])
    progress(len(outcomes), len(items), "Preparing remaining files…")
    if checkpoint and check_sources and not cancelled():
        check_sources(item for item, *_ in pending)
    for item, key, revision, name in pending:
        if cancelled():
            break
        progress(len(outcomes), len(items), name)
        outcome = process(item)
        if outcome is None:
            break  # The current file did not finish.
        if checkpoint:
            checkpoint.record(key, revision, outcome)
        outcomes.append(outcome)
    return outcomes


def photo_identity(photo):
    return str(photo["id"]), photo["sha256"], photo["filename"]
