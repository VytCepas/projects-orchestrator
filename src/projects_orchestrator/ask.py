"""Optional LLM ``/ask`` mode — English in, an existing intent out.

Off by default: the controller stays deterministic unless the operator sets
``ORCHESTRATOR_ASK_MODEL`` (a Claude model id) and ``ANTHROPIC_API_KEY``.
When enabled, the model is shown the fleet's project names and the fixed
intent list and must answer with one JSON object naming an existing intent —
it only ever *selects among existing actions* (ADR-003); the deterministic
dispatcher still executes it. Anything unparseable or outside the allowed
verb set degrades to an explanatory line, never an exception.

The API call is a single bounded stdlib HTTP request (no SDK dependency —
the core stays light and the mode is optional). Tests inject a fake
completer; no live API is ever needed.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from projects_orchestrator.controller import Intent

if TYPE_CHECKING:
    import os

ASK_MODEL_ENV = "ORCHESTRATOR_ASK_MODEL"
API_KEY_ENV = "ANTHROPIC_API_KEY"

DISABLED_MESSAGE = (
    "natural-language mode is not enabled — this controller is deterministic "
    f"(set {ASK_MODEL_ENV} to a Claude model id to enable /ask)"
)
NO_KEY_MESSAGE = f"/ask is configured but {API_KEY_ENV} is not set — export it to enable the call"

# The only verbs the model may select; everything else in the reply is rejected.
ALLOWED_VERBS = frozenset(
    {
        "status",
        "check",
        "run",
        "memory",
        "drift",
        "doctor",
        "audit",
        "ci",
        "upgrade",
        "cloud",
        "events",
        "detail",
        "projects",
        "refresh",
        "help",
    }
)

_API_URL = "https://api.anthropic.com/v1/messages"
_API_TIMEOUT = 30.0

# (model, prompt) -> raw model text; injectable so tests never hit the API.
Completer = Callable[[str, str], str]


def ask_enabled(env: Mapping[str, str] | os._Environ[str]) -> bool:
    """Return whether the operator has opted into /ask."""
    return bool(env.get(ASK_MODEL_ENV, "").strip())


def build_prompt(question: str, project_names: tuple[str, ...]) -> str:
    """Render the intent-selection prompt (pure).

    Args:
        question: The operator's natural-language question.
        project_names: Names of the discovered projects.

    Returns:
        A prompt instructing the model to answer with one JSON intent.
    """
    verbs = ", ".join(sorted(ALLOWED_VERBS))
    projects = ", ".join(project_names) or "none discovered"
    return (
        "You route one operator request to one command of a project-fleet "
        "controller. Reply with ONLY a JSON object, no prose:\n"
        '{"verb": "<verb>", "target": "<project or all>", "args": ["..."]}\n'
        f"Allowed verbs: {verbs}. Known projects: {projects}.\n"
        'Verb notes: "check" runs gates (args = task names, e.g. ["lint","test"]); '
        '"run" runs one declared task (args = [task]); "memory" searches memories '
        "(args = [query]). Omit target/args when not applicable.\n"
        f"Request: {question}"
    )


def parse_intent_reply(text: str) -> Intent | None:
    """Extract a valid intent from the model's reply (pure).

    Args:
        text: Raw model output.

    Returns:
        The selected :class:`Intent`, or ``None`` when no JSON object with an
        allowed verb can be extracted.
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        return None
    try:
        raw = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    verb = str(raw.get("verb") or "").strip().lower()
    if verb not in ALLOWED_VERBS:
        return None
    target = raw.get("target")
    raw_args = raw.get("args")
    args = tuple(str(a) for a in raw_args) if isinstance(raw_args, list) else ()
    # Verbs that dispatch on args[0] must carry a non-empty first arg; the
    # deterministic parser guarantees this, but a model reply might not, and an
    # argless memory/run would crash the dispatcher.
    if verb in _ARGS_REQUIRED and (not args or not args[0].strip()):
        return None
    return Intent(
        verb=verb,
        target=str(target) if isinstance(target, str) and target.strip() else None,
        args=args,
    )


_ARGS_REQUIRED = frozenset({"memory", "run"})


def _api_complete(model: str, prompt: str, api_key: str) -> str:
    """One bounded Anthropic Messages API call; empty string on any failure."""
    body = json.dumps(
        {
            "model": model,
            "max_tokens": 256,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 — fixed https URL
        _API_URL,
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_API_TIMEOUT) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return ""
    return _extract_text(payload)


def _extract_text(payload: object) -> str:
    """Pull the answer text from a Messages API payload (pure); ``""`` if none.

    Returns the first *text* content block: thinking-enabled models emit a
    leading ``thinking`` block, so ``content[0]`` is not necessarily the text.
    """
    content = payload.get("content") if isinstance(payload, dict) else None
    if not isinstance(content, list) or not content:
        return ""
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            return str(block.get("text", ""))
    first = content[0]
    return str(first.get("text", "")) if isinstance(first, dict) else ""


def resolve_ask(
    question: str,
    project_names: tuple[str, ...],
    env: Mapping[str, str] | os._Environ[str],
    complete: Completer | None = None,
) -> Intent | str:
    """Resolve a natural-language question to an intent, or an error line.

    Args:
        question: The operator's question.
        project_names: Names of the discovered projects.
        env: Environment mapping (``os.environ`` in production).
        complete: Completer override; ``None`` uses the real API.

    Returns:
        A dispatchable :class:`Intent`, or a human-readable line explaining
        why nothing will be dispatched. Never raises.
    """
    model = env.get(ASK_MODEL_ENV, "").strip()
    if not model:
        return DISABLED_MESSAGE
    if complete is None:
        api_key = env.get(API_KEY_ENV, "").strip()
        if not api_key:
            return NO_KEY_MESSAGE

        def complete(m: str, p: str, key: str = api_key) -> str:
            return _api_complete(m, p, key)

    if not question.strip():
        return "usage: /ask <question>"
    reply = complete(model, build_prompt(question, project_names))
    intent = parse_intent_reply(reply)
    if intent is None:
        return "could not map the question to a known command (try: help)"
    return intent
