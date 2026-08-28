"""A deliberately broken logging call, for the scanner test to detect.

Not part of any importable package — never collected by pytest, never
imported by anything, and excluded from the project-wide scan's own
roots (it lives under tests/, which DEFAULT_ROOTS never walks). Exists
only to be scanned explicitly, by path, from one test.
"""

import logging

log = logging.getLogger("argus.fixture")


def handle_login(password: str) -> None:
    log.info("login attempt", extra={"password": password})
