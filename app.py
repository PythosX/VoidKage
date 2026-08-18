import os
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file, abort
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from database.database import db, init_db
from database.models import User, Document
from auth.security import hash_pin, verify_pin
from storage.manager import save_file, get_file_path, delete_file
import requests
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-me-in-production")
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "20")) * 1024 * 1024
app.config["UPLOAD_FOLDER"] = os.getenv("UPLOAD_FOLDER", "storage/files")

@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    import os
    from flask import request, jsonify

    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET")

    # Verify Telegram's secret header
    if secret:
        received_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token"
        )

        if received_secret != secret:
            return jsonify({
                "ok": False,
                "error": "Unauthorized"
            }), 403

    update = request.get_json(silent=True) or {}

    # Temporary testing response/logging
    app.logger.info("Telegram update received: %s", update)

    message = update.get("message", {})
    text = message.get("text", "")

    if text == "/start":
        chat_id = message["chat"]["id"]

        # Temporary response.
        # We will replace this with the real VoidKage account system.
        send_telegram_message(
            chat_id,
            "🌑 Welcome to VoidKage!\n\nYour Telegram connection is working."
        )

    return jsonify({"ok": True})

def send_telegram_message(chat_id, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")

    if not token:
        app.logger.error("TELEGRAM_BOT_TOKEN is missing")
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text
            },
            timeout=10
        )

        response.raise_for_status()

        return True

    except Exception:
        app.logger.exception("Telegram message failed")
        return False

@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():

    # Verify Telegram webhook secret
    expected_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET")

    if expected_secret:
        received_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token"
        )

        if received_secret != expected_secret:
            return jsonify({
                "ok": False,
                "error": "Unauthorized"
            }), 403

    update = request.get_json(silent=True) or {}

    app.logger.info(
        "Telegram update received: %s",
        update
    )

    message = update.get("message", {})

    if not message:
        return jsonify({"ok": True})

    chat = message.get("chat", {})
    user = message.get("from", {})

    chat_id = chat.get("id")
    text = message.get("text", "")

    if not chat_id:
        return jsonify({"ok": True})

    # /start
    if text == "/start":

        first_name = user.get("first_name", "User")
        username = user.get("username")

        if username:
            display_name = f"@{username}"
        else:
            display_name = first_name

        reply = (
            "🌑 VOIDKAGE\n\n"
            f"Welcome, {display_name}.\n\n"
            "Your Telegram connection is working.\n\n"
            "📁 My Documents\n"
            "➕ Add Document\n"
            "🌐 Web Vault\n"
            "⚙️ Account\n"
            "🚨 Kill All Activity"
        )

        send_telegram_message(chat_id, reply)

    return jsonify({"ok": True})

def configure_telegram_webhook():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET")

    if not token:
        app.logger.warning(
            "TELEGRAM_BOT_TOKEN is not configured"
        )
        return

    if not secret:
        app.logger.warning(
            "TELEGRAM_WEBHOOK_SECRET is not configured"
        )
        return

    webhook_url = "https://voidkage.onrender.com/telegram/webhook"

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/setWebhook",
            json={
                "url": webhook_url,
                "secret_token": secret,
                "allowed_updates": [
                    "message",
                    "callback_query"
                ]
            },
            timeout=15
        )

        result = response.json()

        if result.get("ok"):
            app.logger.info(
                "VoidKage Telegram webhook configured successfully."
            )
        else:
            app.logger.error(
                "Telegram webhook configuration failed: %s",
                result
            )

    except Exception:
        app.logger.exception(
            "Unable to configure Telegram webhook"
        )






os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
init_db(app)

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    user = User.query.get(uid)
    if not user or session.get("security_version") != user.security_version:
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

@app.route("/")
def index():
    if current_user():
        return redirect(url_for("dashboard"))
    return render_template("login.html")

@app.route("/login")
def login():
    return render_template("login.html")

@app.route("/demo-login", methods=["POST"])
def demo_login():
    # Development fallback. Replace with Telegram Login integration before production.
    telegram_id = request.form.get("telegram_id", "").strip()
    username = request.form.get("username", "").strip() or "VoidKageUser"
    if not telegram_id.isdigit():
        return render_template("login.html", error="Enter a numeric Telegram user ID for demo login.")
    user = User.query.filter_by(telegram_id=telegram_id).first()
    if not user:
        user = User(telegram_id=telegram_id, telegram_username=username, first_name=username)
        db.session.add(user)
        db.session.commit()
    session["user_id"] = user.id
    session["security_version"] = user.security_version
    return redirect(url_for("dashboard"))

@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    docs = Document.query.filter_by(user_id=user.id).order_by(Document.created_at.desc()).all()
    total = sum(d.file_size for d in docs)
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
    stored_name, size, mime = save_file(file, app.config["UPLOAD_FOLDER"], user.id)
    doc = Document(user_id=user.id, display_name=name[:120], original_filename=secure_filename(file.filename),
                   storage_key=stored_name, mime_type=mime, file_size=size)
    db.session.add(doc)
    db.session.commit()
    return redirect(url_for("dashboard"))

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
    delete_file(doc.storage_key, app.config["UPLOAD_FOLDER"])
    db.session.delete(doc)
    db.session.commit()
    return redirect(url_for("dashboard"))

@app.route("/kill-all", methods=["POST"])
@login_required
def kill_all():
    user = current_user()
    user.security_version += 1
    db.session.commit()
    session.clear()
    return redirect(url_for("login"))

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "VoidKage"})

configure_telegram_webhook()

if __name__ == "__main__":
    configure_telegram_webhook()
    app.run()
