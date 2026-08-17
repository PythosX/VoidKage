import os
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file, abort
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from database.database import db, init_db
from database.models import User, Document
from auth.security import hash_pin, verify_pin
from storage.manager import save_file, get_file_path, delete_file

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-me-in-production")
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "20")) * 1024 * 1024
app.config["UPLOAD_FOLDER"] = os.getenv("UPLOAD_FOLDER", "storage/files")

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
    stored_name, size, mime = save_file(file, user.id, app.config["UPLOAD_FOLDER"])
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
