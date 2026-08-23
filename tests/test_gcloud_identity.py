"""The named gcloud identity: the pin, and the overrides that would defeat it.

The property under test is that the identity is *named rather than inherited*. A
gcloud subprocess that picks up the ambient account does not fail — it succeeds
with a narrower answer, which is the one failure mode the adapters' pessimistic
contract cannot see. So both halves matter: pinning ``CLOUDSDK_CONFIG``, and
dropping the variables that silently outrank it.
"""

from __future__ import annotations

from pathlib import Path

from projects_orchestrator.gcloud_identity import (
    CONFIG_DIR_ENV,
    gcloud_config_dir,
    gcloud_env,
)

_DEFAULT = Path.home() / ".config" / "gcloud" / "identities" / "tools"


def test_gcloud_config_dir_defaults_to_the_broad_identity() -> None:
    assert gcloud_config_dir({}) == _DEFAULT


def test_gcloud_config_dir_honours_the_override() -> None:
    assert gcloud_config_dir({CONFIG_DIR_ENV: "/var/tmp/elsewhere"}) == Path("/var/tmp/elsewhere")


def test_gcloud_config_dir_treats_an_empty_override_as_unset() -> None:
    # An empty variable is an unset one, not a request to use the process cwd.
    assert gcloud_config_dir({CONFIG_DIR_ENV: ""}) == _DEFAULT


def test_gcloud_env_pins_the_config_dir() -> None:
    assert gcloud_env({CONFIG_DIR_ENV: "/var/tmp/id"})["CLOUDSDK_CONFIG"] == "/var/tmp/id"


def test_gcloud_env_drops_an_inherited_account_override() -> None:
    # THE guard. Every gcloud property has a CLOUDSDK_<SECTION>_<NAME> env
    # override, so an inherited CLOUDSDK_CORE_ACCOUNT beats the account inside the
    # directory we just pinned. A pin that this variable can defeat is not a pin.
    scrubbed = gcloud_env({"CLOUDSDK_CORE_ACCOUNT": "narrower@example.com"})
    assert "CLOUDSDK_CORE_ACCOUNT" not in scrubbed


def test_gcloud_env_drops_an_inherited_active_config_name() -> None:
    assert "CLOUDSDK_ACTIVE_CONFIG_NAME" not in gcloud_env({"CLOUDSDK_ACTIVE_CONFIG_NAME": "other"})


def test_gcloud_env_drops_an_inherited_adc_path() -> None:
    # GOOGLE_APPLICATION_CREDENTIALS outranks the ADC inside the pinned directory.
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in gcloud_env(
        {"GOOGLE_APPLICATION_CREDENTIALS": "/var/tmp/adc.json"}
    )


def test_gcloud_env_drops_an_inherited_project_override() -> None:
    assert "GOOGLE_CLOUD_PROJECT" not in gcloud_env({"GOOGLE_CLOUD_PROJECT": "wrong-project"})


def test_gcloud_env_keeps_unrelated_variables() -> None:
    # The scrub is targeted, not a rebuild-from-nothing like sandbox.agent_env:
    # gcloud still needs PATH, HOME and the rest to run at all.
    assert gcloud_env({"PATH": "/usr/bin"})["PATH"] == "/usr/bin"


def test_gcloud_env_pin_survives_a_competing_override_in_the_same_input() -> None:
    # Both mechanisms in one mapping: the scrub removes the competitor and the pin
    # still lands. This is the combination a real inherited environment presents.
    env = gcloud_env({CONFIG_DIR_ENV: "/var/tmp/id", "CLOUDSDK_CONFIG": "/var/tmp/ambient"})
    assert env["CLOUDSDK_CONFIG"] == "/var/tmp/id"
