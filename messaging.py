"""Provider-neutral message addressing shared by bot integrations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


REPLY_PROVIDERS = frozenset({"telegram", "vk"})


@dataclass(frozen=True)
class ReplyTarget:
    provider: str
    conversation_id: str | int

    def __post_init__(self) -> None:
        provider = str(self.provider or "").strip().lower()
        if provider not in REPLY_PROVIDERS:
            raise ValueError(f"unsupported reply provider: {provider or '<empty>'}")
        if (
            not isinstance(self.conversation_id, (str, int))
            or isinstance(self.conversation_id, bool)
        ):
            raise ValueError("reply conversation_id must be a string or integer")
        conversation_id = str(self.conversation_id).strip()
        if not conversation_id:
            raise ValueError("reply conversation_id is required")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "conversation_id", conversation_id)

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "conversation_id": self.conversation_id,
        }

    @classmethod
    def from_value(cls, value: Any) -> "ReplyTarget":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ValueError("reply_target must be an object")
        return cls(
            provider=value.get("provider", ""),
            conversation_id=value.get("conversation_id", ""),
        )
