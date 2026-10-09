# syntax=docker/dockerfile:1
#
# Hermetic test image for Ssrrrnn/claude-telegram-bot.
#
# Two clearly separated phases:
#   Stage 1 (deps)    -- network IS used here, only to download Python wheels.
#   Stage 2 (runtime) -- the image CI runs tests from.  CI always starts it
#                        with `--network none`, so the test process has only a
#                        loopback interface.
#
# The runtime stage does not bake in any credential, secret, database or
# virtualenv: those paths are filtered out by the repository .dockerignore.

# ---------------------------------------------------------------------------
# Stage 1: resolve and install dependencies (network allowed)
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS deps

WORKDIR /build

# Copied alone so this layer is cached across source-only changes.
COPY requirements.txt ./

RUN pip install \
        --no-cache-dir \
        --disable-pip-version-check \
        --prefix=/install \
        --requirement requirements.txt

# ---------------------------------------------------------------------------
# Stage 2: runtime test image (started with --network none by CI)
# ---------------------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=0 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp

# Bring the pre-built dependencies into the interpreter's real prefix
# (/usr/local/lib/python3.12/site-packages, which is already on sys.path).
COPY --from=deps /install /usr/local

# Unprivileged runtime user; CI additionally passes --user 10001:10001.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin runner

WORKDIR /app

# Source, tests and egress_guard.py.  Sensitive paths are excluded by
# .dockerignore, not by this COPY.
COPY --chown=runner:runner . .

USER 10001:10001

# Default action: prove the guard is installed and behaving.
CMD ["python", "egress_guard.py", "--selfcheck"]
