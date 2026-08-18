from datetime import datetime, timezone
from database.database import db


def utcnow():
    return datetime.now(timezone.utc)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    telegram_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    telegram_username = db.Column(db.String(128), index=True)
    first_name = db.Column(db.String(128))
    last_name = db.Column(db.String(128))

    # Existing field retained for backward compatibility with your tested DB.
    # It now stores the user's production password hash.
    vault_pin_hash = db.Column(db.String(255))

    security_version = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    last_login = db.Column(db.DateTime(timezone=True))

    telegram_auth_until = db.Column(db.DateTime(timezone=True))
    pending_password_hash = db.Column(db.String(255))
    pending_password_expires_at = db.Column(db.DateTime(timezone=True))
    failed_login_attempts = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime(timezone=True))
    password_changed_at = db.Column(db.DateTime(timezone=True))


class Document(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    display_name = db.Column(db.String(120), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    storage_key = db.Column(db.String(255), nullable=False, unique=True)
    telegram_file_id = db.Column(db.String(255))
    mime_type = db.Column(db.String(120))
    file_size = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)
