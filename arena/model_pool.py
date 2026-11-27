"""Round-robin one agent's inference across a pool of models.

Rate limits are charged per model (and per API key), so a model pool is free
capacity: when the day's favourite model answers 429, the agent keeps trading on
the next one instead of skipping the tick. The pool *is* the agent's
``model_client``, so nothing else in the agent (tools, strategy, ledger) knows or
cares that the answer came from a different model.

This module deliberately imports no provider error classes — pydantic-ai's error
types are only reachable through calfkit's vendored tree, and the signals we need
(``status_code``, transport failure) are duck-typed instead. That gap is filed as
calf-ai/calfkit-sdk#377.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

import httpx

from calfkit.providers.pydantic_ai.model_client import PydanticModelClient

if TYPE_CHECKING:
    # calfkit exposes ``PydanticModelClient`` as the extension point for custom
    # model clients but not the pydantic-ai message types it speaks, so the
    # annotations reach into the vendored tree. Annotations only: nothing here is
    # imported at runtime, so a calfkit reshuffle breaks types, never execution.
    from calfkit._vendor.pydantic_ai.messages import ModelMessage, ModelResponse
    from calfkit._vendor.pydantic_ai.models import Model, ModelRequestParameters
    from calfkit._vendor.pydantic_ai.profiles import ModelProfile
    from calfkit._vendor.pydantic_ai.settings import ModelSettings

logger = logging.getLogger(__name__)

# Long enough to clear a per-minute window, short enough that a throttled model
# rejoins the rotation within the same trading session.
DEFAULT_COOLDOWN_SECONDS = 30.0

# A 429 from a free tier is usually a *quota* window (OpenRouter resets daily, OpenCode
# per rolling 5h), not a blip. The OpenAI SDK's Retry-After logic is disabled for pooled
# clients, so this is the only backoff: it must outlast a quota, or the pool spends the
# session re-discovering the same exhausted model every 30 seconds.
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 900.0

# Statuses where a *different* model can answer the same request: request timeout /
# conflict / throttled, plus 403/404, which mean "this model id is not usable with this
# key" — a typo'd id must cost one tick, not every Nth tick forever.
_TRANSIENT_STATUS = frozenset({403, 404, 408, 409, 429})


def normalize_model_ids(value: str | Sequence[str]) -> list[str]:
    """Normalize a configured model (one ID, or a pool) into a clean pool.

    Shared by config validation and the deploy CLI: both must reject a pool that
    rotates into the same rate-limit bucket it was built to escape.
    """
    ids = [value] if isinstance(value, str) else list(value)
    cleaned = [identifier.strip() for identifier in ids]
    if not cleaned or any(not identifier for identifier in cleaned):
        raise ValueError("model needs at least one model ID, none of them blank")
    repeated = sorted({identifier for identifier in cleaned if cleaned.count(identifier) > 1})
    if repeated:
        raise ValueError(f"model pool repeats {', '.join(repeated)}")
    return cleaned


def _status_of(error: BaseException) -> int | None:
    """The HTTP status behind a failure, however the layer in between wrapped it."""
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return status
    # A bare httpx.HTTPStatusError (a provider client that never mapped it to a model
    # error) keeps the status on its response instead of on the exception.
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def is_transient(error: BaseException) -> bool:
    """Could another model have answered this request?

    A 429 or a 5xx is the provider's problem, so the pool rotates past it. A 400 is
    the caller's bug — rotating would only burn the other models' quotas.
    """
    status = _status_of(error)
    if status is not None:
        return status in _TRANSIENT_STATUS or status >= 500
    # pydantic-ai reports a transport failure as its provider error (a model name and
    # no status); the underlying httpx exception stays chained and never reaches us.
    if hasattr(error, "model_name"):
        return True
    return isinstance(error, (httpx.HTTPError, TimeoutError, ConnectionError))


class RoundRobinModel(PydanticModelClient):
    """A ``model_client`` that spreads requests over several models.

    Order is strict round-robin, so load — and therefore rate-limit pressure — is
    spread evenly. A model that fails transiently rests for ``cooldown_seconds``
    and is tried last meanwhile; once the cooldown lapses it rejoins the rotation
    at its old position.
    """

    def __init__(
        self,
        models: Sequence[Model],
        *,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        rate_limit_cooldown_seconds: float = DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        single_attempt: bool = False,
        paid_fallback: str | None = None,
    ) -> None:
        super().__init__()
        self.models = list(models)
        if not self.models:
            raise ValueError("RoundRobinModel needs at least one model")
        self.cooldown_seconds = cooldown_seconds
        self.rate_limit_cooldown_seconds = rate_limit_cooldown_seconds
        self._clock = clock
        self._single_attempt = single_attempt
        self._paid_fallback = paid_fallback
        self._resting_until = [0.0] * len(self.models)
        self._cursor = 0
        self._retired: set[int] = set()

    @property
    def model_name(self) -> str:
        return "round-robin:" + ",".join(model.model_name for model in self.models)

    @property
    def system(self) -> str:
        return self.models[0].system

    @property
    def base_url(self) -> str | None:
        return self.models[0].base_url

    @property
    def profile(self) -> ModelProfile:
        """Capabilities are the pool's, not whichever model answered last.

        Telemetry only in practice: each delegate prepares its own request, so this
        is never the capability set a request is validated against.
        """
        return self.models[0].profile

    def _slot(self, model_id: str) -> int:
        return [model.model_name for model in self.models].index(model_id)

    def retire_model(self, model_id: str) -> None:
        index = self._slot(model_id)
        if len(self._retired | {index}) == len(self.models):
            raise ValueError("Cannot retire the last active model")
        self._retired.add(index)

    def replace_model(self, model_id: str, replacement: Model) -> None:
        index = self._slot(model_id)
        if any(
            other != index and model.model_name == replacement.model_name
            for other, model in enumerate(self.models)
        ):
            raise ValueError("Replacement would duplicate a model in the pool")
        self.models[index] = replacement
        self._retired.discard(index)
        self._resting_until[index] = 0.0

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        """Ask each model in rotation order; return the first answer."""
        last_error: BaseException | None = None
        order = self._rotation()
        if self._single_attempt:
            primary = order[0]
            attempts = [primary]
            if self.models[primary].model_name.endswith(":free") and self._paid_fallback:
                attempts.extend(
                    index for index in order
                    if index != primary and self.models[index].model_name == self._paid_fallback
                )
            order = attempts
        elif all(self._resting_until[index] > self._clock() for index in order):
            # Probe only one model when the whole pool is cooling down.
            order = order[:1]
        for index in order:
            if index in self._retired:
                continue
            # Claim the slot before awaiting: concurrent requests (max_workers > 1)
            # must not both start on the same model.
            self._cursor = (index + 1) % len(self.models)
            model = self.models[index]
            try:
                response = await model.request(messages, model_settings, model_request_parameters)
            except Exception as error:
                if not is_transient(error):
                    raise
                if self.models[index] is model:
                    self._rest(index, error)
                last_error = error
                continue
            logger.debug("pool served this request with %s", model.model_name)
            return response
        assert last_error is not None, "rotation visits every model exactly once"
        raise last_error

    def _rotation(self) -> list[int]:
        """This request's model order: fresh from the cursor, then the resting ones.

        Resting models are attempted last rather than dropped — refusing to trade
        because every model is briefly throttled is worse than one more 429.
        """
        now = self._clock()
        size = len(self.models)
        start = self._cursor % size
        fresh: list[int] = []
        resting: list[int] = []
        for offset in range(size):
            index = (start + offset) % size
            if index in self._retired:
                continue
            bucket = resting if self._resting_until[index] > now else fresh
            bucket.append(index)
        return [*fresh, *resting]

    def _rest(self, index: int, error: BaseException) -> None:
        # A throttled model needs the whole quota window; anything else is a shorter blip.
        cooldown = (
            self.rate_limit_cooldown_seconds
            if _status_of(error) == 429
            else self.cooldown_seconds
        )
        self._resting_until[index] = self._clock() + cooldown
        logger.warning(
            "%s is unavailable (%.0fs cooldown), resting it: %s",
            self.models[index].model_name,
            cooldown,
            error,
        )
