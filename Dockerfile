# ARGUS — one image, seven commands.
#
# ## Why one image and not two
#
# The obvious split is "a slim image for the four API services, a fat one
# for the scanner's pandas/numpy work". It was measured rather than
# assumed, and it does not exist: importing all four service apps pulls
# in pandas and numpy transitively, because every one of them reads code
# under `core/` that reads a DataFrame. A "slim" API image would contain
# the same dependencies as the scanner and differ only in the layers
# above them.
#
# So the split would buy nothing and cost the thing that matters most
# here: two images are two builds that can drift, and a scanner writing
# signals under one build while an API reads them under another is a
# version skew inside a system whose entire premise is that a stored
# result is attributable to the code that produced it. One image, seven
# commands, one `git` SHA answering "what produced this row".
#
# The commands themselves are defined once, in
# `infra/deploy/processes.py`, and the Railway configuration is
# generated from it. This file deliberately does not repeat them.
#
# ## Multi-stage, and what the second stage does not have
#
# The builder installs into a virtualenv; the runtime copies that
# virtualenv and the source. The runtime has no compiler, no build
# headers, and no pip cache — a smaller attack surface, and Module 24's
# posture applied to the image rather than only to the request path.

# ---------------------------------------------------------------------
# Stage 1 — build
# ---------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# `build-essential` and `libpq-dev` are needed to compile argon2-cffi's
# bindings and psycopg's C extension. Both stay in this stage.
RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# Dependency metadata first, source second. Docker caches by layer, and
# the dependency set changes far less often than the code does — so an
# ordinary code change rebuilds the last layer rather than reinstalling
# pandas.
COPY pyproject.toml README.md ./
COPY packages/ ./packages/
RUN pip install --upgrade pip setuptools wheel && pip install .

COPY core/ ./core/
COPY data/ ./data/
COPY infra/ ./infra/
COPY services/ ./services/
RUN pip install --no-deps .

# ARGUS Public's static frontend. Not a Python package — nothing here is
# `pip install`ed — but `infra/deploy/public_web.py` mounts it onto the
# public_stats app at runtime, so it has to be on disk next to the code
# that reads it. See that module for why it lives outside `services/`.
COPY web/ ./web/

# ---------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# `libpq5` is the runtime half of `libpq-dev`; psycopg needs it and
# nothing else here does.
#
# `postgresql-client` is deliberate rather than incidental:
# `infra/deploy/backup.py` shells out to `pg_dump` and `pg_restore`, and
# a disaster-recovery tool that is not present in the image is a
# disaster-recovery tool you discover is missing during a disaster.
RUN apt-get update \
    && apt-get install --no-install-recommends -y libpq5 postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# A non-root user. Nothing in ARGUS needs root at runtime, and a
# container process that cannot write to its own filesystem cannot be
# made to persist anything by an attacker who reaches it.
RUN useradd --create-home --uid 10001 argus
WORKDIR /app
# The source tree, not just the installed package. `pip install .` alone
# is not enough: `infra/db/migrations/` has no `__init__.py`, so
# setuptools' package discovery does not see it and the migration scripts
# never reach site-packages. Alembic would then start against an empty
# versions directory and report the schema as already at head — a
# migration step that silently does nothing, which is worse than one that
# fails. The source copy is what `infra/deploy/migrate.py` reads
# `alembic.ini` and `versions/` from.
COPY --from=builder --chown=argus:argus /build /app
USER argus

# Documentation only — Railway injects the real port as $PORT and the
# start commands below bind to it. EXPOSE does not publish anything.
EXPOSE 8000

# The default is the Terminal; every service overrides this. Listed here
# so `docker run` without arguments does something legible rather than
# failing with an empty command.
#
# `--factory` because `infra/deploy/asgi.py` exposes builder functions
# rather than module-level app objects: composing an app has side effects
# (it opens a connection pool and validates the deployment profile) and
# those should happen when uvicorn starts the app, not when something
# merely imports the module.
CMD ["sh", "-c", "uvicorn infra.deploy.asgi:terminal_app --factory --host 0.0.0.0 --port ${PORT:-8000}"]
