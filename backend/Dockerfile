# syntax=docker/dockerfile:1.7
# SeereonOS API image — Python only (no Node.js, no frontend, no demo data). One image, three roles (compose services):
#   api       gunicorn -c gunicorn.conf.py app.main:app            (DATABASE_URL: runtime role seereon_app)
#   worker    python -m app.worker                                  (DATABASE_URL: runtime role seereon_app)
#   migrate   python -m app.db.migrate                              (MIGRATION_DATABASE_URL: owner role, one-shot job)
# The database migrations (database/migrations, the database deliverable) are copied in at build time, so the build
# context is the REPOSITORY ROOT:
#   docker build -f backend/Dockerfile -t seereonos-api .
# (backend/deploy/docker-compose.yml does that). To build from a backend-only checkout instead, place the migrations at
# backend/migrations and pass --build-arg MIGRATIONS_SRC=backend/migrations.

ARG MIGRATIONS_SRC=database/migrations

# ---- python dependencies ----
FROM python:3.11-slim AS pydeps
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --prefix=/install -r /tmp/requirements.txt

# ---- runtime ----
FROM python:3.11-slim
ARG MIGRATIONS_SRC
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin seereon
ENV APP_ENV=production \
    TZ=Asia/Kolkata \
    PORT=8000 \
    WEB_CONCURRENCY=2 \
    GRACEFUL_TIMEOUT=20 \
    MIGRATIONS_DIR=/app/migrations \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
COPY --from=pydeps /install /usr/local
WORKDIR /app/backend
COPY --chown=root:root backend/app ./app
COPY --chown=root:root backend/gunicorn.conf.py ./gunicorn.conf.py
COPY --chown=root:root ${MIGRATIONS_SRC} /app/migrations
# development-only tools are refused at runtime in production; they are not needed in the image
RUN rm -rf ./app/devtools && python -m compileall -q app
# files are root-owned and read-only for the app user
USER seereon
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/readyz' % os.environ.get('PORT', '8000'), timeout=2)" || exit 1
# workers, timeouts, keep-alive, recycling: gunicorn.conf.py (environment variables, PERFORMANCE.md "Tuning knobs").
# The API never migrates on start: run the migrate job (it alone holds the owner credential) before rolling out.
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app.main:app"]
