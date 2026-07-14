"""The credential scrub: an agent run cannot see the data plane's keys.

ADR-007 §4. The tool allow-list stops the `claude` CLI from running `gcloud`; this
stops the credentials from *existing* in the agent's environment, so nothing it
reaches — an MCP server, a hook, a tool nobody foresaw — can find them.
"""

from __future__ import annotations

from projects_orchestrator.sandbox import agent_env

# A representative slice of the real thing. The point of the allowlist is that it
# does not matter whether these exact names were predicted — anything not
# explicitly kept is gone — so the list is illustrative, not exhaustive.
_CREDENTIALS = {
    "GOOGLE_APPLICATION_CREDENTIALS": "/home/me/gcp-key.json",
    "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI",
    "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7",
    "FLY_API_TOKEN": "fo1_secret",
    "GH_TOKEN": "ghp_secret",
    "GITHUB_TOKEN": "ghs_secret",
    "STRIPE_SECRET_KEY": "sk_live_abc",
    "DATABASE_URL": "postgres://user:pw@host/db",
    "KUBECONFIG": "/home/me/.kube/config",
    # None of these contains TOKEN/SECRET/KEY as a substring — the exact reason a
    # denylist would have leaked them.
    "SNOWFLAKE_PASSWORD": "hunter2",
    "GCP_PROJECT": "prod-233",
    "PGPASSWORD": "swordfish",
}

_HARMLESS = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/me",
    "LANG": "en_US.UTF-8",
    "LC_TIME": "en_GB.UTF-8",
    "XDG_CONFIG_HOME": "/home/me/.config",
    "TERM": "xterm-256color",
}


def test_every_credential_is_scrubbed() -> None:
    env = agent_env({**_CREDENTIALS, **_HARMLESS})
    for secret in _CREDENTIALS:
        assert secret not in env, f"{secret} leaked into the agent environment"


def test_a_credential_without_a_telltale_substring_is_still_scrubbed() -> None:
    # The denylist trap, made explicit: SNOWFLAKE_PASSWORD has no TOKEN/SECRET/KEY
    # in its name. An allowlist does not care — it was not on the list, so it is
    # gone.
    assert "SNOWFLAKE_PASSWORD" not in agent_env({"SNOWFLAKE_PASSWORD": "x"})


def test_an_unknown_future_variable_is_absent_by_default() -> None:
    # A variable nobody has invented yet is handled correctly with no code change:
    # it is not on the allowlist, so it does not exist.
    assert "SOME_VENDOR_2027_API_SECRET" not in agent_env({"SOME_VENDOR_2027_API_SECRET": "x"})


def test_the_harmless_variables_survive() -> None:
    # The scrub must not lobotomise the agent. PATH-less, an agent cannot find git.
    env = agent_env({**_CREDENTIALS, **_HARMLESS})
    for name, value in _HARMLESS.items():
        assert env[name] == value


def test_the_agents_own_key_survives() -> None:
    # ANTHROPIC_API_KEY is the agent's OWN credential (ADR-012) — the model it is.
    # Without it the agent cannot run at all. It is not an operator cloud secret.
    assert agent_env({"ANTHROPIC_API_KEY": "sk-ant-xxx"})["ANTHROPIC_API_KEY"] == "sk-ant-xxx"


def test_locale_and_xdg_families_pass_by_prefix() -> None:
    env = agent_env({"LC_NUMERIC": "C", "XDG_CACHE_HOME": "/c", "LC_MESSAGES": "en"})
    assert env == {"LC_NUMERIC": "C", "XDG_CACHE_HOME": "/c", "LC_MESSAGES": "en"}


def test_the_result_is_a_fresh_dict_not_a_view_of_the_source() -> None:
    source = {"PATH": "/bin", "GH_TOKEN": "secret"}
    env = agent_env(source)
    env["INJECTED"] = "x"
    assert "INJECTED" not in source


def test_an_empty_environment_yields_an_empty_scrub() -> None:
    assert agent_env({}) == {}


def test_a_prefix_lookalike_that_is_not_the_prefix_is_scrubbed() -> None:
    # "LCD_BRIGHTNESS" starts with "LC" but not "LC_"; "XDGSECRET" starts with
    # "XDG" but not "XDG_". Neither is a locale/XDG var, so neither is kept.
    env = agent_env({"LCD_BRIGHTNESS": "5", "XDGSECRET": "leak"})
    assert env == {}
