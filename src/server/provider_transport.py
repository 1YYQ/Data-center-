"""Provider protocol seam. Only the admission transport may perform I/O."""
from __future__ import annotations

from typing import Protocol


class ProviderError(RuntimeError):
    """A stable safe code; never include provider bodies, URLs or credentials."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ProviderTransport(Protocol):
    def request(self, method: str, target: str, *, body: bytes = b"",
                purpose: str = "api", content_type: str | None = "application/json") -> bytes: ...
