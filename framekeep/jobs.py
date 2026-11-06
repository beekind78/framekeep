"""One background worker keeps disk work off HTTP handlers and avoids competing scans."""

import json
import math
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy

from .checkpoints import SCHEMA, Checkpoint

ACTIVE = ("queued", "running")


class RemainingTime:
    def __init__(self):
        self.total = None
        self.done = 0
        self.started = self.last = 0.0
        self.seconds_per_item = None

    def update(self, done, total, now):
        if total != self.total or done < self.done:
            self.done, self.total = done, total
            self.started = self.last = now
            self.seconds_per_item = None
        elif done > self.done:
            sample = (now - self.last) / (done - self.done)
            self.seconds_per_item = sample if self.seconds_per_item is None else .75 * self.seconds_per_item + .25 * sample
            self.done, self.last = done, now
        if not self.seconds_per_item or done >= total or now - self.started < .6:
            return None
        return max(1, math.ceil((total - done) * self.seconds_per_item))


class Jobs:
    def __init__(self, connect, handlers):
        self.connect, self.handlers = connect, handlers
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="framekeep")
        self.lock = threading.RLock()
        self.current = None
        self.spec = None
        self.stop = threading.Event()
        self.closed = False
        with connect() as db:
            db.executescript(SCHEMA)
            saved = db.execute("SELECT * FROM saved_job").fetchone()
        if saved:
            self.spec = saved["operation"], json.loads(saved["arguments"])
            self.current = self._task(saved["id"], saved["label"], resumable=True)
            self.current["total"] = saved["total"]
            self.current.update(state="paused", done=self._saved_count(),
                                message="Progress saved on this PC. Resume when you're ready.")

    @staticmethod
    def _task(job_id, label, *, resumable=False):
        return {"id": job_id, "label": label, "state": "queued", "done": 0, "total": 0,
                "message": "Starting…", "result": None, "resumable": resumable, "stopping": "", "eta_seconds": None}

    def _saved_count(self):
        with self.connect() as db:
            count = db.execute("SELECT count(*) FROM job_receipts WHERE job_id=?", (self.current["id"],)).fetchone()[0]
        return min(count, self.current["total"])

    def _forget(self):
        if self.spec is not None:
            with self.connect() as db:
                db.execute("DELETE FROM saved_job WHERE id=?", (self.current["id"],))
            self.spec = None

    def snapshot(self):
        with self.lock:
            return deepcopy(self.current)

    def busy(self):
        with self.lock:
            return bool(self.current and self.current["state"] in (*ACTIVE, "paused"))

    @contextmanager
    def while_idle(self):
        with self.lock:
            if self.closed:
                raise ValueError("Framekeep is closing.")
            if self.current and self.current["state"] == "paused":
                raise ValueError("Resume or discard the paused task first.")
            if self.busy():
                raise ValueError("Wait for the current task to finish, or cancel it first.")
            yield

    def submit(self, label, work):
        with self.while_idle():
            self.current = self._task(uuid.uuid4().hex, label)
            return self._start(work)

    def submit_resumable(self, operation, label, arguments):
        with self.while_idle():
            if operation not in self.handlers:
                raise ValueError("This task does not support resume.")
            job_id = uuid.uuid4().hex
            with self.connect() as db:
                db.execute("INSERT INTO saved_job(slot,id,operation,label,arguments) VALUES (1,?,?,?,?)",
                           (job_id, operation, label, json.dumps(arguments)))
            self.spec = operation, arguments
            self.current = self._task(job_id, label, resumable=True)
            return self._start(self._resumable_work())

    def _resumable_work(self):
        operation, arguments = self.spec
        handler = self.handlers.get(operation)
        if handler is None:
            raise ValueError("This version of Framekeep cannot resume the saved task.")
        checkpoint = Checkpoint(self.connect, self.current["id"])
        return lambda progress, cancelled: handler(arguments, progress, cancelled, checkpoint)

    def _start(self, work):
        self.stop.clear()
        job_id = self.current["id"]
        estimate = RemainingTime()
        self.current["eta_seconds"] = None

        def update(done, total, message):
            with self.lock:
                if self.spec is not None and total != self.current["total"]:
                    with self.connect() as db:
                        db.execute("UPDATE saved_job SET total=? WHERE id=?", (total, job_id))
                self.current.update(done=done, total=total, message=message,
                                    eta_seconds=estimate.update(done, total, time.monotonic()))

        def run():
            with self.lock:
                self.current["state"] = "running"
            result, error = None, None
            try:
                if not self.stop.is_set():
                    result = work(update, self.stop.is_set)
            except Exception as exc:  # noqa: BLE001 -- worker boundary reports failures to the UI
                error = str(exc)
            with self.lock:
                stopping = self.current["stopping"]
                self.current["eta_seconds"] = None
                if self.spec is not None and (stopping == "pause" or (error is not None and not stopping)):
                    self.current.update(state="paused", stopping="", done=self._saved_count(),
                                        message=error or "Progress saved on this PC. You can close Framekeep.")
                else:
                    self._forget()
                    status = "failed" if error is not None else "cancelled" if stopping else "complete"
                    self.current.update(state=status, stopping="", result=result)
                    if error is not None:
                        self.current["message"] = error

        self.executor.submit(run)
        return {"job_id": job_id}

    def _require(self, job_id):
        if not self.current or (job_id is not None and self.current["id"] != job_id):
            raise ValueError("This task is no longer current.")

    def pause(self, job_id):
        with self.lock:
            self._require(job_id)
            if self.spec is None or self.current["state"] not in ACTIVE or self.current["stopping"] == "cancel":
                raise ValueError("This task cannot be paused.")
            self.current["stopping"] = "pause"
            self.stop.set()

    def resume(self, job_id):
        with self.lock:
            self._require(job_id)
            if self.closed or self.current["state"] != "paused":
                raise ValueError("There is no paused task to resume.")
            work = self._resumable_work()
            self.current.update(state="queued", message="Resuming…")
            return self._start(work)

    def cancel(self, job_id=None):
        with self.lock:
            if job_id is None and self.current is None:
                return
            self._require(job_id)
            if self.current["state"] == "paused":
                self._forget()
                self.current.update(state="cancelled", message="Paused task discarded.")
            elif self.current["state"] in ACTIVE:
                self.current["stopping"] = "cancel"
                self.stop.set()

    def interrupt(self):
        with self.lock:
            self.closed = True
            if self.current and self.current["state"] in ACTIVE and not self.current["stopping"]:
                self.current["stopping"] = "pause" if self.spec is not None else "cancel"
                self.stop.set()

    def close(self):
        self.interrupt()
        self.executor.shutdown(wait=True)
