"""Parsing adapters.  No provider response is exposed to business routes."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .local_pdf import parse_local_pdf

ParsedBlock = tuple[str, dict, str, dict]


class ProviderBlocked(RuntimeError):
    """Raised when external data-flow approval or credentials are absent."""


class ParseProvider(Protocol):
    name: str

    def parse(self, path: Path) -> list[ParsedBlock]: ...


class LocalPdfProvider:
    name = "local-pdf"

    def __init__(self, mineru_executable: Path | None = None, mineru_home: Path | None = None):
        self.mineru_executable = mineru_executable
        self.mineru_home = mineru_home

    def parse(self, path: Path) -> list[ParsedBlock]:
        return parse_local_pdf(path, self.mineru_executable, self.mineru_home)


class MinerUProvider:
    name = "mineru"

    def __init__(self, *, approved: bool = False):
        self.approved = approved

    def parse(self, path: Path) -> list[ParsedBlock]:
        if not self.approved:
            raise ProviderBlocked("MinerU provider is pending data-flow approval")
        raise ProviderBlocked("MinerU adapter requires deployment credentials")
