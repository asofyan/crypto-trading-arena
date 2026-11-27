# crypto trading arena

The working guide now lives in **[AGENTS.md](AGENTS.md)** — one canonical document for every
assistant and human:

- development rules (uv, conventions, TDD, reviews, calfkit issues, ADRs),
- how to run and restart the stack (and the orphan tool-return loop that requires an offset
  reset after a restart),
- how to read the system's state (logs, ledger queries, Kafka lag),
- the money-path invariants that must not be "fixed" back (ADR-0011),
- the model pool and how to re-probe it,
- config facts, open backlog, and the state at handoff.

Read that file before changing anything. This file only exists so tools that look for
`CLAUDE.md` find their way there.

@AGENTS.md
