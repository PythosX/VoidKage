import os
import uuid
from werkzeug.utils import secure_filename

ALLOWED_EXTENSIONS = {
    "pdf","doc","docx","txt","jpg","jpeg","png","webp","gif",
    "xlsx","xls","ppt","pptx","csv","zip"
}

def save_file(file, base_dir, user_id):
    original = secure_filename(file.filename)
    ext = original.rsplit(".", 1)[1].lower() if "." in original else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError("File type is not allowed.")
    user_dir = os.path.join(base_dir, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    key = f"{user_id}_{uuid.uuid4().hex}.{ext}"
    path = os.path.join(user_dir, key)
    file.save(path)
    size = os.path.getsize(path)
    return os.path.join(str(user_id), key), size, file.mimetype or "application/octet-stream"

def get_file_path(storage_key, base_dir):
    return os.path.join(base_dir, storage_key)

def delete_file(storage_key, base_dir):
    path = get_file_path(storage_key, base_dir)
    if os.path.exists(path):
        os.remove(path)
