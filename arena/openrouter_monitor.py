"""Detect OpenRouter delistings and replace free models in their original slots."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated

import httpx
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)
DEFAULT_INTERVAL_SECONDS = 900
DEFAULT_MIN_CONTEXT = 16_000
PROBES_PER_CANDIDATE = 3
CANDIDATES_PER_CHECK = 2


def _is_free(model: dict) -> bool:
    pricing = model.get("pricing")
    if not isinstance(pricing, dict) or not {"prompt", "completion"} <= pricing.keys():
        return False
    try:
        return all(Decimal(str(price)).is_zero() for price in pricing.values())
    except InvalidOperation:
        return False


class _Slot(BaseModel):
    replacement: str | None = None
    retired: bool = False
    missing_checks: int = Field(default=0, ge=0)
    context_length: int = Field(default=0, ge=0)
    expiration: str | None = None
    notified_missing: bool = False
    pending: list[Annotated[list[str], Field(min_length=2, max_length=2)]] = Field(
        default_factory=list
    )


class OpenRouterMonitor:
    """Use verified catalog omissions to remove slots and test replacements off the trade path.

    One instance owns a state path. Original configured IDs key the state so an
    operator can replace a slot directly by changing its configured ID.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        state_path: Path,
        model_ids: Sequence[str],
        replace: Callable[[str, str], None],
        send: Callable[[str, str], Awaitable[None]],
        tools: list[dict],
        retire: Callable[[str], None] | None = None,
        min_context: int = DEFAULT_MIN_CONTEXT,
        reasoning_effort: str | None = None,
        can_probe: Callable[[], bool] | None = None,
    ) -> None:
        names = {tool["function"]["name"] for tool in tools}
        if "get_portfolio" not in names:
            raise ValueError("OpenRouter monitor requires the get_portfolio tool schema")
        self._client = client
        self._path = state_path
        self._configured = list(model_ids)
        self._replace = replace
        self._retire = retire
        self._send = send
        self._tools = tools
        self._reasoning_effort = reasoning_effort
        self._can_probe = can_probe
        self._probe_cases = [
            ("get_portfolio", {}, "Call get_portfolio with no arguments.")
        ] * PROBES_PER_CANDIDATE
        if "calculator" in names:
            self._probe_cases[1] = (
                "calculator",
                {"expression": "123 * 7"},
                "Call calculator using the exact expression string '123 * 7'.",
            )
        self._min_context = min_context
        self._candidate_cursors: dict[str, int] = {}
        self._retired_cursor = 0
        self._slots = self._load()

    def effective_models(self) -> list[str]:
        return [
            self._slots[name].replacement or name if name in self._slots else name
            for name in self._configured
        ]

    def retired_models(self) -> set[str]:
        return {
            slot.replacement or name
            for name, slot in self._slots.items()
            if slot.retired
        }

    def _load(self) -> dict[str, _Slot]:
        original = {name: _Slot() for name in self._configured if name.endswith(":free")}
        try:
            saved = json.loads(self._path.read_text())
            if not isinstance(saved, dict):
                raise ValueError("invalid monitor state")
        except FileNotFoundError:
            return original
        except (OSError, ValueError):
            logger.warning("Could not read OpenRouter monitor state at %s", self._path)
            return original
        used = set(self._configured)
        for name in original:
            if name not in saved:
                continue
            try:
                slot = _Slot.model_validate(saved[name], strict=True)
            except ValidationError:
                logger.warning("Could not read persisted slot state for %s", name)
                continue
            if slot.replacement is not None:
                if not slot.replacement.endswith(":free") or slot.replacement in used:
                    continue
                used.add(slot.replacement)
            original[name] = slot
        return original

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(mode="w", dir=self._path.parent, delete=False) as file:
            temp_path = Path(file.name)
            try:
                json.dump({name: slot.model_dump() for name, slot in self._slots.items()}, file)
                file.flush()
                os.replace(temp_path, self._path)
            finally:
                temp_path.unlink(missing_ok=True)

    async def _catalog(self) -> dict[str, dict]:
        response = await self._client.get("models")
        response.raise_for_status()
        rows = response.json()["data"]
        if not isinstance(rows, list) or not rows or any(
            not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]
            for row in rows
        ):
            raise ValueError("invalid or empty OpenRouter catalog")
        return {row["id"]: row for row in rows}

    def _eligible(self, row: dict, min_context: int) -> bool:
        return (
            not row.get("expiration_date")
            and isinstance(row.get("context_length"), int)
            and row["context_length"] >= min_context
            and isinstance(row.get("supported_parameters"), list)
            and "tools" in row["supported_parameters"]
            and _is_free(row)
        )

    def _candidates(self, catalog: dict[str, dict], min_context: int) -> list[str]:
        in_use = set(self.effective_models()) | set(self._configured)
        return sorted(
            name for name, row in catalog.items()
            if name.endswith(":free") and name not in in_use and self._eligible(row, min_context)
        )

    def _apply_slot(self, name: str, slot: _Slot, apply: Callable[[], None]) -> None:
        # Persist before changing the in-process delegate so a restart restores it.
        before = self._slots[name]
        self._slots[name] = slot
        try:
            self._save()
        except OSError:
            self._slots[name] = before
            raise
        try:
            apply()
        except Exception:
            self._slots[name] = before
            self._save()
            raise

    async def _probe(self, candidate: str) -> bool:
        for name, expected, instruction in self._probe_cases:
            if self._can_probe is not None and not self._can_probe():
                return False
            request = {
                "model": candidate,
                "messages": [{"role": "user", "content": instruction}],
                "tools": self._tools,
                "max_tokens": 1024,
                "provider": {"require_parameters": True},
            }
            if self._reasoning_effort is not None:
                request["reasoning_effort"] = self._reasoning_effort
            try:
                response = await self._client.post(
                    "chat/completions", json=request,
                )
                response.raise_for_status()
                choice = response.json()["choices"][0]
                calls = choice["message"]["tool_calls"]
                if (
                    choice["finish_reason"] != "tool_calls"
                    or len(calls) != 1
                    or calls[0]["type"] != "function"
                    or calls[0]["function"]["name"] != name
                    or json.loads(calls[0]["function"]["arguments"]) != expected
                ):
                    return False
            except (httpx.HTTPError, ValueError, IndexError, KeyError, TypeError):
                return False
        return True

    async def check(self) -> None:
        """Run a catalog check, then retry pending email independently of catalog health."""
        try:
            catalog = await self._catalog()
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as error:
            logger.warning("OpenRouter catalog check failed (%s)", type(error).__name__)
        else:
            for name, slot in self._slots.items():
                self._observe(name, slot, catalog)
            await self._replace_retired(catalog)
            self._save()

        for slot in self._slots.values():
            while slot.pending:
                try:
                    await self._send(*slot.pending[0])
                except Exception as error:
                    logger.warning("Could not send OpenRouter email (%s)", type(error).__name__)
                    return
                slot.pending.pop(0)
                self._save()

    def _observe(self, name: str, slot: _Slot, catalog: dict[str, dict]) -> None:
        if slot.retired:
            return
        model = slot.replacement or name
        if model in catalog:
            slot.missing_checks = 0
            row = catalog[model]
            context_length = row.get("context_length")
            if type(context_length) is int and context_length > 0:
                slot.context_length = context_length
            expiry = row.get("expiration_date")
            if isinstance(expiry, str) and expiry and expiry != slot.expiration:
                slot.pending.append([
                    "OpenRouter free-model retirement announced",
                    f"{model} has an announced expiration date of {expiry}.",
                ])
                slot.expiration = expiry
            return
        slot.missing_checks += 1
        if slot.missing_checks >= 2:
            retire = self._retire
            if retire is None:
                slot.retired = True
            else:
                self._apply_slot(
                    name, slot.model_copy(update={"retired": True}), lambda: retire(model),
                )

    async def _replace_retired(self, catalog: dict[str, dict]) -> None:
        retired = [(name, slot) for name, slot in self._slots.items() if slot.retired]
        if not retired:
            return
        # Rotate the first slot so one difficult replacement cannot use the whole budget forever.
        first = self._retired_cursor % len(retired)
        self._retired_cursor += 1
        remaining = CANDIDATES_PER_CHECK
        attempted: set[str] = set()
        for name, slot in retired[first:] + retired[:first]:
            model = slot.replacement or name
            min_context = max(self._min_context, slot.context_length)
            candidates = self._candidates(catalog, min_context)
            cursor = self._candidate_cursors.get(name, 0)
            if candidates:
                start = cursor % len(candidates)
                candidates = candidates[start:] + candidates[:start]
            if model in catalog and self._eligible(catalog[model], min_context):
                candidates.insert(0, model)
            replaced = False
            for candidate in candidates:
                if remaining == 0:
                    break
                if candidate in attempted:
                    continue
                attempted.add(candidate)
                remaining -= 1
                if self._can_probe is not None and not self._can_probe():
                    return
                if await self._probe(candidate):
                    self._activate(name, slot, model, candidate, catalog[candidate]["context_length"])
                    self._candidate_cursors.pop(name, None)
                    replaced = True
                    break
                if candidate != model:
                    cursor += 1
                    self._candidate_cursors[name] = cursor
            if not replaced and not slot.notified_missing:
                slot.pending.append([
                    "OpenRouter free model removed",
                    f"{model} was removed from the catalog. No replacement passed the tool-call "
                    "checks yet. The agent continues with the remaining models.",
                ])
                slot.notified_missing = True

    def _activate(
        self, name: str, slot: _Slot, model: str, candidate: str, context_length: int,
    ) -> None:
        result = f"passed {PROBES_PER_CANDIDATE}/{PROBES_PER_CANDIDATE} tool-call checks"
        if candidate == model:
            subject = "OpenRouter free model restored"
            body = f"{model} reappeared in the catalog, {result} and was restored."
        else:
            subject = "OpenRouter free-model replacement"
            body = f"{model} was removed from the catalog. Replacement {candidate} {result}."
        active = _Slot(
            replacement=candidate if candidate != name else None,
            context_length=context_length,
            pending=[*slot.pending, [subject, body]],
        )
        self._apply_slot(name, active, lambda: self._replace(model, candidate))

    async def run(self, interval_seconds: float = DEFAULT_INTERVAL_SECONDS) -> None:
        """Keep monitoring in a background task even if a cycle fails."""
        while True:
            try:
                await self.check()
            except Exception:
                logger.exception("OpenRouter monitor cycle failed")
            await asyncio.sleep(interval_seconds)
