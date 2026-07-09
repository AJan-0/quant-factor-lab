from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


class PipelineCancelled(RuntimeError):
    """Raised when a running pipeline observes a cancellation request."""


@dataclass(frozen=True)
class PipelineContext:
    run_id: str | None = None
    log: Callable[[str, str], None] | None = None
    is_cancelled: Callable[[], bool] | None = None

    def info(self, message: str) -> None:
        self.emit("INFO", message)

    def warning(self, message: str) -> None:
        self.emit("WARN", message)

    def emit(self, level: str, message: str) -> None:
        if self.log is not None:
            self.log(level.upper(), message)

    def checkpoint(self, message: str | None = None) -> None:
        if message:
            self.info(message)
        if self.is_cancelled is not None and self.is_cancelled():
            self.emit("WARN", "Pipeline cancellation requested")
            raise PipelineCancelled("Pipeline cancelled")


NULL_PIPELINE_CONTEXT = PipelineContext()
