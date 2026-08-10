from __future__ import annotations

from typing import Any


class FakeChatResult:
    """Minimal result object returned by fake JSON/chat clients."""

    def __init__(
        self,
        data: Any = None,
        content: str = "{}",
        usage: dict[str, int] | None = None,
    ) -> None:
        self.data = data or {}
        self.content = content
        self.usage = usage or {"total_tokens": 1}
