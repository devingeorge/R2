"""Bounded startup milestones. Never record credentials, prompts, or audio."""
from __future__ import annotations

import logging
import time
import uuid


class StartupTrace:
    def __init__(self, trigger="direct", started=None):
        self.id = uuid.uuid4().hex[:12]
        self.trigger = trigger
        self.started = time.monotonic() if started is None else started
        self.marks = {}

    def mark(self, name, at=None):
        if name not in self.marks:
            self.marks[name] = round(((time.monotonic() if at is None else at) - self.started) * 1000, 3)

    def snapshot(self):
        return {"id": self.id, "trigger": self.trigger, "clock": "server_monotonic_ms", "marks": dict(self.marks)}

    def log(self):
        logging.getLogger("uvicorn.error").info("R2 startup %s", self.snapshot())
