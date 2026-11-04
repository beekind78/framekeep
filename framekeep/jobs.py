"""One background worker keeps disk work off HTTP handlers and avoids competing scans."""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy


class Jobs:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="framekeep")
        self.lock = threading.Lock()
        self.current = None
        self.stop = threading.Event()

    def snapshot(self):
        with self.lock:
            return deepcopy(self.current)

    def busy(self):
        current = self.snapshot()
        return current and current["state"] in ("queued", "running")

    def submit(self, label, work):
        with self.lock:
            if self.current and self.current["state"] in ("queued", "running"):
                raise ValueError("Wait for the current task to finish, or cancel it first.")
            self.stop.clear()
            self.current = {
                "id": uuid.uuid4().hex,
                "label": label,
                "state": "queued",
                "done": 0,
                "total": 0,
                "message": "Starting…",
                "result": None,
            }
            job_id = self.current["id"]

        def update(done, total, message):
            with self.lock:
                self.current.update(done=done, total=total, message=message)

        def run():
            with self.lock:
                self.current["state"] = "running"
            try:
                result = work(update, self.stop.is_set)
                with self.lock:
                    self.current.update(
                        state="cancelled" if self.stop.is_set() else "complete", result=result
                    )
            except Exception as error:  # noqa: BLE001 -- worker boundary must report failures to the UI
                with self.lock:
                    self.current.update(state="failed", message=str(error))

        self.executor.submit(run)
        return {"job_id": job_id}

    def close(self):
        self.stop.set()
        self.executor.shutdown(wait=True, cancel_futures=True)
