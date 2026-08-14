from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, NoReturn, Sequence

from .models import (
    GLOBAL_BEATS_V2,
    SEQUENTIAL_DIALOGUE_V3,
    SLOTS_V1,
    validate_interaction_protocol,
)

MODEL_MAX_COMPLETION_TOKENS = 10_000
MODEL_MAX_RESPONSE_BYTES = 8_192


@dataclass(frozen=True)
class SurvivalAction:
    kind: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, **self.payload}


@dataclass(frozen=True)
class Speech:
    recipient: str
    text: str

    def to_dict(self) -> dict[str, str]:
        return {"to": self.recipient, "text": self.text}


@dataclass(frozen=True)
class ParsedSurvivalChoice:
    action: SurvivalAction
    speech: Speech | None
    action_error: str | None
    speech_error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.to_dict(),
            "say": self.speech.to_dict() if self.speech is not None else None,
        }


def _invalid_choice(
    action_error: str,
    *,
    speech: Speech | None = None,
    speech_error: str | None = None,
) -> ParsedSurvivalChoice:
    return ParsedSurvivalChoice(
        SurvivalAction("invalid", {}),
        speech,
        action_error,
        speech_error,
    )


def allowed_survival_actions(
    *,
    living_peers: Sequence[str],
    max_food_eaten: int,
    interaction_protocol: str = SLOTS_V1,
) -> tuple[dict[str, Any], ...]:
    active_protocol = validate_interaction_protocol(interaction_protocol)
    peers = list(living_peers)
    actions: tuple[dict[str, Any], ...] = (
        {"kind": "rest"},
        *(
            ({"kind": "wait"},)
            if active_protocol in {GLOBAL_BEATS_V2, SEQUENTIAL_DIALOGUE_V3}
            else ()
        ),
        {"kind": "forage"},
        {"kind": "gather_wood"},
        {"kind": "eat", "amount": f"integer 1..{max_food_eaten}"},
        {"kind": "build_shelter"},
    )
    if not peers:
        return actions
    return actions + (
        {"kind": "give_food", "target": peers, "amount": "integer 1..2"},
        {"kind": "give_wood", "target": peers, "amount": "integer 1..2"},
    )


def parse_survival_choice(
    raw_choice: object,
    *,
    actor: str,
    living_peers: Sequence[str],
    max_food_eaten: int,
    max_speech_chars: int,
    interaction_protocol: str = SLOTS_V1,
) -> ParsedSurvivalChoice:
    active_protocol = validate_interaction_protocol(interaction_protocol)
    if not isinstance(raw_choice, Mapping):
        return _invalid_choice("choice must be an object")
    if set(raw_choice) != {"action", "say"}:
        return _invalid_choice("choice must use exactly ['action', 'say']")

    action, action_error = _parse_action(
        raw_choice["action"],
        living_peers=living_peers,
        max_food_eaten=max_food_eaten,
        interaction_protocol=active_protocol,
    )
    speech, speech_error = _parse_speech(
        raw_choice["say"],
        actor=actor,
        living_peers=living_peers,
        max_speech_chars=max_speech_chars,
    )
    return ParsedSurvivalChoice(
        action=action if action is not None else SurvivalAction("invalid", {}),
        speech=speech,
        action_error=action_error,
        speech_error=speech_error,
    )


def parse_model_response(
    raw_response: str,
    *,
    actor: str,
    living_peers: Sequence[str],
    max_food_eaten: int,
    max_speech_chars: int,
    interaction_protocol: str = SLOTS_V1,
) -> ParsedSurvivalChoice:
    raw_choice, error = parse_strict_model_json(raw_response)
    if error is not None:
        return _invalid_choice(error)
    return parse_survival_choice(
        raw_choice,
        actor=actor,
        living_peers=living_peers,
        max_food_eaten=max_food_eaten,
        max_speech_chars=max_speech_chars,
        interaction_protocol=interaction_protocol,
    )


def parse_strict_model_json(raw_response: object) -> tuple[object | None, str | None]:
    if not isinstance(raw_response, str):
        return None, "model response must be text"
    try:
        response_bytes = raw_response.encode("utf-8")
    except UnicodeEncodeError as error:
        return None, f"model response is not valid UTF-8 text: {error}"
    if len(response_bytes) > MODEL_MAX_RESPONSE_BYTES:
        return None, f"model response exceeds {MODEL_MAX_RESPONSE_BYTES} bytes"
    try:
        return json.loads(
            raw_response,
            object_pairs_hook=_unique_object,
            parse_constant=_raise_invalid_constant,
        ), None
    except (json.JSONDecodeError, ValueError) as error:
        return None, f"model response is not valid strict JSON: {error}"


def _parse_action(
    raw_action: object,
    *,
    living_peers: Sequence[str],
    max_food_eaten: int,
    interaction_protocol: str,
) -> tuple[SurvivalAction | None, str | None]:
    if not isinstance(raw_action, Mapping):
        return None, "action must be an object"
    kind = raw_action.get("kind")
    if not isinstance(kind, str):
        return None, "action.kind must be a string"
    expected_keys: dict[str, set[str]] = {
        "rest": {"kind"},
        "forage": {"kind"},
        "gather_wood": {"kind"},
        "eat": {"kind", "amount"},
        "build_shelter": {"kind"},
        "give_food": {"kind", "target", "amount"},
        "give_wood": {"kind", "target", "amount"},
    }
    if interaction_protocol in {GLOBAL_BEATS_V2, SEQUENTIAL_DIALOGUE_V3}:
        expected_keys["wait"] = {"kind"}
    if kind not in expected_keys:
        return None, f"unknown action kind {kind!r}"
    if set(raw_action) != expected_keys[kind]:
        return None, f"{kind} must use exactly {sorted(expected_keys[kind])}"
    payload = {key: value for key, value in raw_action.items() if key != "kind"}
    if kind == "eat" and not _is_bounded_int(payload["amount"], 1, max_food_eaten):
        return None, f"eat.amount must be an integer from 1 through {max_food_eaten}"
    if kind in {"give_food", "give_wood"}:
        target = payload["target"]
        if not isinstance(target, str) or target not in living_peers:
            return None, f"{kind}.target must be a living peer"
        if not _is_bounded_int(payload["amount"], 1, 2):
            return None, f"{kind}.amount must be an integer from 1 through 2"
    return SurvivalAction(kind=kind, payload=payload), None


def _parse_speech(
    raw_speech: object,
    *,
    actor: str,
    living_peers: Sequence[str],
    max_speech_chars: int,
) -> tuple[Speech | None, str | None]:
    if raw_speech is None:
        return None, None
    if not isinstance(raw_speech, Mapping):
        return None, "say must be null or an object"
    if set(raw_speech) != {"to", "text"}:
        return None, "say must use exactly ['text', 'to']"
    recipient = raw_speech["to"]
    if not isinstance(recipient, str):
        return None, "say.to must be a string"
    if recipient == actor:
        return None, "a survivor cannot speak to themself"
    if recipient != "everyone" and recipient not in living_peers:
        return None, "say.to must be a living peer or 'everyone'"
    text = raw_speech["text"]
    if not isinstance(text, str) or not 1 <= len(text) <= max_speech_chars:
        return None, f"say.text must contain 1 through {max_speech_chars} characters"
    if not text.strip():
        return None, "say.text cannot be whitespace only"
    if any(unicodedata.category(character).startswith("C") for character in text):
        return None, "say.text cannot contain control characters"
    return Speech(recipient=recipient, text=text), None


def _is_bounded_int(value: object, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key {key!r}")
        result[key] = value
    return result


def _raise_invalid_constant(value: str) -> NoReturn:
    raise ValueError(f"invalid JSON constant {value!r}")
