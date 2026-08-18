from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
import os


db = SQLAlchemy()


def _add_column_if_missing(connection, table, column, definition):
    dialect = connection.dialect.name
    if dialect == "postgresql":
        connection.execute(text(f'ALTER TABLE \"{table}\" ADD COLUMN IF NOT EXISTS {column} {definition}'))
        return

    # SQLite: inspect existing columns before ALTER TABLE.
    result = connection.execute(text(f"PRAGMA table_info({table})"))
    columns = {row[1] for row in result.fetchall()}
    if column not in columns:
        connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {definition}'))


def init_db(app):
    database_url = os.getenv("DATABASE_URL", "sqlite:///voidkage.db")
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)

    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }

    db.init_app(app)

    with app.app_context():
        from database.models import User, Document
        db.create_all()

        # Lightweight, dependency-free migration for existing VoidKage installs.
        # This preserves existing users/documents while adding production auth fields.
        with db.engine.begin() as connection:
            _add_column_if_missing(connection, "user", "telegram_auth_until", "TIMESTAMP WITH TIME ZONE")
            _add_column_if_missing(connection, "user", "pending_password_hash", "VARCHAR(255)")
            _add_column_if_missing(connection, "user", "pending_password_expires_at", "TIMESTAMP WITH TIME ZONE")
            _add_column_if_missing(connection, "user", "failed_login_attempts", "INTEGER NOT NULL DEFAULT 0")
            _add_column_if_missing(connection, "user", "locked_until", "TIMESTAMP WITH TIME ZONE")
            _add_column_if_missing(connection, "user", "password_changed_at", "TIMESTAMP WITH TIME ZONE")
