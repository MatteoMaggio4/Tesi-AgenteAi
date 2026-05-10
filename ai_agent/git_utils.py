from pathlib import Path
from typing import List, Optional

from .process_utils import run_process


def git_output(args: List[str], cwd: Optional[Path] = None) -> Optional[str]:
    """Restituisce stdout di un comando git, oppure None in caso di errore."""
    try:
        res = run_process(["git"] + args, cwd=cwd)
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return None


def get_git_dir() -> Optional[Path]:
    """
    Restituisce la directory .git reale.

    Nei worktree moderni .git puo essere un file di puntamento, quindi si usa
    prima --absolute-git-dir e solo dopo si ripiega su --git-dir.
    """
    out = git_output(["rev-parse", "--absolute-git-dir"])
    if not out:
        out = git_output(["rev-parse", "--git-dir"])
    if not out:
        return None

    git_dir = Path(out)
    if not git_dir.is_absolute():
        git_dir = (Path.cwd() / git_dir).resolve()
    return git_dir


def get_repo_root() -> Optional[Path]:
    out = git_output(["rev-parse", "--show-toplevel"])
    return Path(out).resolve() if out else None


def to_git_path(path: Path, repo_root: Path) -> str:
    """Converte un percorso locale nel formato pathspec usato da Git."""
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()
