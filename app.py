import os
from functools import wraps

import requests
import json
from dotenv import load_dotenv
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    jsonify,
    send_file,
    abort,
)
from werkzeug.utils import secure_filename

from database.database import db, init_db
from database.models import User, Document
from auth.security import hash_pin, verify_pin
from storage.manager import save_file, get_file_path, delete_file


# ============================================================
# ENVIRONMENT
# ============================================================

load_dotenv()


# ============================================================
# FLASK APPLICATION
# ============================================================

app = Flask(__name__)

app.secret_key = os.getenv(
    "SECRET_KEY",
    "change-me-in-production"
)

app.config["MAX_CONTENT_LENGTH"] = (
    int(os.getenv("MAX_UPLOAD_MB", "20"))
    * 1024
    * 1024
)

app.config["UPLOAD_FOLDER"] = os.getenv(
    "UPLOAD_FOLDER",
    "storage/files"
)


# ============================================================
# TELEGRAM CONFIGURATION
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN"
)

TELEGRAM_WEBHOOK_SECRET = os.getenv(
    "TELEGRAM_WEBHOOK_SECRET"
)


# ============================================================
# VOIDKAGE TELEGRAM
# ============================================================

TELEGRAM_API = "https://api.telegram.org/bot{}"


def telegram_request(method, payload=None):
    token = os.getenv("TELEGRAM_BOT_TOKEN")

    if not token:
        app.logger.error(
            "TELEGRAM_BOT_TOKEN is missing"
        )
        return None

    try:
        response = requests.post(
            f"{TELEGRAM_API.format(token)}/{method}",
            json=payload or {},
            timeout=15
        )

        response.raise_for_status()

        return response.json()

    except Exception:
        app.logger.exception(
            "Telegram API request failed: %s",
            method
        )

        return None


def send_telegram_message(
    chat_id,
    text,
    keyboard=None
):

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }

    if keyboard:
        payload["reply_markup"] = {
            "inline_keyboard": keyboard
        }

    return telegram_request(
        "sendMessage",
        payload
    )


def answer_callback_query(
    callback_query_id,
    text=None
):

    payload = {
        "callback_query_id": callback_query_id
    }

    if text:
        payload["text"] = text

    return telegram_request(
        "answerCallbackQuery",
        payload
    )


def edit_telegram_message(
    chat_id,
    message_id,
    text,
    keyboard=None
):

    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML"
    }

    if keyboard is not None:
        payload["reply_markup"] = {
            "inline_keyboard": keyboard
        }

    return telegram_request(
        "editMessageText",
        payload
    )

def voidkage_main_keyboard():

    return [

        [
            {
                "text": "📂 MY DOCUMENTS",
                "callback_data": "documents"
            }
        ],

        [
            {
                "text": "➕ ADD DOCUMENT",
                "callback_data": "add_document"
            }
        ],

        [
            {
                "text": "🌐 WEB VAULT",
                "url": "https://voidkage.onrender.com"
            }
        ],

        [
            {
                "text": "⚙️ ACCOUNT",
                "callback_data": "account"
            }
        ],

        [
            {
                "text": "🚨 KILL ALL ACTIVITY",
                "callback_data": "kill_all"
            }
        ]

    ]


def send_voidkage_home(chat_id, username=None):

    if username:
        identity = f"@{username}"
    else:
        identity = "Traveler"

    text = (
        "🌑 <b>VOIDKAGE</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        f"⚡ Welcome back, <b>{identity}</b>.\n\n"

        "🔐 <b>YOUR DIGITAL VAULT</b>\n"
        "🟢 Connection: <b>ONLINE</b>\n"
        "🛡️ Security: <b>ACTIVE</b>\n\n"

        "What would you like to do?\n\n"

        "📂 Access your documents\n"
        "➕ Add a new document\n"
        "🌐 Open your web vault\n"
        "⚙️ Manage your account\n"
        "🚨 Kill active sessions\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "🜏 <i>VOIDKAGE // SECURE VAULT</i>"
    )

    send_telegram_message(
        chat_id,
        text,
        voidkage_main_keyboard()
    )





# ============================================================
# TELEGRAM SEND MESSAGE
# ============================================================

def send_telegram_message(chat_id, text):
    """
    Send a text message to a Telegram user.
    """

    token = os.getenv("TELEGRAM_BOT_TOKEN")

    if not token:
        app.logger.error(
            "TELEGRAM_BOT_TOKEN is missing"
        )
        return False

    try:

        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
            },
            timeout=10,
        )

        response.raise_for_status()

        result = response.json()

        if not result.get("ok"):
            app.logger.error(
                "Telegram API error: %s",
                result
            )
            return False

        return True

    except Exception:
        app.logger.exception(
            "Telegram message failed"
        )
        return False


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

@app.route(
    "/telegram/webhook",
    methods=["POST"]
)
def telegram_webhook():

    # --------------------------------------------------------
    # Verify Telegram secret header
    # --------------------------------------------------------

    expected_secret = os.getenv(
        "TELEGRAM_WEBHOOK_SECRET"
    )

    if expected_secret:

        received_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token"
        )

        if received_secret != expected_secret:

            app.logger.warning(
                "Unauthorized Telegram webhook request"
            )

            return jsonify({
                "ok": False,
                "error": "Unauthorized",
            }), 403

    # --------------------------------------------------------
    # Read Telegram update
    # --------------------------------------------------------

    update = request.get_json(
        silent=True
    ) or {}

    app.logger.info(
        "Telegram update received"
    )

    # --------------------------------------------------------
    # Normal message
    # --------------------------------------------------------

    message = update.get(
        "message"
    )

    if not message:
        return jsonify({
            "ok": True
        })

    chat = message.get(
        "chat",
        {}
    )

    telegram_user = message.get(
        "from",
        {}
    )

    chat_id = chat.get(
        "id"
    )

    text = message.get(
        "text",
        ""
    )

    if not chat_id:
        return jsonify({
            "ok": True
        })

    # --------------------------------------------------------
    # TELEGRAM USER INFORMATION
    # --------------------------------------------------------

    telegram_id = telegram_user.get(
        "id"
    )

    username = telegram_user.get(
        "username"
    )

    first_name = telegram_user.get(
        "first_name",
        "User"
    )

    display_name = (
        f"@{username}"
        if username
        else first_name
    )

    # --------------------------------------------------------
    # /start
    # --------------------------------------------------------

    if text.strip() == "/start":

        reply = (
            "🌑 VOIDKAGE\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"Welcome, {display_name}.\n\n"
            "Your Telegram connection is working.\n\n"
            "📁 My Documents\n"
            "➕ Add Document\n"
            "🌐 Web Vault\n"
            "⚙️ Account\n"
            "🚨 Kill All Activity\n\n"
            "Your Telegram ID:\n"
            f"{telegram_id}"
        )

        send_telegram_message(
            chat_id,
            reply
        )

        return jsonify({
            "ok": True
        })

    # --------------------------------------------------------
    # Simple help
    # --------------------------------------------------------

    if text.strip() == "/help":

        send_telegram_message(
            chat_id,
            (
                "🌑 VOIDKAGE HELP\n\n"
                "/start - Open VoidKage\n"
                "/help - Show this help\n\n"
                "Document management and secure "
                "cross-device access will be available "
                "through the VoidKage menu."
            )
        )

        return jsonify({
            "ok": True
        })

    # --------------------------------------------------------
    # Unknown command/message
    # --------------------------------------------------------

    if text.startswith("/"):

        send_telegram_message(
            chat_id,
            (
                "❓ Unknown command.\n\n"
                "Send /start to open VoidKage."
            )
        )

    return jsonify({
        "ok": True
    })


# ============================================================
# DATABASE / STORAGE INITIALIZATION
# ============================================================

os.makedirs(
    app.config["UPLOAD_FOLDER"],
    exist_ok=True
)

init_db(app)


# ============================================================
# CURRENT USER
# ============================================================

def current_user():

    uid = session.get(
        "user_id"
    )

    if not uid:
        return None

    user = User.query.get(
        uid
    )

    if not user:
        session.clear()
        return None

    if session.get(
        "security_version"
    ) != user.security_version:

        session.clear()

        return None

    return user


# ============================================================
# LOGIN REQUIRED
# ============================================================

def login_required(fn):

    @wraps(fn)
    def wrapper(*args, **kwargs):

        if not current_user():

            return redirect(
                url_for("login")
            )

        return fn(
            *args,
            **kwargs
        )

    return wrapper


# ============================================================
# HOME
# ============================================================

@app.route("/")
def index():

    if current_user():

        return redirect(
            url_for("dashboard")
        )

    return render_template(
        "login.html"
    )


# ============================================================
# LOGIN
# ============================================================

@app.route("/login")
def login():

    return render_template(
        "login.html"
    )


# ============================================================
# DEMO LOGIN
# ============================================================

@app.route(
    "/demo-login",
    methods=["POST"]
)
def demo_login():

    telegram_id = request.form.get(
        "telegram_id",
        ""
    ).strip()

    username = request.form.get(
        "username",
        ""
    ).strip()

    username = (
        username
        or "VoidKageUser"
    )

    if not telegram_id.isdigit():

        return render_template(
            "login.html",
            error=(
                "Enter a numeric Telegram "
                "user ID for demo login."
            )
        )

    user = User.query.filter_by(
        telegram_id=telegram_id
    ).first()

    if not user:

        user = User(
            telegram_id=telegram_id,
            telegram_username=username,
            first_name=username,
        )

        db.session.add(user)

        db.session.commit()

    else:

        # Keep Telegram username updated
        user.telegram_username = username

        db.session.commit()

    session["user_id"] = user.id

    session["security_version"] = (
        user.security_version
    )

    return redirect(
        url_for("dashboard")
    )


# ============================================================
# DASHBOARD
# ============================================================

@app.route("/dashboard")
@login_required
def dashboard():

    user = current_user()

    docs = (
        Document.query
        .filter_by(
            user_id=user.id
        )
        .order_by(
            Document.created_at.desc()
        )
        .all()
    )

    total = sum(
        d.file_size
        for d in docs
    )

    return render_template(
        "dashboard.html",
        user=user,
        documents=docs,
        total_bytes=total,
    )


# ============================================================
# UPLOAD
# ============================================================

@app.route("/upload", methods=["POST"])
@login_required
def upload():

    user = current_user()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Not authenticated"
        }), 401

    file = request.files.get("file")

    name = request.form.get(
        "display_name",
        ""
    ).strip()

    # --------------------------------------------------------
    # Check file
    # --------------------------------------------------------

    if not file or not file.filename:

        return jsonify({
            "ok": False,
            "error": "No file selected"
        }), 400

    if not name:

        name = os.path.splitext(
            file.filename
        )[0][:120]

    app.logger.info(
        "VOIDKAGE UPLOAD START | user_id=%s | filename=%s",
        user.id,
        file.filename
    )

    try:

        # ----------------------------------------------------
        # Save physical file
        # ----------------------------------------------------

        app.logger.info(
            "VOIDKAGE UPLOAD | Saving file..."
        )

        stored_name, size, mime = save_file(
            file,
            app.config["UPLOAD_FOLDER"],
            user.id
        )

        app.logger.info(
            "VOIDKAGE UPLOAD | File saved | key=%s | size=%s",
            stored_name,
            size
        )

        # ----------------------------------------------------
        # Create database document
        # ----------------------------------------------------

        safe_filename = secure_filename(
            file.filename
        )

        doc = Document(
            user_id=user.id,
            display_name=name[:120],
            original_filename=safe_filename,
            storage_key=stored_name,
            mime_type=mime,
            file_size=size
        )

        app.logger.info(
            "VOIDKAGE UPLOAD | Creating database record..."
        )

        db.session.add(doc)

        db.session.commit()

        app.logger.info(
            "VOIDKAGE UPLOAD SUCCESS | document_id=%s",
            doc.id
        )

        return redirect(
            url_for("dashboard")
        )

    except Exception as e:

        db.session.rollback()

        app.logger.exception(
            "VOIDKAGE UPLOAD FAILED | user_id=%s | filename=%s",
            user.id,
            file.filename
        )

        return jsonify({
            "ok": False,
            "error": "Upload failed",
            "stage": "Check Render logs"
        }), 500
# ============================================================
# DOWNLOAD
# ============================================================

@app.route(
    "/download/<int:doc_id>"
)
@login_required
def download(doc_id):

    user = current_user()

    doc = (
        Document.query
        .filter_by(
            id=doc_id,
            user_id=user.id
        )
        .first_or_404()
    )

    path = get_file_path(
        doc.storage_key,
        app.config["UPLOAD_FOLDER"]
    )

    if not os.path.exists(path):

        abort(404)

    return send_file(
        path,
        as_attachment=True,
        download_name=doc.original_filename
    )


# ============================================================
# RENAME
# ============================================================

@app.route(
    "/rename/<int:doc_id>",
    methods=["POST"]
)
@login_required
def rename(doc_id):

    user = current_user()

    doc = (
        Document.query
        .filter_by(
            id=doc_id,
            user_id=user.id
        )
        .first_or_404()
    )

    name = request.form.get(
        "display_name",
        ""
    ).strip()

    if name:

        doc.display_name = name[:120]

        db.session.commit()

    return redirect(
        url_for("dashboard")
    )


# ============================================================
# DELETE DOCUMENT
# ============================================================

@app.route(
    "/delete/<int:doc_id>",
    methods=["POST"]
)
@login_required
def delete(doc_id):

    user = current_user()

    doc = (
        Document.query
        .filter_by(
            id=doc_id,
            user_id=user.id
        )
        .first_or_404()
    )

    try:

        delete_file(
            doc.storage_key,
            app.config["UPLOAD_FOLDER"]
        )

        db.session.delete(doc)

        db.session.commit()

    except Exception:

        db.session.rollback()

        app.logger.exception(
            "Document deletion failed"
        )

        return jsonify({
            "ok": False,
            "error": "Delete failed",
        }), 500

    return redirect(
        url_for("dashboard")
    )


# ============================================================
# KILL ALL ACTIVITY
# ============================================================

@app.route(
    "/kill-all",
    methods=["POST"]
)
@login_required
def kill_all():

    user = current_user()

    # Incrementing this invalidates
    # existing sessions using the old version.
    user.security_version += 1

    db.session.commit()

    session.clear()

    return redirect(
        url_for("login")
    )


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "service": "VoidKage",
        "telegram": bool(
            os.getenv(
                "TELEGRAM_BOT_TOKEN"
            )
        ),
    })


# ============================================================
# APPLICATION START
# ============================================================

if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "5000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
