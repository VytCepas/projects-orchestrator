"""The credential scrub: an agent run cannot see the data plane's keys.

ADR-007 §4. The tool allow-list stops the `claude` CLI from running `gcloud`; this
stops the credentials from *existing* in the agent's environment, so nothing it
reaches — an MCP server, a hook, a tool nobody foresaw — can find them.
"""

from __future__ import annotations

from projects_orchestrator.sandbox import agent_env as _agent_env

_HOME = "/sandbox/home"


def agent_env(base):
    """Test wrapper: every call supplies the required fresh HOME."""
    return _agent_env(base, home=_HOME)


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

# HOME and XDG_* are deliberately NOT here: they are set fresh under the sandbox
# home, not copied from the operator (whose HOME points at ~/.config/gcloud).
_HARMLESS = {
    "PATH": "/usr/bin:/bin",
    "LANG": "en_US.UTF-8",
    "LC_TIME": "en_GB.UTF-8",
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


def test_the_locale_family_passes_by_prefix() -> None:
    env = agent_env({"LC_NUMERIC": "C", "LC_MESSAGES": "en"})
    assert env["LC_NUMERIC"] == "C"
    assert env["LC_MESSAGES"] == "en"


def test_the_operators_xdg_config_home_is_not_inherited() -> None:
    # ~/.config is the gcloud/aws credential search path. Inheriting the
    # operator's XDG_CONFIG_HOME re-opens exactly what the HOME redirect closed.
    env = agent_env({"XDG_CONFIG_HOME": "/home/me/.config"})
    assert env["XDG_CONFIG_HOME"] == f"{_HOME}/.config"
    assert "/home/me" not in env["XDG_CONFIG_HOME"]


def test_the_result_is_a_fresh_dict_not_a_view_of_the_source() -> None:
    source = {"PATH": "/bin", "GH_TOKEN": "secret"}
    env = agent_env(source)
    env["INJECTED"] = "x"
    assert "INJECTED" not in source


def test_an_empty_environment_still_gets_an_isolated_home() -> None:
    # Even from nothing, HOME and the XDG base dirs are set — under the sandbox,
    # never the operator's home.
    env = agent_env({})
    assert env["HOME"] == _HOME
    assert env["XDG_CONFIG_HOME"] == f"{_HOME}/.config"
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in env


def test_a_prefix_lookalike_that_is_not_the_prefix_is_scrubbed() -> None:
    # "LCD_BRIGHTNESS" starts with "LC" but not "LC_". Not a locale var, not kept.
    env = agent_env({"LCD_BRIGHTNESS": "5", "XDGSECRET": "leak"})
    assert "LCD_BRIGHTNESS" not in env
    assert "XDGSECRET" not in env


# --- The file-backed credential leak (the P1) ---------------------------------


def test_home_is_isolated_so_file_backed_creds_are_unreachable() -> None:
    # THE P1. Scrubbing GOOGLE_APPLICATION_CREDENTIALS the VAR is not enough:
    # Google ADC falls back to $HOME/.config/gcloud/application_default_credentials.json,
    # AWS reads ~/.aws/credentials, flyctl reads ~/.config/fly. Preserving the
    # operator's HOME re-opens all of them. It must point at the fresh sandbox.
    env = agent_env({"HOME": "/home/me"})
    assert env["HOME"] == _HOME
    assert env["HOME"] != "/home/me"


def test_all_xdg_base_dirs_are_redirected_under_the_sandbox_home() -> None:
    env = agent_env({})
    for var in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        assert env[var].startswith(_HOME), f"{var} escaped the sandbox home"


def test_home_is_required_and_has_no_default() -> None:
    # There is no "reuse the operator's HOME" fallback — that was the bug. A
    # caller must supply a fresh directory explicitly.
    import pytest

    with pytest.raises(TypeError):
        _agent_env({})  # missing the required keyword-only `home`
