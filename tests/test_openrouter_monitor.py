import asyncio
import json

import httpx
import pytest

from arena.openrouter_monitor import OpenRouterMonitor

OLD = "someone/old:free"
PAID = "openai/gpt-oss-20b"
NEW = "someone/new:free"
TOOLS = [
    {
        "type": "function",
        "function": {"name": "get_portfolio", "parameters": {"type": "object", "properties": {}}},
    }
]


def entry(model_id, **overrides):
    return {
        "id": model_id,
        "context_length": 32_768,
        "pricing": {"prompt": "0", "completion": "0", "request": "0"},
        "supported_parameters": ["tools"],
        **overrides,
    }


def answer(name="get_portfolio", arguments="{}"):
    return {
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "tool_calls": [{
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }],
            },
        }],
    }


class Scenario:
    def __init__(self, state_path):
        self.state_path = state_path
        self.rows = [entry(PAID), entry(NEW)]
        self.status = 200
        self.calls = []
        self.swaps = []
        self.retired = []
        self.mail = []
        self.fail_mail = False
        self.probe_answer = answer()
        self.probe_status = 200
        self.probe_response = lambda _request: self.probe_answer

    async def send(self, subject, body):
        if self.fail_mail:
            raise OSError("SMTP down")
        self.mail.append((subject, body))

    def handle(self, request):
        if request.url.path.endswith("/models"):
            return httpx.Response(self.status, json={"data": self.rows})
        body = json.loads(request.content)
        self.calls.append(body)
        return httpx.Response(self.probe_status, json=self.probe_response(body))

    def client(self):
        return httpx.AsyncClient(
            base_url="https://openrouter.example/api/v1/",
            transport=httpx.MockTransport(self.handle),
        )

    def monitor(self, client, model_ids=(OLD, PAID), **options):
        return OpenRouterMonitor(
            client=client,
            state_path=self.state_path,
            model_ids=model_ids,
            replace=options.pop("replace", lambda old, new: self.swaps.append((old, new))),
            retire=options.pop("retire", self.retired.append),
            send=self.send,
            tools=options.pop("tools", TOOLS),
            **options,
        )


async def test_confirm_delisting_replaces_one_slot_and_persists_override_even_if_email_is_down(
    tmp_path,
):
    scenario = Scenario(tmp_path / "state.json")
    scenario.fail_mail = True
    async with scenario.client() as client:
        monitor = scenario.monitor(client)
        await monitor.check()
        assert not scenario.retired
        assert monitor.effective_models() == [OLD, PAID]
        # A failed catalog lookup must not count as another verified omission.
        scenario.status = 503
        await monitor.check()
        assert not scenario.retired
        scenario.status = 200
        await monitor.check()
        assert scenario.retired == [OLD]
        assert scenario.swaps == [(OLD, NEW)]
        assert [call["model"] for call in scenario.calls] == [NEW] * 3
        assert all(call["tools"] == TOOLS for call in scenario.calls)

        restarted = scenario.monitor(client)
        assert restarted.effective_models() == [NEW, PAID]
        assert restarted.retired_models() == set()
        scenario.fail_mail = False
        await restarted.check()
        await restarted.check()
        assert len(scenario.mail) == 1
        assert OLD in scenario.mail[0][1] and NEW in scenario.mail[0][1]
        assert scenario.monitor(client, ("operator/model:free", PAID)).effective_models() == [
            "operator/model:free", PAID,
        ]


async def test_announced_expiration_warns_once_without_retiring_a_listed_model(tmp_path):
    scenario = Scenario(tmp_path / "state.json")
    scenario.rows.append(entry(OLD, expiration_date="2030-10-10"))
    async with scenario.client() as client:
        monitor = scenario.monitor(client)
        await monitor.check()
        await scenario.monitor(client).check()
    assert len(scenario.mail) == 1
    assert "2030-10-10" in scenario.mail[0][1]
    assert not scenario.swaps and not scenario.retired and not scenario.calls


@pytest.mark.parametrize(
    ("status", "payload"),
    [(429, {}), (200, answer(arguments='{"unexpected": 1}')), (200, answer("wrong"))],
)
async def test_bad_probes_keep_paid_model_and_notify_only_once(tmp_path, status, payload):
    scenario = Scenario(tmp_path / "state.json")
    scenario.probe_answer = payload
    scenario.probe_status = status
    scenario.rows.extend([
        entry("someone/not-free"),
        entry("someone/charges:free", pricing={"prompt": "0", "completion": "0.01"}),
        entry("someone/no-price:free", pricing={"prompt": "0"}),
        entry("someone/bad-price:free", pricing={"prompt": "0", "completion": "unknown"}),
        entry("someone/no-tools:free", supported_parameters=[]),
        entry("someone/short:free", context_length=4000),
    ])
    async with scenario.client() as client:
        monitor = scenario.monitor(client)
        await monitor.check()
        await monitor.check()
        restarted = scenario.monitor(client)
        await restarted.check()
        assert restarted.retired_models() == {OLD}
        assert restarted.effective_models() == [OLD, PAID]
    assert not scenario.swaps
    assert scenario.retired == [OLD]
    assert len(scenario.mail) == 1
    assert [call["model"] for call in scenario.calls] == [NEW, NEW]


async def test_return_to_catalog_clears_missing_count(tmp_path):
    scenario = Scenario(tmp_path / "state.json")
    async with scenario.client() as client:
        monitor = scenario.monitor(client)
        await monitor.check()
        scenario.rows.append(entry(OLD))
        await monitor.check()
        scenario.rows.pop()
        await monitor.check()
    assert not scenario.retired


async def test_retired_model_that_reappears_is_probed_and_restored(tmp_path):
    scenario = Scenario(tmp_path / "state.json")
    scenario.probe_status = 429
    async with scenario.client() as client:
        monitor = scenario.monitor(client)
        await monitor.check()
        await monitor.check()
        assert monitor.retired_models() == {OLD}

        scenario.rows.append(entry(OLD))
        scenario.probe_status = 200
        await monitor.check()

    assert [call["model"] for call in scenario.calls][-3:] == [OLD] * 3
    assert scenario.swaps == [(OLD, OLD)]
    assert monitor.retired_models() == set()
    assert "restored" in scenario.mail[-1][1]


async def test_corrupt_slot_state_is_discarded_and_catalog_checks_continue(tmp_path):
    scenario = Scenario(tmp_path / "state.json")
    scenario.state_path.write_text(json.dumps({OLD: {"missing_checks": "invalid"}}))
    async with scenario.client() as client:
        monitor = scenario.monitor(client)
        await monitor.check()
        await monitor.check()
    assert scenario.swaps == [(OLD, NEW)]


async def test_replacement_preserves_the_last_observed_context_capacity(tmp_path):
    scenario = Scenario(tmp_path / "state.json")
    larger = "someone/qualified:free"
    scenario.rows.extend([entry(OLD, context_length=100_000), entry(larger, context_length=100_000)])
    async with scenario.client() as client:
        await scenario.monitor(client).check()
        scenario.rows = [row for row in scenario.rows if row["id"] != OLD]
        restarted = scenario.monitor(client)
        await restarted.check()
        await restarted.check()
    assert [call["model"] for call in scenario.calls] == [larger] * 3
    assert scenario.swaps == [(OLD, larger)]


async def test_calculator_probe_checks_exact_arguments(tmp_path):
    scenario = Scenario(tmp_path / "state.json")
    calculator = {
        "type": "function",
        "function": {
            "name": "calculator",
            "parameters": {
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
            },
        },
    }
    async with scenario.client() as client:
        with pytest.raises(ValueError, match="requires the get_portfolio tool"):
            scenario.monitor(client, tools=[calculator])
        monitor = scenario.monitor(
            client, tools=[*TOOLS, calculator], retire=None, reasoning_effort="low",
        )
        await monitor.check()
        await monitor.check()
        assert monitor.retired_models() == {OLD}

        def probe_response(request):
            if "calculator" in request["messages"][0]["content"]:
                return answer("calculator", '{"expression": "123 * 7"}')
            return answer()

        scenario.probe_response = probe_response
        await monitor.check()
    assert scenario.swaps == [(OLD, NEW)]
    assert all(call["tools"] == [*TOOLS, calculator] for call in scenario.calls)
    assert all(call["reasoning_effort"] == "low" for call in scenario.calls)


async def test_ignores_invalid_state_and_empty_catalog(tmp_path):
    scenario = Scenario(tmp_path / "state.json")
    scenario.state_path.write_text("[]")
    scenario.rows = []
    async with scenario.client() as client:
        monitor = scenario.monitor(client)
        await monitor.check()
        await monitor.check()
    assert not scenario.retired


async def test_operator_pinning_candidate_stops_duplicate_restoration(tmp_path):
    scenario = Scenario(tmp_path / "state.json")
    scenario.state_path.write_text(json.dumps({OLD: {"replacement": NEW}}))
    async with scenario.client() as client:
        monitor = scenario.monitor(client, (OLD, PAID, NEW))
    assert monitor.effective_models() == [OLD, PAID, NEW]


async def test_save_failure_keeps_live_slot_active(tmp_path, monkeypatch):
    scenario = Scenario(tmp_path / "state.json")

    def denied(*_):
        raise OSError("write failed")

    async with scenario.client() as client:
        monitor = scenario.monitor(client)
        await monitor.check()
        with monkeypatch.context() as patch:
            patch.setattr("arena.openrouter_monitor.os.replace", denied)
            with pytest.raises(OSError, match="write failed"):
                await monitor.check()
        assert monitor.retired_models() == set()
        assert not scenario.retired
        await monitor.check()
    assert scenario.swaps == [(OLD, NEW)]


async def test_replacement_callback_failure_restores_retired_state_on_disk(tmp_path):
    scenario = Scenario(tmp_path / "state.json")

    def fail(*_):
        raise RuntimeError("could not replace")

    async with scenario.client() as client:
        monitor = scenario.monitor(client, replace=fail)
        await monitor.check()
        with pytest.raises(RuntimeError, match="could not replace"):
            await monitor.check()
        restarted = scenario.monitor(client)
        assert restarted.retired_models() == {OLD}
        assert restarted.effective_models() == [OLD, PAID]
        await restarted.check()
    assert scenario.swaps == [(OLD, NEW)]


@pytest.mark.parametrize("candidates", [1, 3])
async def test_candidate_probe_budget_is_shared_across_retired_slots(tmp_path, candidates):
    scenario = Scenario(tmp_path / "state.json")
    another_old = "someone/also-old:free"
    scenario.rows = [entry(PAID)] + [entry(f"candidate/{idx}:free") for idx in range(candidates)]
    scenario.probe_status = 429
    async with scenario.client() as client:
        monitor = scenario.monitor(client, (OLD, another_old, PAID))
        await monitor.check()
        await monitor.check()
    assert len(scenario.calls) == min(candidates, 2)
    assert len({call["model"] for call in scenario.calls}) == len(scenario.calls)
    assert scenario.retired == [OLD, another_old]
    assert len(scenario.mail) == 2


async def test_periodic_run_retries_when_a_cycle_raises(tmp_path, monkeypatch):
    scenario = Scenario(tmp_path / "state.json")
    async with scenario.client() as client:
        monitor = scenario.monitor(client)
        cycle_count = 0

        async def check():
            nonlocal cycle_count
            cycle_count += 1
            if cycle_count == 1:
                raise OSError("fake transient failure")
            raise asyncio.CancelledError

        monkeypatch.setattr(monitor, "check", check)
        with pytest.raises(asyncio.CancelledError):
            await monitor.run(interval_seconds=0)
    assert cycle_count == 2


async def test_persistent_earlier_slot_cannot_starve_a_later_slot(tmp_path):
    scenario = Scenario(tmp_path / "state.json")
    another_old = "someone/also-old:free"
    low_context = "candidate/available-low:free"
    scenario.rows.extend([entry(OLD, context_length=128_000), entry(another_old)])
    scenario.probe_response = lambda req: answer() if req["model"] == low_context else answer("bad")
    async with scenario.client() as client:
        monitor = scenario.monitor(client, (OLD, another_old, PAID))
        await monitor.check()
        scenario.rows = [
            entry(PAID),
            entry(low_context),
            entry("candidate/high-a:free", context_length=128_000),
            entry("candidate/high-b:free", context_length=128_000),
        ]
        await monitor.check()
        await monitor.check()
        assert not scenario.swaps
        await monitor.check()
    assert scenario.swaps == [(another_old, low_context)]
    assert monitor.retired_models() == {OLD}
