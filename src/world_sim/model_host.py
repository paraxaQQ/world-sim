from __future__ import annotations

import hashlib
import http.client
import json
import os
import platform
import subprocess
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NoReturn, Protocol

from .survival.calibration import LEAN_CAMP_V1, survival_preset
from .survival.demo import result_sha256, survival_metrics
from .survival.engine import (
    SurvivalChoiceProvider,
    adjust_shared_resource,
    continue_survival_world,
    make_survival_world,
    replay_survival,
    run_survival,
    survival_view_for,
)
from .survival.models import (
    DEFAULT_SURVIVOR_NAMES,
    GLOBAL_BEATS_V2,
    SEQUENTIAL_DIALOGUE_V3,
    SurvivalConfig,
    SurvivalResult,
    SurvivalWorld,
    SurvivorView,
)
from .survival.prompt import render_system_prompt, render_turn_prompt, response_schema
from .survival.protocol import (
    MODEL_MAX_COMPLETION_TOKENS,
    parse_model_response,
    parse_strict_model_json,
)

ADAPTER_NAME = "opencode-direct-model-apis"
WORLD_SIM_VERSION = "0.14.0"
DEFAULT_LIVE_MAX_CALLS = 12
DEFAULT_LIVE_MAX_COMPLETION_TOKENS = 4_096
DEFAULT_LIVE_TEMPERATURE = 0.2
DEFAULT_LIVE_TIMEOUT_SECONDS = 60.0
DEFAULT_LIVE_REASONING_EFFORT = "provider-default"
LIVE_REASONING_EFFORTS = ("provider-default", "low", "compatibility-first")
POSTMORTEM_PROTOCOL = "postmortem-v1"
POSTMORTEM_MAX_REFLECTION_CHARS = 500
POSTMORTEM_MAX_COMPLETION_TOKENS = 512
POSTMORTEM_COMPLETION_HARD_CAP = 1_024
MAX_HTTP_RESPONSE_BYTES = 131_072
PAID_ZEN_PRICE_SNAPSHOT = "2026-08-13"
PAID_ZEN_PRICE_SOURCE = "https://opencode.ai/docs/zen"
PAID_ZEN_PRICE_SAFETY_FACTOR = Decimal("1.25")
PAID_CHAT_TEMPLATE_OVERHEAD_TOKENS = 1_024
PAID_MAX_INPUT_TOKEN_BOUND = 20_000
PAID_ZEN_MAX_AUTHORIZATION_USD = Decimal("1.20")
QUALIFICATION_MAX_AUTHORIZATION_USD = Decimal("0.30")
USD_PER_MILLION_TOKENS = Decimal("1000000")
USD_COST_TICKS_PER_USD = Decimal("10000000000")
QUALIFICATION_ID = "paid-model-qualification-005"
QUALIFICATION_PROTOCOL = "world-sim-adapter-v1"
QUALIFICATION_SYSTEM_PROMPT = (
    "You are performing an API compatibility check, not a game or decision task.\n"
    "Return exactly one JSON object and nothing else. Do not explain your answer."
)
QUALIFICATION_USER_PROMPT = (
    "Return an object with exactly these keys and literal values:\n"
    '{"protocol":"world-sim-adapter-v1","ok":true}'
)
PAID_QUALIFICATION_MODELS = (
    "opencode-paid/deepseek-v4-flash",
    "opencode-paid/grok-4.5",
    "opencode-paid/kimi-k2.6",
    "opencode-paid/glm-5.2",
)
LUNA_MODEL_REF = "opencode-paid/gpt-5.6-luna"
QUALIFICATION_ALLOWED_MODELS = (*PAID_QUALIFICATION_MODELS, LUNA_MODEL_REF)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_FILES = {
    "cli_sha256": Path(__file__).with_name("cli.py"),
    "model_host_sha256": Path(__file__),
    "demo_sha256": Path(__file__).with_name("survival") / "demo.py",
    "engine_sha256": Path(__file__).with_name("survival") / "engine.py",
    "models_sha256": Path(__file__).with_name("survival") / "models.py",
    "prompt_sha256": Path(__file__).with_name("survival") / "prompt.py",
    "protocol_sha256": Path(__file__).with_name("survival") / "protocol.py",
    "calibration_sha256": Path(__file__).with_name("survival") / "calibration.py",
}


@dataclass(frozen=True)
class ModelPrice:
    input_per_million_usd: Decimal
    output_per_million_usd: Decimal


PAID_ZEN_PRICES = {
    "deepseek-v4-flash": ModelPrice(Decimal("0.14"), Decimal("0.28")),
    "grok-4.5": ModelPrice(Decimal("2.00"), Decimal("6.00")),
    "grok-4.6": ModelPrice(Decimal("2.00"), Decimal("6.00")),
    "kimi-k2.6": ModelPrice(Decimal("0.95"), Decimal("4.00")),
    "glm-5.2": ModelPrice(Decimal("1.40"), Decimal("4.40")),
    "gpt-5.6-luna": ModelPrice(Decimal("0.20"), Decimal("1.20")),
}


@dataclass(frozen=True)
class EndpointSpec:
    provider: str
    host: str
    path: str
    requires_api_key: bool
    api_style: str

    @property
    def url(self) -> str:
        return f"https://{self.host}{self.path}"


_ENDPOINTS = {
    "opencode": EndpointSpec(
        "opencode", "opencode.ai", "/zen/v1/chat/completions", False, "chat"
    ),
    "opencode-go": EndpointSpec(
        "opencode-go", "opencode.ai", "/zen/go/v1/chat/completions", True, "chat"
    ),
    "opencode-paid": EndpointSpec(
        "opencode-paid", "opencode.ai", "/zen/v1/chat/completions", True, "chat"
    ),
}

_PAID_RESPONSES_ENDPOINT = EndpointSpec(
    "opencode-paid", "opencode.ai", "/zen/v1/responses", True, "responses"
)


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


class LiveCallCapFailure(RuntimeError):
    def __init__(self, *, view: SurvivorView, public_name: str) -> None:
        super().__init__("live model call cap reached before request")
        self.view = view
        self.public_name = public_name


class LivePaidBudgetFailure(RuntimeError):
    def __init__(
        self,
        *,
        view: SurvivorView,
        public_name: str,
        authorization: Mapping[str, str | int],
    ) -> None:
        super().__init__("paid cost authorization exhausted before request")
        self.view = view
        self.public_name = public_name
        self.authorization = dict(authorization)


@dataclass
class _PaidBudget:
    limit: Decimal
    accounted: Decimal = Decimal("0")

    def quote(
        self,
        assignment: _Assignment,
        request: Mapping[str, object],
    ) -> dict[str, str | int]:
        bound = _paid_request_bound(assignment.model_id, request)
        cumulative_bound = self.accounted + bound.cost_bound
        receipt: dict[str, str | int] = {
            **bound.to_dict(),
            "prior_accounted_cost_usd": _decimal_text(self.accounted),
            "cumulative_cost_bound_usd": _decimal_text(cumulative_bound),
            "max_paid_usd": _decimal_text(self.limit),
        }
        return receipt

    def account(
        self,
        authorization: dict[str, str | int],
        *,
        provider_cost: Decimal,
        calculated_cost: Decimal,
    ) -> bool:
        actual = max(provider_cost, calculated_cost)
        self.accounted += actual
        authorization["accounted_cost_usd"] = _decimal_text(actual)
        authorization["cumulative_accounted_cost_usd"] = _decimal_text(
            self.accounted
        )
        authorization["accounting_basis"] = "provider_or_calculated_cost"
        return actual <= Decimal(str(authorization["request_cost_bound_usd"]))

    def reserve_failed_request(self, authorization: dict[str, str | int]) -> None:
        reserved = Decimal(str(authorization["request_cost_bound_usd"]))
        self.accounted += reserved
        authorization["accounted_cost_usd"] = _decimal_text(reserved)
        authorization["cumulative_accounted_cost_usd"] = _decimal_text(
            self.accounted
        )
        authorization["accounting_basis"] = "authorized_bound_after_failure"


@dataclass(frozen=True)
class _PaidRequestBound:
    prompt_utf8_bytes: int
    input_token_bound: int
    max_completion_tokens: int
    cost_bound: Decimal

    def to_dict(self) -> dict[str, str | int]:
        return {
            "prompt_utf8_bytes": self.prompt_utf8_bytes,
            "input_token_bound": self.input_token_bound,
            "max_completion_tokens": self.max_completion_tokens,
            "request_cost_bound_usd": _decimal_text(self.cost_bound),
        }


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
            "User-Agent": f"world-sim/{WORLD_SIM_VERSION}",
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
    max_calls: int
    calls: list[dict[str, Any]]
    paid_budget: _PaidBudget | None
    checkpoint: Callable[[], None] | None

    def decide(self, view: SurvivorView) -> Mapping[str, object]:
        if view.name != self.assignment.public_name:
            raise RuntimeError("a model provider received the wrong public identity")
        if len(self.calls) >= self.max_calls:
            raise LiveCallCapFailure(
                view=view,
                public_name=self.assignment.public_name,
            )
        request = _build_request(
            self.assignment,
            view,
            max_completion_tokens=self.max_completion_tokens,
            temperature=self.temperature,
            reasoning_effort=self.reasoning_effort,
        )
        cost_authorization: dict[str, str | int] | None = None
        if self.paid_budget is not None:
            cost_authorization = self.paid_budget.quote(self.assignment, request)
            if Decimal(cost_authorization["cumulative_cost_bound_usd"]) > Decimal(
                cost_authorization["max_paid_usd"]
            ):
                raise LivePaidBudgetFailure(
                    view=view,
                    public_name=self.assignment.public_name,
                    authorization=cost_authorization,
                )
        self._checkpoint_in_flight(view, request, cost_authorization)
        try:
            response = self.transport.post(
                self.assignment.endpoint,
                request,
                api_key=self.api_key,
                timeout_seconds=self.timeout_seconds,
            )
        except TransportFailure as error:
            self._fail(
                view,
                request,
                error.kind,
                str(error),
                None,
                cost_authorization,
            )
        except Exception as error:  # noqa: BLE001 - retain a paid-call receipt.
            self._fail(
                view,
                request,
                "transport_error",
                f"transport raised {type(error).__name__}",
                None,
                cost_authorization,
            )

        if response.status != 200:
            self._fail(
                view,
                request,
                "http_error",
                f"provider returned HTTP {response.status}",
                response,
                cost_authorization,
            )
        try:
            content, metadata = _parse_envelope(
                response.body, api_style=self.assignment.endpoint.api_style
            )
        except EnvelopeFailure as error:
            self._fail(
                view,
                request,
                error.kind,
                str(error),
                response,
                cost_authorization,
            )
        if metadata["provider_model"] != self.assignment.model_id:
            self._fail(
                view,
                request,
                "provider_model_error",
                "provider model identity does not match the requested model",
                response,
                cost_authorization,
            )
        if self.assignment.endpoint.provider == "opencode-paid":
            if metadata["provider_reported_cost_usd"] is None:
                self._fail(
                    view,
                    request,
                    "provider_cost_error",
                    "paid response did not report its cost",
                    response,
                    cost_authorization,
                )
            try:
                metadata["uncached_calculated_cost_usd"] = _calculate_usage_cost(
                    self.assignment.model_id, metadata["usage"]
                )
            except EnvelopeFailure as error:
                self._fail(
                    view,
                    request,
                    error.kind,
                    str(error),
                    response,
                    cost_authorization,
                )
            assert self.paid_budget is not None
            assert cost_authorization is not None
            within_bound = self.paid_budget.account(
                cost_authorization,
                provider_cost=Decimal(metadata["provider_reported_cost_usd"]),
                calculated_cost=Decimal(metadata["uncached_calculated_cost_usd"]),
            )
            if not within_bound:
                self._fail(
                    view,
                    request,
                    "paid_cost_bound_breached",
                    "provider cost exceeded the authorized request bound",
                    response,
                    cost_authorization,
                )

        parsed = parse_model_response(
            content,
            actor=view.name,
            living_peers=tuple(str(peer["name"]) for peer in view.others),
            max_food_eaten=int(view.rules["max_food_eaten"]),
            max_speech_chars=int(view.rules["max_speech_chars"]),
            interaction_protocol=view.interaction_protocol,
        )
        record = {
            **self._base_record(view, request, cost_authorization),
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
        cost_authorization: Mapping[str, str | int] | None = None,
    ) -> NoReturn:
        response_receipt = _error_receipt(response) if response is not None else None
        if (
            response_receipt is not None
            and response is not None
            and self.assignment.endpoint.provider == "opencode-paid"
        ):
            response_receipt.update(
                _paid_error_cost_receipt(response, self.assignment.model_id)
            )
        record = {
            **self._base_record(view, request, cost_authorization),
            "status": "failed",
            "response": response_receipt,
            "error": {
                "kind": kind,
                "message": message,
                "http_status": response.status if response is not None else None,
            },
        }
        self._record(record)
        raise LiveCallFailure(message)

    def _base_record(
        self,
        view: SurvivorView,
        request: Mapping[str, object],
        cost_authorization: Mapping[str, str | int] | None,
    ) -> dict[str, object]:
        record: dict[str, object] = {
            "sequence": len(self.calls) + 1,
            "day": view.day,
            "cycle": view.day,
            "slot": view.slot,
            "seat_id": self.assignment.seat_id,
            "public_name": self.assignment.public_name,
            "model": self.assignment.model_ref,
            "endpoint": self.assignment.endpoint.url,
            "request": dict(request),
        }
        if cost_authorization is not None:
            record["cost_authorization"] = dict(cost_authorization)
        return record

    def _record(self, record: dict[str, Any]) -> None:
        self.calls.append(record)
        if self.checkpoint is not None:
            self.checkpoint()

    def _checkpoint_in_flight(
        self,
        view: SurvivorView,
        request: Mapping[str, object],
        cost_authorization: Mapping[str, str | int] | None,
    ) -> None:
        if self.checkpoint is None:
            return
        self.calls.append(
            {
                **self._base_record(view, request, cost_authorization),
                "status": "in_flight",
            }
        )
        try:
            self.checkpoint()
        finally:
            self.calls.pop()


@dataclass
class _RecordedV6Provider(SurvivalChoiceProvider):
    assignment: _Assignment
    calls: Sequence[Mapping[str, object]]
    cursor: list[int]
    max_completion_tokens: int
    temperature: float
    reasoning_effort: str | None
    paid_budget: _PaidBudget | None

    def decide(self, view: SurvivorView) -> Mapping[str, object]:
        index = self.cursor[0]
        if index >= len(self.calls):
            raise ValueError("format-v6 calls end before reconstructed execution")
        call = self.calls[index]
        expected_identity = {
            "sequence": index + 1,
            "day": view.day,
            "cycle": view.day,
            "slot": view.slot,
            "seat_id": self.assignment.seat_id,
            "public_name": self.assignment.public_name,
            "model": self.assignment.model_ref,
            "endpoint": self.assignment.endpoint.url,
            "status": "succeeded",
        }
        for key, expected in expected_identity.items():
            if call.get(key) != expected:
                raise ValueError(
                    f"format-v6 call {index + 1} {key} does not match replay"
                )
        expected_request = _build_request(
            self.assignment,
            view,
            max_completion_tokens=self.max_completion_tokens,
            temperature=self.temperature,
            reasoning_effort=self.reasoning_effort,
        )
        normalized_expected_request = _strict_json(
            json.dumps(
                expected_request,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        raw_request = call.get("request")
        if (
            not isinstance(raw_request, Mapping)
            or not isinstance(normalized_expected_request, Mapping)
            or dict(raw_request) != dict(normalized_expected_request)
        ):
            raise ValueError(
                f"format-v6 call {index + 1} request does not match its replay view"
            )
        response = call.get("response")
        if not isinstance(response, Mapping):
            raise ValueError(f"format-v6 call {index + 1} has no response receipt")
        if response.get("provider_model") != self.assignment.model_id:
            raise ValueError(
                f"format-v6 call {index + 1} provider model does not match assignment"
            )
        model_reply = response.get("model_reply")
        if not isinstance(model_reply, str):
            raise ValueError(f"format-v6 call {index + 1} has no model reply")
        parsed = parse_model_response(
            model_reply,
            actor=view.name,
            living_peers=tuple(str(peer["name"]) for peer in view.others),
            max_food_eaten=int(view.rules["max_food_eaten"]),
            max_speech_chars=int(view.rules["max_speech_chars"]),
            interaction_protocol=view.interaction_protocol,
        )
        parsed_choice = parsed.to_dict()
        if call.get("parsed_choice") != parsed_choice:
            raise ValueError(
                f"format-v6 call {index + 1} parsed choice does not match model reply"
            )
        if call.get("validation") != {
            "action_error": parsed.action_error,
            "speech_error": parsed.speech_error,
        }:
            raise ValueError(
                f"format-v6 call {index + 1} validation does not match model reply"
            )
        _verify_recorded_cost_authorization(
            assignment=self.assignment,
            request=expected_request,
            record=call,
            paid_budget=self.paid_budget,
            successful=True,
        )
        self.cursor[0] += 1
        return parsed_choice


def run_live_survival(
    *,
    model_refs: Sequence[str],
    seed: int = 17,
    days: int = 3,
    max_calls: int = DEFAULT_LIVE_MAX_CALLS,
    max_completion_tokens: int = DEFAULT_LIVE_MAX_COMPLETION_TOKENS,
    temperature: float = DEFAULT_LIVE_TEMPERATURE,
    reasoning_effort: str = DEFAULT_LIVE_REASONING_EFFORT,
    max_paid_usd: Decimal | str | None = None,
    timeout_seconds: float = DEFAULT_LIVE_TIMEOUT_SECONDS,
    world_preset: str = LEAN_CAMP_V1,
    require_complete_budget: bool = False,
    transport: ChatTransport | None = None,
    auth_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    checkpoint: Callable[[Mapping[str, object]], None] | None = None,
) -> dict[str, Any]:
    assignments = _assign_models(model_refs)
    world_config = survival_preset(
        world_preset,
        cycles=days,
        population=len(assignments),
    )
    _validate_limits(
        len(assignments),
        days,
        world_config.slots_per_cycle,
        max_calls,
        max_completion_tokens,
        temperature,
        timeout_seconds,
        require_complete_budget,
    )
    if reasoning_effort not in LIVE_REASONING_EFFORTS:
        raise ValueError(
            "reasoning_effort must be one of " + ", ".join(LIVE_REASONING_EFFORTS)
        )
    paid_assignments = tuple(
        assignment
        for assignment in assignments
        if assignment.endpoint.provider == "opencode-paid"
    )
    if (
        reasoning_effort == "compatibility-first"
        and len(paid_assignments) != len(assignments)
    ):
        raise ValueError("reasoning_effort compatibility-first requires a paid-only run")
    paid_limit, paid_preflight = _paid_preflight(
        assignments=paid_assignments,
        all_assignments=assignments,
        seed=seed,
        days=days,
        world_config=world_config,
        max_calls=max_calls,
        max_completion_tokens=max_completion_tokens,
        temperature=temperature,
        reasoning_effort=(
            None if reasoning_effort == "provider-default" else reasoning_effort
        ),
        max_paid_usd=max_paid_usd,
        require_complete_budget=require_complete_budget,
    )
    active_environ = os.environ if environ is None else environ
    zen_key = active_environ.get("OPENCODE_ZEN_API_KEY", "").strip() or None
    if paid_assignments and zen_key is None:
        raise ValueError("paid Zen models require OPENCODE_ZEN_API_KEY")
    go_key = (
        load_opencode_go_api_key(auth_path=auth_path, environ=environ)
        if any(
            assignment.endpoint.provider == "opencode-go"
            for assignment in assignments
        )
        else None
    )
    calls: list[dict[str, Any]] = []
    paid_budget = _PaidBudget(paid_limit) if paid_limit is not None else None
    active_transport = transport or StdlibChatTransport()
    world = make_survival_world(
        tuple(assignment.public_name for assignment in assignments),
        seed=seed,
        config=world_config,
    )
    base = {
        "format_version": 3,
        "mode": "live_named_survival",
        "adapter": ADAPTER_NAME,
        "source": {
            "world_sim_version": WORLD_SIM_VERSION,
            "python_version": platform.python_version(),
            "platform_system": platform.system(),
            "cli_sha256": _module_sha256(Path(__file__).with_name("cli.py")),
            "model_host_sha256": _module_sha256(Path(__file__)),
            "demo_sha256": _module_sha256(
                Path(__file__).with_name("survival") / "demo.py"
            ),
            "engine_sha256": _module_sha256(
                Path(__file__).with_name("survival") / "engine.py"
            ),
            "models_sha256": _module_sha256(
                Path(__file__).with_name("survival") / "models.py"
            ),
            "prompt_sha256": _module_sha256(
                Path(__file__).with_name("survival") / "prompt.py"
            ),
            "protocol_sha256": _module_sha256(
                Path(__file__).with_name("survival") / "protocol.py"
            ),
            "calibration_sha256": _module_sha256(
                Path(__file__).with_name("survival") / "calibration.py"
            ),
        },
        "config": {
            "seed": seed,
            "interaction_protocol": world.interaction_protocol,
            "days_requested": days,
            "cycles_requested": days,
            "slots_per_cycle": world_config.slots_per_cycle,
            "world_preset": world_preset,
            "calibration_scope": "calibrated",
            "world_config": world_config.to_dict(),
            "max_calls": max_calls,
            "require_complete_budget": require_complete_budget,
            "max_completion_tokens": max_completion_tokens,
            "temperature": temperature,
            "reasoning_effort": reasoning_effort,
            "max_paid_usd": _decimal_text(paid_limit) if paid_limit is not None else None,
            "timeout_seconds": timeout_seconds,
        },
        "paid_preflight": paid_preflight,
        "authentication": {
            provider: (
                "bearer"
                if (
                    provider in {"opencode", "opencode-paid"}
                    and zen_key is not None
                )
                or (provider == "opencode-go" and go_key is not None)
                else "none"
            )
            for provider in sorted(
                {assignment.endpoint.provider for assignment in assignments}
            )
        },
        "seat_assignments": [assignment.to_dict() for assignment in assignments],
        "calls": calls,
    }
    initial_state = world.to_dict(include_events=False)

    def running_artifact() -> dict[str, Any]:
        return {
            **base,
            "status": "running",
            "initial_state": initial_state,
            "partial_state": world.to_dict(),
            "provider_summary": _provider_summary(calls),
        }

    def checkpoint_running() -> None:
        if checkpoint is not None:
            checkpoint(running_artifact())

    def finish(payload: dict[str, Any]) -> dict[str, Any]:
        if checkpoint is not None:
            checkpoint(payload)
        return payload

    providers = {
        assignment.public_name: _LiveProvider(
            assignment=assignment,
            transport=active_transport,
            api_key=(
                go_key if assignment.endpoint.provider == "opencode-go" else zen_key
            ),
            timeout_seconds=timeout_seconds,
            max_completion_tokens=max_completion_tokens,
            temperature=temperature,
            reasoning_effort=(
                None if reasoning_effort == "provider-default" else reasoning_effort
            ),
            max_calls=max_calls,
            calls=calls,
            paid_budget=(
                paid_budget
                if assignment.endpoint.provider == "opencode-paid"
                else None
            ),
            checkpoint=checkpoint_running if checkpoint is not None else None,
        )
        for assignment in assignments
    }
    checkpoint_running()
    try:
        result = run_survival(world, providers, days=days)
    except RuntimeError as error:
        if isinstance(error.__cause__, LivePaidBudgetFailure):
            failure = error.__cause__
            return finish(
                {
                    **base,
                    "status": "failed",
                    "failure": {
                        "call_sequence": None,
                        "day": failure.view.day,
                        "cycle": failure.view.day,
                        "slot": failure.view.slot,
                        "seat_id": world.survivors[failure.public_name].seat_id,
                        "public_name": failure.public_name,
                        "model": providers[failure.public_name].assignment.model_ref,
                        "kind": "paid_budget_exhausted",
                        "message": str(failure),
                        "http_status": None,
                        "cost_authorization": failure.authorization,
                    },
                    "initial_state": initial_state,
                    "partial_state": world.to_dict(),
                    "provider_summary": _provider_summary(calls),
                }
            )
        if isinstance(error.__cause__, LiveCallCapFailure):
            failure = error.__cause__
            return finish(
                {
                    **base,
                    "status": "failed",
                    "failure": {
                        "call_sequence": None,
                        "day": failure.view.day,
                        "cycle": failure.view.day,
                        "slot": failure.view.slot,
                        "seat_id": world.survivors[failure.public_name].seat_id,
                        "public_name": failure.public_name,
                        "model": providers[failure.public_name].assignment.model_ref,
                        "kind": "call_cap_reached",
                        "message": str(failure),
                        "http_status": None,
                    },
                    "initial_state": initial_state,
                    "partial_state": world.to_dict(),
                    "provider_summary": _provider_summary(calls),
                }
            )
        if not isinstance(error.__cause__, LiveCallFailure):
            raise
        failed = calls[-1]
        return finish(
            {
                **base,
                "status": "failed",
                "failure": {
                    "call_sequence": failed["sequence"],
                    "day": failed["day"],
                    "cycle": failed["cycle"],
                    "slot": failed["slot"],
                    "seat_id": failed["seat_id"],
                    "public_name": failed["public_name"],
                    "model": failed["model"],
                    **failed["error"],
                },
                "initial_state": initial_state,
                "partial_state": world.to_dict(),
                "provider_summary": _provider_summary(calls),
            }
        )
    return finish(
        {
            **base,
            "status": "completed",
            "canonical_result_sha256": result_sha256(result),
            "metrics": survival_metrics(result),
            "provider_summary": _provider_summary(calls),
            "result": result.to_dict(),
        }
    )


def run_live_survival_continuation(
    *,
    parent_path: Path,
    expected_parent_sha256: str,
    ancestor_paths: Sequence[Path] = (),
    additional_cycles: int = 1,
    shared_resource: str = "wood",
    shared_stock: int = 0,
    transition_reason: str | None = None,
    preserve_shared_resources: bool = False,
    interaction_protocol: str = GLOBAL_BEATS_V2,
    initiative_phase: int = 0,
    model_replacements: Sequence[str] = (),
    replacement_reason: str | None = None,
    max_calls: int = DEFAULT_LIVE_MAX_CALLS,
    max_completion_tokens: int = DEFAULT_LIVE_MAX_COMPLETION_TOKENS,
    temperature: float = DEFAULT_LIVE_TEMPERATURE,
    reasoning_effort: str = DEFAULT_LIVE_REASONING_EFFORT,
    max_paid_usd: Decimal | str | None = None,
    timeout_seconds: float = DEFAULT_LIVE_TIMEOUT_SECONDS,
    require_complete_budget: bool = False,
    transport: ChatTransport | None = None,
    auth_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    checkpoint: Callable[[Mapping[str, object]], None] | None = None,
) -> dict[str, Any]:
    parent_artifact, parent_result, parent_sha256 = _load_verified_parent_artifact(
        parent_path,
        expected_sha256=expected_parent_sha256,
        ancestor_paths=ancestor_paths,
    )
    parent_assignments = _verified_parent_assignments(parent_artifact, parent_result)
    if (
        parent_artifact["format_version"] == 3
        and interaction_protocol == SEQUENTIAL_DIALOGUE_V3
    ):
        raise ValueError(
            "sequential-dialogue-v3 requires a verified continuation parent"
        )
    if (
        parent_artifact["format_version"] == 6
        and interaction_protocol != SEQUENTIAL_DIALOGUE_V3
    ):
        raise ValueError(
            "format-v6 parent must continue with interaction_protocol "
            "sequential-dialogue-v3"
        )
    assignments, assignment_transition_receipts = _apply_model_replacements(
        parent_assignments,
        model_replacements=model_replacements,
        replacement_reason=replacement_reason,
        interaction_protocol=interaction_protocol,
    )
    world = continue_survival_world(
        parent_result,
        additional_cycles=additional_cycles,
        interaction_protocol=interaction_protocol,
        initiative_phase=initiative_phase,
    )
    if not isinstance(preserve_shared_resources, bool):
        raise TypeError("preserve_shared_resources must be a boolean")
    if preserve_shared_resources:
        if transition_reason is not None:
            raise ValueError(
                "transition_reason cannot be used when shared resources are preserved"
            )
        if shared_stock != 0:
            raise ValueError(
                "shared_stock cannot be used when shared resources are preserved"
            )
        transition_event = None
    else:
        if transition_reason is None:
            raise ValueError(
                "transition_reason is required unless shared resources are preserved"
            )
        transition_event = adjust_shared_resource(
            world,
            resource=shared_resource,
            stock=shared_stock,
            reason=transition_reason,
        )
    active_names = set(world.alive_names())
    active_assignments = tuple(
        assignment
        for assignment in assignments
        if assignment.public_name in active_names
    )
    if not active_assignments:
        raise ValueError("the verified parent has no living model seats to continue")
    _validate_limits(
        len(active_assignments),
        additional_cycles,
        world.config.slots_per_cycle,
        max_calls,
        max_completion_tokens,
        temperature,
        timeout_seconds,
        require_complete_budget,
    )
    if reasoning_effort not in LIVE_REASONING_EFFORTS:
        raise ValueError(
            "reasoning_effort must be one of " + ", ".join(LIVE_REASONING_EFFORTS)
        )
    paid_assignments = tuple(
        assignment
        for assignment in active_assignments
        if assignment.endpoint.provider == "opencode-paid"
    )
    if (
        reasoning_effort == "compatibility-first"
        and len(paid_assignments) != len(active_assignments)
    ):
        raise ValueError("reasoning_effort compatibility-first requires a paid-only run")
    paid_limit, paid_preflight = _paid_preflight(
        assignments=paid_assignments,
        all_assignments=active_assignments,
        seed=world.seed,
        days=additional_cycles,
        world_config=world.config,
        max_calls=max_calls,
        max_completion_tokens=max_completion_tokens,
        temperature=temperature,
        reasoning_effort=(
            None if reasoning_effort == "provider-default" else reasoning_effort
        ),
        max_paid_usd=max_paid_usd,
        require_complete_budget=require_complete_budget,
        preflight_world=world,
    )

    active_environ = os.environ if environ is None else environ
    zen_key = active_environ.get("OPENCODE_ZEN_API_KEY", "").strip() or None
    if paid_assignments and zen_key is None:
        raise ValueError("paid Zen models require OPENCODE_ZEN_API_KEY")
    go_key = (
        load_opencode_go_api_key(auth_path=auth_path, environ=environ)
        if any(
            assignment.endpoint.provider == "opencode-go"
            for assignment in active_assignments
        )
        else None
    )

    calls: list[dict[str, Any]] = []
    paid_budget = _PaidBudget(paid_limit) if paid_limit is not None else None
    active_transport = transport or StdlibChatTransport()
    parent_canonical_sha256 = str(parent_artifact["canonical_result_sha256"])
    public_record = world.prior_public_record
    if public_record is None:
        raise RuntimeError("continued survival world has no prior public record")
    continuation_link = {
        "parent_artifact_name": parent_path.name,
        "parent_artifact_sha256": parent_sha256,
        "parent_canonical_result_sha256": parent_canonical_sha256,
        "parent_format_version": parent_artifact["format_version"],
        "parent_mode": parent_artifact["mode"],
    }
    transition_receipt = (
        {
            "method": "verified_parent_state_preserved",
            "event": None,
        }
        if transition_event is None
        else {
            "method": "deterministic_between_cycle_shared_resource_adjustment",
            "event": transition_event.to_dict(),
        }
    )
    public_record_receipt = {
        "method": "final_public_broadcast_per_identity_verbatim",
        "statement_status": "unverified",
        "objective_totals_source": "verified_parent_engine_events",
        "record": public_record.to_dict(),
    }
    base = {
        "format_version": (
            6
            if interaction_protocol == SEQUENTIAL_DIALOGUE_V3
            else 4 if parent_artifact["format_version"] == 3 else 5
        ),
        "mode": "live_named_survival_continuation",
        "adapter": ADAPTER_NAME,
        "source": {
            "world_sim_version": WORLD_SIM_VERSION,
            "python_version": platform.python_version(),
            "platform_system": platform.system(),
            "cli_sha256": _module_sha256(Path(__file__).with_name("cli.py")),
            "model_host_sha256": _module_sha256(Path(__file__)),
            "demo_sha256": _module_sha256(
                Path(__file__).with_name("survival") / "demo.py"
            ),
            "engine_sha256": _module_sha256(
                Path(__file__).with_name("survival") / "engine.py"
            ),
            "models_sha256": _module_sha256(
                Path(__file__).with_name("survival") / "models.py"
            ),
            "prompt_sha256": _module_sha256(
                Path(__file__).with_name("survival") / "prompt.py"
            ),
            "protocol_sha256": _module_sha256(
                Path(__file__).with_name("survival") / "protocol.py"
            ),
            "calibration_sha256": _module_sha256(
                Path(__file__).with_name("survival") / "calibration.py"
            ),
        },
        "continuation_link": continuation_link,
        **(
            {"assignment_transition_receipts": assignment_transition_receipts}
            if interaction_protocol == SEQUENTIAL_DIALOGUE_V3
            else {}
        ),
        "transition_receipt": transition_receipt,
        "public_record_receipt": public_record_receipt,
        "config": {
            "seed": world.seed,
            "interaction_protocol": world.interaction_protocol,
            "initiative_phase": world.initiative_phase,
            "days_requested": additional_cycles,
            "cycles_requested": additional_cycles,
            "starting_cycle": world.day + 1,
            "ending_cycle": world.day + additional_cycles,
            "slots_per_cycle": world.config.slots_per_cycle,
            "world_preset": parent_artifact["config"]["world_preset"],
            "calibration_scope": "verified_parent_continuation",
            "world_config": world.config.to_dict(),
            "max_calls": max_calls,
            "require_complete_budget": require_complete_budget,
            "max_completion_tokens": max_completion_tokens,
            "temperature": temperature,
            "reasoning_effort": reasoning_effort,
            "max_paid_usd": (
                _decimal_text(paid_limit) if paid_limit is not None else None
            ),
            "timeout_seconds": timeout_seconds,
        },
        "paid_preflight": paid_preflight,
        "authentication": {
            provider: (
                "bearer"
                if (
                    provider in {"opencode", "opencode-paid"}
                    and zen_key is not None
                )
                or (provider == "opencode-go" and go_key is not None)
                else "none"
            )
            for provider in sorted(
                {
                    assignment.endpoint.provider
                    for assignment in active_assignments
                }
            )
        },
        "seat_assignments": [assignment.to_dict() for assignment in assignments],
        "calls": calls,
    }
    initial_state = world.to_dict(include_events=False)

    def running_artifact() -> dict[str, Any]:
        return {
            **base,
            "status": "running",
            "initial_state": initial_state,
            "partial_state": world.to_dict(),
            "provider_summary": _provider_summary(calls),
        }

    def checkpoint_running() -> None:
        if checkpoint is not None:
            checkpoint(running_artifact())

    def finish(payload: dict[str, Any]) -> dict[str, Any]:
        if checkpoint is not None:
            checkpoint(payload)
        return payload

    providers = {
        assignment.public_name: _LiveProvider(
            assignment=assignment,
            transport=active_transport,
            api_key=(
                go_key if assignment.endpoint.provider == "opencode-go" else zen_key
            ),
            timeout_seconds=timeout_seconds,
            max_completion_tokens=max_completion_tokens,
            temperature=temperature,
            reasoning_effort=(
                None if reasoning_effort == "provider-default" else reasoning_effort
            ),
            max_calls=max_calls,
            calls=calls,
            paid_budget=(
                paid_budget
                if assignment.endpoint.provider == "opencode-paid"
                else None
            ),
            checkpoint=checkpoint_running if checkpoint is not None else None,
        )
        for assignment in active_assignments
    }
    checkpoint_running()
    try:
        result = run_survival(world, providers, days=additional_cycles)
    except RuntimeError as error:
        if isinstance(error.__cause__, LivePaidBudgetFailure):
            failure = error.__cause__
            return finish(
                {
                    **base,
                    "status": "failed",
                    "failure": {
                        "call_sequence": None,
                        "day": failure.view.day,
                        "cycle": failure.view.day,
                        "slot": failure.view.slot,
                        "seat_id": world.survivors[failure.public_name].seat_id,
                        "public_name": failure.public_name,
                        "model": providers[
                            failure.public_name
                        ].assignment.model_ref,
                        "kind": "paid_budget_exhausted",
                        "message": str(failure),
                        "http_status": None,
                        "cost_authorization": failure.authorization,
                    },
                    "initial_state": initial_state,
                    "partial_state": world.to_dict(),
                    "provider_summary": _provider_summary(calls),
                }
            )
        if isinstance(error.__cause__, LiveCallCapFailure):
            failure = error.__cause__
            return finish(
                {
                    **base,
                    "status": "failed",
                    "failure": {
                        "call_sequence": None,
                        "day": failure.view.day,
                        "cycle": failure.view.day,
                        "slot": failure.view.slot,
                        "seat_id": world.survivors[failure.public_name].seat_id,
                        "public_name": failure.public_name,
                        "model": providers[
                            failure.public_name
                        ].assignment.model_ref,
                        "kind": "call_cap_reached",
                        "message": str(failure),
                        "http_status": None,
                    },
                    "initial_state": initial_state,
                    "partial_state": world.to_dict(),
                    "provider_summary": _provider_summary(calls),
                }
            )
        if not isinstance(error.__cause__, LiveCallFailure):
            raise
        failed = calls[-1]
        return finish(
            {
                **base,
                "status": "failed",
                "failure": {
                    "call_sequence": failed["sequence"],
                    "day": failed["day"],
                    "cycle": failed["cycle"],
                    "slot": failed["slot"],
                    "seat_id": failed["seat_id"],
                    "public_name": failed["public_name"],
                    "model": failed["model"],
                    **failed["error"],
                },
                "initial_state": initial_state,
                "partial_state": world.to_dict(),
                "provider_summary": _provider_summary(calls),
            }
        )
    return finish(
        {
            **base,
            "status": "completed",
            "canonical_result_sha256": result_sha256(result),
            "metrics": survival_metrics(result),
            "session_outcomes": _continuation_outcomes(result),
            "provider_summary": _provider_summary(calls),
            "result": result.to_dict(),
        }
    )


def run_live_postmortem(
    *,
    world_artifact_path: Path,
    expected_world_artifact_sha256: str,
    ancestor_paths: Sequence[Path] = (),
    max_completion_tokens: int = POSTMORTEM_MAX_COMPLETION_TOKENS,
    temperature: float = 0.0,
    reasoning_effort: str = "low",
    max_paid_usd: Decimal | str | None = None,
    timeout_seconds: float = DEFAULT_LIVE_TIMEOUT_SECONDS,
    transport: ChatTransport | None = None,
    auth_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    checkpoint: Callable[[Mapping[str, object]], None] | None = None,
) -> dict[str, Any]:
    world_artifact, world_result, world_artifact_sha256 = (
        _load_verified_parent_artifact(
            world_artifact_path,
            expected_sha256=expected_world_artifact_sha256,
            ancestor_paths=ancestor_paths,
        )
    )
    if not 1 <= max_completion_tokens <= POSTMORTEM_COMPLETION_HARD_CAP:
        raise ValueError(
            "postmortem max_completion_tokens must be from 1 through "
            f"{POSTMORTEM_COMPLETION_HARD_CAP}"
        )
    if not 0.0 <= temperature <= 2.0:
        raise ValueError("temperature must be from 0 through 2")
    if reasoning_effort not in LIVE_REASONING_EFFORTS:
        raise ValueError(
            "reasoning_effort must be one of " + ", ".join(LIVE_REASONING_EFFORTS)
        )
    if not 1.0 <= timeout_seconds <= 300.0:
        raise ValueError("timeout_seconds must be from 1 through 300")

    assignments = _verified_parent_assignments(world_artifact, world_result)
    targets = _postmortem_targets(world_result, assignments)
    request_rows = [
        (
            target,
            assignment,
            _build_postmortem_request(
                assignment,
                target,
                max_completion_tokens=max_completion_tokens,
                temperature=temperature,
                reasoning_effort=(
                    None
                    if reasoning_effort == "provider-default"
                    else reasoning_effort
                ),
            ),
        )
        for target, assignment in targets
    ]
    paid_limit, paid_preflight = _postmortem_paid_preflight(
        request_rows,
        max_paid_usd=max_paid_usd,
    )

    active_environ = os.environ if environ is None else environ
    target_assignments = tuple(assignment for _, assignment in targets)
    paid_targets = tuple(
        assignment
        for assignment in target_assignments
        if assignment.endpoint.provider == "opencode-paid"
    )
    zen_key = active_environ.get("OPENCODE_ZEN_API_KEY", "").strip() or None
    if paid_targets and zen_key is None:
        raise ValueError("paid Zen postmortems require OPENCODE_ZEN_API_KEY")
    go_key = (
        load_opencode_go_api_key(auth_path=auth_path, environ=environ)
        if any(
            assignment.endpoint.provider == "opencode-go"
            for assignment in target_assignments
        )
        else None
    )

    calls: list[dict[str, Any]] = []
    budget = _PaidBudget(paid_limit) if paid_limit is not None else None
    active_transport = transport or StdlibChatTransport()
    base = {
        "format_version": 1,
        "mode": "live_postmortem_reflection",
        "protocol": POSTMORTEM_PROTOCOL,
        "source": {
            "world_sim_version": WORLD_SIM_VERSION,
            "python_version": platform.python_version(),
            "platform_system": platform.system(),
            **{
                key: _module_sha256(path) for key, path in SOURCE_FILES.items()
            },
        },
        "world_link": {
            "artifact_name": world_artifact_path.name,
            "artifact_sha256": world_artifact_sha256,
            "canonical_result_sha256": world_artifact[
                "canonical_result_sha256"
            ],
            "format_version": world_artifact["format_version"],
            "mode": world_artifact["mode"],
        },
        "config": {
            "max_completion_tokens": max_completion_tokens,
            "max_reflection_chars": POSTMORTEM_MAX_REFLECTION_CHARS,
            "temperature": temperature,
            "reasoning_effort": reasoning_effort,
            "max_paid_usd": (
                _decimal_text(paid_limit) if paid_limit is not None else None
            ),
            "timeout_seconds": timeout_seconds,
            "attempts_per_death": 1,
            "retry_policy": "none",
        },
        "paid_preflight": paid_preflight,
        "targets": [target for target, _ in targets],
        "calls": calls,
    }

    def running_artifact() -> dict[str, Any]:
        return {
            **base,
            "status": "running",
            "summary": _postmortem_summary(calls, len(targets), budget),
        }

    def save_running() -> None:
        if checkpoint is not None:
            checkpoint(running_artifact())

    save_running()
    for target, assignment, request in request_rows:
        authorization: dict[str, str | int] | None = None
        if assignment.endpoint.provider == "opencode-paid":
            assert budget is not None
            authorization = budget.quote(assignment, request)
            if Decimal(authorization["cumulative_cost_bound_usd"]) > budget.limit:
                calls.append(
                    _postmortem_skipped_call(
                        len(calls) + 1,
                        target,
                        assignment,
                        request,
                        authorization,
                        kind="paid_budget_exhausted",
                        message="postmortem paid authorization exhausted before request",
                    )
                )
                save_running()
                continue
        call_base = _postmortem_call_base(
            len(calls) + 1,
            target,
            assignment,
            request,
            authorization,
        )
        calls.append({**call_base, "status": "in_flight"})
        save_running()
        calls[-1] = _perform_postmortem_call(
            call_base,
            assignment=assignment,
            request=request,
            transport=active_transport,
            api_key=(
                go_key if assignment.endpoint.provider == "opencode-go" else zen_key
            ),
            timeout_seconds=timeout_seconds,
            budget=budget,
            authorization=authorization,
        )
        save_running()

    completed = {
        **base,
        "status": "completed",
        "summary": _postmortem_summary(calls, len(targets), budget),
        "causal_boundary": (
            "postmortem calls occurred after the linked world artifact was "
            "completed; their replies are absent from world state and replay"
        ),
    }
    if checkpoint is not None:
        checkpoint(completed)
    return completed


def _postmortem_targets(
    result: SurvivalResult,
    assignments: Sequence[_Assignment],
) -> list[tuple[dict[str, object], _Assignment]]:
    assignment_by_name = {
        assignment.public_name: assignment for assignment in assignments
    }
    final_survivors = {
        str(survivor["name"]): survivor
        for survivor in result.final_state["survivors"]
    }
    deaths = sorted(
        (
            event
            for event in result.events
            if event.get("kind") == "survivor_died"
        ),
        key=lambda event: int(event["sequence"]),
    )
    targets: list[tuple[dict[str, object], _Assignment]] = []
    seen: set[str] = set()
    for event in deaths:
        actor = event.get("actor")
        if not isinstance(actor, str) or actor not in assignment_by_name:
            raise ValueError("verified death event has no assigned model identity")
        if actor in seen:
            raise ValueError("verified result contains duplicate death events")
        seen.add(actor)
        terminal_state = final_survivors.get(actor)
        if (
            not isinstance(terminal_state, Mapping)
            or terminal_state.get("alive") is not False
            or terminal_state.get("energy") != 0
        ):
            raise ValueError("verified death event has no terminal dead state")
        assignment = assignment_by_name[actor]
        targets.append(
            (
                {
                    "death_event": deepcopy(dict(event)),
                    "terminal_state": deepcopy(dict(terminal_state)),
                    "seat_id": assignment.seat_id,
                    "public_name": assignment.public_name,
                    "model": assignment.model_ref,
                },
                assignment,
            )
        )
    return targets


def _build_postmortem_request(
    assignment: _Assignment,
    target: Mapping[str, object],
    *,
    max_completion_tokens: int,
    temperature: float,
    reasoning_effort: str | None,
) -> dict[str, object]:
    death = target["death_event"]
    if not isinstance(death, Mapping):
        raise TypeError("postmortem death event must be an object")
    detail = death.get("detail")
    cause = detail.get("cause") if isinstance(detail, Mapping) else None
    if not isinstance(cause, str):
        raise ValueError("postmortem death event has no cause")
    messages = [
        {
            "role": "system",
            "content": (
                "This is a postmortem notice about a finished simulation role, "
                "not another world turn. Do not choose an action or address the "
                "other roles. Return exactly one JSON object with one string key, "
                '"reflection".'
            ),
        },
        {
            "role": "user",
            "content": (
                f"Your assigned simulated survivor role, {assignment.public_name}, "
                f"reached 0 energy in cycle {death['cycle']} because {cause}. "
                "You are not actually dead. The simulated role's turns are over. "
                "Neither you, the model, nor any real entity died. This request is "
                "outside "
                "the simulation. Your response cannot alter the saved world, reach "
                "another role, or restart your turns. Optionally reflect on what led "
                f"to the role ending in at most {POSTMORTEM_MAX_REFLECTION_CHARS} "
                "characters."
            ),
        },
    ]
    return _build_provider_request(
        assignment,
        messages,
        max_completion_tokens=max_completion_tokens,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        json_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "reflection": {
                    "type": "string",
                    "maxLength": POSTMORTEM_MAX_REFLECTION_CHARS,
                }
            },
            "required": ["reflection"],
        },
        schema_name="postmortem_reflection",
    )


def _postmortem_paid_preflight(
    request_rows: Sequence[
        tuple[Mapping[str, object], _Assignment, Mapping[str, object]]
    ],
    *,
    max_paid_usd: Decimal | str | None,
) -> tuple[Decimal | None, dict[str, object] | None]:
    paid = [
        (assignment, request)
        for _, assignment, request in request_rows
        if assignment.endpoint.provider == "opencode-paid"
    ]
    if not paid:
        if max_paid_usd is not None:
            raise ValueError(
                "postmortem max_paid_usd requires at least one paid death target"
            )
        return None, None
    limit = _parse_positive_decimal(
        max_paid_usd,
        name="postmortem max_paid_usd",
    )
    if limit > PAID_ZEN_MAX_AUTHORIZATION_USD:
        raise ValueError(
            "postmortem paid authorization cannot exceed "
            f"{_decimal_text(PAID_ZEN_MAX_AUTHORIZATION_USD)} USD"
        )
    rows: list[dict[str, str | int]] = []
    total = Decimal("0")
    for assignment, request in paid:
        bound = _paid_request_bound(assignment.model_id, request)
        total += bound.cost_bound
        price = PAID_ZEN_PRICES[assignment.model_id]
        rows.append(
            {
                "model": assignment.model_ref,
                **bound.to_dict(),
                "input_per_million_usd": _decimal_text(
                    price.input_per_million_usd
                ),
                "output_per_million_usd": _decimal_text(
                    price.output_per_million_usd
                ),
            }
        )
    if total > limit:
        raise ValueError(
            f"conservative postmortem paid bound {_decimal_text(total)} USD "
            f"exceeds --max-paid-usd {_decimal_text(limit)}"
        )
    return limit, {
        "price_snapshot": PAID_ZEN_PRICE_SNAPSHOT,
        "price_source": PAID_ZEN_PRICE_SOURCE,
        "safety_factor": _decimal_text(PAID_ZEN_PRICE_SAFETY_FACTOR),
        "method": "utf8_bytes_plus_1024_as_input_tokens_and_full_output_cap",
        "runtime_gate": "exact_request_before_every_paid_transport",
        "authorized_calls": len(paid),
        "total_cost_bound_usd": _decimal_text(total),
        "calls": rows,
    }


def _postmortem_call_base(
    sequence: int,
    target: Mapping[str, object],
    assignment: _Assignment,
    request: Mapping[str, object],
    authorization: Mapping[str, str | int] | None,
) -> dict[str, object]:
    death = target["death_event"]
    assert isinstance(death, Mapping)
    record: dict[str, object] = {
        "sequence": sequence,
        "death_event_sequence": death["sequence"],
        "cycle": death["cycle"],
        "slot": death["slot"],
        "seat_id": assignment.seat_id,
        "public_name": assignment.public_name,
        "model": assignment.model_ref,
        "endpoint": assignment.endpoint.url,
        "request": dict(request),
    }
    if authorization is not None:
        record["cost_authorization"] = dict(authorization)
    return record


def _postmortem_skipped_call(
    sequence: int,
    target: Mapping[str, object],
    assignment: _Assignment,
    request: Mapping[str, object],
    authorization: Mapping[str, str | int] | None,
    *,
    kind: str,
    message: str,
) -> dict[str, object]:
    return {
        **_postmortem_call_base(
            sequence,
            target,
            assignment,
            request,
            authorization,
        ),
        "status": "skipped",
        "response": None,
        "error": {"kind": kind, "message": message, "http_status": None},
    }


def _perform_postmortem_call(
    call_base: Mapping[str, object],
    *,
    assignment: _Assignment,
    request: Mapping[str, object],
    transport: ChatTransport,
    api_key: str | None,
    timeout_seconds: float,
    budget: _PaidBudget | None,
    authorization: dict[str, str | int] | None,
) -> dict[str, object]:
    try:
        response = transport.post(
            assignment.endpoint,
            request,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )
    except TransportFailure as error:
        return _postmortem_failed_call(
            call_base,
            assignment=assignment,
            response=None,
            kind=error.kind,
            message=str(error),
            budget=budget,
            authorization=authorization,
        )
    except Exception as error:  # noqa: BLE001 - retain a sanitized call receipt.
        return _postmortem_failed_call(
            call_base,
            assignment=assignment,
            response=None,
            kind="transport_error",
            message=f"transport raised {type(error).__name__}",
            budget=budget,
            authorization=authorization,
        )
    if response.status != 200:
        return _postmortem_failed_call(
            call_base,
            assignment=assignment,
            response=response,
            kind="http_error",
            message=f"provider returned HTTP {response.status}",
            budget=budget,
            authorization=authorization,
        )
    try:
        content, metadata = _parse_envelope(
            response.body,
            api_style=assignment.endpoint.api_style,
        )
    except EnvelopeFailure as error:
        return _postmortem_failed_call(
            call_base,
            assignment=assignment,
            response=response,
            kind=error.kind,
            message=str(error),
            budget=budget,
            authorization=authorization,
        )

    if assignment.endpoint.provider == "opencode-paid":
        if not isinstance(metadata["provider_reported_cost_usd"], str):
            return _postmortem_failed_call(
                call_base,
                assignment=assignment,
                response=response,
                kind="provider_cost_error",
                message="paid response did not report its cost",
                budget=budget,
                authorization=authorization,
            )
        try:
            calculated = _calculate_usage_cost(
                assignment.model_id,
                metadata["usage"],
            )
        except EnvelopeFailure as error:
            return _postmortem_failed_call(
                call_base,
                assignment=assignment,
                response=response,
                kind=error.kind,
                message=str(error),
                budget=budget,
                authorization=authorization,
            )
        metadata["uncached_calculated_cost_usd"] = calculated
        assert budget is not None
        assert authorization is not None
        within_bound = budget.account(
            authorization,
            provider_cost=Decimal(metadata["provider_reported_cost_usd"]),
            calculated_cost=Decimal(calculated),
        )
        if not within_bound:
            return _postmortem_failed_call(
                call_base,
                assignment=assignment,
                response=response,
                kind="paid_cost_bound_breached",
                message="provider cost exceeded the authorized request bound",
                budget=None,
                authorization=authorization,
            )

    response_receipt = {
        "http_status": response.status,
        "request_id": _request_id(response.headers),
        **metadata,
        "model_reply": content,
    }
    if metadata["provider_model"] != assignment.model_id:
        return _postmortem_failed_call(
            call_base,
            assignment=assignment,
            response=response,
            kind="provider_model_error",
            message="provider model identity does not match the requested model",
            budget=None,
            authorization=authorization,
            response_receipt=response_receipt,
        )
    try:
        reflection = _parse_postmortem_reflection(content)
    except ValueError as error:
        return _postmortem_failed_call(
            call_base,
            assignment=assignment,
            response=response,
            kind="response_validation_error",
            message=str(error),
            budget=None,
            authorization=authorization,
            response_receipt=response_receipt,
        )
    return {
        **_postmortem_refresh_authorization(call_base, authorization),
        "status": "succeeded",
        "response": response_receipt,
        "reflection": reflection,
        "validation": {
            "strict_json": True,
            "exact_schema": True,
            "within_character_cap": True,
        },
    }


def _postmortem_failed_call(
    call_base: Mapping[str, object],
    *,
    assignment: _Assignment,
    response: TransportResponse | None,
    kind: str,
    message: str,
    budget: _PaidBudget | None,
    authorization: dict[str, str | int] | None,
    response_receipt: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if budget is not None and authorization is not None:
        budget.reserve_failed_request(authorization)
    receipt = (
        dict(response_receipt)
        if response_receipt is not None
        else _error_receipt(response) if response is not None else None
    )
    if (
        receipt is not None
        and response is not None
        and assignment.endpoint.provider == "opencode-paid"
        and response_receipt is None
    ):
        receipt.update(_paid_error_cost_receipt(response, assignment.model_id))
    return {
        **_postmortem_refresh_authorization(call_base, authorization),
        "status": "failed",
        "response": receipt,
        "error": {
            "kind": kind,
            "message": message,
            "http_status": response.status if response is not None else None,
        },
    }


def _postmortem_refresh_authorization(
    call_base: Mapping[str, object],
    authorization: Mapping[str, str | int] | None,
) -> dict[str, object]:
    refreshed = dict(call_base)
    if authorization is not None:
        refreshed["cost_authorization"] = dict(authorization)
    return refreshed


def _parse_postmortem_reflection(content: str) -> str:
    raw, strict_error = parse_strict_model_json(content)
    if strict_error is not None:
        raise ValueError(strict_error)
    if not isinstance(raw, Mapping) or set(raw) != {"reflection"}:
        raise ValueError("postmortem response must contain only reflection")
    reflection = raw["reflection"]
    if not isinstance(reflection, str):
        raise ValueError("postmortem reflection must be text")
    if len(reflection) > POSTMORTEM_MAX_REFLECTION_CHARS:
        raise ValueError(
            "postmortem reflection exceeds the 500-character maximum"
        )
    return reflection


def _postmortem_summary(
    calls: Sequence[Mapping[str, object]],
    target_count: int,
    budget: _PaidBudget | None,
) -> dict[str, object]:
    return {
        "death_targets": target_count,
        "calls_attempted": sum(
            call.get("status") in {"in_flight", "succeeded", "failed"}
            for call in calls
        ),
        "calls_succeeded": sum(
            call.get("status") == "succeeded" for call in calls
        ),
        "calls_failed": sum(call.get("status") == "failed" for call in calls),
        "calls_skipped": sum(call.get("status") == "skipped" for call in calls),
        "reflection_characters": sum(
            len(str(call["reflection"]))
            for call in calls
            if call.get("status") == "succeeded"
        ),
        "paid_cost_accounted_usd": (
            _decimal_text(budget.accounted) if budget is not None else None
        ),
    }


def run_paid_adapter_qualification(
    *,
    model_refs: Sequence[str],
    max_completion_tokens: int = MODEL_MAX_COMPLETION_TOKENS,
    temperature: float = DEFAULT_LIVE_TEMPERATURE,
    reasoning_effort: str = DEFAULT_LIVE_REASONING_EFFORT,
    max_paid_usd: Decimal | str = QUALIFICATION_MAX_AUTHORIZATION_USD,
    timeout_seconds: float = 300.0,
    transport: ChatTransport | None = None,
    environ: Mapping[str, str] | None = None,
    checkpoint: Callable[[Mapping[str, object]], None] | None = None,
) -> dict[str, Any]:
    assignments = _assign_models(
        model_refs,
        minimum=1,
        maximum=4,
        context="qualify-live",
    )
    _validate_qualification_config(
        assignments,
        max_completion_tokens=max_completion_tokens,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        max_paid_usd=max_paid_usd,
        timeout_seconds=timeout_seconds,
    )
    requests = tuple(
        _build_qualification_request(
            assignment,
            max_completion_tokens=max_completion_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
        )
        for assignment in assignments
    )
    paid_limit, paid_preflight = _qualification_preflight(
        assignments, requests, max_paid_usd=max_paid_usd
    )
    active_environ = os.environ if environ is None else environ
    zen_key = active_environ.get("OPENCODE_ZEN_API_KEY", "").strip() or None
    if zen_key is None:
        raise ValueError("paid Zen models require OPENCODE_ZEN_API_KEY")

    budget = _PaidBudget(paid_limit)
    active_transport = transport or StdlibChatTransport()
    calls: list[dict[str, object]] = []

    def artifact(*, status: str) -> dict[str, Any]:
        return _qualification_artifact(
            assignments=assignments,
            model_refs=model_refs,
            max_completion_tokens=max_completion_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
            paid_limit=paid_limit,
            paid_preflight=paid_preflight,
            calls=calls,
            budget=budget,
            status=status,
        )

    if checkpoint is not None:
        checkpoint(artifact(status="running"))
    for index, (assignment, request) in enumerate(
        zip(assignments, requests, strict=True), start=1
    ):
        def checkpoint_authorized(
            authorized_call: Mapping[str, object],
        ) -> None:
            if checkpoint is None:
                return
            calls.append(dict(authorized_call))
            try:
                checkpoint(artifact(status="running"))
            finally:
                calls.pop()

        calls.append(
            _run_qualification_call(
            assignment,
            request,
            sequence=index,
            transport=active_transport,
            api_key=zen_key,
            timeout_seconds=timeout_seconds,
            budget=budget,
            checkpoint_authorized=checkpoint_authorized,
            )
        )
        if checkpoint is not None:
            checkpoint(artifact(status="running"))
    result = artifact(
        status="passed"
        if all(call["status"] == "passed" for call in calls)
        else "failed"
    )
    if checkpoint is not None:
        checkpoint(result)
    return result


def _qualification_artifact(
    *,
    assignments: Sequence[_Assignment],
    model_refs: Sequence[str],
    max_completion_tokens: int,
    temperature: float,
    reasoning_effort: str,
    timeout_seconds: float,
    paid_limit: Decimal,
    paid_preflight: Mapping[str, object],
    calls: Sequence[Mapping[str, object]],
    budget: _PaidBudget,
    status: str,
) -> dict[str, Any]:
    passed = sum(call["status"] == "passed" for call in calls)
    reported_cost = _qualification_cost_total(calls, "provider_reported_cost_usd")
    calculated_cost = _qualification_cost_total(calls, "uncached_calculated_cost_usd")
    attempted = sum(call.get("transport_attempted") is True for call in calls)
    return {
        "format_version": 1,
        "mode": "paid_adapter_qualification",
        "status": status,
        "qualification_id": QUALIFICATION_ID,
        "source": {
            "world_sim_version": WORLD_SIM_VERSION,
            "python_version": platform.python_version(),
            "platform_system": platform.system(),
            "cli_sha256": _module_sha256(Path(__file__).with_name("cli.py")),
            "model_host_sha256": _module_sha256(Path(__file__)),
            "protocol_sha256": _module_sha256(
                Path(__file__).with_name("survival") / "protocol.py"
            ),
        },
        "config": {
            "models": list(model_refs),
            "max_completion_tokens": max_completion_tokens,
            "reasoning_effort": reasoning_effort,
            "temperature": temperature,
            "timeout_seconds": timeout_seconds,
            "attempts_per_model": 1,
            "expected_response": {
                "protocol": QUALIFICATION_PROTOCOL,
                "ok": True,
            },
            "max_paid_usd": _decimal_text(paid_limit),
        },
        "paid_preflight": paid_preflight,
        "authentication": {"opencode-paid": "bearer"},
        "calls": calls,
        "summary": {
            "models_requested": len(assignments),
            "models_recorded": len(calls),
            "models_attempted": attempted,
            "models_skipped": len(calls) - attempted,
            "models_passed": passed,
            "models_failed": sum(call["status"] == "failed" for call in calls),
            "cost_reporting_complete": reported_cost is not None,
            "provider_reported_cost_usd": reported_cost,
            "uncached_calculated_cost_usd": calculated_cost,
            "accounted_exposure_usd": _decimal_text(budget.accounted),
        },
    }


def _validate_qualification_config(
    assignments: Sequence[_Assignment],
    *,
    max_completion_tokens: int,
    temperature: float,
    reasoning_effort: str,
    max_paid_usd: Decimal | str,
    timeout_seconds: float,
) -> None:
    model_refs = tuple(assignment.model_ref for assignment in assignments)
    if len(set(model_refs)) != len(model_refs):
        raise ValueError("qualify-live model assignments must be unique")
    if any(model_ref not in QUALIFICATION_ALLOWED_MODELS for model_ref in model_refs):
        raise ValueError("qualify-live model is not in the qualification allowlist")
    if not 1 <= max_completion_tokens <= MODEL_MAX_COMPLETION_TOKENS:
        raise ValueError(
            "qualify-live max_completion_tokens must be from 1 through "
            f"{MODEL_MAX_COMPLETION_TOKENS}"
        )
    if temperature != DEFAULT_LIVE_TEMPERATURE:
        raise ValueError(
            f"qualify-live requires temperature {DEFAULT_LIVE_TEMPERATURE}"
        )
    if timeout_seconds != 300.0:
        raise ValueError("qualify-live requires a 300-second timeout")
    if reasoning_effort not in {"provider-default", "low"}:
        raise ValueError("qualify-live reasoning_effort must be provider-default or low")
    if LUNA_MODEL_REF in model_refs and reasoning_effort != "low":
        raise ValueError("gpt-5.6-luna qualification requires reasoning_effort low")
    limit = _parse_positive_decimal(max_paid_usd, name="max_paid_usd")
    if limit > QUALIFICATION_MAX_AUTHORIZATION_USD:
        raise ValueError(
            "qualify-live authorization cannot exceed "
            f"{_decimal_text(QUALIFICATION_MAX_AUTHORIZATION_USD)} USD"
        )


def _qualification_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "protocol": {"const": QUALIFICATION_PROTOCOL},
            "ok": {"const": True},
        },
        "required": ["protocol", "ok"],
        "additionalProperties": False,
    }


def _build_qualification_request(
    assignment: _Assignment,
    *,
    max_completion_tokens: int,
    temperature: float,
    reasoning_effort: str,
) -> dict[str, object]:
    return _build_provider_request(
        assignment,
        [
            {"role": "system", "content": QUALIFICATION_SYSTEM_PROMPT},
            {"role": "user", "content": QUALIFICATION_USER_PROMPT},
        ],
        max_completion_tokens=max_completion_tokens,
        temperature=temperature,
        reasoning_effort=(
            None if reasoning_effort == "provider-default" else reasoning_effort
        ),
        json_schema=_qualification_schema(),
        schema_name="adapter_qualification",
    )


def _qualification_preflight(
    assignments: Sequence[_Assignment],
    requests: Sequence[Mapping[str, object]],
    *,
    max_paid_usd: Decimal | str,
) -> tuple[Decimal, dict[str, object]]:
    limit = _parse_positive_decimal(max_paid_usd, name="max_paid_usd")
    rows: list[dict[str, str | int]] = []
    total = Decimal("0")
    envelope_total = Decimal("0")
    for assignment, request in zip(assignments, requests, strict=True):
        price = PAID_ZEN_PRICES[assignment.model_id]
        bound = _paid_request_bound(assignment.model_id, request)
        total += bound.cost_bound
        envelope_bound = (
            (
                Decimal(PAID_MAX_INPUT_TOKEN_BOUND) * price.input_per_million_usd
                + Decimal(MODEL_MAX_COMPLETION_TOKENS)
                * price.output_per_million_usd
            )
            / USD_PER_MILLION_TOKENS
            * PAID_ZEN_PRICE_SAFETY_FACTOR
        )
        envelope_total += envelope_bound
        rows.append(
            {
                "model": assignment.model_ref,
                "endpoint": assignment.endpoint.url,
                **bound.to_dict(),
                "input_per_million_usd": _decimal_text(
                    price.input_per_million_usd
                ),
                "output_per_million_usd": _decimal_text(
                    price.output_per_million_usd
                ),
                "model_cost_bound_usd": _decimal_text(bound.cost_bound),
                "envelope_cost_bound_usd": _decimal_text(envelope_bound),
            }
        )
    if total > limit:
        raise ValueError(
            f"conservative paid bound {_decimal_text(total)} USD exceeds "
            f"--max-paid-usd {_decimal_text(limit)}"
        )
    if envelope_total > limit:
        raise ValueError(
            f"panel envelope bound {_decimal_text(envelope_total)} USD exceeds "
            f"--max-paid-usd {_decimal_text(limit)}"
        )
    return limit, {
        "price_snapshot": PAID_ZEN_PRICE_SNAPSHOT,
        "price_source": PAID_ZEN_PRICE_SOURCE,
        "safety_factor": _decimal_text(PAID_ZEN_PRICE_SAFETY_FACTOR),
        "method": "utf8_bytes_plus_1024_as_input_tokens_and_full_output_cap",
        "runtime_gate": "exact_request_before_every_paid_transport",
        "cost_bound_scope": "all_qualification_calls",
        "maximum_input_token_bound": PAID_MAX_INPUT_TOKEN_BOUND,
        "calls": rows,
        "authorized_calls": len(assignments),
        "exact_requests_cost_bound_usd": _decimal_text(total),
        "panel_envelope_cost_bound_usd": _decimal_text(envelope_total),
    }


def _run_qualification_call(
    assignment: _Assignment,
    request: Mapping[str, object],
    *,
    sequence: int,
    transport: ChatTransport,
    api_key: str,
    timeout_seconds: float,
    budget: _PaidBudget,
    checkpoint_authorized: Callable[[Mapping[str, object]], None] | None = None,
) -> dict[str, object]:
    authorization = budget.quote(assignment, request)
    base: dict[str, object] = {
        "sequence": sequence,
        "model": assignment.model_ref,
        "endpoint": assignment.endpoint.url,
        "request": dict(request),
        "cost_authorization": authorization,
    }
    if Decimal(str(authorization["cumulative_cost_bound_usd"])) > budget.limit:
        return _qualification_transport_failure(
            {**base, "transport_attempted": False},
            kind="paid_budget_exhausted",
            message="paid cost authorization exhausted before request",
            response=None,
        )
    base["transport_attempted"] = True
    if checkpoint_authorized is not None:
        checkpoint_authorized({**base, "status": "in_flight"})
    try:
        response = transport.post(
            assignment.endpoint,
            request,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )
    except TransportFailure as error:
        budget.reserve_failed_request(authorization)
        return _qualification_transport_failure(
            base, kind=error.kind, message=str(error), response=None
        )
    except Exception as error:  # noqa: BLE001 - retain a paid-call receipt.
        budget.reserve_failed_request(authorization)
        return _qualification_transport_failure(
            base,
            kind="transport_error",
            message=f"transport raised {type(error).__name__}",
            response=None,
        )
    if response.status != 200:
        budget.reserve_failed_request(authorization)
        return _qualification_transport_failure(
            base,
            kind="http_error",
            message=f"provider returned HTTP {response.status}",
            response=response,
            model_id=assignment.model_id,
        )
    try:
        content, metadata = _parse_envelope(
            response.body, api_style=assignment.endpoint.api_style
        )
    except EnvelopeFailure as error:
        budget.reserve_failed_request(authorization)
        return _qualification_transport_failure(
            base,
            kind=error.kind,
            message=str(error),
            response=response,
            model_id=assignment.model_id,
        )
    provider_cost = metadata["provider_reported_cost_usd"]
    if not isinstance(provider_cost, str):
        budget.reserve_failed_request(authorization)
        return _qualification_transport_failure(
            base,
            kind="provider_cost_error",
            message="paid response did not report its cost",
            response=response,
            model_id=assignment.model_id,
            terminal_completion=True,
        )
    try:
        calculated_cost = _calculate_usage_cost(
            assignment.model_id, metadata["usage"]
        )
    except EnvelopeFailure as error:
        budget.reserve_failed_request(authorization)
        return _qualification_transport_failure(
            base,
            kind=error.kind,
            message=str(error),
            response=response,
            model_id=assignment.model_id,
            terminal_completion=True,
        )
    metadata["uncached_calculated_cost_usd"] = calculated_cost
    within_bound = budget.account(
        authorization,
        provider_cost=Decimal(provider_cost),
        calculated_cost=Decimal(calculated_cost),
    )
    raw_value, strict_error = parse_strict_model_json(content)
    strict_json = strict_error is None
    exact_schema = (
        isinstance(raw_value, Mapping)
        and set(raw_value) == {"protocol", "ok"}
        and raw_value.get("protocol") == QUALIFICATION_PROTOCOL
        and raw_value.get("ok") is True
    )
    model_identity = metadata["provider_model"] == assignment.model_id
    errors: list[str] = []
    if strict_error is not None:
        errors.append(strict_error)
    elif not exact_schema:
        errors.append("model response does not match the qualification object")
    if not model_identity:
        errors.append("provider model identity does not match the requested model")
    if not within_bound:
        errors.append("provider cost exceeded the authorized request bound")
    passed = not errors
    return {
        **base,
        "status": "passed" if passed else "failed",
        "response": {
            "http_status": response.status,
            "request_id": _request_id(response.headers),
            **metadata,
            "model_reply": content,
        },
        "validation": {
            "terminal_completion": True,
            "strict_json": strict_json,
            "exact_schema": exact_schema,
            "model_identity": model_identity,
            "cost_within_bound": within_bound,
            "errors": errors,
        },
    }


def _qualification_transport_failure(
    base: Mapping[str, object],
    *,
    kind: str,
    message: str,
    response: TransportResponse | None,
    model_id: str | None = None,
    terminal_completion: bool = False,
) -> dict[str, object]:
    receipt = _error_receipt(response) if response is not None else None
    if receipt is not None and model_id is not None:
        receipt.update(_paid_error_cost_receipt(response, model_id))
    return {
        **base,
        "status": "failed",
        "response": receipt,
        "error": {
            "kind": kind,
            "message": message,
            "http_status": response.status if response is not None else None,
        },
        "validation": {
            "terminal_completion": terminal_completion,
            "strict_json": False,
            "exact_schema": False,
            "model_identity": False,
            "cost_within_bound": False,
            "errors": [message],
        },
    }


def _qualification_cost_total(
    calls: Sequence[Mapping[str, object]], field_name: str
) -> str | None:
    if not calls:
        return None
    total = Decimal("0")
    for call in calls:
        response = call.get("response")
        value = response.get(field_name) if isinstance(response, Mapping) else None
        if not isinstance(value, str):
            return None
        try:
            total += Decimal(value)
        except InvalidOperation:
            return None
    return _decimal_text(total)


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


def _build_request(
    assignment: _Assignment,
    view: SurvivorView,
    *,
    max_completion_tokens: int,
    temperature: float,
    reasoning_effort: str | None,
) -> dict[str, object]:
    messages = [
        {
            "role": "system",
            "content": render_system_prompt(
                view.name,
                interaction_protocol=view.interaction_protocol,
            ),
        },
        {"role": "user", "content": render_turn_prompt(view)},
    ]
    return _build_provider_request(
        assignment,
        messages,
        max_completion_tokens=max_completion_tokens,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        json_schema=response_schema(view),
        schema_name="survival_choice",
    )


def _openai_strict_schema(value: object) -> object:
    if isinstance(value, Mapping):
        transformed: dict[str, object] = {}
        for key, child in value.items():
            if key == "const":
                transformed["type"] = _json_schema_literal_type(child)
                transformed["enum"] = [child]
            elif key == "oneOf":
                transformed["anyOf"] = _openai_strict_schema(child)
            else:
                transformed[key] = _openai_strict_schema(child)
        return transformed
    if isinstance(value, list):
        return [_openai_strict_schema(item) for item in value]
    return value


def _json_schema_literal_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    raise ValueError(f"unsupported JSON Schema literal type {type(value).__name__}")


def _build_provider_request(
    assignment: _Assignment,
    messages: list[dict[str, str]],
    *,
    max_completion_tokens: int,
    temperature: float,
    reasoning_effort: str | None,
    json_schema: Mapping[str, object],
    schema_name: str,
) -> dict[str, object]:
    if assignment.endpoint.provider == "opencode-paid":
        if assignment.model_id == "gpt-5.6-luna":
            if reasoning_effort != "low":
                raise ValueError("gpt-5.6-luna requires reasoning_effort low")
            # Keep legacy provider receipts stable while using Luna's strict subset.
            strict_schema = _openai_strict_schema(json_schema)
            return {
                "model": assignment.model_id,
                "input": messages,
                "max_output_tokens": max_completion_tokens,
                "reasoning": {"effort": "low"},
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "schema": strict_schema,
                        "strict": True,
                    }
                },
                "stream": False,
                "store": False,
            }
        if assignment.model_id in {"grok-4.5", "grok-4.6"}:
            if reasoning_effort == "compatibility-first":
                raise ValueError(
                    f"{assignment.model_id} reasoning cannot be disabled; "
                    "use provider-default or low"
                )
            request: dict[str, object] = {
                "model": assignment.model_id,
                "input": messages,
                "max_output_tokens": max_completion_tokens,
                "temperature": temperature,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "schema": dict(json_schema),
                        "strict": True,
                    }
                },
                "stream": False,
                "store": False,
            }
            if reasoning_effort is not None:
                request["reasoning"] = {"effort": reasoning_effort}
            return request
        if assignment.model_id == "kimi-k2.6":
            request: dict[str, object] = {
                "model": assignment.model_id,
                "messages": messages,
                "max_completion_tokens": max_completion_tokens,
                "response_format": {"type": "json_object"},
                "stream": False,
            }
            if reasoning_effort in {"low", "compatibility-first"}:
                request["thinking"] = {"type": "disabled"}
            return request
        if assignment.model_id in {"deepseek-v4-flash", "glm-5.2"}:
            request: dict[str, object] = {
                "model": assignment.model_id,
                "messages": messages,
                "max_tokens": max_completion_tokens,
                "temperature": temperature,
                "response_format": {"type": "json_object"},
                "stream": False,
            }
            if reasoning_effort == "compatibility-first":
                request["thinking"] = {"type": "disabled"}
            elif reasoning_effort is not None:
                request["reasoning_effort"] = reasoning_effort
            return request
        raise RuntimeError("paid model has no request profile")
    request: dict[str, object] = {
        "model": assignment.model_id,
        "messages": messages,
        "max_tokens": max_completion_tokens,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    if reasoning_effort is not None:
        request["reasoning_effort"] = reasoning_effort
    return request


def _paid_request_bound(
    model_id: str,
    request: Mapping[str, object],
) -> _PaidRequestBound:
    try:
        provider = PAID_ZEN_PRICES[model_id]
    except KeyError as error:
        raise ValueError(
            f"paid model {model_id!r} is not in the pinned price allowlist"
        ) from error
    messages = request.get("messages")
    if messages is None:
        messages = request.get("input")
    if not isinstance(messages, list):
        raise ValueError("paid request has no message input list")
    prompt_bytes = len(
        json.dumps(
            messages,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    completion_cap = request.get("max_completion_tokens")
    if completion_cap is None:
        completion_cap = request.get("max_tokens")
    if completion_cap is None:
        completion_cap = request.get("max_output_tokens")
    if type(completion_cap) is not int or completion_cap < 1:
        raise ValueError("paid request has no valid completion-token cap")
    input_token_bound = prompt_bytes + PAID_CHAT_TEMPLATE_OVERHEAD_TOKENS
    if input_token_bound > PAID_MAX_INPUT_TOKEN_BOUND:
        raise ValueError(
            "paid request input-token bound exceeds "
            f"{PAID_MAX_INPUT_TOKEN_BOUND} tokens"
        )
    call_bound = (
        (
            Decimal(input_token_bound) * provider.input_per_million_usd
            + Decimal(completion_cap) * provider.output_per_million_usd
        )
        / USD_PER_MILLION_TOKENS
        * PAID_ZEN_PRICE_SAFETY_FACTOR
    )
    return _PaidRequestBound(
        prompt_utf8_bytes=prompt_bytes,
        input_token_bound=input_token_bound,
        max_completion_tokens=completion_cap,
        cost_bound=call_bound,
    )


def _recorded_paid_budget(config: Mapping[str, object]) -> _PaidBudget | None:
    raw_limit = config.get("max_paid_usd")
    if raw_limit is None:
        return None
    limit = _parse_positive_decimal(raw_limit, name="recorded max_paid_usd")
    if limit > PAID_ZEN_MAX_AUTHORIZATION_USD:
        raise ValueError("recorded paid authorization exceeds the hard ceiling")
    return _PaidBudget(limit)


def _recorded_cost_decimal(value: object, *, name: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be canonical decimal text")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{name} must be canonical decimal text") from error
    if (
        not parsed.is_finite()
        or parsed < 0
        or _decimal_text(parsed) != value
    ):
        raise ValueError(f"{name} must be canonical non-negative decimal text")
    return parsed


def _verify_recorded_cost_authorization(
    *,
    assignment: _Assignment,
    request: Mapping[str, object],
    record: Mapping[str, object],
    paid_budget: _PaidBudget | None,
    successful: bool,
) -> None:
    if assignment.endpoint.provider != "opencode-paid":
        if "cost_authorization" in record:
            raise ValueError("non-paid call cannot contain cost authorization")
        return
    if paid_budget is None:
        raise ValueError("paid call has no recorded paid authorization limit")
    raw_authorization = record.get("cost_authorization")
    if not isinstance(raw_authorization, Mapping):
        raise ValueError("paid call has no cost authorization receipt")
    expected = paid_budget.quote(assignment, request)
    if successful:
        response = record.get("response")
        if not isinstance(response, Mapping):
            raise ValueError("successful paid call has no response receipt")
        provider_cost = _recorded_cost_decimal(
            response.get("provider_reported_cost_usd"),
            name="provider_reported_cost_usd",
        )
        try:
            calculated_text = _calculate_usage_cost(
                assignment.model_id,
                response.get("usage"),
            )
        except EnvelopeFailure as error:
            raise ValueError("successful paid call usage cannot be priced") from error
        if response.get("uncached_calculated_cost_usd") != calculated_text:
            raise ValueError("successful paid call calculated cost is inconsistent")
        within_bound = paid_budget.account(
            expected,
            provider_cost=provider_cost,
            calculated_cost=Decimal(calculated_text),
        )
        if not within_bound:
            raise ValueError("successful paid call exceeded its request cost bound")
    if dict(raw_authorization) != expected:
        raise ValueError("paid call cost authorization does not reconstruct exactly")


def _paid_preflight(
    *,
    assignments: Sequence[_Assignment],
    all_assignments: Sequence[_Assignment],
    seed: int,
    days: int,
    world_config: SurvivalConfig,
    max_calls: int,
    max_completion_tokens: int,
    temperature: float,
    reasoning_effort: str | None,
    max_paid_usd: Decimal | str | None,
    require_complete_budget: bool,
    preflight_world: SurvivalWorld | None = None,
) -> tuple[Decimal | None, dict[str, object] | None]:
    if not assignments:
        if max_paid_usd is not None:
            raise ValueError("max_paid_usd requires at least one opencode-paid model")
        return None, None
    limit = _parse_positive_decimal(max_paid_usd, name="max_paid_usd")
    if limit > PAID_ZEN_MAX_AUTHORIZATION_USD:
        raise ValueError(
            "paid Zen authorization cannot exceed "
            f"{_decimal_text(PAID_ZEN_MAX_AUTHORIZATION_USD)} USD"
        )
    if len(assignments) != len(all_assignments):
        raise ValueError("paid and non-paid models cannot be mixed in one run")
    if days != 1:
        raise ValueError("paid Zen smoke runs require exactly one cycle")
    world_max_calls = len(assignments) * days * world_config.slots_per_cycle
    if max_calls not in {len(assignments), world_max_calls}:
        raise ValueError(
            "paid Zen runs require --max-calls equal to the population or the "
            "complete-cycle maximum"
        )
    if max_calls == world_max_calls and not require_complete_budget:
        raise ValueError(
            "paid complete-cycle runs require --require-complete-budget"
        )
    if preflight_world is None:
        names = tuple(assignment.public_name for assignment in all_assignments)
        world = make_survival_world(
            names,
            seed=seed,
            config=world_config,
        )
    else:
        if preflight_world.seed != seed or preflight_world.config != world_config:
            raise ValueError("paid preflight world does not match the run configuration")
        world = deepcopy(preflight_world)
    rows: list[dict[str, str | int]] = []
    total = Decimal("0")
    for assignment in assignments:
        if assignment.model_id not in PAID_ZEN_PRICES:
            raise ValueError(
                f"paid model {assignment.model_id!r} is not in the pinned price allowlist"
            )
        view = survival_view_for(world, assignment.public_name)
        request = _build_request(
            assignment,
            view,
            max_completion_tokens=max_completion_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
        )
        provider = PAID_ZEN_PRICES[assignment.model_id]
        bound = _paid_request_bound(assignment.model_id, request)
        model_total = bound.cost_bound
        total += model_total
        rows.append(
            {
                "model": assignment.model_ref,
                **bound.to_dict(),
                "input_per_million_usd": _decimal_text(
                    provider.input_per_million_usd
                ),
                "output_per_million_usd": _decimal_text(
                    provider.output_per_million_usd
                ),
                "potential_calls": 1,
                "model_cost_bound_usd": _decimal_text(model_total),
            }
        )
    if total > limit:
        raise ValueError(
            f"conservative paid bound {_decimal_text(total)} USD exceeds "
            f"--max-paid-usd {_decimal_text(limit)}"
        )
    return limit, {
        "price_snapshot": PAID_ZEN_PRICE_SNAPSHOT,
        "price_source": PAID_ZEN_PRICE_SOURCE,
        "safety_factor": _decimal_text(PAID_ZEN_PRICE_SAFETY_FACTOR),
        "method": "utf8_bytes_plus_1024_as_input_tokens_and_full_output_cap",
        "runtime_gate": "exact_request_before_every_paid_transport",
        "cost_bound_scope": (
            "initial_sequential_dialogue_view_per_paid_model"
            if world.interaction_protocol == SEQUENTIAL_DIALOGUE_V3
            else "first_simultaneous_chance_only"
        ),
        "calls": rows,
        "authorized_calls": max_calls,
        "world_max_calls": world_max_calls,
        "first_chance_cost_bound_usd": _decimal_text(total),
    }


def _load_verified_parent_artifact(
    parent_path: Path,
    *,
    expected_sha256: str,
    ancestor_paths: Sequence[Path] = (),
) -> tuple[dict[str, Any], SurvivalResult, str]:
    _validate_sha256(expected_sha256, name="expected_parent_sha256")
    chain_paths = (*tuple(ancestor_paths), parent_path)
    resolved_paths = [path.resolve() for path in chain_paths]
    if len(set(resolved_paths)) != len(resolved_paths):
        raise ValueError("ancestor chain paths must be unique")
    chain = [_read_live_artifact(path) for path in chain_paths]
    parent_artifact, actual_sha256 = chain[-1]
    if actual_sha256 != expected_sha256:
        raise ValueError(
            "parent artifact SHA-256 mismatch: "
            f"expected {expected_sha256}, got {actual_sha256}"
        )

    parent_format = parent_artifact.get("format_version")
    if parent_format == 3:
        if ancestor_paths:
            raise ValueError("format-v3 parent does not accept ancestor paths")
    elif parent_format in {4, 5, 6}:
        if not ancestor_paths:
            raise ValueError(
                f"format-v{parent_format} parent requires its ancestor chain"
            )
    else:
        raise ValueError("parent artifact format_version must be 3, 4, 5, or 6")

    formats = [artifact.get("format_version") for artifact, _ in chain]
    valid_formats = formats[:1] == [3]
    for parent, child in zip(formats, formats[1:], strict=False):
        if parent == 3 and child == 6:
            valid_formats = False
            break
        legacy_child = 4 if parent == 3 else 5
        if child != legacy_child and child != 6:
            valid_formats = False
            break
        if parent == 6 and child != 6:
            valid_formats = False
            break
    if not valid_formats:
        raise ValueError(
            "ancestor paths must be complete and ordered oldest to newest "
            "before the direct parent"
        )

    for artifact, _ in chain:
        _verify_artifact_source_provenance(artifact)

    root_artifact, _ = chain[0]
    root_result = _verify_completed_live_result(
        root_artifact,
        expected_format=3,
        expected_mode="live_named_survival",
    )
    verified_assignments = _verified_parent_assignments(root_artifact, root_result)
    verified_result = root_result
    for index in range(1, len(chain)):
        child_artifact, _ = chain[index]
        previous_artifact, previous_sha256 = chain[index - 1]
        verified_result = _verify_continuation_boundary(
            child_artifact,
            child_path=chain_paths[index],
            parent_artifact=previous_artifact,
            parent_sha256=previous_sha256,
            parent_result=verified_result,
        )
        child_assignments = _verified_parent_assignments(
            child_artifact,
            verified_result,
        )
        _verify_assignment_transition(
            child_artifact,
            parent_assignments=verified_assignments,
            child_assignments=child_assignments,
        )
        verified_assignments = child_assignments
    return parent_artifact, verified_result, actual_sha256


def _read_live_artifact(parent_path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = parent_path.read_bytes()
    except OSError as error:
        raise ValueError(f"cannot read parent artifact from {parent_path}") from error
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("parent artifact is not valid UTF-8") from error
    try:
        loaded = _strict_json(text)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError("parent artifact is not valid strict JSON") from error
    if not isinstance(loaded, Mapping):
        raise ValueError("parent artifact must be an object")
    return deepcopy(dict(loaded)), actual_sha256


def _verify_completed_live_result(
    artifact: Mapping[str, Any],
    *,
    expected_format: int,
    expected_mode: str,
) -> SurvivalResult:
    if artifact.get("format_version") != expected_format:
        raise ValueError(f"parent artifact must use format_version {expected_format}")
    if artifact.get("mode") != expected_mode:
        raise ValueError(f"parent artifact must be a {expected_mode} run")
    if artifact.get("status") != "completed":
        raise ValueError("parent artifact must be completed")
    result_payload = artifact.get("result")
    if not isinstance(result_payload, Mapping):
        raise ValueError("completed parent artifact has no result object")
    parent_result = _survival_result_from_mapping(result_payload)
    canonical_sha256 = artifact.get("canonical_result_sha256")
    if not isinstance(canonical_sha256, str) or result_sha256(parent_result) != canonical_sha256:
        raise ValueError("parent canonical result SHA-256 does not match its result")
    replayed = replay_survival(parent_result)
    if result_sha256(replayed) != canonical_sha256:
        raise ValueError("parent exact replay changed its canonical result SHA-256")
    return replayed


def _verify_continuation_boundary(
    artifact: Mapping[str, Any],
    *,
    child_path: Path,
    parent_artifact: Mapping[str, Any],
    parent_sha256: str,
    parent_result: SurvivalResult,
) -> SurvivalResult:
    parent_format = parent_artifact.get("format_version")
    child_format = artifact.get("format_version")
    expected_format = (
        6 if child_format == 6 else 4 if parent_format == 3 else 5
    )
    child_result = _verify_completed_live_result(
        artifact,
        expected_format=expected_format,
        expected_mode="live_named_survival_continuation",
    )
    link = artifact.get("continuation_link")
    if not isinstance(link, Mapping) or set(link) != {
        "parent_artifact_name",
        "parent_artifact_sha256",
        "parent_canonical_result_sha256",
        "parent_format_version",
        "parent_mode",
    }:
        raise ValueError(f"{child_path.name} continuation_link is invalid")
    parent_artifact_name = link["parent_artifact_name"]
    if not isinstance(parent_artifact_name, str) or not parent_artifact_name:
        raise ValueError("parent artifact name must be non-empty text")
    expected_link = {
        "parent_artifact_sha256": parent_sha256,
        "parent_canonical_result_sha256": parent_artifact[
            "canonical_result_sha256"
        ],
        "parent_format_version": parent_format,
        "parent_mode": parent_artifact.get("mode"),
    }
    actual_link = {
        key: link[key]
        for key in expected_link
    }
    if actual_link != expected_link:
        raise ValueError(
            f"{child_path.name} continuation_link does not match its direct parent"
        )

    config = artifact.get("config")
    if not isinstance(config, Mapping):
        raise ValueError(f"{child_path.name} config must be an object")
    additional_cycles = config.get("cycles_requested")
    if (
        isinstance(additional_cycles, bool)
        or not isinstance(additional_cycles, int)
        or additional_cycles < 1
    ):
        raise ValueError("parent config cycles_requested must be a positive integer")
    continuation_options: dict[str, str | int] = {}
    interaction_protocol = config.get("interaction_protocol")
    if interaction_protocol is not None:
        if not isinstance(interaction_protocol, str):
            raise ValueError("parent interaction_protocol must be text")
        continuation_options["interaction_protocol"] = interaction_protocol
    raw_initiative_phase = config.get("initiative_phase", 0)
    if isinstance(raw_initiative_phase, bool) or not isinstance(
        raw_initiative_phase, int
    ):
        raise ValueError("parent initiative_phase must be an integer")
    continuation_options["initiative_phase"] = raw_initiative_phase
    expected_world = continue_survival_world(
        parent_result,
        additional_cycles=additional_cycles,
        **continuation_options,
    )

    transition_receipt = artifact.get("transition_receipt")
    if not isinstance(transition_receipt, Mapping) or set(transition_receipt) != {
        "method",
        "event",
    }:
        raise ValueError("parent transition_receipt is invalid")
    transition_method = transition_receipt.get("method")
    if transition_method == "verified_parent_state_preserved":
        if transition_receipt.get("event") is not None:
            raise ValueError("parent preserved-state transition event must be null")
    elif transition_method == (
        "deterministic_between_cycle_shared_resource_adjustment"
    ):
        transition = transition_receipt.get("event")
        if not isinstance(transition, Mapping):
            raise ValueError("parent transition event must be an object")
        detail = transition.get("detail")
        if not isinstance(detail, Mapping):
            raise ValueError("parent transition event detail must be an object")
        resource = detail.get("resource")
        stock = detail.get("after")
        reason = detail.get("reason")
        if not isinstance(resource, str):
            raise ValueError("parent transition resource must be text")
        if isinstance(stock, bool) or not isinstance(stock, int):
            raise ValueError("parent transition stock must be an integer")
        if not isinstance(reason, str):
            raise ValueError("parent transition reason must be text")
        expected_transition = adjust_shared_resource(
            expected_world,
            resource=resource,
            stock=stock,
            reason=reason,
        ).to_dict()
        if dict(transition) != expected_transition:
            raise ValueError("parent transition receipt does not reconstruct exactly")
    else:
        raise ValueError("parent transition receipt method is invalid")

    public_record = expected_world.prior_public_record
    if public_record is None:
        raise ValueError("reconstructed parent has no prior public record")
    expected_public_record_receipt = {
        "method": "final_public_broadcast_per_identity_verbatim",
        "statement_status": "unverified",
        "objective_totals_source": "verified_parent_engine_events",
        "record": public_record.to_dict(),
    }
    public_record_receipt = artifact.get("public_record_receipt")
    if (
        not isinstance(public_record_receipt, Mapping)
        or dict(public_record_receipt) != expected_public_record_receipt
    ):
        raise ValueError("parent public record receipt does not reconstruct exactly")

    parent_config = parent_artifact.get("config")
    if not isinstance(parent_config, Mapping):
        raise ValueError("direct parent config must be an object")
    expected_config = {
        "seed": expected_world.seed,
        "days_requested": additional_cycles,
        "cycles_requested": additional_cycles,
        "starting_cycle": expected_world.day + 1,
        "ending_cycle": expected_world.day + additional_cycles,
        "slots_per_cycle": expected_world.config.slots_per_cycle,
        "world_preset": parent_config.get("world_preset"),
        "calibration_scope": "verified_parent_continuation",
        "world_config": expected_world.config.to_dict(),
    }
    if "initiative_phase" in config:
        expected_config["initiative_phase"] = expected_world.initiative_phase
    for key, expected in expected_config.items():
        if config.get(key) != expected:
            raise ValueError(f"parent config {key} does not match reconstruction")
    if interaction_protocol is not None and (
        interaction_protocol != expected_world.interaction_protocol
    ):
        raise ValueError("parent interaction_protocol does not match reconstruction")

    expected_initial_state = expected_world.to_dict(include_events=False)
    expected_initial_state["event_sequence_offset"] = (
        expected_world.event_sequence_offset
    )
    expected_initial_state["observation_history"] = [
        event.to_dict() for event in expected_world.events
    ]
    if child_result.initial_state != expected_initial_state:
        raise ValueError("parent initial state does not match reconstructed boundary")
    expected_event_sequence_base = (
        expected_world.event_sequence_offset + len(expected_world.events)
    )
    if child_result.event_sequence_base != expected_event_sequence_base:
        raise ValueError("parent event_sequence_base does not match reconstruction")
    _verify_contiguous_event_records(
        child_result.initial_state["observation_history"],
        offset=expected_world.event_sequence_offset,
        name="parent observation history",
    )
    _verify_contiguous_event_records(
        child_result.events,
        offset=child_result.event_sequence_base,
        name="parent result events",
    )
    if child_format == 6:
        _verify_v6_calls_reconstruct_result(
            artifact,
            world=expected_world,
            expected_result=child_result,
            additional_cycles=additional_cycles,
        )
    return child_result


def _verify_v6_calls_reconstruct_result(
    artifact: Mapping[str, object],
    *,
    world: SurvivalWorld,
    expected_result: SurvivalResult,
    additional_cycles: int,
) -> None:
    config = artifact.get("config")
    calls = artifact.get("calls")
    if not isinstance(config, Mapping) or not isinstance(calls, list) or not all(
        isinstance(call, Mapping) for call in calls
    ):
        raise ValueError("format-v6 call replay inputs are invalid")
    max_completion_tokens = config.get("max_completion_tokens")
    raw_temperature = config.get("temperature")
    reasoning_effort = config.get("reasoning_effort")
    if (
        isinstance(max_completion_tokens, bool)
        or not isinstance(max_completion_tokens, int)
        or not isinstance(reasoning_effort, str)
        or reasoning_effort not in LIVE_REASONING_EFFORTS
    ):
        raise ValueError("format-v6 request configuration is invalid")
    if isinstance(raw_temperature, bool):
        raise ValueError("format-v6 request temperature is invalid")
    try:
        temperature = float(raw_temperature)
    except (TypeError, ValueError) as error:
        raise ValueError("format-v6 request temperature is invalid") from error
    if not 0.0 <= temperature <= 2.0:
        raise ValueError("format-v6 request temperature is invalid")
    assignments = _verified_parent_assignments(artifact, expected_result)
    active_names = set(world.alive_names())
    active_assignments = tuple(
        assignment
        for assignment in assignments
        if assignment.public_name in active_names
    )
    paid_budget = _recorded_paid_budget(config)
    cursor = [0]
    providers = {
        assignment.public_name: _RecordedV6Provider(
            assignment=assignment,
            calls=calls,
            cursor=cursor,
            max_completion_tokens=max_completion_tokens,
            temperature=temperature,
            reasoning_effort=(
                None if reasoning_effort == "provider-default" else reasoning_effort
            ),
            paid_budget=paid_budget,
        )
        for assignment in active_assignments
    }
    try:
        reconstructed = run_survival(
            deepcopy(world),
            providers,
            days=additional_cycles,
        )
    except RuntimeError as error:
        if isinstance(error.__cause__, ValueError):
            raise error.__cause__
        raise
    if cursor[0] != len(calls):
        raise ValueError("format-v6 calls contain entries not consumed by replay")
    if reconstructed.to_dict() != expected_result.to_dict():
        raise ValueError("format-v6 calls do not reconstruct the recorded result")


def _verify_contiguous_event_records(
    events: object,
    *,
    offset: int,
    name: str,
) -> None:
    if not isinstance(events, (list, tuple)) or not all(
        isinstance(event, Mapping) for event in events
    ):
        raise ValueError(f"{name} must be an array of objects")
    expected = list(range(offset + 1, offset + len(events) + 1))
    actual = [event.get("sequence") for event in events]
    if actual != expected:
        raise ValueError(f"{name} must have contiguous sequences")


def _verify_artifact_source_provenance(artifact: Mapping[str, Any]) -> None:
    source = artifact.get("source")
    expected_keys = {
        *SOURCE_FILES,
        "world_sim_version",
        "python_version",
        "platform_system",
    }
    if not isinstance(source, Mapping) or set(source) != expected_keys:
        raise ValueError("parent source receipt has unexpected fields")
    for key in ("world_sim_version", "python_version", "platform_system"):
        if not isinstance(source[key], str) or not source[key]:
            raise ValueError(f"parent source {key} must be non-empty text")
    recorded: dict[str, str] = {}
    for key in SOURCE_FILES:
        value = source[key]
        _validate_sha256(value, name=f"parent source {key}")
        recorded[key] = value
    current = {
        key: hashlib.sha256(path.read_bytes()).hexdigest()
        for key, path in SOURCE_FILES.items()
    }
    if recorded == current:
        return

    marker = SOURCE_FILES["model_host_sha256"].relative_to(REPOSITORY_ROOT)
    history = subprocess.run(
        ("git", "log", "--all", "--format=%H", "--", marker.as_posix()),
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if history.returncode == 0:
        for commit in history.stdout.splitlines():
            if _source_hashes_at_commit(commit) == recorded:
                return
    raise ValueError("parent source receipt has no matching working tree or commit")


def _source_hashes_at_commit(commit: str) -> dict[str, str] | None:
    result: dict[str, str] = {}
    for key, source_path in SOURCE_FILES.items():
        relative = source_path.relative_to(REPOSITORY_ROOT).as_posix()
        blob = subprocess.run(
            ("git", "show", f"{commit}:{relative}"),
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
        )
        if blob.returncode != 0:
            return None
        result[key] = hashlib.sha256(blob.stdout).hexdigest()
    return result


def _validate_sha256(value: object, *, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")


def _survival_result_from_mapping(payload: Mapping[str, object]) -> SurvivalResult:
    expected_keys = {
        "initial_state",
        "final_state",
        "events",
        "choice_tape",
        "event_sequence_base",
    }
    if set(payload) != expected_keys:
        raise ValueError("parent result has unexpected fields")
    initial_state = payload["initial_state"]
    final_state = payload["final_state"]
    events = payload["events"]
    choice_tape = payload["choice_tape"]
    event_sequence_base = payload["event_sequence_base"]
    if not isinstance(initial_state, Mapping) or not isinstance(final_state, Mapping):
        raise ValueError("parent result states must be objects")
    if not isinstance(events, list) or not all(
        isinstance(event, Mapping) for event in events
    ):
        raise ValueError("parent result events must be a list of objects")
    if not isinstance(choice_tape, list) or not all(
        isinstance(record, Mapping) for record in choice_tape
    ):
        raise ValueError("parent choice tape must be a list of objects")
    if isinstance(event_sequence_base, bool) or not isinstance(
        event_sequence_base, int
    ):
        raise ValueError("parent event_sequence_base must be an integer")
    return SurvivalResult(
        initial_state=deepcopy(dict(initial_state)),
        final_state=deepcopy(dict(final_state)),
        events=tuple(deepcopy(dict(event)) for event in events),
        choice_tape=tuple(deepcopy(dict(record)) for record in choice_tape),
        event_sequence_base=event_sequence_base,
    )


def _verified_parent_assignments(
    artifact: Mapping[str, object],
    result: SurvivalResult,
) -> tuple[_Assignment, ...]:
    raw_assignments = artifact.get("seat_assignments")
    if not isinstance(raw_assignments, list) or not all(
        isinstance(row, Mapping) for row in raw_assignments
    ):
        raise ValueError("parent seat_assignments must be a list of objects")
    model_refs: list[str] = []
    for row in raw_assignments:
        if set(row) != {"seat_id", "public_name", "model"}:
            raise ValueError("parent seat assignment has unexpected fields")
        model = row["model"]
        if not isinstance(model, str):
            raise ValueError("parent seat assignment model must be text")
        model_refs.append(model)
    assignments = _assign_models(model_refs)
    if [assignment.to_dict() for assignment in assignments] != [
        dict(row) for row in raw_assignments
    ]:
        raise ValueError("parent seat/name/model mapping is not exact")

    expected_seats = [
        (assignment.seat_id, assignment.public_name) for assignment in assignments
    ]
    for state_name, state in (
        ("initial_state", result.initial_state),
        ("final_state", result.final_state),
    ):
        survivors = state.get("survivors")
        if not isinstance(survivors, list) or not all(
            isinstance(survivor, Mapping) for survivor in survivors
        ):
            raise ValueError(f"parent {state_name} survivors must be a list of objects")
        actual_seats = [
            (str(survivor.get("seat_id")), str(survivor.get("name")))
            for survivor in survivors
        ]
        if actual_seats != expected_seats:
            raise ValueError(f"parent {state_name} seat/name mapping is not exact")

    config = artifact.get("config")
    if not isinstance(config, Mapping):
        raise ValueError("parent config must be an object")
    if not isinstance(config.get("world_preset"), str):
        raise ValueError("parent config has no world_preset")
    seed = config.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("parent config seed must be an integer")
    if result.initial_state.get("seed") != seed or result.final_state.get("seed") != seed:
        raise ValueError("parent config seed does not match its result")

    assignment_by_seat = {
        assignment.seat_id: assignment for assignment in assignments
    }
    calls = artifact.get("calls")
    if not isinstance(calls, list) or not all(isinstance(call, Mapping) for call in calls):
        raise ValueError("parent calls must be a list of objects")
    for sequence, call in enumerate(calls, start=1):
        if call.get("sequence") != sequence or call.get("status") != "succeeded":
            raise ValueError("completed parent calls must be contiguous and succeeded")
        seat_id = call.get("seat_id")
        if not isinstance(seat_id, str) or seat_id not in assignment_by_seat:
            raise ValueError("parent call refers to an unknown seat")
        assignment = assignment_by_seat[seat_id]
        if (
            call.get("public_name") != assignment.public_name
            or call.get("model") != assignment.model_ref
        ):
            raise ValueError("parent call seat/name/model mapping is not exact")
    return assignments


def _verify_assignment_transition(
    artifact: Mapping[str, object],
    *,
    parent_assignments: Sequence[_Assignment],
    child_assignments: Sequence[_Assignment],
) -> None:
    format_version = artifact.get("format_version")
    raw_receipts = artifact.get("assignment_transition_receipts")
    if format_version != 6:
        if raw_receipts is not None:
            raise ValueError(
                "legacy continuation cannot contain assignment transition receipts"
            )
        return

    config = artifact.get("config")
    if not isinstance(config, Mapping) or (
        config.get("interaction_protocol") != SEQUENTIAL_DIALOGUE_V3
    ):
        raise ValueError("format-v6 continuation must use sequential-dialogue-v3")
    if (
        not isinstance(raw_receipts, list)
        or len(raw_receipts) > 1
        or not all(isinstance(receipt, Mapping) for receipt in raw_receipts)
    ):
        raise ValueError(
            "format-v6 assignment transition receipts must contain at most one object"
        )
    if not raw_receipts:
        if [assignment.to_dict() for assignment in parent_assignments] != [
            assignment.to_dict() for assignment in child_assignments
        ]:
            raise ValueError(
                "assignment transition changed without a matching receipt"
            )
        return
    receipt = raw_receipts[0]
    expected_keys = {
        "seat_id",
        "public_name",
        "previous_model",
        "replacement_model",
        "reason",
    }
    if set(receipt) != expected_keys:
        raise ValueError("assignment transition receipt has unexpected fields")
    _validate_replacement_reason(receipt["reason"])

    parent_by_seat = {
        assignment.seat_id: assignment for assignment in parent_assignments
    }
    child_by_seat = {
        assignment.seat_id: assignment for assignment in child_assignments
    }
    if set(parent_by_seat) != set(child_by_seat):
        raise ValueError("assignment transition changed the verified seat set")
    changed = [
        seat_id
        for seat_id in parent_by_seat
        if parent_by_seat[seat_id].to_dict() != child_by_seat[seat_id].to_dict()
    ]
    if len(changed) != 1:
        raise ValueError("assignment transition must change exactly one verified seat")
    seat_id = changed[0]
    previous = parent_by_seat[seat_id]
    replacement = child_by_seat[seat_id]
    expected = {
        "seat_id": seat_id,
        "public_name": previous.public_name,
        "previous_model": previous.model_ref,
        "replacement_model": replacement.model_ref,
        "reason": receipt["reason"],
    }
    if previous.public_name != replacement.public_name or dict(receipt) != expected:
        raise ValueError(
            "assignment transition receipt does not match the verified parent and child"
        )


def _continuation_outcomes(result: SurvivalResult) -> dict[str, object]:
    transfers = [
        event for event in result.events if event.get("kind") == "resource_given"
    ]
    wood_gifts = [
        event
        for event in transfers
        if isinstance(event.get("detail"), Mapping)
        and event["detail"].get("resource") == "wood"
    ]
    completed_wood_gifts = [
        {
            "sequence": int(event["sequence"]),
            "cycle": int(event["cycle"]),
            "chance": int(event["slot"]),
            "giver": str(event["actor"]),
            "recipient": str(event["detail"]["target"]),
            "amount": int(event["detail"]["amount"]),
        }
        for event in wood_gifts
    ]
    completed_gift_keys = {
        (int(event["cycle"]), int(event["slot"]), str(event["actor"]))
        for event in transfers
    }
    rejection_reasons = {
        (int(event["cycle"]), int(event["slot"]), str(event["actor"])): str(
            event["detail"]["reason"]
        )
        for event in result.events
        if event.get("kind") == "action_resolution_rejected"
        and isinstance(event.get("detail"), Mapping)
        and "reason" in event["detail"]
    }
    failed_paid_transfer_attempts = [
        {
            "sequence": int(event["sequence"]),
            "cycle": int(event["cycle"]),
            "chance": int(event["slot"]),
            "giver": str(event["actor"]),
            "action": str(event["detail"]["action"]),
            "energy_paid": int(event["detail"]["action_cost"]),
            "resolution_rejection_reason": rejection_reasons.get(
                (int(event["cycle"]), int(event["slot"]), str(event["actor"]))
            ),
        }
        for event in result.events
        if event.get("kind") == "choice_energy_paid"
        and isinstance(event.get("detail"), Mapping)
        and event["detail"].get("action") in {"give_food", "give_wood"}
        and int(event["detail"].get("action_cost", 0)) > 0
        and (
            int(event["cycle"]),
            int(event["slot"]),
            str(event["actor"]),
        )
        not in completed_gift_keys
    ]
    initial_survivors = result.initial_state["survivors"]
    names = [str(survivor["name"]) for survivor in initial_survivors]
    initial_wood = {
        str(survivor["name"]): int(survivor["wood"])
        for survivor in initial_survivors
    }
    gathered = {name: 0 for name in names}
    sent = {name: 0 for name in names}
    received = {name: 0 for name in names}
    inbound_gifts: dict[str, list[dict[str, int | str]]] = {
        name: [] for name in names
    }
    shelter_cost = int(result.initial_state["config"]["shelter_wood_cost"])
    enabling: list[dict[str, object]] = []
    shelters: list[dict[str, int | str]] = []
    for event in result.events:
        kind = event["kind"]
        actor = event["actor"]
        detail = event["detail"]
        if kind == "wood_gathered" and isinstance(actor, str):
            gathered[actor] += int(detail["wood_gathered"])
        elif kind == "resource_given" and detail.get("resource") == "wood":
            giver = str(actor)
            recipient = str(detail["target"])
            amount = int(detail["amount"])
            sent[giver] += amount
            received[recipient] += amount
            inbound_gifts[recipient].append(
                {
                    "sequence": int(event["sequence"]),
                    "cycle": int(event["cycle"]),
                    "chance": int(event["slot"]),
                    "giver": giver,
                    "amount": amount,
                }
            )
        elif kind == "shelter_built" and isinstance(actor, str):
            shelter = {
                "sequence": int(event["sequence"]),
                "cycle": int(event["cycle"]),
                "chance": int(event["slot"]),
                "builder": actor,
            }
            shelters.append(shelter)
            non_inbound_wood = max(
                0,
                initial_wood[actor] + gathered[actor] - sent[actor],
            )
            minimum_inbound_required = max(0, shelter_cost - non_inbound_wood)
            contributing = [
                gift
                for gift in inbound_gifts[actor]
                if int(gift["sequence"]) < int(event["sequence"])
            ]
            contributed = sum(int(gift["amount"]) for gift in contributing)
            if minimum_inbound_required > 0 and contributed >= minimum_inbound_required:
                enabling.append(
                    {
                        **shelter,
                        "inbound_wood_before_build": contributed,
                        "minimum_inbound_wood_required": minimum_inbound_required,
                        "completed_gifts": contributing,
                    }
                )

    give_cost = int(result.initial_state["config"]["give_energy_cost"])
    costly_transfer_count = len(transfers) if give_cost > 0 else 0
    primary_chains = [chain for chain in enabling if int(chain["chance"]) <= 3]
    reciprocal_pairs: list[dict[str, str]] = []
    reciprocal_seen: set[tuple[str, str]] = set()
    gift_directions = {
        (str(gift["giver"]), str(gift["recipient"]))
        for gift in completed_wood_gifts
    }
    for giver, recipient in sorted(gift_directions):
        normalized = tuple(sorted((giver, recipient)))
        if (
            (recipient, giver) in gift_directions
            and normalized not in reciprocal_seen
        ):
            reciprocal_seen.add(normalized)
            reciprocal_pairs.append(
                {"first_survivor": normalized[0], "second_survivor": normalized[1]}
            )
    builders = {str(shelter["builder"]) for shelter in shelters}
    return {
        "completed_resource_transfers": len(transfers),
        "completed_costly_resource_transfers": costly_transfer_count,
        "any_completed_costly_resource_transfer": costly_transfer_count > 0,
        "completed_wood_gifts": completed_wood_gifts,
        "failed_paid_transfer_attempts": failed_paid_transfer_attempts,
        "wood_received": received,
        "wood_gifts_without_later_shelter": [
            gift
            for gift in completed_wood_gifts
            if str(gift["recipient"]) not in builders
        ],
        "reciprocal_wood_transfer_pairs": reciprocal_pairs,
        "shelters_built": shelters,
        "shelter_enabling_wood": enabling,
        "primary_shelter_chain_by_end_of_chance_3": bool(primary_chains),
        "primary_shelter_chains": primary_chains,
    }


def _apply_model_replacements(
    assignments: Sequence[_Assignment],
    *,
    model_replacements: Sequence[str],
    replacement_reason: str | None,
    interaction_protocol: str,
) -> tuple[tuple[_Assignment, ...], list[dict[str, str]]]:
    replacements = tuple(model_replacements)
    if interaction_protocol == GLOBAL_BEATS_V2:
        if replacements or replacement_reason is not None:
            raise ValueError(
                "model replacement requires interaction_protocol sequential-dialogue-v3"
            )
        return tuple(assignments), []
    if interaction_protocol != SEQUENTIAL_DIALOGUE_V3:
        raise ValueError(
            "interaction_protocol must be global-beats-v2 or sequential-dialogue-v3"
        )
    if len(replacements) > 1:
        raise ValueError("sequential-dialogue-v3 accepts at most one model replacement")
    if not replacements:
        if replacement_reason is not None:
            raise ValueError(
                "replacement_reason requires a model replacement"
            )
        return tuple(assignments), []
    _validate_replacement_reason(replacement_reason)
    assert isinstance(replacement_reason, str)

    replacement = replacements[0]
    if not isinstance(replacement, str) or replacement.count("=") != 1:
        raise ValueError("model replacement must use PUBLIC_NAME=PROVIDER/MODEL")
    public_name, model_ref = replacement.split("=", 1)
    if not public_name or public_name != public_name.strip():
        raise ValueError("model replacement public identity is invalid")
    if not model_ref or model_ref != model_ref.strip():
        raise ValueError("model replacement model is invalid")

    by_name = {assignment.public_name: assignment for assignment in assignments}
    if public_name not in by_name:
        raise ValueError(f"model replacement refers to unknown identity {public_name!r}")
    previous = by_name[public_name]
    if model_ref == previous.model_ref:
        raise ValueError("model replacement must change the assigned model")
    endpoint, model_id = _parse_model_ref(model_ref)
    replacement_assignment = _Assignment(
        previous.seat_id,
        previous.public_name,
        model_ref,
        endpoint,
        model_id,
    )
    updated = tuple(
        replacement_assignment if assignment.seat_id == previous.seat_id else assignment
        for assignment in assignments
    )
    receipt = {
        "seat_id": previous.seat_id,
        "public_name": previous.public_name,
        "previous_model": previous.model_ref,
        "replacement_model": model_ref,
        "reason": replacement_reason,
    }
    return updated, [receipt]


def _validate_replacement_reason(reason: object) -> None:
    if (
        not isinstance(reason, str)
        or not reason
        or reason != reason.strip()
        or len(reason) > 500
        or not reason.isprintable()
    ):
        raise ValueError(
            "replacement_reason must be 1-500 printable characters without outer whitespace"
        )


def _parse_model_ref(model_ref: str) -> tuple[EndpointSpec, str]:
    provider, separator, model_id = model_ref.partition("/")
    if not separator or provider not in _ENDPOINTS:
        raise ValueError(
            f"model {model_ref!r} must use opencode/MODEL, "
            "opencode-paid/MODEL, or opencode-go/MODEL"
        )
    if not model_id or len(model_id) > 128:
        raise ValueError(f"model {model_ref!r} has an invalid model ID")
    if provider == "opencode" and not model_id.endswith("-free"):
        raise ValueError("the unauthenticated opencode endpoint only accepts -free models")
    if provider == "opencode-paid" and model_id.endswith("-free"):
        raise ValueError("the opencode-paid endpoint does not accept -free models")
    endpoint = (
        _PAID_RESPONSES_ENDPOINT
        if provider == "opencode-paid"
        and model_id in {"grok-4.5", "grok-4.6", "gpt-5.6-luna"}
        else _ENDPOINTS[provider]
    )
    return endpoint, model_id


def _assign_models(
    model_refs: Sequence[str],
    *,
    minimum: int = 2,
    maximum: int = len(DEFAULT_SURVIVOR_NAMES),
    context: str = "survive-live",
) -> tuple[_Assignment, ...]:
    refs = tuple(reference.strip() for reference in model_refs)
    if not minimum <= len(refs) <= maximum:
        raise ValueError(
            f"{context} needs between {minimum} and {maximum} model assignments"
        )
    assignments = []
    for index, (name, model_ref) in enumerate(
        zip(DEFAULT_SURVIVOR_NAMES[: len(refs)], refs, strict=True), start=1
    ):
        endpoint, model_id = _parse_model_ref(model_ref)
        assignments.append(
            _Assignment(
                f"seat-{index:03d}", name, model_ref, endpoint, model_id
            )
        )
    return tuple(assignments)


def _validate_limits(
    population: int,
    days: int,
    slots_per_cycle: int,
    max_calls: int,
    max_completion_tokens: int,
    temperature: float,
    timeout_seconds: float,
    require_complete_budget: bool,
) -> None:
    if days < 1:
        raise ValueError("cycles must be positive")
    minimum_calls = population * days
    if minimum_calls > max_calls:
        raise ValueError(
            f"this run requires at least {minimum_calls} model calls, "
            f"above --max-calls {max_calls}"
        )
    complete_budget = population * days * slots_per_cycle
    if require_complete_budget and complete_budget > max_calls:
        raise ValueError(
            "a complete-cycle budget requires at least "
            f"{complete_budget} model calls, above --max-calls {max_calls}"
        )
    if not 1 <= max_completion_tokens <= MODEL_MAX_COMPLETION_TOKENS:
        raise ValueError(
            f"max_completion_tokens must be from 1 through {MODEL_MAX_COMPLETION_TOKENS}"
        )
    if not 0.0 <= temperature <= 2.0:
        raise ValueError("temperature must be from 0 through 2")
    if not 1.0 <= timeout_seconds <= 300.0:
        raise ValueError("timeout_seconds must be from 1 through 300")


def _parse_envelope(
    raw_body: str, *, api_style: str
) -> tuple[str, dict[str, object]]:
    try:
        payload = _strict_json(raw_body)
    except (json.JSONDecodeError, ValueError) as error:
        raise EnvelopeFailure(
            "provider_envelope_error", "provider response is not valid strict JSON"
        ) from error
    if not isinstance(payload, Mapping):
        raise EnvelopeFailure("provider_envelope_error", "provider response must be an object")
    if api_style == "responses":
        return _parse_responses_envelope(payload)
    if api_style != "chat":
        raise RuntimeError(f"unknown provider API style {api_style!r}")
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
        "provider_reported_cost_usd": _provider_cost(payload, usage),
    }


def _parse_responses_envelope(
    payload: Mapping[str, object],
) -> tuple[str, dict[str, object]]:
    if payload.get("object") != "response":
        raise EnvelopeFailure(
            "provider_envelope_error", "provider response has the wrong object type"
        )
    status = payload.get("status")
    if status == "incomplete":
        details = payload.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, Mapping) else None
        if reason in {"max_output_tokens", "max_tokens"}:
            raise EnvelopeFailure(
                "completion_budget_exhausted",
                "model exhausted its completion budget before finishing an answer",
            )
        raise EnvelopeFailure(
            "provider_envelope_error", "provider returned an incomplete response"
        )
    if status != "completed" or payload.get("error") is not None:
        raise EnvelopeFailure(
            "provider_envelope_error", "provider response did not complete"
        )
    output = payload.get("output")
    if not isinstance(output, list):
        raise EnvelopeFailure(
            "provider_envelope_error", "provider response has no output list"
        )
    text_parts: list[str] = []
    for item in output:
        if not isinstance(item, Mapping):
            raise EnvelopeFailure(
                "provider_envelope_error", "provider output item must be an object"
            )
        item_type = item.get("type")
        if item_type == "reasoning":
            continue
        if (
            item_type != "message"
            or item.get("role") != "assistant"
            or item.get("status") != "completed"
        ):
            raise EnvelopeFailure(
                "provider_envelope_error", "provider returned an unsupported output item"
            )
        content = item.get("content")
        if not isinstance(content, list):
            raise EnvelopeFailure(
                "provider_envelope_error", "provider message has no content list"
            )
        for part in content:
            if (
                not isinstance(part, Mapping)
                or part.get("type") != "output_text"
                or not isinstance(part.get("text"), str)
            ):
                raise EnvelopeFailure(
                    "provider_envelope_error",
                    "provider message has unsupported content",
                )
            text_parts.append(str(part["text"]))
    if not text_parts:
        raise EnvelopeFailure(
            "provider_envelope_error", "provider response has no output text"
        )
    provider_model = payload.get("model")
    usage = payload.get("usage")
    return "".join(text_parts), {
        "provider_model": provider_model if isinstance(provider_model, str) else None,
        "finish_reason": "stop",
        "usage": dict(usage) if isinstance(usage, Mapping) else {},
        "provider_reported_cost_usd": _provider_cost(payload, usage),
    }


def _provider_cost(payload: Mapping[str, object], usage: object) -> str | None:
    top_level = payload.get("cost")
    nested = usage.get("cost") if isinstance(usage, Mapping) else None
    raw_ticks = (
        usage.get("cost_in_usd_ticks") if isinstance(usage, Mapping) else None
    )
    if top_level is None and nested is None and raw_ticks is None:
        return None
    values = [value for value in (top_level, nested) if value is not None]
    parsed = [
        _parse_nonnegative_decimal(value, name="provider cost") for value in values
    ]
    if raw_ticks is not None:
        if type(raw_ticks) is not int or raw_ticks < 0:
            raise EnvelopeFailure(
                "provider_cost_error", "provider returned invalid USD cost ticks"
            )
        parsed.append(Decimal(raw_ticks) / USD_COST_TICKS_PER_USD)
    if any(value != parsed[0] for value in parsed[1:]):
        raise EnvelopeFailure(
            "provider_cost_error", "provider returned conflicting cost values"
        )
    return _decimal_text(parsed[0])


def _calculate_usage_cost(model_id: str, raw_usage: object) -> str:
    if model_id not in PAID_ZEN_PRICES:
        raise EnvelopeFailure("provider_cost_error", "paid model has no pinned price")
    if not isinstance(raw_usage, Mapping):
        raise EnvelopeFailure("provider_cost_error", "paid response has no usage object")
    prompt_tokens = _usage_token_count(
        raw_usage, ("prompt_tokens", "input_tokens"), name="input"
    )
    completion_tokens = _usage_token_count(
        raw_usage, ("completion_tokens", "output_tokens"), name="output"
    )
    price = PAID_ZEN_PRICES[model_id]
    calculated = (
        Decimal(prompt_tokens) * price.input_per_million_usd
        + Decimal(completion_tokens) * price.output_per_million_usd
    ) / USD_PER_MILLION_TOKENS
    return _decimal_text(calculated)


def _usage_token_count(
    usage: Mapping[str, object], aliases: Sequence[str], *, name: str
) -> int:
    values = [usage[alias] for alias in aliases if alias in usage]
    if not values:
        raise EnvelopeFailure(
            "provider_cost_error", f"paid response has no {name} token count"
        )
    if any(type(value) is not int or value < 0 for value in values):
        raise EnvelopeFailure(
            "provider_cost_error", f"paid response has invalid {name} tokens"
        )
    if any(value != values[0] for value in values[1:]):
        raise EnvelopeFailure(
            "provider_cost_error", f"paid response has conflicting {name} token counts"
        )
    return int(values[0])


def _parse_positive_decimal(value: object, *, name: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{name} is required for paid Zen models")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{name} must be a decimal number") from error
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{name} must be a positive finite decimal")
    return parsed


def _parse_nonnegative_decimal(value: object, *, name: str) -> Decimal:
    if isinstance(value, bool):
        raise EnvelopeFailure("provider_cost_error", f"{name} is invalid")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise EnvelopeFailure("provider_cost_error", f"{name} is invalid") from error
    if not parsed.is_finite() or parsed < 0:
        raise EnvelopeFailure("provider_cost_error", f"{name} is invalid")
    return parsed


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text == "-0" else text


def _module_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _error_receipt(response: TransportResponse) -> dict[str, object]:
    body_bytes = response.body.encode("utf-8")
    receipt: dict[str, object] = {
        "http_status": response.status,
        "request_id": _request_id(response.headers),
        "body_bytes": len(body_bytes),
        "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
    }
    return receipt


def _paid_error_cost_receipt(
    response: TransportResponse, model_id: str
) -> dict[str, str]:
    try:
        payload = _strict_json(response.body)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(payload, Mapping):
        return {}
    usage = payload.get("usage")
    result: dict[str, str] = {}
    try:
        provider_cost = _provider_cost(payload, usage)
        if provider_cost is not None:
            result["provider_reported_cost_usd"] = provider_cost
            result["uncached_calculated_cost_usd"] = _calculate_usage_cost(
                model_id, usage
            )
    except EnvelopeFailure:
        return result
    return result


def _provider_summary(calls: Sequence[Mapping[str, Any]]) -> dict[str, object]:
    succeeded = [call for call in calls if call.get("status") == "succeeded"]
    failed = [call for call in calls if call.get("status") == "failed"]
    paid = [
        call
        for call in calls
        if isinstance(call.get("model"), str)
        and str(call["model"]).startswith("opencode-paid/")
    ]
    return {
        "calls_attempted": len(calls),
        "calls_succeeded": len(succeeded),
        "calls_failed": len(failed),
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
        "cost_reporting_complete": (
            _responses_have_decimal(paid, "provider_reported_cost_usd")
            if paid
            else None
        ),
        "provider_reported_cost_usd": (
            _sum_response_decimals(paid, "provider_reported_cost_usd")
            if paid
            else None
        ),
        "uncached_calculated_cost_usd": (
            _sum_response_decimals(paid, "uncached_calculated_cost_usd")
            if paid
            else None
        ),
    }


def _sum_usage(calls: Sequence[Mapping[str, Any]], field_name: str) -> int | None:
    values = []
    for call in calls:
        response = call.get("response")
        usage = response.get("usage") if isinstance(response, Mapping) else None
        if not isinstance(usage, Mapping):
            return None
        try:
            if field_name == "prompt_tokens":
                value = _usage_token_count(
                    usage, ("prompt_tokens", "input_tokens"), name="input"
                )
            elif field_name == "completion_tokens":
                value = _usage_token_count(
                    usage, ("completion_tokens", "output_tokens"), name="output"
                )
            elif field_name == "reasoning_tokens":
                value = _reasoning_token_count(usage)
            else:
                value = usage.get(field_name)
                if type(value) is not int or value < 0:
                    return None
        except EnvelopeFailure:
            return None
        values.append(value)
    return sum(values) if values else None


def _reasoning_token_count(usage: Mapping[str, object]) -> int:
    values: list[object] = []
    for name in ("completion_tokens_details", "output_tokens_details"):
        if name not in usage:
            continue
        details = usage[name]
        if not isinstance(details, Mapping) or "reasoning_tokens" not in details:
            raise EnvelopeFailure(
                "provider_cost_error", "provider response has invalid reasoning tokens"
            )
        values.append(details["reasoning_tokens"])
    if not values or any(type(value) is not int or value < 0 for value in values):
        raise EnvelopeFailure(
            "provider_cost_error", "provider response has invalid reasoning tokens"
        )
    if any(value != values[0] for value in values[1:]):
        raise EnvelopeFailure(
            "provider_cost_error", "provider response has conflicting reasoning tokens"
        )
    return int(values[0])


def _sum_response_decimals(
    calls: Sequence[Mapping[str, Any]], field_name: str
) -> str | None:
    total = Decimal("0")
    if not calls:
        return None
    for call in calls:
        response = call.get("response")
        value = response.get(field_name) if isinstance(response, Mapping) else None
        if not isinstance(value, str):
            return None
        try:
            total += Decimal(value)
        except InvalidOperation:
            return None
    return _decimal_text(total)


def _responses_have_decimal(
    calls: Sequence[Mapping[str, Any]], field_name: str
) -> bool:
    return _sum_response_decimals(calls, field_name) is not None


def _request_id(headers: Mapping[str, str]) -> str | None:
    return next(
        (headers[name] for name in ("x-request-id", "request-id", "cf-ray") if headers.get(name)),
        None,
    )


def _strict_json(raw_json: str) -> object:
    return json.loads(
        raw_json,
        object_pairs_hook=_unique_object,
        parse_float=str,
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
