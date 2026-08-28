"""Deployment configuration: the settings Module 24 built and left switched off.

Module 24's handover was explicit that its mechanisms defaulted to
inactive — no trusted proxies, no CORS origins, no HSTS — because a
security default guessed by a library is a security default nobody chose.
This is where they are chosen, per environment, and where a production
process configured wrongly is refused rather than warned about.
"""

from __future__ import annotations

import dataclasses

import pytest

from infra.deploy.config import (
    PROFILES,
    RAILWAY_PRIVATE_NETWORK,
    DeploymentProfile,
    ProductionMisconfigured,
    profile_for,
    security_config_for,
)
from packages.config.environment import Environment

DEPLOYED = (Environment.STAGING, Environment.PRODUCTION)


# --------------------------------------------------------------------------
# Item 3: trusted proxy configuration, unset by default
# --------------------------------------------------------------------------


def test_development_trusts_no_proxy():
    """A laptop has no proxy in front of it, so a forwarded header is a lie."""
    assert PROFILES[Environment.DEVELOPMENT].trusted_proxies == ()


@pytest.mark.parametrize("environment", DEPLOYED)
def test_deployed_environments_trust_the_platform_private_network(environment: Environment):
    assert PROFILES[environment].trusted_proxies == RAILWAY_PRIVATE_NETWORK


def test_trusted_proxies_can_be_narrowed_from_the_environment():
    """Stated as a precondition in `config.py`: if the container port is
    ever exposed directly, the private-range default is wrong."""
    profile = profile_for(env={"ARGUS_ENV": "production", "ARGUS_TRUSTED_PROXIES": "10.1.2.0/24"})
    assert profile.trusted_proxies == ("10.1.2.0/24",)


# --------------------------------------------------------------------------
# Item 4: CORS origins, empty by default
# --------------------------------------------------------------------------


@pytest.mark.parametrize("environment", list(Environment))
def test_no_profile_ships_with_an_origin_allowed(environment: Environment):
    """An allowed origin is a deployment fact, and no code can know it."""
    assert PROFILES[environment].cors_allowed_origins == ()


def test_origins_are_read_from_the_environment():
    profile = profile_for(
        env={
            "ARGUS_ENV": "production",
            "ARGUS_CORS_ORIGINS": "https://app.argus.test, https://argus.test",
        }
    )
    assert profile.cors_allowed_origins == ("https://app.argus.test", "https://argus.test")
    profile.validate()


def test_a_wildcard_origin_refuses_to_start_in_production():
    """Refuses rather than warns. A warning in a deploy log is not read."""
    profile = profile_for(env={"ARGUS_ENV": "production", "ARGUS_CORS_ORIGINS": "*"})
    with pytest.raises(ProductionMisconfigured, match="wildcard origin"):
        profile.validate()


def test_a_wildcard_origin_is_permitted_in_development():
    profile_for(env={"ARGUS_ENV": "development", "ARGUS_CORS_ORIGINS": "*"}).validate()


def test_no_origins_in_production_is_a_working_state():
    """No browser origin allowed is a decision, not a broken configuration."""
    profile_for(env={"ARGUS_ENV": "production"}).validate()


# --------------------------------------------------------------------------
# Item 2: the rate limiter's shared store
# --------------------------------------------------------------------------


def test_more_than_one_worker_without_a_shared_store_refuses_to_start():
    """The failure this prevents is invisible from inside any worker.

    Each has its own counters, so each sees a correct-looking ceiling
    while the service as a whole allows N times what was configured.
    """
    profile = profile_for(env={"ARGUS_ENV": "production", "WEB_CONCURRENCY": "4"})
    with pytest.raises(ProductionMisconfigured, match="WEB_CONCURRENCY"):
        profile.validate()


def test_more_than_one_worker_is_permitted_once_a_shared_store_is_configured():
    profile = profile_for(
        env={
            "ARGUS_ENV": "production",
            "WEB_CONCURRENCY": "4",
            "ARGUS_RATE_LIMIT_STORE_URL": "redis://cache.internal:6379/0",
        }
    )
    profile.validate()


def test_one_worker_needs_no_shared_store():
    profile_for(env={"ARGUS_ENV": "production", "WEB_CONCURRENCY": "1"}).validate()


def test_development_may_run_several_workers_unshared():
    profile_for(env={"ARGUS_ENV": "development", "WEB_CONCURRENCY": "8"}).validate()


# --------------------------------------------------------------------------
# Item 1: HSTS, once TLS is guaranteed
# --------------------------------------------------------------------------


def test_development_sends_no_hsts():
    """Browsers key HSTS by hostname, and `localhost` is shared with every
    other project on the machine."""
    assert PROFILES[Environment.DEVELOPMENT].hsts_enabled is False


def test_staging_hsts_is_short_and_does_not_claim_subdomains():
    """A year-long HSTS sent by mistake from staging is a year of outage."""
    staging = PROFILES[Environment.STAGING]
    assert staging.hsts_enabled is True
    assert staging.hsts_max_age_seconds == 86_400
    assert staging.hsts_include_subdomains is False


def test_production_hsts_is_a_year_including_subdomains():
    production = PROFILES[Environment.PRODUCTION]
    assert production.hsts_max_age_seconds == 31_536_000
    assert production.hsts_include_subdomains is True


@pytest.mark.parametrize("environment", DEPLOYED)
def test_deployed_environments_require_https(environment: Environment):
    assert PROFILES[environment].require_https is True


# --------------------------------------------------------------------------
# The profile reaches Module 24's own settings object
# --------------------------------------------------------------------------


def test_the_profile_fills_in_module_24s_security_settings():
    """Otherwise this module would be configuring something nothing reads."""
    profile = profile_for(
        env={"ARGUS_ENV": "production", "ARGUS_CORS_ORIGINS": "https://app.argus.test"}
    )
    security = security_config_for(profile)
    assert security.settings.trusted_proxies == RAILWAY_PRIVATE_NETWORK
    assert security.settings.cors_allowed_origins == ("https://app.argus.test",)


def test_an_unknown_environment_value_refuses_rather_than_defaulting():
    """Defaulting here would pick a security posture nobody chose.

    A typo in `ARGUS_ENV` on a production service, silently read as
    development, runs production without HSTS, without proxy trust and
    without any check in this file.
    """
    with pytest.raises(ProductionMisconfigured, match="ARGUS_ENV"):
        profile_for(env={"ARGUS_ENV": "nonsense"})


def test_a_profile_is_frozen():
    """So a request handler cannot mutate the deployment's policy."""
    profile = DeploymentProfile(environment=Environment.PRODUCTION)
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.require_https = False  # type: ignore[misc]
