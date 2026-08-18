import os
import io
import html
import secrets
import threading
from datetime import datetime, timedelta, timezone
from functools import wraps

import requests
from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect, url_for, session,
    jsonify, send_file, abort
)
from werkzeug.utils import secure_filename

from database.database import db, init_db
from database.models import User, Document
from auth.security import hash_pin, verify_pin, validate_password
from storage.manager import save_file, download_file, delete_file, StorageError

load_dotenv()

# ============================================================
# VOIDKAGE PRODUCTION CONFIG
# ============================================================

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-me-in-production")
app.config.update(
    MAX_CONTENT_LENGTH=int(os.getenv("MAX_UPLOAD_MB", "20")) * 1024 * 1024,
    UPLOAD_FOLDER=os.getenv("UPLOAD_FOLDER", "storage/files"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true",
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=int(os.getenv("WEB_SESSION_HOURS", "24"))),
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")
WEB_URL = os.getenv("WEB_URL", "https://voidkage.onrender.com").rstrip("/")
TELEGRAM_WEBHOOK_URL = f"{WEB_URL}/telegram/webhook"
TELEGRAM_API = "https://api.telegram.org/bot{}"
TELEGRAM_FILE_API = "https://api.telegram.org/file/bot{}"
TELEGRAM_SESSION_MINUTES = int(os.getenv("TELEGRAM_SESSION_MINUTES", "60"))
PASSWORD_SETUP_MINUTES = int(os.getenv("PASSWORD_SETUP_MINUTES", "10"))
MAX_LOGIN_ATTEMPTS = int(os.getenv("MAX_LOGIN_ATTEMPTS", "5"))
LOCKOUT_MINUTES = int(os.getenv("LOCKOUT_MINUTES", "15"))

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
init_db(app)
telegram_lock = threading.Lock()

# ============================================================
# TIME / SECURITY HELPERS
# ============================================================

def utcnow():
    return datetime.now(timezone.utc)


def as_utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def is_locked(user):
    until = as_utc(user.locked_until)
    if until and until > utcnow():
        return True
    if until:
        user.locked_until = None
        user.failed_login_attempts = 0
        db.session.commit()
    return False


def password_is_set(user):
    return bool(user.vault_pin_hash)


def telegram_authenticated(user):
    until = as_utc(user.telegram_auth_until)
    return bool(until and until > utcnow()) and not is_locked(user)


def refresh_telegram_auth(user):
    user.telegram_auth_until = utcnow() + timedelta(minutes=TELEGRAM_SESSION_MINUTES)
    user.last_login = utcnow()
    user.failed_login_attempts = 0
    user.locked_until = None
    db.session.commit()


def register_failed_attempt(user):
    user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
    if user.failed_login_attempts >= MAX_LOGIN_ATTEMPTS:
        user.locked_until = utcnow() + timedelta(minutes=LOCKOUT_MINUTES)
        user.failed_login_attempts = 0
    db.session.commit()


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


@app.context_processor
def inject_globals():
    return {
        "csrf_token": csrf_token(),
        "max_upload_mb": int(os.getenv("MAX_UPLOAD_MB", "20")),
    }


def require_csrf():
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    expected = session.get("csrf_token")
    if not expected or not supplied or not secrets.compare_digest(supplied, expected):
        abort(400, description="Invalid CSRF token.")

# ============================================================
# TELEGRAM API
# ============================================================

def tg_request(method, payload=None, timeout=15):
    if not TELEGRAM_BOT_TOKEN:
        app.logger.error("TELEGRAM_BOT_TOKEN is missing")
        return None
    try:
        response = requests.post(
            f"{TELEGRAM_API.format(TELEGRAM_BOT_TOKEN)}/{method}",
            json=payload or {}, timeout=timeout
        )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            app.logger.error("Telegram API error [%s]: %s", method, result)
        return result
    except Exception:
        app.logger.exception("Telegram API request failed: %s", method)
        return None


def send_telegram_message(chat_id, text, keyboard=None):
    payload = {
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return tg_request("sendMessage", payload)


def answer_callback_query(callback_query_id, text=None, show_alert=False):
    payload = {"callback_query_id": callback_query_id, "show_alert": show_alert}
    if text:
        payload["text"] = text
    return tg_request("answerCallbackQuery", payload)


def send_telegram_document(chat_id, file_bytes, filename, caption=None):
    if not TELEGRAM_BOT_TOKEN:
        return None
    try:
        response = requests.post(
            f"{TELEGRAM_API.format(TELEGRAM_BOT_TOKEN)}/sendDocument",
            data={
                "chat_id": str(chat_id),
                "caption": caption or "🌑 <b>VOIDKAGE</b> • Secure document",
                "parse_mode": "HTML",
            },
            files={"document": (filename, file_bytes)},
            timeout=90,
        )
        response.raise_for_status()
        return response.json()
    except Exception:
        app.logger.exception("Telegram document send failed")
        return None

# ============================================================
# USER / TELEGRAM AUTH
# ============================================================

def get_or_create_telegram_user(telegram_user):
    telegram_id = str(telegram_user.get("id"))
    username = telegram_user.get("username") or ""
    first_name = telegram_user.get("first_name") or "User"
    last_name = telegram_user.get("last_name") or ""

    user = User.query.filter_by(telegram_id=telegram_id).first()
    if not user:
        user = User(
            telegram_id=telegram_id,
            telegram_username=username,
            first_name=first_name,
            last_name=last_name,
        )
        db.session.add(user)
    else:
        user.telegram_username = username
        user.first_name = first_name
        user.last_name = last_name
    db.session.commit()
    return user


def telegram_display_name(user):
    if user.telegram_username:
        return f"@{html.escape(user.telegram_username)}"
    return html.escape(user.first_name or "Traveler")


def voidkage_main_keyboard():
    return [
        [{"text": "📂 MY DOCUMENTS", "callback_data": "documents"}],
        [{"text": "➕ ADD DOCUMENT", "callback_data": "add_document"}],
        [{"text": "🌐 WEB VAULT", "url": WEB_URL}],
        [{"text": "⚙️ ACCOUNT", "callback_data": "account"}],
        [{"text": "🔑 CHANGE PASSWORD", "callback_data": "change_password"}],
        [{"text": "🚪 LOGOUT TELEGRAM", "callback_data": "logout_tg"}],
        [{"text": "🚨 KILL ALL ACTIVITY", "callback_data": "kill_all"}],
    ]


def send_auth_required(chat_id, user):
    if not password_is_set(user):
        return send_telegram_message(
            chat_id,
            "🌑 <b>VOIDKAGE ACTIVATION</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "🔐 Your account does not have a password yet.\n\n"
            "Create a password now. It must be 8–128 characters.\n"
            "⚠️ Do not reuse a sensitive password from another service.\n\n"
            "<b>Send your new password as the next message.</b>",
        )
    return send_telegram_message(
        chat_id,
        "🔐 <b>VOIDKAGE AUTHENTICATION</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "Your Telegram vault session has expired.\n\n"
        "🔑 Send your VoidKage password to continue.\n"
        f"⏱️ Session duration: {TELEGRAM_SESSION_MINUTES} minutes.",
    )


def send_voidkage_home(chat_id, user):
    identity = telegram_display_name(user)
    text = (
        "🌑 <b>VOIDKAGE</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        f"⚡ Welcome, <b>{identity}</b>.\n\n"
        "🔐 <b>YOUR DIGITAL VAULT</b>\n"
        "🟢 Connection: <b>ONLINE</b>\n"
        "🛡️ Security: <b>ACTIVE</b>\n\n"
        "Choose an action below.\n\n"
        "📂 Access your documents\n➕ Store a new document\n"
        "🌐 Open your web vault\n⚙️ Manage your account\n"
        "🚨 Kill all active web sessions\n\n"
        "━━━━━━━━━━━━━━━━━━\n🜏 <i>VOIDKAGE // SECURE VAULT</i>"
    )
    return send_telegram_message(chat_id, text, voidkage_main_keyboard())


def telegram_documents_keyboard(user_id):
    docs = Document.query.filter_by(user_id=user_id).order_by(Document.created_at.desc()).all()
    keyboard = []
    for doc in docs:
        title = (doc.display_name or doc.original_filename or "Document").strip()
        if len(title) > 30:
            title = title[:27] + "..."
        keyboard.append([{"text": f"⬇️ {title}", "callback_data": f"doc:{doc.id}"}])
        keyboard.append([{"text": f"🗑️ DELETE {title}", "callback_data": f"del:{doc.id}"}])
    keyboard.append([{"text": "➕ ADD DOCUMENT", "callback_data": "add_document"}])
    keyboard.append([{"text": "↩️ HOME", "callback_data": "home"}])
    return keyboard


def format_bytes(value):
    value = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024


def send_documents(chat_id, user):
    docs = Document.query.filter_by(user_id=user.id).order_by(Document.created_at.desc()).all()
    total = sum((d.file_size or 0) for d in docs)
    text = (
        "📂 <b>MY DOCUMENTS</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        f"🗃️ Files: <b>{len(docs)}</b>\n💾 Stored: <b>{format_bytes(total)}</b>\n\n"
        + ("Tap a file to download it to this Telegram device." if docs else "Your vault is empty.\n\n➕ Add your first document.")
    )
    return send_telegram_message(chat_id, text, telegram_documents_keyboard(user.id))


def telegram_account(chat_id, user):
    docs_count = Document.query.filter_by(user_id=user.id).count()
    return send_telegram_message(
        chat_id,
        "⚙️ <b>ACCOUNT</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 Name: <b>{html.escape(user.first_name or 'User')}</b>\n"
        f"🔹 Username: <b>{telegram_display_name(user)}</b>\n"
        f"🆔 Telegram ID: <code>{html.escape(str(user.telegram_id))}</code>\n"
        f"📂 Documents: <b>{docs_count}</b>\n"
        f"🛡️ Telegram session: <b>{TELEGRAM_SESSION_MINUTES} min</b>\n\n"
        "Your password is never displayed by VoidKage.",
        [[{"text": "🔑 CHANGE PASSWORD", "callback_data": "change_password"}],
         [{"text": "🚪 LOGOUT TELEGRAM", "callback_data": "logout_tg"}],
         [{"text": "↩️ HOME", "callback_data": "home"}]],
    )


def telegram_add_instructions(chat_id):
    return send_telegram_message(
        chat_id,
        "➕ <b>ADD DOCUMENT</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        "📎 Send me a supported document now.\n\n"
        "🏷️ <b>Custom name:</b> add a caption to the file.\n"
        "Example: <code>College Certificate</code>\n\n"
        "🌐 The same document will appear in your web vault.",
        [[{"text": "📂 VIEW DOCUMENTS", "callback_data": "documents"}],
         [{"text": "↩️ HOME", "callback_data": "home"}]],
    )


def telegram_file_to_storage(message, user):
    document = message.get("document") or {}
    file_id = document.get("file_id")
    if not file_id:
        raise StorageError("Telegram document has no file_id")

    file_info = tg_request("getFile", {"file_id": file_id}, timeout=30)
    if not file_info or not file_info.get("ok"):
        raise StorageError("Telegram could not prepare the file")

    telegram_path = file_info["result"].get("file_path")
    if not telegram_path:
        raise StorageError("Telegram returned no file path")

    response = requests.get(f"{TELEGRAM_FILE_API.format(TELEGRAM_BOT_TOKEN)}/{telegram_path}", timeout=90)
    response.raise_for_status()

    filename = secure_filename(document.get("file_name") or os.path.basename(telegram_path) or "telegram_document")
    from werkzeug.datastructures import FileStorage
    uploaded = FileStorage(
        stream=io.BytesIO(response.content),
        filename=filename,
        content_type=document.get("mime_type") or response.headers.get("Content-Type", "application/octet-stream"),
    )
    stored_name, size, mime = save_file(uploaded, app.config["UPLOAD_FOLDER"], user.id)
    caption = (message.get("caption") or "").strip()
    display_name = caption[:120] if caption else os.path.splitext(filename)[0][:120]
    doc = Document(
        user_id=user.id,
        display_name=display_name,
        original_filename=filename,
        storage_key=stored_name,
        telegram_file_id=file_id,
        mime_type=mime,
        file_size=size,
    )
    db.session.add(doc)
    db.session.commit()
    return doc

# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    expected_secret = TELEGRAM_WEBHOOK_SECRET
    if expected_secret:
        received = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if not received or not secrets.compare_digest(received, expected_secret):
            return jsonify({"ok": False, "error": "Unauthorized"}), 403

    update = request.get_json(silent=True) or {}
    callback = update.get("callback_query")

    if callback:
        callback_id = callback.get("id")
        data = callback.get("data", "")
        message = callback.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        telegram_user = callback.get("from") or {}
        if not chat_id:
            return jsonify({"ok": True})

        with telegram_lock:
            try:
                user = get_or_create_telegram_user(telegram_user)
                if data == "home":
                    answer_callback_query(callback_id)
                    if telegram_authenticated(user): send_voidkage_home(chat_id, user)
                    else: send_auth_required(chat_id, user)
                elif not telegram_authenticated(user):
                    answer_callback_query(callback_id, "🔐 Authenticate first.", True)
                    send_auth_required(chat_id, user)
                elif data == "documents":
                    answer_callback_query(callback_id); send_documents(chat_id, user)
                elif data == "add_document":
                    answer_callback_query(callback_id); telegram_add_instructions(chat_id)
                elif data == "account":
                    answer_callback_query(callback_id); telegram_account(chat_id, user)
                elif data == "logout_tg":
                    user.telegram_auth_until = None; db.session.commit()
                    answer_callback_query(callback_id, "🚪 Logged out.", True)
                    send_auth_required(chat_id, user)
                elif data == "change_password":
                    answer_callback_query(callback_id)
                    send_telegram_message(chat_id, "🔑 <b>CHANGE PASSWORD</b>\n\nSend your new password (8–128 characters).\n\nType /cancel to abort.")
                    user.pending_password_expires_at = utcnow() + timedelta(minutes=PASSWORD_SETUP_MINUTES)
                    user.pending_password_hash = "CHANGE_PENDING"
                    db.session.commit()
                elif data == "kill_all":
                    user.security_version += 1
                    user.telegram_auth_until = None
                    db.session.commit()
                    answer_callback_query(callback_id, "🚨 All sessions invalidated.", True)
                    send_telegram_message(chat_id, "🚨 <b>ALL ACTIVITY KILLED</b>\n\n🔒 Existing web sessions are invalid.\n🛡️ Documents remain stored.\n🔐 Telegram authentication has also been cleared.", [[{"text":"🌐 OPEN WEB VAULT","url":WEB_URL}]])
                elif data.startswith("doc:"):
                    answer_callback_query(callback_id, "Preparing…")
                    doc = Document.query.filter_by(id=int(data.split(":",1)[1]), user_id=user.id).first()
                    if not doc:
                        send_telegram_message(chat_id, "❌ Document not found.")
                    else:
                        file_obj = download_file(doc.storage_key, app.config["UPLOAD_FOLDER"])
                        if isinstance(file_obj, str):
                            with open(file_obj, "rb") as fp: payload = fp.read()
                        else:
                            payload = file_obj.getvalue()
                        result = send_telegram_document(chat_id, io.BytesIO(payload), doc.original_filename, f"📄 <b>{html.escape(doc.display_name)}</b>\n🌑 VOIDKAGE")
                        if not result or not result.get("ok"):
                            send_telegram_message(chat_id, "❌ Could not send that document.")
                elif data.startswith("del:"):
                    doc = Document.query.filter_by(id=int(data.split(":",1)[1]), user_id=user.id).first()
                    if not doc:
                        answer_callback_query(callback_id, "Document not found.", True)
                    else:
                        delete_file(doc.storage_key, app.config["UPLOAD_FOLDER"])
                        db.session.delete(doc); db.session.commit()
                        answer_callback_query(callback_id, "🗑️ Deleted.")
                        send_documents(chat_id, user)
                else:
                    answer_callback_query(callback_id, "Unknown action.", True)
            except Exception:
                db.session.rollback()
                app.logger.exception("Telegram callback handling failed")
                if callback_id: answer_callback_query(callback_id, "VoidKage error.", True)
        return jsonify({"ok": True})

    message = update.get("message")
    if not message:
        return jsonify({"ok": True})
    chat_id = (message.get("chat") or {}).get("id")
    telegram_user = message.get("from") or {}
    text = (message.get("text") or "").strip()
    if not chat_id:
        return jsonify({"ok": True})

    with telegram_lock:
        try:
            user = get_or_create_telegram_user(telegram_user)

            if text == "/start":
                user.pending_password_hash = None
                user.pending_password_expires_at = None
                db.session.commit()
                if telegram_authenticated(user): send_voidkage_home(chat_id, user)
                else: send_auth_required(chat_id, user)
                return jsonify({"ok": True})

            if text == "/cancel":
                user.pending_password_hash = None; user.pending_password_expires_at = None
                db.session.commit(); send_telegram_message(chat_id, "↩️ Current password operation cancelled.")
                if telegram_authenticated(user): send_voidkage_home(chat_id, user)
                else: send_auth_required(chat_id, user)
                return jsonify({"ok": True})

            # Password setup / change confirmation flow.
            pending = user.pending_password_hash
            pending_until = as_utc(user.pending_password_expires_at)
            if pending and pending_until and pending_until > utcnow() and not text.startswith("/") and not message.get("document"):
                if pending == "CHANGE_PENDING":
                    ok, error = validate_password(text)
                    if not ok:
                        send_telegram_message(chat_id, f"❌ {html.escape(error)}\n\nSend a new password or /cancel.")
                    else:
                        user.pending_password_hash = hash_pin(text)
                        user.pending_password_expires_at = utcnow() + timedelta(minutes=PASSWORD_SETUP_MINUTES)
                        user.failed_login_attempts = 0
                        db.session.commit()
                        send_telegram_message(chat_id, "🔐 Now send the same new password again to confirm it.")
                    return jsonify({"ok": True})

                # pending hash is the first password entry; confirm it.
                if verify_pin(text, pending):
                    user.vault_pin_hash = pending
                    user.pending_password_hash = None
                    user.pending_password_expires_at = None
                    user.password_changed_at = utcnow()
                    user.security_version += 1
                    user.telegram_auth_until = utcnow() + timedelta(minutes=TELEGRAM_SESSION_MINUTES)
                    user.last_login = utcnow()
                    user.failed_login_attempts = 0
                    db.session.commit()
                    send_telegram_message(chat_id, "✅ <b>PASSWORD UPDATED</b>\n\nYour VoidKage password is active.\n🌐 The same password works on the web vault.\n🔒 Existing web sessions were invalidated.")
                    send_voidkage_home(chat_id, user)
                else:
                    user.pending_password_hash = None; user.pending_password_expires_at = None
                    db.session.commit()
                    send_telegram_message(chat_id, "❌ Passwords did not match. Start again with /start.")
                return jsonify({"ok": True})

            if text == "/help":
                send_telegram_message(chat_id, "🜏 <b>VOIDKAGE COMMAND CENTER</b>\n\n/start — authenticate\n/documents — list documents\n/add — upload\n/account — account\n/password — change password\n/logout — logout Telegram\n/kill — kill all activity\n/help — commands")
                return jsonify({"ok": True})

            if text == "/password":
                if not telegram_authenticated(user): send_auth_required(chat_id, user)
                else:
                    user.pending_password_hash = "CHANGE_PENDING"; user.pending_password_expires_at = utcnow() + timedelta(minutes=PASSWORD_SETUP_MINUTES); db.session.commit()
                    send_telegram_message(chat_id, "🔑 Send your new password (8–128 characters).\n\nType /cancel to abort.")
                return jsonify({"ok": True})

            if text == "/logout":
                user.telegram_auth_until = None; db.session.commit(); send_telegram_message(chat_id, "🚪 Logged out.\n\nUse /start to authenticate again.")
                return jsonify({"ok": True})

            if text == "/kill":
                if not telegram_authenticated(user): send_auth_required(chat_id, user)
                else:
                    user.security_version += 1; user.telegram_auth_until = None; db.session.commit()
                    send_telegram_message(chat_id, "🚨 <b>ALL ACTIVITY KILLED</b>\n\n🔒 Web sessions invalidated.\n🔐 Telegram session ended.\n📁 Documents untouched.")
                return jsonify({"ok": True})

            # If a password exists, any normal message is treated as authentication while locked out.
            if not telegram_authenticated(user):
                if text and not text.startswith("/") and not message.get("document"):
                    if is_locked(user):
                        send_telegram_message(chat_id, "⛔ Too many failed attempts. Try again later.")
                    elif not password_is_set(user):
                        ok, error = validate_password(text)
                        if not ok:
                            send_telegram_message(chat_id, f"❌ {html.escape(error)}\n\nSend a new password.")
                        else:
                            user.pending_password_hash = hash_pin(text)
                            user.pending_password_expires_at = utcnow() + timedelta(minutes=PASSWORD_SETUP_MINUTES)
                            db.session.commit()
                            send_telegram_message(chat_id, "🔐 Password received.\n\nSend it again to confirm.")
                    elif verify_pin(text, user.vault_pin_hash):
                        refresh_telegram_auth(user); send_voidkage_home(chat_id, user)
                    else:
                        register_failed_attempt(user)
                        send_telegram_message(chat_id, "❌ Incorrect password.\n\nTry again or use /start.")
                else:
                    send_auth_required(chat_id, user)
                return jsonify({"ok": True})

            if text == "/documents": send_documents(chat_id, user); return jsonify({"ok": True})
            if text == "/add": telegram_add_instructions(chat_id); return jsonify({"ok": True})
            if text == "/account": telegram_account(chat_id, user); return jsonify({"ok": True})

            if message.get("document"):
                try:
                    doc = telegram_file_to_storage(message, user)
                    send_telegram_message(chat_id, "✅ <b>DOCUMENT STORED</b>\n━━━━━━━━━━━━━━━━━━\n\n"
                        f"🏷️ Name: <b>{html.escape(doc.display_name)}</b>\n📦 Size: <b>{format_bytes(doc.file_size)}</b>\n\n"
                        "🌐 Available in your web vault.", [[{"text":"📂 MY DOCUMENTS","callback_data":"documents"}], [{"text":"🌐 WEB VAULT","url":WEB_URL}]])
                except Exception:
                    db.session.rollback(); app.logger.exception("Telegram upload failed | chat_id=%s", chat_id)
                    send_telegram_message(chat_id, "❌ <b>UPLOAD FAILED</b>\n\nCheck file type/size and try again.")
                return jsonify({"ok": True})

            if text.startswith("/"):
                send_telegram_message(chat_id, "❓ Unknown command. Use /help.")
            else:
                send_voidkage_home(chat_id, user)
        except Exception:
            db.session.rollback(); app.logger.exception("Telegram message handling failed")
            send_telegram_message(chat_id, "⚠️ VoidKage encountered an internal error. Please try /start again.")

    return jsonify({"ok": True})

# ============================================================
# TELEGRAM WEBHOOK CONFIGURATION
# ============================================================

def configure_telegram_webhook():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_WEBHOOK_SECRET:
        app.logger.warning("Telegram webhook not configured: token/secret missing")
        return
    result = tg_request("setWebhook", {
        "url": TELEGRAM_WEBHOOK_URL,
        "secret_token": TELEGRAM_WEBHOOK_SECRET,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
    })
    if result and result.get("ok"):
        app.logger.info("VoidKage Telegram webhook configured: %s", TELEGRAM_WEBHOOK_URL)

# ============================================================
# WEBSITE AUTH
# ============================================================

def current_user():
    uid = session.get("user_id")
    version = session.get("security_version")
    if not uid or version is None:
        return None
    user = db.session.get(User, uid)
    if not user or version != user.security_version:
        session.clear()
        return None
    return user


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def find_user(identifier):
    identifier = (identifier or "").strip().lstrip("@")
    if identifier.isdigit():
        return User.query.filter_by(telegram_id=identifier).first()
    return User.query.filter(db.func.lower(User.telegram_username) == identifier.lower()).first()


@app.route("/")
def index():
    return redirect(url_for("dashboard")) if current_user() else redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        require_csrf()
        identifier = request.form.get("identifier", "").strip()
        password = request.form.get("password", "")
        user = find_user(identifier)
        if not user:
            return render_template("login.html", error="Account not found. Open @VoidKageBot and send /start first.", identifier=identifier)
        if is_locked(user):
            return render_template("login.html", error="Too many failed attempts. Try again later.", identifier=identifier)
        if not password_is_set(user):
            return render_template("login.html", error="Password not activated yet. Open @VoidKageBot and send /start to create it.", identifier=identifier)
        if not verify_pin(password, user.vault_pin_hash):
            register_failed_attempt(user)
            return render_template("login.html", error="Incorrect password.", identifier=identifier)

        session.clear()
        session.permanent = True
        session["user_id"] = user.id
        session["security_version"] = user.security_version
        session["csrf_token"] = secrets.token_urlsafe(32)
        user.last_login = utcnow(); user.failed_login_attempts = 0; user.locked_until = None
        db.session.commit()
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    require_csrf(); session.clear(); return redirect(url_for("login"))


@app.route("/change-password", methods=["POST"])
@login_required
def change_password():
    require_csrf()
    user = current_user()
    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")
    if not verify_pin(current, user.vault_pin_hash):
        return redirect(url_for("dashboard", error="Current password is incorrect."))
    ok, error = validate_password(new)
    if not ok:
        return redirect(url_for("dashboard", error=error))
    if new != confirm:
        return redirect(url_for("dashboard", error="New passwords do not match."))
    user.vault_pin_hash = hash_pin(new)
    user.password_changed_at = utcnow()
    user.security_version += 1
    user.telegram_auth_until = None
    db.session.commit()
    session.clear()
    return redirect(url_for("login", changed="1"))


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    docs = Document.query.filter_by(user_id=user.id).order_by(Document.created_at.desc()).all()
    total = sum((d.file_size or 0) for d in docs)
    return render_template("dashboard.html", user=user, documents=docs, total_bytes=total, error=request.args.get("error"))


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    require_csrf()
    user = current_user()
    file = request.files.get("file")
    name = request.form.get("display_name", "").strip()
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "No file selected"}), 400
    if not name:
        name = os.path.splitext(file.filename)[0][:120]
    try:
        stored_name, size, mime = save_file(file, app.config["UPLOAD_FOLDER"], user.id)
        doc = Document(user_id=user.id, display_name=name[:120], original_filename=secure_filename(file.filename), storage_key=stored_name, mime_type=mime, file_size=size)
        db.session.add(doc); db.session.commit()
        return redirect(url_for("dashboard"))
    except Exception:
        db.session.rollback(); app.logger.exception("Web upload failed | user_id=%s", user.id)
        return jsonify({"ok": False, "error": "Upload failed"}), 500


@app.route("/download/<int:doc_id>")
@login_required
def download(doc_id):
    user = current_user()
    doc = Document.query.filter_by(id=doc_id, user_id=user.id).first_or_404()
    try:
        result = download_file(doc.storage_key, app.config["UPLOAD_FOLDER"])
        if isinstance(result, str):
            return send_file(result, as_attachment=True, download_name=doc.original_filename)
        result.seek(0)
        return send_file(result, as_attachment=True, download_name=doc.original_filename, mimetype=doc.mime_type)
    except StorageError:
        abort(404)


@app.route("/rename/<int:doc_id>", methods=["POST"])
@login_required
def rename(doc_id):
    require_csrf(); user = current_user()
    doc = Document.query.filter_by(id=doc_id, user_id=user.id).first_or_404()
    name = request.form.get("display_name", "").strip()
    if name:
        doc.display_name = name[:120]; db.session.commit()
    return redirect(url_for("dashboard"))


@app.route("/delete/<int:doc_id>", methods=["POST"])
@login_required
def delete(doc_id):
    require_csrf(); user = current_user()
    doc = Document.query.filter_by(id=doc_id, user_id=user.id).first_or_404()
    try:
        delete_file(doc.storage_key, app.config["UPLOAD_FOLDER"])
        db.session.delete(doc); db.session.commit()
        return redirect(url_for("dashboard"))
    except Exception:
        db.session.rollback(); app.logger.exception("Document deletion failed")
        return jsonify({"ok": False, "error": "Delete failed"}), 500


@app.route("/kill-all", methods=["POST"])
@login_required
def kill_all():
    require_csrf(); user = current_user()
    user.security_version += 1
    user.telegram_auth_until = None
    db.session.commit()
    session.clear()
    return redirect(url_for("login"))

# ============================================================
# HEALTH / TELEGRAM STATUS
# ============================================================

@app.route("/admin/telegram-status")
def telegram_status():
    if not TELEGRAM_BOT_TOKEN:
        return jsonify({"ok": False, "telegram_configured": False}), 503
    me = tg_request("getMe")
    webhook = tg_request("getWebhookInfo")
    return jsonify({
        "ok": bool(me and me.get("ok")),
        "telegram_configured": True,
        "bot": me.get("result") if me and me.get("ok") else None,
        "webhook": webhook.get("result") if webhook and webhook.get("ok") else None,
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "VoidKage", "telegram": bool(TELEGRAM_BOT_TOKEN)})


if TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET:
    threading.Thread(target=configure_telegram_webhook, daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
