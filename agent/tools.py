import os
import subprocess
import tempfile


def _safe_resolve(repo_path: str, rel_path: str) -> str:
    repo_abs = os.path.realpath(repo_path)
    target = os.path.realpath(os.path.join(repo_abs, rel_path))
    if not target.startswith(repo_abs + os.sep) and target != repo_abs:
        raise ValueError(f"Path escapes repo root: {rel_path!r}")
    return target


def list_files(repo_path: str) -> list[str]:
    repo_abs = os.path.realpath(repo_path)
    results = []
    for dirpath, dirnames, filenames in os.walk(repo_abs):
        dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", ".venv"}]
        for fname in sorted(filenames):
            full = os.path.join(dirpath, fname)
            results.append(os.path.relpath(full, repo_abs))
    return sorted(results)


def read_file(repo_path: str, rel_path: str) -> str:
    target = _safe_resolve(repo_path, rel_path)
    with open(target, encoding="utf-8") as f:
        return f.read()


def write_file(repo_path: str, rel_path: str, content: str) -> None:
    target = _safe_resolve(repo_path, rel_path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(content)


def apply_patch(repo_path: str, patch_text: str) -> tuple[bool, str]:
    repo_abs = os.path.realpath(repo_path)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".patch", delete=False, encoding="utf-8") as tf:
        tf.write(patch_text)
        patch_path = tf.name
    try:
        result = subprocess.run(
            ["patch", "-p1", "--input", patch_path],
            cwd=repo_abs,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0, (result.stderr + result.stdout).strip()
    finally:
        os.unlink(patch_path)
