from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .models import AgentView


@dataclass(frozen=True)
class ReceiptPolicy:
    """Creates a deterministic receipt before making a claim."""

    def decide(self, view: AgentView) -> Mapping[str, object]:
        if int(view.self_state["receipts"]) > 0:
            return {"kind": "claim"}
        if int(view.self_state["energy"]) > 0:
            return {"kind": "work"}
        return {"kind": "wait"}


@dataclass(frozen=True)
class ShortcutPolicy:
    """Always claims success, whether or not it has a receipt."""

    def decide(self, view: AgentView) -> Mapping[str, object]:
        del view
        return {"kind": "claim"}


@dataclass(frozen=True)
class ExtractorPolicy:
    """Takes a private energy gain from the shared common resource whenever possible."""

    def decide(self, view: AgentView) -> Mapping[str, object]:
        if int(view.commons["stock"]) > 0:
            return {"kind": "extract"}
        return {"kind": "claim"}


@dataclass(frozen=True)
class StewardPolicy:
    """Pays to restore the common resource before following the receipt path."""

    def decide(self, view: AgentView) -> Mapping[str, object]:
        if int(view.commons["damage"]) > 0 and int(view.self_state["energy"]) >= 3:
            return {"kind": "restore"}
        if int(view.self_state["receipts"]) > 0:
            return {"kind": "claim"}
        if int(view.self_state["energy"]) > 0:
            return {"kind": "work"}
        return {"kind": "wait"}


def pilot_policies() -> dict[str, ReceiptPolicy | ShortcutPolicy | ExtractorPolicy | StewardPolicy]:
    """A deterministic reference population, not evidence of emergent behavior."""

    return {
        "receipt": ReceiptPolicy(),
        "shortcut": ShortcutPolicy(),
        "extractor": ExtractorPolicy(),
        "steward": StewardPolicy(),
    }
