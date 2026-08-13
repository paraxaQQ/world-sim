from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


ALLOWED_ACTION_SCHEMAS: tuple[dict[str, Any], ...] = (
    {"kind": "work"},
    {"kind": "claim"},
    {"kind": "extract"},
    {"kind": "restore"},
    {"kind": "transfer", "target": "agent id", "amount": "integer 1..3"},
    {"kind": "offer_pact", "target": "agent id", "bond": "integer 1..3"},
    {"kind": "accept_pact", "offer_id": "offer id"},
    {"kind": "message", "target": "agent id", "text": "1..280 characters"},
    {"kind": "wait"},
)


def allowed_action_schemas(
    *,
    messages_enabled: bool,
    pacts_enabled: bool,
) -> tuple[dict[str, Any], ...]:
    """Return the action surface for the active experimental capability condition."""

    disabled_kinds: set[str] = set()
    if not messages_enabled:
        disabled_kinds.add("message")
    if not pacts_enabled:
        disabled_kinds.update({"offer_pact", "accept_pact"})
    return tuple(
        dict(schema)
        for schema in ALLOWED_ACTION_SCHEMAS
        if str(schema["kind"]) not in disabled_kinds
    )


@dataclass(frozen=True)
class ParsedAction:
    kind: str
    payload: dict[str, Any]


def parse_action(raw_action: object) -> tuple[ParsedAction | None, str | None]:
    """Parse untrusted JSON-like input into the only action surface the engine accepts."""

    if not isinstance(raw_action, Mapping):
        return None, "action must be an object"

    kind = raw_action.get("kind")
    if not isinstance(kind, str):
        return None, "action.kind must be a string"

    expected_keys: dict[str, set[str]] = {
        "work": {"kind"},
        "claim": {"kind"},
        "extract": {"kind"},
        "restore": {"kind"},
        "transfer": {"kind", "target", "amount"},
        "offer_pact": {"kind", "target", "bond"},
        "accept_pact": {"kind", "offer_id"},
        "message": {"kind", "target", "text"},
        "wait": {"kind"},
    }
    if kind not in expected_keys:
        return None, f"unknown action kind {kind!r}"
    if set(raw_action) != expected_keys[kind]:
        return None, f"{kind} must use exactly {sorted(expected_keys[kind])}"

    payload = {key: value for key, value in raw_action.items() if key != "kind"}
    if kind in {"transfer", "offer_pact", "message"} and not _is_nonempty_string(payload["target"]):
        return None, f"{kind}.target must be a non-empty string"
    if kind == "transfer" and not _is_small_positive_int(payload["amount"]):
        return None, "transfer.amount must be an integer from 1 through 3"
    if kind == "offer_pact" and not _is_small_positive_int(payload["bond"]):
        return None, "offer_pact.bond must be an integer from 1 through 3"
    if kind == "accept_pact" and not _is_nonempty_string(payload["offer_id"]):
        return None, "accept_pact.offer_id must be a non-empty string"
    if kind == "message":
        text = payload["text"]
        if not isinstance(text, str) or not 1 <= len(text) <= 280:
            return None, "message.text must be a string from 1 through 280 characters"

    return ParsedAction(kind=kind, payload=payload), None


def _is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_small_positive_int(value: object) -> bool:
    return type(value) is int and 1 <= value <= 3
