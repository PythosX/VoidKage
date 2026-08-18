import io
import mimetypes
import os
import uuid
from werkzeug.utils import secure_filename
import requests
from urllib.parse import quote

ALLOWED_EXTENSIONS = {
    "pdf", "txt", "rtf", "md", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "odt", "ods", "odp", "html", "htm", "css", "js", "json", "xml", "csv",
    "jpg", "jpeg", "png", "gif", "webp", "svg", "zip", "7z", "tar", "gz"
}


class StorageError(Exception):
    pass


def allowed_file(filename):
    if not filename:
        return False
    filename = secure_filename(filename)
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_extension(filename):
    filename = secure_filename(filename or "")
    return filename.rsplit(".", 1)[1].lower() if "." in filename else ""


def _supabase_configured():
    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY") and os.getenv("SUPABASE_BUCKET"))


def _supabase_headers(content_type=None):
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _supabase_url(storage_key):
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    bucket = os.getenv("SUPABASE_BUCKET", "voidkage-documents")
    encoded = "/".join(quote(p, safe="") for p in storage_key.split("/"))
    return f"{base}/storage/v1/object/{quote(bucket, safe='')}/{encoded}"


def save_file(file, upload_folder, user_id):
    if not file:
        raise StorageError("No file provided.")

    original_filename = secure_filename(file.filename or "")
    if not original_filename:
        raise StorageError("Invalid filename.")
    if not allowed_file(original_filename):
        extension = get_extension(original_filename)
        raise StorageError(f"File type is not allowed: .{extension or 'unknown'}")

    extension = get_extension(original_filename)
    unique_name = f"{uuid.uuid4().hex}.{extension}"
    stored_name = f"{user_id}/{unique_name}"
    mime_type = file.mimetype or mimetypes.guess_type(original_filename)[0] or "application/octet-stream"

    if _supabase_configured():
        try:
            data = file.read()
            response = requests.post(
                _supabase_url(stored_name),
                headers={**_supabase_headers(mime_type), "x-upsert": "false"},
                data=data,
                timeout=90,
            )
            if response.status_code >= 300:
                raise StorageError(f"Supabase upload failed ({response.status_code}).")
            return stored_name, len(data), mime_type
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("Cloud storage upload failed.") from exc

    user_folder = os.path.join(upload_folder, str(user_id))
    os.makedirs(user_folder, exist_ok=True)
    file_path = os.path.join(user_folder, unique_name)
    file.save(file_path)
    return stored_name, os.path.getsize(file_path), mime_type


def download_file(storage_key, upload_folder):
    if _supabase_configured():
        response = requests.get(_supabase_url(storage_key), headers=_supabase_headers(), timeout=90)
        if response.status_code != 200:
            raise StorageError("Stored file is unavailable.")
        return io.BytesIO(response.content)

    path = get_file_path(storage_key, upload_folder)
    if not os.path.exists(path):
        raise StorageError("Stored file is unavailable.")
    return path


def get_file_path(storage_key, upload_folder):
    storage_key = (storage_key or "").replace("\\", "/")
    parts = [part for part in storage_key.split("/") if part not in ("", ".", "..")]
    if not parts:
        raise StorageError("Invalid storage key.")
    return os.path.join(upload_folder, *parts)


def delete_file(storage_key, upload_folder):
    if _supabase_configured():
        response = requests.delete(_supabase_url(storage_key), headers=_supabase_headers(), timeout=30)
        if response.status_code not in (200, 204):
            raise StorageError("Cloud deletion failed.")
        return

    path = get_file_path(storage_key, upload_folder)
    if os.path.exists(path):
        os.remove(path)
