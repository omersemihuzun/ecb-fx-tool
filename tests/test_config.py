"""Configuration is read once, at startup, and a bad value stops the process
there rather than surfacing as a strange answer under load.
"""

from __future__ import annotations

import pytest

from app.config import ConfigError, Settings


def test_defaults_match_the_brief():
    settings = Settings.from_env({})

    assert settings.upstream_base == "https://api.frankfurter.dev"
    assert settings.port == 8080


def test_the_upstream_base_is_overridable_and_normalised():
    settings = Settings.from_env({"FX_UPSTREAM_BASE": "http://localhost:9999/"})

    assert settings.upstream_base == "http://localhost:9999"


def test_an_empty_variable_falls_back_to_the_default():
    assert Settings.from_env({"PORT": "   "}).port == 8080


@pytest.mark.parametrize(
    "env",
    [
        {"PORT": "not-a-port"},
        {"PORT": "0"},
        {"PORT": "70000"},
        {"FX_UPSTREAM_TIMEOUT_SECONDS": "0"},
        {"FX_CACHE_TTL_SECONDS": "-1"},
        {"FX_MAX_AMOUNT": "banana"},
        {"FX_MAX_AMOUNT": "-5"},
    ],
)
def test_an_unusable_value_fails_at_startup(env):
    with pytest.raises(ConfigError):
        Settings.from_env(env)
