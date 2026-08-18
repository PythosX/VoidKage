import os
import io
import html
import threading
from functools import wraps

import requests
from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect, url_for, session,
    jsonify, send_file, abort
)
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from database.database import db, init_db
from database.models import User, Document
from storage.manager import save_file, get_file_path, delete_file

load_dotenv()

# ============================================================
# VOIDKAGE CONFIG
# ============================================================

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-me-in-production")
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "20")) * 1024 * 1024
app.config["UPLOAD_FOLDER"] = os.getenv("UPLOAD_FOLDER", "storage/files")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")
WEB_URL = os.getenv("WEB_URL", "https://voidkage.onrender.com").rstrip("/")
TELEGRAM_WEBHOOK_URL = f"{WEB_URL}/telegram/webhook"
TELEGRAM_API = "https://api.telegram.org/bot{}"
TELEGRAM_FILE_API = "https://api.telegram.org/file/bot{}"

# ============================================================
# DATABASE / STORAGE
# ============================================================

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
init_db(app)

# Telegram state is deliberately minimal: document names can be supplied
# as Telegram captions, so no fragile in-memory conversation state is needed.
telegram_lock = threading.Lock()

# ============================================================
# HELPERS
# ============================================================

def tg_request(method, payload=None, timeout=15):
    """Call Telegram Bot API and return decoded JSON or None."""
    if not TELEGRAM_BOT_TOKEN:
        app.logger.error("TELEGRAM_BOT_TOKEN is missing")
        return None

    try:
        response = requests.post(
            f"{TELEGRAM_API.format(TELEGRAM_BOT_TOKEN)}/{method}",
            json=payload or {},
            timeout=timeout,
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
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return tg_request("sendMessage", payload)


def edit_telegram_message(chat_id, message_id, text, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return tg_request("editMessageText", payload)


def answer_callback_query(callback_query_id, text=None, show_alert=False):
    payload = {
        "callback_query_id": callback_query_id,
        "show_alert": show_alert,
    }
    if text:
        payload["text"] = text
    return tg_request("answerCallbackQuery", payload)


def send_telegram_document(chat_id, path, filename, caption=None):
    """Send a stored VoidKage file back to the Telegram user."""
    if not TELEGRAM_BOT_TOKEN:
        return None

    try:
        with open(path, "rb") as fp:
            response = requests.post(
                f"{TELEGRAM_API.format(TELEGRAM_BOT_TOKEN)}/sendDocument",
                data={
                    "chat_id": str(chat_id),
                    "caption": caption or "🌑 <b>VOIDKAGE</b> • Secure document",
                    "parse_mode": "HTML",
                },
                files={"document": (filename, fp)},
                timeout=60,
            )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            app.logger.error("Telegram sendDocument error: %s", result)
        return result
    except Exception:
        app.logger.exception("Telegram document send failed")
        return None


def get_or_create_telegram_user(telegram_user):
    """Create the VoidKage account automatically from Telegram /start."""
    telegram_id = str(telegram_user.get("id"))
    username = telegram_user.get("username") or ""
    first_name = telegram_user.get("first_name") or "User"

    user = User.query.filter_by(telegram_id=telegram_id).first()
    if not user:
        user = User(
            telegram_id=telegram_id,
            telegram_username=username,
            first_name=first_name,
        )
        db.session.add(user)
    else:
        user.telegram_username = username
        user.first_name = first_name

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
        [{"text": "🚨 KILL ALL ACTIVITY", "callback_data": "kill_all"}],
    ]


def send_voidkage_home(chat_id, user):
    identity = telegram_display_name(user)
    text = (
        "🌑 <b>VOIDKAGE</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"⚡ Welcome back, <b>{identity}</b>.\n\n"
        "🔐 <b>YOUR DIGITAL VAULT</b>\n"
        "🟢 Connection: <b>ONLINE</b>\n"
        "🛡️ Security: <b>ACTIVE</b>\n\n"
        "Choose an action below.\n\n"
        "📂 Access your documents\n"
        "➕ Store a new document\n"
        "🌐 Open your web vault\n"
        "⚙️ View account\n"
        "🚨 Invalidate active web sessions\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🜏 <i>VOIDKAGE // SECURE VAULT</i>"
    )
    return send_telegram_message(chat_id, text, voidkage_main_keyboard())


def documents_keyboard(user_id):
    docs = Document.query.filter_by(user_id=user_id).order_by(Document.created_at.desc()).all()
    keyboard = []
    for doc in docs:
        title = (doc.display_name or doc.original_filename or "Document").strip()
        # Telegram button labels have practical length limits; keep them compact.
        if len(title) > 32:
            title = title[:29] + "..."
        keyboard.append([{"text": f"⬇️ {title}", "callback_data": f"doc:{doc.id}"}])
        keyboard.append([{"text": f"🗑️ Delete {title}", "callback_data": f"del:{doc.id}"}])
    keyboard.append([{"text": "➕ ADD DOCUMENT", "callback_data": "add_document"}])
    keyboard.append([{"text": "↩️ HOME", "callback_data": "home"}])
    return keyboard


def send_documents(chat_id, user):
    docs = Document.query.filter_by(user_id=user.id).order_by(Document.created_at.desc()).all()
    if not docs:
        text = (
            "📂 <b>MY DOCUMENTS</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Your VoidKage vault is empty.\n\n"
            "➕ Tap <b>ADD DOCUMENT</b> and send a file.\n"
            "💡 Add a Telegram caption to give it a custom name."
        )
    else:
        total = sum((d.file_size or 0) for d in docs)
        text = (
            "📂 <b>MY DOCUMENTS</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"🗃️ Files: <b>{len(docs)}</b>\n"
            f"💾 Stored: <b>{format_bytes(total)}</b>\n\n"
            "Tap a file to send it back to this Telegram device."
        )
    return send_telegram_message(chat_id, text, documents_keyboard(user.id))


def format_bytes(value):
    value = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024


def telegram_add_instructions(chat_id):
    return send_telegram_message(
        chat_id,
        "➕ <b>ADD DOCUMENT</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "📎 Send me any supported document now.\n\n"
        "🏷️ <b>Custom name:</b> add a Telegram caption to the file.\n"
        "Example caption: <code>College Certificate</code>\n\n"
        "🔒 The document is attached to your Telegram account automatically.\n"
        "🌐 It will also appear in your VoidKage web vault.",
        [[{"text": "📂 VIEW DOCUMENTS", "callback_data": "documents"}],
         [{"text": "↩️ HOME", "callback_data": "home"}]],
    )


def telegram_account(chat_id, user):
    docs_count = Document.query.filter_by(user_id=user.id).count()
    return send_telegram_message(
        chat_id,
        "⚙️ <b>ACCOUNT</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 Name: <b>{html.escape(user.first_name or 'User')}</b>\n"
        f"🔹 Username: <b>{telegram_display_name(user)}</b>\n"
        f"🆔 Telegram ID: <code>{html.escape(str(user.telegram_id))}</code>\n"
        f"📂 Documents: <b>{docs_count}</b>\n\n"
        "🛡️ Your Telegram identity is used to locate your VoidKage account.\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚠️ <i>KILL ALL ACTIVITY invalidates existing VoidKage web sessions.</i>",
        [[{"text": "🚨 KILL ALL ACTIVITY", "callback_data": "kill_all"}],
         [{"text": "↩️ HOME", "callback_data": "home"}]],
    )


def telegram_file_to_storage(message, user):
    """Download a Telegram document and save it using the existing storage manager."""
    document = message.get("document") or {}
    file_id = document.get("file_id")
    if not file_id:
        raise ValueError("Telegram document has no file_id")

    file_info = tg_request("getFile", {"file_id": file_id})
    if not file_info or not file_info.get("ok"):
        raise ValueError("Telegram could not prepare the file for download")

    telegram_path = file_info["result"].get("file_path")
    if not telegram_path:
        raise ValueError("Telegram returned no file path")

    response = requests.get(
        f"{TELEGRAM_FILE_API.format(TELEGRAM_BOT_TOKEN)}/{telegram_path}",
        timeout=60,
    )
    response.raise_for_status()

    filename = secure_filename(document.get("file_name") or os.path.basename(telegram_path) or "telegram_document")
    if not filename:
        filename = "telegram_document"

    # Match Flask/Werkzeug's FileStorage interface expected by save_file().
    uploaded = FileStorage(
        stream=io.BytesIO(response.content),
        filename=filename,
        content_type=document.get("mime_type") or response.headers.get("Content-Type", "application/octet-stream"),
    )

    stored_name, size, mime = save_file(
        uploaded,
        app.config["UPLOAD_FOLDER"],
        user.id,
    )

    caption = (message.get("caption") or "").strip()
    display_name = caption[:120] if caption else os.path.splitext(filename)[0][:120]

    doc = Document(
        user_id=user.id,
        display_name=display_name,
        original_filename=filename,
        storage_key=stored_name,
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
    expected_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET")
    if expected_secret:
        received_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if received_secret != expected_secret:
            app.logger.warning("Unauthorized Telegram webhook request")
            return jsonify({"ok": False, "error": "Unauthorized"}), 403

    update = request.get_json(silent=True) or {}

    # --------------------------------------------------------
    # Callback buttons
    # --------------------------------------------------------
    callback = update.get("callback_query")
    if callback:
        callback_id = callback.get("id")
        data = callback.get("data", "")
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        telegram_user = callback.get("from") or {}

        if not chat_id:
            return jsonify({"ok": True})

        with telegram_lock:
            try:
                user = get_or_create_telegram_user(telegram_user)

                if data == "home":
                    answer_callback_query(callback_id)
                    send_voidkage_home(chat_id, user)

                elif data == "documents":
                    answer_callback_query(callback_id)
                    send_documents(chat_id, user)

                elif data == "add_document":
                    answer_callback_query(callback_id)
                    telegram_add_instructions(chat_id)

                elif data == "account":
                    answer_callback_query(callback_id)
                    telegram_account(chat_id, user)

                elif data == "kill_all":
                    # This invalidates web sessions for this account only.
                    user.security_version += 1
                    db.session.commit()
                    answer_callback_query(callback_id, "🔐 All active web sessions were invalidated.", True)
                    send_telegram_message(
                        chat_id,
                        "🚨 <b>ALL ACTIVITY KILLED</b>\n"
                        "━━━━━━━━━━━━━━━━━━\n\n"
                        "🔒 Existing VoidKage web sessions have been invalidated.\n"
                        "🛡️ Your documents remain safely stored.\n\n"
                        "You can sign in again through the web vault.",
                        [[{"text": "🌐 OPEN WEB VAULT", "url": WEB_URL}],
                         [{"text": "↩️ HOME", "callback_data": "home"}]],
                    )

                elif data.startswith("doc:"):
                    answer_callback_query(callback_id, "Preparing your document…")
                    try:
                        doc_id = int(data.split(":", 1)[1])
                        doc = Document.query.filter_by(id=doc_id, user_id=user.id).first()
                        if not doc:
                            send_telegram_message(chat_id, "❌ Document not found in your vault.")
                        else:
                            path = get_file_path(doc.storage_key, app.config["UPLOAD_FOLDER"])
                            if not os.path.exists(path):
                                send_telegram_message(chat_id, "⚠️ The stored file is currently unavailable.")
                            else:
                                send_telegram_document(chat_id, path, doc.original_filename, f"📄 <b>{html.escape(doc.display_name)}</b>\n🌑 VOIDKAGE")
                    except Exception:
                        app.logger.exception("Telegram document download failed")
                        send_telegram_message(chat_id, "❌ Could not retrieve that document.")

                elif data.startswith("del:"):
                    try:
                        doc_id = int(data.split(":", 1)[1])
                        doc = Document.query.filter_by(id=doc_id, user_id=user.id).first()
                        if not doc:
                            answer_callback_query(callback_id, "Document not found.", True)
                        else:
                            delete_file(doc.storage_key, app.config["UPLOAD_FOLDER"])
                            db.session.delete(doc)
                            db.session.commit()
                            answer_callback_query(callback_id, "🗑️ Document deleted.")
                            send_documents(chat_id, user)
                    except Exception:
                        db.session.rollback()
                        app.logger.exception("Telegram document deletion failed")
                        answer_callback_query(callback_id, "Delete failed.", True)

                else:
                    answer_callback_query(callback_id, "Unknown VoidKage action.", True)

            except Exception:
                db.session.rollback()
                app.logger.exception("Telegram callback handling failed")
                if callback_id:
                    answer_callback_query(callback_id, "VoidKage encountered an error.", True)

        return jsonify({"ok": True})

    # --------------------------------------------------------
    # Normal messages
    # --------------------------------------------------------
    message = update.get("message")
    if not message:
        return jsonify({"ok": True})

    chat = message.get("chat") or {}
    telegram_user = message.get("from") or {}
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()

    if not chat_id:
        return jsonify({"ok": True})

    with telegram_lock:
        try:
            user = get_or_create_telegram_user(telegram_user)

            # /start — automatic Telegram account creation/login.
            if text == "/start":
                send_voidkage_home(chat_id, user)
                return jsonify({"ok": True})

            if text == "/help":
                send_telegram_message(
                    chat_id,
                    "🜏 <b>VOIDKAGE COMMAND CENTER</b>\n\n"
                    "/start — open your vault\n"
                    "/documents — list stored documents\n"
                    "/add — upload a new document\n"
                    "/account — account information\n"
                    "/kill — invalidate web sessions\n"
                    "/help — show commands\n\n"
                    "📎 You can also send a document directly."
                )
                return jsonify({"ok": True})

            if text == "/documents":
                send_documents(chat_id, user)
                return jsonify({"ok": True})

            if text == "/add":
                telegram_add_instructions(chat_id)
                return jsonify({"ok": True})

            if text == "/account":
                telegram_account(chat_id, user)
                return jsonify({"ok": True})

            if text == "/kill":
                user.security_version += 1
                db.session.commit()
                send_telegram_message(
                    chat_id,
                    "🚨 <b>ALL ACTIVITY KILLED</b>\n\n"
                    "🔒 Existing VoidKage web sessions are now invalid.\n"
                    "🛡️ Your stored documents were not deleted."
                )
                return jsonify({"ok": True})

            # Direct Telegram document upload.
            if message.get("document"):
                try:
                    doc = telegram_file_to_storage(message, user)
                    send_telegram_message(
                        chat_id,
                        "✅ <b>DOCUMENT STORED</b>\n"
                        "━━━━━━━━━━━━━━━━━━\n\n"
                        f"🏷️ Name: <b>{html.escape(doc.display_name)}</b>\n"
                        f"📦 Size: <b>{format_bytes(doc.file_size)}</b>\n\n"
                        "🌐 It is now available in your VoidKage web vault.\n"
                        "🔐 Access is tied to your Telegram account.",
                        [[{"text": "📂 MY DOCUMENTS", "callback_data": "documents"}],
                         [{"text": "🌐 WEB VAULT", "url": WEB_URL}]],
                    )
                except Exception:
                    db.session.rollback()
                    app.logger.exception("Telegram upload failed | chat_id=%s", chat_id)
                    send_telegram_message(
                        chat_id,
                        "❌ <b>UPLOAD FAILED</b>\n\n"
                        "The file could not be stored. Check that its file type and size are allowed, then try again."
                    )
                return jsonify({"ok": True})

            if text.startswith("/"):
                send_telegram_message(
                    chat_id,
                    "❓ Unknown command.\n\nSend <b>/start</b> to open VoidKage."
                )
            else:
                send_telegram_message(
                    chat_id,
                    "🌑 <b>VoidKage is listening.</b>\n\n"
                    "📎 Send a document to store it.\n"
                    "🏷️ Add a caption to give it a custom name.\n\n"
                    "Or use <b>/start</b> for the command center."
                )

        except Exception:
            db.session.rollback()
            app.logger.exception("Telegram message handling failed")
            send_telegram_message(chat_id, "⚠️ VoidKage encountered an internal error. Please try /start again.")

    return jsonify({"ok": True})

# ============================================================
# TELEGRAM WEBHOOK CONFIGURATION
# ============================================================

def configure_telegram_webhook():
    if not TELEGRAM_BOT_TOKEN:
        app.logger.warning("TELEGRAM_BOT_TOKEN is not configured")
        return
    if not TELEGRAM_WEBHOOK_SECRET:
        app.logger.warning("TELEGRAM_WEBHOOK_SECRET is not configured")
        return

    result = tg_request(
        "setWebhook",
        {
            "url": TELEGRAM_WEBHOOK_URL,
            "secret_token": TELEGRAM_WEBHOOK_SECRET,
            "allowed_updates": ["message", "callback_query"],
            "drop_pending_updates": False,
        },
    )
    if result and result.get("ok"):
        app.logger.info("VoidKage Telegram webhook configured: %s", TELEGRAM_WEBHOOK_URL)
    else:
        app.logger.error("VoidKage Telegram webhook configuration failed")

# ============================================================
# WEB AUTHENTICATION
# ============================================================

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None

    user = db.session.get(User, uid)
    if not user:
        session.clear()
        return None

    if session.get("security_version") != user.security_version:
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

# ============================================================
# WEB ROUTES
# ============================================================

@app.route("/")
def index():
    return redirect(url_for("dashboard")) if current_user() else render_template("login.html")


@app.route("/login")
def login():
    return render_template("login.html")


@app.route("/demo-login", methods=["POST"])
def demo_login():
    telegram_id = request.form.get("telegram_id", "").strip()
    username = request.form.get("username", "").strip() or "VoidKageUser"

    if not telegram_id.isdigit():
        return render_template("login.html", error="Enter a numeric Telegram user ID.")

    user = User.query.filter_by(telegram_id=telegram_id).first()
    if not user:
        user = User(telegram_id=telegram_id, telegram_username=username, first_name=username)
        db.session.add(user)
    else:
        user.telegram_username = username
    db.session.commit()

    session["user_id"] = user.id
    session["security_version"] = user.security_version
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    docs = Document.query.filter_by(user_id=user.id).order_by(Document.created_at.desc()).all()
    total = sum((d.file_size or 0) for d in docs)
    return render_template("dashboard.html", user=user, documents=docs, total_bytes=total)


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    user = current_user()
    file = request.files.get("file")
    name = request.form.get("display_name", "").strip()

    if not file or not file.filename:
        return jsonify({"ok": False, "error": "No file selected"}), 400

    if not name:
        name = os.path.splitext(file.filename)[0][:120]

    try:
        stored_name, size, mime = save_file(file, app.config["UPLOAD_FOLDER"], user.id)
        doc = Document(
            user_id=user.id,
            display_name=name[:120],
            original_filename=secure_filename(file.filename),
            storage_key=stored_name,
            mime_type=mime,
            file_size=size,
        )
        db.session.add(doc)
        db.session.commit()
        return redirect(url_for("dashboard"))
    except Exception:
        db.session.rollback()
        app.logger.exception("VOIDKAGE WEB UPLOAD FAILED | user_id=%s | filename=%s", user.id, file.filename)
        return jsonify({"ok": False, "error": "Upload failed", "stage": "Check Render logs"}), 500


@app.route("/download/<int:doc_id>")
@login_required
def download(doc_id):
    user = current_user()
    doc = Document.query.filter_by(id=doc_id, user_id=user.id).first_or_404()
    path = get_file_path(doc.storage_key, app.config["UPLOAD_FOLDER"])
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=doc.original_filename)


@app.route("/rename/<int:doc_id>", methods=["POST"])
@login_required
def rename(doc_id):
    user = current_user()
    doc = Document.query.filter_by(id=doc_id, user_id=user.id).first_or_404()
    name = request.form.get("display_name", "").strip()
    if name:
        doc.display_name = name[:120]
        db.session.commit()
    return redirect(url_for("dashboard"))


@app.route("/delete/<int:doc_id>", methods=["POST"])
@login_required
def delete(doc_id):
    user = current_user()
    doc = Document.query.filter_by(id=doc_id, user_id=user.id).first_or_404()
    try:
        delete_file(doc.storage_key, app.config["UPLOAD_FOLDER"])
        db.session.delete(doc)
        db.session.commit()
        return redirect(url_for("dashboard"))
    except Exception:
        db.session.rollback()
        app.logger.exception("Document deletion failed")
        return jsonify({"ok": False, "error": "Delete failed"}), 500


@app.route("/kill-all", methods=["POST"])
@login_required
def kill_all():
    user = current_user()
    user.security_version += 1
    db.session.commit()
    session.clear()
    return redirect(url_for("login"))


@app.route("/admin/telegram-status")
def telegram_status():
    """Non-secret health endpoint for checking the Telegram connection."""
    if not TELEGRAM_BOT_TOKEN:
        return jsonify({"ok": False, "telegram_configured": False, "error": "TELEGRAM_BOT_TOKEN missing"}), 503

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
    return jsonify({
        "status": "ok",
        "service": "VoidKage",
        "telegram": bool(TELEGRAM_BOT_TOKEN),
    })

# ============================================================
# STARTUP
# ============================================================

# Configure webhook once the Flask module is imported by Gunicorn.
# A background thread prevents Telegram network latency from blocking startup.
if TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET:
    threading.Thread(target=configure_telegram_webhook, daemon=True).start()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
