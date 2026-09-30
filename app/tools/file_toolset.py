from pathlib import Path

MAX_CHARS = 20_000  # keep small so a 2B model's context doesn't overflow

def read_file(path: str) -> dict:
    """Read a text file such as README.md and return its contents.

    Args:
        path: Path to the file, e.g. "README.md" or "/tmp/readme.md".

    Returns:
        A dict with "status" ("success" or "error") and either "content"
        or "error_message".
    """
    try:
        file_path = Path(path).expanduser().resolve()

        if not file_path.is_file():
            return {"status": "error", "error_message": f"File not found: {file_path}"}

        text = file_path.read_text(encoding="utf-8", errors="replace")

        truncated = len(text) > MAX_CHARS
        if truncated:
            text = text[:MAX_CHARS] + "\n...[truncated]"

        return {
            "status": "success",
            "path": str(file_path),
            "truncated": truncated,
            "content": text,
        }
    except Exception as e:
        return {"status": "error", "error_message": str(e)}