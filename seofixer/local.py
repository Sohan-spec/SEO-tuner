"""Apply the reviewed changes to a project folder on disk (the CLI's version of the pull request).

The originals are copied to .seofixer/backup first, so "Undo my last fixes" can put them back."""

import shutil
from pathlib import Path

from .github import EDITABLE_EXTENSIONS, IMAGE_EXTENSIONS, SKIPPED_FOLDERS, apply_changes

BACKUP = Path(".seofixer") / "backup"


def project_files(root):
    """Relative paths of the project's files, skipping hidden, build and dependency folders."""
    paths = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if path.is_file() and not any(part.startswith(".") for part in rel.parts) \
                and not any(folder in rel.as_posix() for folder in SKIPPED_FOLDERS):
            paths.append(rel.as_posix())
    return paths


def apply_to_folder(project, fixes, page_url, perf_items):
    root = Path(project)
    paths = project_files(root)
    files = {}
    for rel in paths:
        if rel.endswith(EDITABLE_EXTENSIONS) and (root / rel).stat().st_size <= 200_000:
            try:
                files[rel] = {"text": (root / rel).read_text("utf-8"), "changed": False}
            except UnicodeDecodeError:
                continue  # not a text file we can safely edit
    image_paths = [p for p in paths if p.lower().endswith(IMAGE_EXTENSIONS)]
    result, new_images = apply_changes(files, image_paths, lambda p: (root / p).read_bytes(), fixes, page_url, perf_items)

    changed = {p: f["text"].encode("utf-8") for p, f in files.items() if f["changed"]} | new_images
    shutil.rmtree(root / BACKUP, ignore_errors=True)
    for rel, data in changed.items():
        backup = root / BACKUP / rel
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, backup)  # keep the original before overwriting it
        (root / rel).write_bytes(data)
    return result


def undo(project):
    """Restore the files saved by the last apply_to_folder. Returns the restored paths."""
    root = Path(project)
    backup = root / BACKUP
    restored = []
    for path in sorted(backup.rglob("*")):
        if path.is_file():
            rel = path.relative_to(backup)
            shutil.copy2(path, root / rel)
            restored.append(rel.as_posix())
    shutil.rmtree(backup, ignore_errors=True)
    return restored
