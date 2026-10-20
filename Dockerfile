FROM node:24-bookworm-slim AS frontend
WORKDIR /build/web
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    ASSET_DIR=/data/assets
WORKDIR /app
COPY pyproject.toml requirements.lock ./
COPY codex_anywhere/ ./codex_anywhere/
RUN pip install --no-cache-dir -c requirements.lock . && \
    groupadd --gid 10001 anywhere && \
    useradd --uid 10001 --gid anywhere --no-create-home anywhere && \
    mkdir -p /data/assets /app/instance && \
    chown -R anywhere:anywhere /data /app/instance
COPY --from=frontend /build/web/dist/ ./web/dist/
COPY deploy/entrypoint.sh /usr/local/bin/anywhere-entrypoint
RUN chmod 755 /usr/local/bin/anywhere-entrypoint
USER anywhere
EXPOSE 5000
ENTRYPOINT ["anywhere-entrypoint"]
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "64", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-", "codex_anywhere.hub:create_app()"]
