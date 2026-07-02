---
name: Conventions feedback
description: Rules about how agents should behave in this project
type: feedback
---

# Conventions

- **Rule:** Prioritize the orchestrator's own robustness, monitoring, and memory integration over GitHub-issue/board automation work.
  **Why:** Owner redirect (2026-07-02): "forget project-init issues / ticket creation; make the controller robust, all-knowing, easy to work and monitor."
  **How to apply:** When choosing between fixing scaffold/board automation and improving the engine/controller/TUI or fleet-memory features, pick the latter unless asked otherwise.

- **Rule:** Engine failures must be data, not exceptions.
  **Why:** One broken child project must never take down the fleet view.
  **How to apply:** Wrap external I/O (subprocess, filesystem, YAML) so it degrades to `unknown`/`fail`/warnings; add a test for each new failure mode.
