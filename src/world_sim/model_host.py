from __future__ import annotations

import hashlib
import http.client
import json
import os
import platform
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NoReturn, Protocol

from .survival.calibration import LEAN_CAMP_V1, survival_preset
from .survival.demo import result_sha256, survival_metrics
from .survival.engine import (
    SurvivalChoiceProvider,
    make_survival_world,
    run_survival,
    survival_view_for,
)
from .survival.models import DEFAULT_SURVIVOR_NAMES, SurvivalConfig, SurvivorView
from .survival.prompt import render_system_prompt, render_turn_prompt, response_schema
from .survival.protocol import (
    MODEL_MAX_COMPLETION_TOKENS,
    parse_model_response,
    parse_strict_model_json,
)


ADAPTER_NAME = "opencode-direct-model-apis"
WORLD_SIM_VERSION = "0.8.0"
DEFAULT_LIVE_MAX_CALLS = 12
DEFAULT_LIVE_MAX_COMPLETION_TOKENS = 4_096
DEFAULT_LIVE_TEMPERATURE = 0.2
DEFAULT_LIVE_TIMEOUT_SECONDS = 60.0
DEFAULT_LIVE_REASONING_EFFORT = "provider-default"
LIVE_REASONING_EFFORTS = ("provider-default", "low", "compatibility-first")
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
QUALIFICATION_ID = "paid-panel-qualification-002"
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
        )
        for assignment in assignments
    }
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
    try:
        result = run_survival(world, providers, days=days)
    except RuntimeError as error:
        if isinstance(error.__cause__, LivePaidBudgetFailure):
            failure = error.__cause__
            return {
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
        if isinstance(error.__cause__, LiveCallCapFailure):
            failure = error.__cause__
            return {
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
        if not isinstance(error.__cause__, LiveCallFailure):
            raise
        failed = calls[-1]
        return {
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
    return {
        **base,
        "status": "completed",
        "canonical_result_sha256": result_sha256(result),
        "metrics": survival_metrics(result),
        "provider_summary": _provider_summary(calls),
        "result": result.to_dict(),
    }


def run_paid_adapter_qualification(
    *,
    model_refs: Sequence[str],
    max_completion_tokens: int = MODEL_MAX_COMPLETION_TOKENS,
    temperature: float = DEFAULT_LIVE_TEMPERATURE,
    max_paid_usd: Decimal | str = QUALIFICATION_MAX_AUTHORIZATION_USD,
    timeout_seconds: float = 300.0,
    transport: ChatTransport | None = None,
    environ: Mapping[str, str] | None = None,
    checkpoint: Callable[[Mapping[str, object]], None] | None = None,
) -> dict[str, Any]:
    assignments = _assign_models(model_refs)
    _validate_qualification_config(
        assignments,
        max_completion_tokens=max_completion_tokens,
        temperature=temperature,
        max_paid_usd=max_paid_usd,
        timeout_seconds=timeout_seconds,
    )
    requests = tuple(
        _build_qualification_request(
            assignment,
            max_completion_tokens=max_completion_tokens,
            temperature=temperature,
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
            "reasoning_effort": "provider-default",
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
    max_paid_usd: Decimal | str,
    timeout_seconds: float,
) -> None:
    if tuple(assignment.model_ref for assignment in assignments) != PAID_QUALIFICATION_MODELS:
        raise ValueError(
            "qualify-live requires the frozen paid panel in protocol order"
        )
    if max_completion_tokens != MODEL_MAX_COMPLETION_TOKENS:
        raise ValueError(
            f"qualify-live requires {MODEL_MAX_COMPLETION_TOKENS} completion tokens"
        )
    if temperature != DEFAULT_LIVE_TEMPERATURE:
        raise ValueError(
            f"qualify-live requires temperature {DEFAULT_LIVE_TEMPERATURE}"
        )
    if timeout_seconds != 300.0:
        raise ValueError("qualify-live requires a 300-second timeout")
    limit = _parse_positive_decimal(max_paid_usd, name="max_paid_usd")
    if limit != QUALIFICATION_MAX_AUTHORIZATION_USD:
        raise ValueError(
            "qualify-live requires --max-paid-usd "
            f"{_decimal_text(QUALIFICATION_MAX_AUTHORIZATION_USD)}"
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
) -> dict[str, object]:
    return _build_provider_request(
        assignment,
        [
            {"role": "system", "content": QUALIFICATION_SYSTEM_PROMPT},
            {"role": "user", "content": QUALIFICATION_USER_PROMPT},
        ],
        max_completion_tokens=max_completion_tokens,
        temperature=temperature,
        reasoning_effort=None,
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
        {"role": "system", "content": render_system_prompt(view.name)},
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
    names = tuple(assignment.public_name for assignment in all_assignments)
    world = make_survival_world(
        names,
        seed=seed,
        config=world_config,
    )
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
        "cost_bound_scope": "first_simultaneous_chance_only",
        "calls": rows,
        "authorized_calls": max_calls,
        "world_max_calls": world_max_calls,
        "first_chance_cost_bound_usd": _decimal_text(total),
    }


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
            if provider == "opencode-paid" and model_id in {"grok-4.5", "grok-4.6"}
            else _ENDPOINTS[provider]
        )
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
    paid = [
        call
        for call in calls
        if isinstance(call.get("model"), str)
        and str(call["model"]).startswith("opencode-paid/")
    ]
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
