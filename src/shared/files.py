import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional


def has_audio_file(path: Optional[str]) -> bool:
    """A missing, empty, or non-file path cannot be a completed audio file."""
    if not path:
        return False
    try:
        file = Path(path)
        return file.is_file() and file.stat().st_size > 0
    except FileNotFoundError:
        return False


@contextmanager
def atomic_output(path: Path) -> Iterator[Path]:
    """Publish a complete file in one rename, retaining any previous output on error."""
    fd, name = tempfile.mkstemp(
        prefix=f".{path.stem}-", suffix=path.suffix, dir=path.parent
    )
    os.close(fd)
    temporary = Path(name)
    try:
        yield temporary
        if temporary.stat().st_size == 0:
            raise ValueError(f"Empty output for {path}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
