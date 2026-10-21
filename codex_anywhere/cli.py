"""Local bootstrap, hub and node commands."""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

import click
from dotenv import load_dotenv


@click.group()
def main():
    """Self-host Codex across your own Windows, macOS and Linux machines."""


@main.command("init")
@click.option("--directory", type=click.Path(path_type=Path), default=Path("."))
def initialize(directory):
    """Create a private .env with unique credentials; never overwrite existing secrets."""
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".env"
    contents = (
        f"SECRET_KEY={secrets.token_urlsafe(48)}\nADMIN_USERNAME=admin\n"
        f"ADMIN_PASSWORD={secrets.token_urlsafe(24)}\n"
        f"POSTGRES_PASSWORD={secrets.token_hex(24)}\n"
        "COOKIE_SECURE=false\nPORT=5000\n"
        "# Set PUBLIC_URL=https://your-domain when deploying behind HTTPS.\n"
    )
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise click.ClickException(
            f"{path} already exists; configuration was not overwritten"
        ) from exc
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
        file.write(contents)
    click.echo(f"Created {path}. Login: admin. Read ADMIN_PASSWORD in this file.")


def upgrade_database(app):
    from alembic import command
    from alembic.config import Config

    from .hub.models import db

    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    with app.app_context(), db.engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


@main.command("db-upgrade")
def db_upgrade():
    """Apply all versioned database migrations."""
    from .hub import create_app

    upgrade_database(create_app())
    click.echo("Database is up to date.")


@main.command("server")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", type=int, default=None)
def serve(host, port):
    """Run a local threaded development server. Use Gunicorn for deployment."""
    from .hub import create_app

    app = create_app()
    upgrade_database(app)
    app.run(
        host=host,
        port=port or int(os.getenv("PORT", "5000")),
        threaded=True,
        debug=False,
        use_reloader=False,
    )


@main.group("node")
def node():
    """Pair and run the Codex node on a work machine."""


def default_state():
    return str(Path.home() / ".codex-anywhere")


@node.command("pair")
@click.option("--server", required=True)
@click.option("--code", required=True)
@click.option("--name", required=True)
@click.option("--state-dir", type=click.Path(path_type=Path), default=default_state)
@click.option("--allow-insecure", is_flag=True, help="Allow HTTP to a non-loopback hub explicitly.")
def pair(server, code, name, state_dir, allow_insecure):
    from .node import pair_node

    try:
        host = pair_node(server, name, state_dir, code, allow_insecure)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Paired {host['name']} ({host['id']}). Credentials stored in {state_dir}.")


@node.command("run")
@click.option("--state-dir", type=click.Path(path_type=Path), default=default_state)
@click.option("--codex-bin", default="codex", show_default=True)
@click.option("--allow-insecure", is_flag=True)
def run(state_dir, codex_bin, allow_insecure):
    from .node import run_node

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        run_node(state_dir, codex_bin, allow_insecure)
    except KeyboardInterrupt:
        click.echo("Node stopped.")
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


if __name__ == "__main__":
    load_dotenv()
    main()
