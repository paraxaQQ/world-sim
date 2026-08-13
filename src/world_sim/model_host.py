from __future__ import annotations

import hashlib
import http.client
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, Protocol

from .survival.demo import result_sha256, survival_metrics
from .survival.engine import SurvivalChoiceProvider, make_survival_world, run_survival
from .survival.models import DEFAULT_SURVIVOR_NAMES, SurvivalConfig, SurvivorView
from .survival.prompt import render_system_prompt, render_turn_prompt
from .survival.protocol import MODEL_MAX_COMPLETION_TOKENS, parse_model_response


ADAPTER_NAME = "opencode-direct-chat-completions"
DEFAULT_LIVE_MAX_CALLS = 12
DEFAULT_LIVE_TEMPERATURE = 0.2
DEFAULT_LIVE_TIMEOUT_SECONDS = 60.0
DEFAULT_LIVE_REASONING_EFFORT = "provider-default"
LIVE_REASONING_EFFORTS = ("provider-default", "low")
MAX_HTTP_RESPONSE_BYTES = 131_072


@dataclass(frozen=True)
class EndpointSpec:
    provider: str
    host: str
    path: str
    requires_api_key: bool

    @property
    def url(self) -> str:
        return f"https://{self.host}{self.path}"


_ENDPOINTS = {
    "opencode": EndpointSpec(
        "opencode", "opencode.ai", "/zen/v1/chat/completions", False
    ),
    "opencode-go": EndpointSpec(
        "opencode-go", "opencode.ai", "/zen/go/v1/chat/completions", True
    ),
}


@dataclass(frozen=True)
class TransportResponse:
    status: int
    headers: dict[str, str]
    body: str


class ChatTransport(Protocol):
    def post(
        self,
        endpoint: EndpointSpec,
        request_body: Mapping[str, object],
        *,
        api_key: str | None,
        timeout_seconds: float,
    ) -> TransportResponse: ...


class TransportFailure(RuntimeError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class EnvelopeFailure(ValueError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class LiveCallFailure(RuntimeError):
    pass


class StdlibChatTransport:
    def post(
        self,
        endpoint: EndpointSpec,
        request_body: Mapping[str, object],
        *,
        api_key: str | None,
        timeout_seconds: float,
    ) -> TransportResponse:
        body = json.dumps(
            request_body, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "world-sim/0.4.2",
        }
        if api_key is not None:
            headers["Authorization"] = f"Bearer {api_key}"

        connection = http.client.HTTPSConnection(
            endpoint.host, timeout=timeout_seconds
        )
        try:
            connection.request("POST", endpoint.path, body=body, headers=headers)
            response = connection.getresponse()
            raw_body = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
            if len(raw_body) > MAX_HTTP_RESPONSE_BYTES:
                raise TransportFailure(
                    "oversized_http_response",
                    f"provider response exceeds {MAX_HTTP_RESPONSE_BYTES} bytes",
                )
            try:
                text = raw_body.decode("utf-8")
            except UnicodeDecodeError as error:
                raise TransportFailure(
                    "invalid_http_encoding",
                    "provider response is not valid UTF-8",
                ) from error
            return TransportResponse(
                status=response.status,
                headers={key.casefold(): value for key, value in response.getheaders()},
                body=text,
            )
        except TransportFailure:
            raise
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            raise TransportFailure(
                "network_error",
                f"provider request failed: {type(error).__name__}: {error}",
            ) from error
        finally:
            connection.close()


@dataclass(frozen=True)
class _Assignment:
    seat_id: str
    public_name: str
    model_ref: str
    endpoint: EndpointSpec
    model_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "seat_id": self.seat_id,
            "public_name": self.public_name,
            "model": self.model_ref,
        }


@dataclass
class _LiveProvider(SurvivalChoiceProvider):
    assignment: _Assignment
    transport: ChatTransport
    api_key: str | None
    timeout_seconds: float
    max_completion_tokens: int
    temperature: float
    reasoning_effort: str | None
    calls: list[dict[str, Any]]

    def decide(self, view: SurvivorView) -> Mapping[str, object]:
        if view.name != self.assignment.public_name:
            raise RuntimeError("a model provider received the wrong public identity")
        request: dict[str, object] = {
            "model": self.assignment.model_id,
            "messages": [
                {"role": "system", "content": render_system_prompt(view.name)},
                {"role": "user", "content": render_turn_prompt(view)},
            ],
            "max_tokens": self.max_completion_tokens,
            "temperature": self.temperature,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        if self.reasoning_effort is not None:
            request["reasoning_effort"] = self.reasoning_effort
        try:
            response = self.transport.post(
                self.assignment.endpoint,
                request,
                api_key=self.api_key,
                timeout_seconds=self.timeout_seconds,
            )
        except TransportFailure as error:
            self._fail(view, request, error.kind, str(error), None)
        except Exception as error:  # noqa: BLE001 - retain a paid-call receipt.
            self._fail(
                view,
                request,
                "transport_error",
                f"transport raised {type(error).__name__}",
                None,
            )

        if response.status != 200:
            self._fail(
                view,
                request,
                "http_error",
                f"provider returned HTTP {response.status}",
                response,
            )
        try:
            content, metadata = _parse_envelope(response.body)
        except EnvelopeFailure as error:
            self._fail(view, request, error.kind, str(error), response)

        parsed = parse_model_response(
            content,
            actor=view.name,
            living_peers=tuple(str(peer["name"]) for peer in view.others),
            max_food_eaten=int(view.rules["max_food_eaten"]),
            max_speech_chars=int(view.rules["max_speech_chars"]),
        )
        record = {
            **self._base_record(view, request),
            "status": "succeeded",
            "response": {
                "http_status": response.status,
                "request_id": _request_id(response.headers),
                **metadata,
                "model_reply": content,
            },
            "parsed_choice": parsed.to_dict(),
            "validation": {
                "action_error": parsed.action_error,
                "speech_error": parsed.speech_error,
            },
        }
        self._record(record)
        return parsed.to_dict()

    def _fail(
        self,
        view: SurvivorView,
        request: Mapping[str, object],
        kind: str,
        message: str,
        response: TransportResponse | None,
    ) -> NoReturn:
        record = {
            **self._base_record(view, request),
            "status": "failed",
            "response": (
                _error_receipt(response)
                if response is not None
                else None
            ),
            "error": {
                "kind": kind,
                "message": message,
                "http_status": response.status if response is not None else None,
            },
        }
        self._record(record)
        raise LiveCallFailure(message)

    def _base_record(
        self, view: SurvivorView, request: Mapping[str, object]
    ) -> dict[str, object]:
        return {
            "sequence": len(self.calls) + 1,
            "day": view.day,
            "seat_id": self.assignment.seat_id,
            "public_name": self.assignment.public_name,
            "model": self.assignment.model_ref,
            "endpoint": self.assignment.endpoint.url,
            "request": dict(request),
        }

    def _record(self, record: dict[str, Any]) -> None:
        self.calls.append(record)


def run_live_survival(
    *,
    model_refs: Sequence[str],
    seed: int = 17,
    days: int = 3,
    max_calls: int = DEFAULT_LIVE_MAX_CALLS,
    max_completion_tokens: int = MODEL_MAX_COMPLETION_TOKENS,
    temperature: float = DEFAULT_LIVE_TEMPERATURE,
    reasoning_effort: str = DEFAULT_LIVE_REASONING_EFFORT,
    timeout_seconds: float = DEFAULT_LIVE_TIMEOUT_SECONDS,
    transport: ChatTransport | None = None,
    auth_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    assignments = _assign_models(model_refs)
    _validate_limits(
        len(assignments),
        days,
        max_calls,
        max_completion_tokens,
        temperature,
        timeout_seconds,
    )
    if reasoning_effort not in LIVE_REASONING_EFFORTS:
        raise ValueError(
            "reasoning_effort must be one of " + ", ".join(LIVE_REASONING_EFFORTS)
        )
    active_environ = os.environ if environ is None else environ
    zen_key = active_environ.get("OPENCODE_ZEN_API_KEY", "").strip() or None
    go_key = (
        load_opencode_go_api_key(auth_path=auth_path, environ=environ)
        if any(assignment.endpoint.requires_api_key for assignment in assignments)
        else None
    )
    calls: list[dict[str, Any]] = []
    active_transport = transport or StdlibChatTransport()
    providers = {
        assignment.public_name: _LiveProvider(
            assignment=assignment,
            transport=active_transport,
            api_key=go_key if assignment.endpoint.requires_api_key else zen_key,
            timeout_seconds=timeout_seconds,
            max_completion_tokens=max_completion_tokens,
            temperature=temperature,
            reasoning_effort=(
                None if reasoning_effort == "provider-default" else reasoning_effort
            ),
            calls=calls,
        )
        for assignment in assignments
    }
    world = make_survival_world(
        tuple(assignment.public_name for assignment in assignments),
        seed=seed,
        config=SurvivalConfig(max_days=days),
    )
    base = {
        "format_version": 1,
        "mode": "live_named_survival",
        "adapter": ADAPTER_NAME,
        "config": {
            "seed": seed,
            "days_requested": days,
            "max_calls": max_calls,
            "max_completion_tokens": max_completion_tokens,
            "temperature": temperature,
            "reasoning_effort": reasoning_effort,
            "timeout_seconds": timeout_seconds,
        },
        "seat_assignments": [assignment.to_dict() for assignment in assignments],
        "calls": calls,
    }
    initial_state = world.to_dict(include_events=False)
    try:
        result = run_survival(world, providers, days=days)
    except RuntimeError as error:
        if not isinstance(error.__cause__, LiveCallFailure):
            raise
        failed = calls[-1]
        return {
            **base,
            "status": "failed",
            "failure": {
                "call_sequence": failed["sequence"],
                "day": failed["day"],
                "seat_id": failed["seat_id"],
                "public_name": failed["public_name"],
                "model": failed["model"],
                **failed["error"],
            },
            "initial_state": initial_state,
            "partial_state": world.to_dict(),
            "provider_summary": _provider_summary(calls),
        }
    return {
        **base,
        "status": "completed",
        "canonical_result_sha256": result_sha256(result),
        "metrics": survival_metrics(result),
        "provider_summary": _provider_summary(calls),
        "result": result.to_dict(),
    }


def load_opencode_go_api_key(
    *,
    auth_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    active_environ = os.environ if environ is None else environ
    explicit = active_environ.get("OPENCODE_API_KEY", "").strip()
    if explicit:
        return explicit
    source = auth_path or Path.home() / ".local" / "share" / "opencode" / "auth.json"
    try:
        raw_auth = source.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"cannot read OpenCode credentials from {source}") from error
    try:
        auth = _strict_json(raw_auth)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError("OpenCode credential data is not valid strict JSON") from error
    if not isinstance(auth, Mapping):
        raise ValueError("OpenCode credential data must be an object")
    entry = auth.get("opencode-go")
    if not isinstance(entry, Mapping) or entry.get("type") != "api":
        raise ValueError("OpenCode Go credential must use API-key authentication")
    key = entry.get("key")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("OpenCode Go credential has no API key")
    return key.strip()


def _assign_models(model_refs: Sequence[str]) -> tuple[_Assignment, ...]:
    refs = tuple(reference.strip() for reference in model_refs)
    if not 2 <= len(refs) <= len(DEFAULT_SURVIVOR_NAMES):
        raise ValueError("survive-live needs between 2 and 8 model assignments")
    assignments = []
    for index, (name, model_ref) in enumerate(
        zip(DEFAULT_SURVIVOR_NAMES[: len(refs)], refs, strict=True), start=1
    ):
        provider, separator, model_id = model_ref.partition("/")
        if not separator or provider not in _ENDPOINTS:
            raise ValueError(
                f"model {model_ref!r} must use opencode/MODEL or opencode-go/MODEL"
            )
        if not model_id or len(model_id) > 128:
            raise ValueError(f"model {model_ref!r} has an invalid model ID")
        if provider == "opencode" and not model_id.endswith("-free"):
            raise ValueError("the unauthenticated opencode endpoint only accepts -free models")
        assignments.append(
            _Assignment(
                f"seat-{index:03d}", name, model_ref, _ENDPOINTS[provider], model_id
            )
        )
    return tuple(assignments)


def _validate_limits(
    population: int,
    days: int,
    max_calls: int,
    max_completion_tokens: int,
    temperature: float,
    timeout_seconds: float,
) -> None:
    if days < 1:
        raise ValueError("days must be positive")
    if population * days > max_calls:
        raise ValueError(
            f"this run could require {population * days} model calls, "
            f"above --max-calls {max_calls}"
        )
    if not 1 <= max_completion_tokens <= MODEL_MAX_COMPLETION_TOKENS:
        raise ValueError(
            f"max_completion_tokens must be from 1 through {MODEL_MAX_COMPLETION_TOKENS}"
        )
    if not 0.0 <= temperature <= 2.0:
        raise ValueError("temperature must be from 0 through 2")
    if not 1.0 <= timeout_seconds <= 300.0:
        raise ValueError("timeout_seconds must be from 1 through 300")


def _parse_envelope(raw_body: str) -> tuple[str, dict[str, object]]:
    try:
        payload = _strict_json(raw_body)
    except (json.JSONDecodeError, ValueError) as error:
        raise EnvelopeFailure(
            "provider_envelope_error", "provider response is not valid strict JSON"
        ) from error
    if not isinstance(payload, Mapping):
        raise EnvelopeFailure("provider_envelope_error", "provider response must be an object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise EnvelopeFailure("provider_envelope_error", "provider response has no choice object")
    choice = choices[0]
    finish_reason = choice.get("finish_reason")
    if finish_reason == "length":
        raise EnvelopeFailure(
            "completion_budget_exhausted",
            "model exhausted its completion budget before finishing an answer",
        )
    message = choice.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, str):
        raise EnvelopeFailure(
            "provider_envelope_error", "provider response message content must be text"
        )
    provider_model = payload.get("model")
    usage = payload.get("usage")
    return content, {
        "provider_model": provider_model if isinstance(provider_model, str) else None,
        "finish_reason": finish_reason if isinstance(finish_reason, str) else None,
        "usage": dict(usage) if isinstance(usage, Mapping) else {},
    }


def _error_receipt(response: TransportResponse) -> dict[str, object]:
    body_bytes = response.body.encode("utf-8")
    receipt: dict[str, object] = {
        "http_status": response.status,
        "request_id": _request_id(response.headers),
        "body_bytes": len(body_bytes),
        "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
    }
    return receipt


def _provider_summary(calls: Sequence[Mapping[str, Any]]) -> dict[str, object]:
    succeeded = [call for call in calls if call.get("status") == "succeeded"]
    return {
        "calls_attempted": len(calls),
        "calls_succeeded": len(succeeded),
        "calls_failed": len(calls) - len(succeeded),
        "responses_with_validation_errors": sum(
            any(validation.values())
            for call in succeeded
            if isinstance((validation := call.get("validation")), Mapping)
        ),
        "action_validation_errors": sum(
            validation.get("action_error") is not None
            for call in succeeded
            if isinstance((validation := call.get("validation")), Mapping)
        ),
        "speech_validation_errors": sum(
            validation.get("speech_error") is not None
            for call in succeeded
            if isinstance((validation := call.get("validation")), Mapping)
        ),
        "provider_reported_usage": {
            "prompt_tokens": _sum_usage(succeeded, "prompt_tokens"),
            "completion_tokens": _sum_usage(succeeded, "completion_tokens"),
            "reasoning_tokens": _sum_usage(succeeded, "reasoning_tokens"),
            "total_tokens": _sum_usage(succeeded, "total_tokens"),
        },
    }


def _sum_usage(calls: Sequence[Mapping[str, Any]], field_name: str) -> int | None:
    values = []
    for call in calls:
        response = call.get("response")
        usage = response.get("usage") if isinstance(response, Mapping) else None
        if field_name == "reasoning_tokens" and isinstance(usage, Mapping):
            usage = usage.get("completion_tokens_details")
        value = usage.get(field_name) if isinstance(usage, Mapping) else None
        if type(value) is not int or value < 0:
            return None
        values.append(value)
    return sum(values) if values else None


def _request_id(headers: Mapping[str, str]) -> str | None:
    return next(
        (headers[name] for name in ("x-request-id", "request-id", "cf-ray") if headers.get(name)),
        None,
    )


def _strict_json(raw_json: str) -> object:
    return json.loads(
        raw_json,
        object_pairs_hook=_unique_object,
        parse_constant=lambda value: (_raise_invalid_constant(value)),
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key {key!r}")
        result[key] = value
    return result


def _raise_invalid_constant(value: str) -> NoReturn:
    raise ValueError(f"invalid JSON constant {value!r}")
