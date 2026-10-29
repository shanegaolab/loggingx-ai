"""Finds the source code stack frames point to in a local clone of the repository."""

from pathlib import Path

from loggingx_ai.schemas import CodeRef
from loggingx_ai.stacktrace import Frame

# Folders that hold dependencies or build output rather than the project's code.
_SKIPPED_FOLDERS = frozenset({
    ".git", "node_modules", "target", "build", "dist", "out", ".gradle", ".idea", ".venv", "venv", "__pycache__",
    "vendor", ".next",
})
_MAX_FILE_BYTES = 1_000_000


def store_repo(root: Path | None, store_id: int | None) -> Path | None:
    """The clone loggingx-java keeps for a log store, when it exists."""

    if root is None or store_id is None:
        return None
    path = root / f"store-{store_id}"
    return path if path.is_dir() else None


def list_files(repo: Path) -> list[str]:
    files: list[str] = []
    for folder, subfolders, names in repo.walk():
        subfolders[:] = [name for name in subfolders if name not in _SKIPPED_FOLDERS]
        for name in names:
            path = folder / name
            try:
                if path.stat().st_size <= _MAX_FILE_BYTES:
                    files.append(path.relative_to(repo).as_posix())
            except OSError:
                continue
    return files


def locate(paths: list[str], probable: str) -> str | None:
    """The file the trace's path names: the longest tail of the path some file ends with, shortest file first."""

    segments = [segment for segment in probable.replace("\\", "/").lstrip("/").split("/") if segment]
    for start in range(len(segments)):
        tail = "/".join(segments[start:])
        matches = [path for path in paths if path == tail or path.endswith("/" + tail)]
        if matches:
            return min(matches, key=len)
    return None


def snippets(repo: Path, frames: list[Frame], max_refs: int, context: int, max_chars: int) -> list[CodeRef]:
    """Takes the code around each frame's line; all snippets together are cut at max_chars."""

    paths = list_files(repo)
    refs: list[CodeRef] = []
    seen: set[str] = set()
    chars = 0
    for frame in frames:
        if len(refs) >= max_refs or chars >= max_chars:
            break
        path = locate(paths, frame.probable_path())
        if path is None:
            continue
        try:
            lines = (repo / path).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        if not lines:
            continue
        focus = frame.line if 0 < frame.line <= len(lines) else 0
        start = 1 if focus == 0 else max(1, focus - context)
        end = min(len(lines), 2 * context) if focus == 0 else min(len(lines), focus + context)
        key = f"{path}:{start}-{end}"
        if key in seen:
            continue
        seen.add(key)
        text = "\n".join(lines[start - 1:end])[: max_chars - chars]
        chars += len(text)
        refs.append(CodeRef(path=path, start_line=start, focus_line=focus, frame=frame.label(), text=text))
    return refs


def render(refs: list[CodeRef]) -> str:
    """Code with line numbers, the frame's line marked with >."""

    parts: list[str] = []
    for ref in refs:
        lines = [f"### {ref.path}（{ref.frame}）", "```"]
        for offset, line in enumerate(ref.text.split("\n")):
            number = ref.start_line + offset
            lines.append(f"{'>' if number == ref.focus_line else ' '}{number:5d} | {line}")
        lines.append("```")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
