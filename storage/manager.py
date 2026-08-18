import os
import uuid
from werkzeug.utils import secure_filename


# ============================================================
# VOIDKAGE ALLOWED DOCUMENT TYPES
# ============================================================

ALLOWED_EXTENSIONS = {
    # Documents
    "pdf",
    "txt",
    "rtf",
    "md",

    # Microsoft Office
    "doc",
    "docx",
    "xls",
    "xlsx",
    "ppt",
    "pptx",

    # OpenDocument
    "odt",
    "ods",
    "odp",

    # Web / text files
    "html",
    "htm",
    "css",
    "js",
    "json",
    "xml",
    "csv",

    # Images
    "jpg",
    "jpeg",
    "png",
    "gif",
    "webp",
    "svg",

    # Archives
    "zip",
    "7z",
    "tar",
    "gz"
}


def allowed_file(filename):
    """
    Check whether the uploaded file has an allowed extension.
    """

    if not filename:
        return False

    filename = secure_filename(filename)

    if "." not in filename:
        return False

    extension = filename.rsplit(".", 1)[1].lower()

    return extension in ALLOWED_EXTENSIONS


def get_extension(filename):
    """
    Return the lowercase extension without the dot.
    """

    filename = secure_filename(filename)

    if "." not in filename:
        return ""

    return filename.rsplit(".", 1)[1].lower()


def save_file(file, upload_folder, user_id):
    """
    Save an uploaded file to the user's VoidKage storage.

    Returns:
        stored_name
        file_size
        mime_type
    """

    if not file:
        raise ValueError("No file provided.")

    original_filename = secure_filename(
        file.filename or ""
    )

    if not original_filename:
        raise ValueError("Invalid filename.")

    if not allowed_file(original_filename):
        extension = get_extension(original_filename)

        raise ValueError(
            f"File type is not allowed: .{extension}"
        )

    # --------------------------------------------------------
    # Create user-specific folder
    # --------------------------------------------------------

    user_folder = os.path.join(
        upload_folder,
        str(user_id)
    )

    os.makedirs(
        user_folder,
        exist_ok=True
    )

    # --------------------------------------------------------
    # Generate unique storage filename
    # --------------------------------------------------------

    extension = get_extension(
        original_filename
    )

    unique_name = (
        f"{uuid.uuid4().hex}.{extension}"
    )

    file_path = os.path.join(
        user_folder,
        unique_name
    )

    # --------------------------------------------------------
    # Save file
    # --------------------------------------------------------

    file.save(file_path)

    # --------------------------------------------------------
    # Get file size
    # --------------------------------------------------------

    file_size = os.path.getsize(
        file_path
    )

    # --------------------------------------------------------
    # MIME type
    # --------------------------------------------------------

    mime_type = (
        file.mimetype
        or "application/octet-stream"
    )

    # Storage key used by the database
    stored_name = os.path.join(
        str(user_id),
        unique_name
    )

    return (
        stored_name,
        file_size,
        mime_type
    )


def get_file_path(
    storage_key,
    upload_folder
):
    """
    Convert a database storage key into
    the actual filesystem path.
    """

    # Prevent path traversal
    storage_key = storage_key.replace(
        "\\",
        "/"
    )

    parts = [
        part
        for part in storage_key.split("/")
        if part not in ("", ".", "..")
    ]

    safe_key = os.path.join(*parts)

    return os.path.join(
        upload_folder,
        safe_key
    )


def delete_file(
    storage_key,
    upload_folder
):
    """
    Delete a stored file safely.
    """

    path = get_file_path(
        storage_key,
        upload_folder
    )

    if os.path.exists(path):
        os.remove(path)
