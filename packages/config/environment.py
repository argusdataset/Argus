"""ARGUS deployment environment."""

from enum import StrEnum


class Environment(StrEnum):
    """The environment ARGUS is running in, selected via the ARGUS_ENV env var.

    Unset defaults to DEVELOPMENT. An unrecognized value fails config
    loading immediately (see AppConfig) rather than silently falling back.
    """

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
