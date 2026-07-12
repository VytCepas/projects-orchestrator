# Development Conventions

## Code style


- Lint/format/test via `just` recipes — `just --list` shows them; linter is `ruff`
- Docstrings required on public functions (Google style — enforced by the ruff `D` rules in `ruff.toml`, with complexity caps)
- No type annotations required unless they clarify non-obvious interfaces



## Commit messages

Format: `type(PROJECT-123): short description` or `type: short description` (no linked issue)

Types: `feat`, `fix`, `test`, `docs`, `chore`

Example: `feat(PI-42): Add OAuth login`

## Pull requests

- Title: `type(PROJECT-123): Short description`
- Body must include `Closes #<number>` for auto-close on merge
- All tests must pass before merging

## Comments

Only comment when the **why** is non-obvious — a hidden constraint, a workaround for a specific bug, or a counter-intuitive decision. Never describe what the code does.
