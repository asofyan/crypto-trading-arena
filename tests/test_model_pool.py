"""One agent, several models: the pool that spends inference across all of them.

Rate limits are charged per model, so an agent holding a pool keeps trading when
its favourite model answers 429 — the pool simply asks the next one and lets the
throttled model rest. These tests stay offline: the delegates are fakes.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from arena.model_pool import RoundRobinModel, is_transient, normalize_model_ids


class FakeHTTPError(Exception):
    """Stands in for pydantic-ai's ModelHTTPError, which the pool reads by attribute.

    The pool never imports the vendored error class; it only consults
    ``status_code``, so any object carrying one behaves identically.
    """

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"status {status_code}")


class FakeTransportError(Exception):
    """pydantic-ai reports transport failures as a provider error: name, no status.

    The underlying httpx/OpenAI exception is chained and never reaches the pool, so
    this is the shape ``is_transient`` actually sees in production.
    """

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        super().__init__("Connection error.")


class FakeModel:
    """A model delegate that answers with its name, or fails on request."""

    def __init__(self, name: str, error: Exception | None = None) -> None:
        self._name = name
        self.error = error
        self.calls = 0
        self.profile = object()
        self.base_url = f"https://{name}.example/v1"

    @property
    def model_name(self) -> str:
        return self._name

    @property
    def system(self) -> str:
        return "fake"

    async def request(self, messages, model_settings, model_request_parameters) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return f"answer from {self.model_name}"


class FakeClock:
    """Monotonic clock the tests advance by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def _ask(model: RoundRobinModel) -> str:
    return await model.request([], None, None)


async def test_pool_hands_consecutive_requests_to_different_models():
    a, b, c = FakeModel("a"), FakeModel("b"), FakeModel("c")
    pool = RoundRobinModel([a, b, c], clock=FakeClock())

    answers = [await _ask(pool) for _ in range(4)]

    assert answers == ["answer from a", "answer from b", "answer from c", "answer from a"]
    assert (a.calls, b.calls, c.calls) == (2, 1, 1)


async def test_a_throttled_model_rests_while_the_others_carry_the_agent():
    clock = FakeClock()
    throttled = FakeModel("throttled", FakeHTTPError(429))
    standby = FakeModel("standby")
    pool = RoundRobinModel([throttled, standby], cooldown_seconds=30, clock=clock)

    first = await _ask(pool)
    second = await _ask(pool)

    assert (first, second) == ("answer from standby", "answer from standby")
    assert throttled.calls == 1, "a resting model must not be asked again during its cooldown"
    assert standby.calls == 2


async def test_the_throttled_model_returns_to_the_rotation_once_its_cooldown_lapses():
    clock = FakeClock()
    throttled = FakeModel("throttled", FakeHTTPError(429))
    standby = FakeModel("standby")
    pool = RoundRobinModel([throttled, standby], rate_limit_cooldown_seconds=30, clock=clock)
    await _ask(pool)

    clock.now = 31.0
    throttled.error = None
    answer = await _ask(pool)

    assert answer == "answer from throttled"
    assert standby.calls == 1


async def test_a_server_error_rests_for_the_short_cooldown_not_the_quota_window():
    """A 5xx is a blip; only a 429 needs the whole quota window."""
    clock = FakeClock()
    flaky = FakeModel("flaky", FakeHTTPError(503))
    standby = FakeModel("standby")
    pool = RoundRobinModel([flaky, standby], cooldown_seconds=30, clock=clock)
    await _ask(pool)

    clock.now = 31.0
    flaky.error = None

    assert await _ask(pool) == "answer from flaky"


async def test_a_rate_limited_model_rests_until_the_quota_window_not_30_seconds():
    """A 429 on a free tier is a daily quota, not a blip: retrying in 30s burns calls."""
    clock = FakeClock()
    throttled = FakeModel("throttled", FakeHTTPError(429))
    paid = FakeModel("paid")
    pool = RoundRobinModel(
        [throttled, paid], cooldown_seconds=30, rate_limit_cooldown_seconds=900, clock=clock
    )

    assert await _ask(pool) == "answer from paid"
    clock.now = 600.0
    assert await _ask(pool) == "answer from paid", "still resting at 10 minutes"
    assert throttled.calls == 1

    clock.now = 901.0
    throttled.error = None
    assert await _ask(pool) == "answer from throttled"


async def test_a_transport_failure_rotates_instead_of_aborting_the_tick():
    offline = FakeModel("offline", FakeTransportError("offline"))
    standby = FakeModel("standby")
    pool = RoundRobinModel([offline, standby], clock=FakeClock())

    assert await _ask(pool) == "answer from standby"
    assert offline.calls == 1


async def test_a_model_the_key_cannot_use_rests_instead_of_killing_every_nth_tick():
    """A typo'd model id is a bad model, not a bad request: rotating heals it."""
    wrong_id = FakeModel("gpt-typo", FakeHTTPError(404))
    good = FakeModel("good")
    pool = RoundRobinModel([wrong_id, good], clock=FakeClock())

    answers = [await _ask(pool) for _ in range(3)]

    assert answers == ["answer from good"] * 3
    assert wrong_id.calls == 1, "one attempt, then it rests for the cooldown"


async def test_concurrent_requests_do_not_land_on_the_same_model():
    """Two ticks in flight at once (max_workers > 1) must still split the pool."""
    picking = asyncio.Event()
    release = asyncio.Event()

    class SlowModel(FakeModel):
        async def request(self, *args):
            self.calls += 1
            picking.set()
            await release.wait()
            return f"answer from {self.model_name}"

    a, b = SlowModel("a"), SlowModel("b")
    pool = RoundRobinModel([a, b], clock=FakeClock())

    first = asyncio.create_task(_ask(pool))
    await picking.wait()
    second = asyncio.create_task(_ask(pool))
    await asyncio.sleep(0)
    release.set()

    assert {await first, await second} == {"answer from a", "answer from b"}


async def test_a_fully_resting_pool_tries_one_model_not_all_of_them():
    """Every model in a quota window must cost one probe per tick, not one per model."""
    clock = FakeClock()
    a = FakeModel("a", FakeHTTPError(429))
    b = FakeModel("b", FakeHTTPError(429))
    pool = RoundRobinModel([a, b], rate_limit_cooldown_seconds=900, clock=clock)

    with pytest.raises(FakeHTTPError):
        await _ask(pool)
    assert (a.calls, b.calls) == (1, 1), "first tick: each model gets one attempt"

    with pytest.raises(FakeHTTPError):
        await _ask(pool)

    assert a.calls + b.calls == 3, "a resting pool probes one model, not every model"


async def test_a_pool_of_throttled_models_still_tries_every_model_before_failing():
    a = FakeModel("a", FakeHTTPError(429))
    b = FakeModel("b", FakeHTTPError(429))
    pool = RoundRobinModel([a, b], clock=FakeClock())

    with pytest.raises(FakeHTTPError) as raised:
        await _ask(pool)

    assert raised.value.status_code == 429
    assert (a.calls, b.calls) == (1, 1), "each model gets exactly one attempt per request"


async def test_a_permanent_failure_is_not_passed_to_another_model():
    broken = FakeModel("broken", FakeHTTPError(400))
    innocent = FakeModel("innocent")
    pool = RoundRobinModel([broken, innocent], clock=FakeClock())

    with pytest.raises(FakeHTTPError):
        await _ask(pool)

    assert innocent.calls == 0, "a rejected request is the caller's bug, not the model's limit"


async def test_pool_reports_its_whole_pool_as_its_identity():
    a, b = FakeModel("gpt-a"), FakeModel("gpt-b")
    pool = RoundRobinModel([a, b], clock=FakeClock())

    assert pool.model_name == "round-robin:gpt-a,gpt-b"
    assert pool.system == "fake"
    assert pool.base_url == a.base_url
    assert pool.profile is a.profile, "capabilities are the pool's, not whichever model answered last"


def test_a_pool_needs_at_least_one_model():
    with pytest.raises(ValueError, match="at least one"):
        RoundRobinModel([])


def _status_error(status_code: int) -> httpx.HTTPStatusError:
    """An httpx error escaping a provider that never mapped it to a model error."""
    return httpx.HTTPStatusError(
        "boom",
        request=httpx.Request("POST", "https://llm.example/v1/chat/completions"),
        response=httpx.Response(status_code),
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FakeHTTPError(429), True),
        (FakeHTTPError(500), True),
        (FakeHTTPError(503), True),
        (FakeHTTPError(408), True),
        (FakeHTTPError(400), False),
        (FakeHTTPError(422), False),
        (FakeHTTPError(404), True),
        (httpx.ConnectError("boom"), True),
        (FakeTransportError("model"), True),
        (_status_error(400), False),
        (_status_error(503), True),
        (TimeoutError(), True),
        (ValueError("bug in our code"), False),
    ],
)
def test_only_failures_another_model_could_survive_are_retried(error, expected):
    assert is_transient(error) is expected


class TextModel(FakeModel):
    """A delegate that speaks the real wire type, so the agent loop can consume it."""

    async def request(self, messages, model_settings, model_request_parameters):
        from calfkit._vendor.pydantic_ai.messages import ModelResponse, TextPart

        self.calls += 1
        if self.error is not None:
            raise self.error
        return ModelResponse(parts=[TextPart(content=f"answer from {self.model_name}")])


async def test_the_agent_loop_survives_a_rate_limited_model():
    from calfkit._vendor.pydantic_ai import Agent as PydanticAgent

    throttled = TextModel("a", FakeHTTPError(429))
    standby = TextModel("b")
    loop = PydanticAgent(RoundRobinModel([throttled, standby], clock=FakeClock()))

    result = await loop.run("hi")

    assert result.output == "answer from b"


async def test_the_agent_loop_accepts_the_pool_as_its_model():
    """The pool must satisfy pydantic-ai's Model contract, not just our own fake.

    This drives the real agent loop (the one calfkit's Agent wraps) so a contract
    break — a missing profile, an unprepared request — fails here, offline.
    """
    from calfkit._vendor.pydantic_ai import Agent as PydanticAgent

    a, b = TextModel("a"), TextModel("b")
    loop = PydanticAgent(RoundRobinModel([a, b], clock=FakeClock()))

    first = await loop.run("hi")
    second = await loop.run("hi")

    assert (first.output, second.output) == ("answer from a", "answer from b")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("gpt-a", ["gpt-a"]),
        ("  gpt-a  ", ["gpt-a"]),
        (["gpt-a", " gpt-b "], ["gpt-a", "gpt-b"]),
    ],
)
def test_model_ids_are_normalized(value, expected):
    assert normalize_model_ids(value) == expected


@pytest.mark.parametrize("value", ["", "   ", [], ["gpt-a", ""], ["gpt-a", "gpt-a"]])
def test_unusable_model_pools_are_rejected(value):
    with pytest.raises(ValueError):
        normalize_model_ids(value)


async def test_retirement_and_replacement_preserve_the_round_robin_slot():
    free = FakeModel("free")
    paid = FakeModel("paid")
    pool = RoundRobinModel([free, paid], clock=FakeClock())
    pool.retire_model("free")
    assert [await _ask(pool), await _ask(pool)] == ["answer from paid"] * 2

    pool.replace_model("free", FakeModel("new"))
    assert [await _ask(pool), await _ask(pool)] == ["answer from new", "answer from paid"]
    assert free.calls == 0


async def test_replaced_inflight_failure_cannot_cool_down_the_new_model():
    started = asyncio.Event()
    release = asyncio.Event()

    class DelayedFailure(FakeModel):
        async def request(self, *args):
            started.set()
            await release.wait()
            raise FakeHTTPError(429)

    pool = RoundRobinModel([DelayedFailure("old"), FakeModel("paid")], clock=FakeClock())
    request = asyncio.create_task(_ask(pool))
    await started.wait()
    pool.replace_model("old", FakeModel("new"))
    release.set()

    assert await request == "answer from paid"
    assert await _ask(pool) == "answer from new"


def test_pool_guards_model_slot_updates():
    pool = RoundRobinModel([FakeModel("one"), FakeModel("two")])
    with pytest.raises(ValueError, match="duplicate"):
        pool.replace_model("one", FakeModel("two"))
    with pytest.raises(ValueError, match="last"):
        pool.retire_model("two")
        pool.retire_model("one")


async def test_inflight_request_skips_a_slot_retired_during_its_first_attempt():
    started = asyncio.Event()
    release = asyncio.Event()

    class DelayedFailure(FakeModel):
        async def request(self, *args):
            started.set()
            await release.wait()
            raise FakeHTTPError(503)

    retiring = FakeModel("retiring")
    pool = RoundRobinModel([DelayedFailure("slow"), retiring, FakeModel("paid")])
    request = asyncio.create_task(_ask(pool))
    await started.wait()
    pool.retire_model("retiring")
    release.set()

    assert await request == "answer from paid"
    assert retiring.calls == 0


async def test_single_attempt_defers_fallback_until_next_tick():
    throttled = FakeModel("throttled", FakeHTTPError(429))
    standby = FakeModel("standby")
    pool = RoundRobinModel([throttled, standby], clock=FakeClock(), single_attempt=True)

    with pytest.raises(FakeHTTPError):
        await _ask(pool)

    assert throttled.calls == 1
    assert standby.calls == 0
    assert await _ask(pool) == "answer from standby"


@pytest.mark.parametrize("error", [FakeHTTPError(429), FakeTransportError("free")])
async def test_single_decision_falls_back_directly_to_paid_model(error):
    primary = FakeModel("primary:free", error)
    other_free = FakeModel("secondary:free")
    paid = FakeModel("openai/gpt-oss-20b")
    pool = RoundRobinModel(
        [primary, other_free, paid],
        single_attempt=True,
        paid_fallback=paid.model_name,
    )

    assert await _ask(pool) == "answer from openai/gpt-oss-20b"
    assert (primary.calls, other_free.calls, paid.calls) == (1, 0, 1)


@pytest.mark.parametrize("error", [None, FakeHTTPError(429)])
async def test_single_decision_stops_at_a_success_or_paid_failure(error):
    primary = FakeModel("primary:free", error)
    other_free = FakeModel("secondary:free")
    paid = FakeModel("openai/gpt-oss-20b", FakeHTTPError(503))
    pool = RoundRobinModel(
        [primary, other_free, paid],
        single_attempt=True,
        paid_fallback=paid.model_name,
    )

    if error is None:
        assert await _ask(pool) == "answer from primary:free"
        assert paid.calls == 0
    else:
        with pytest.raises(FakeHTTPError, match="503"):
            await _ask(pool)
        assert paid.calls == 1
    assert (primary.calls, other_free.calls) == (1, 0)
