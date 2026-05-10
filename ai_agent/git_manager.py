import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from .config import MAX_FILE_LINES, MAX_FILE_SIZE_BYTES, ZERO_SHA_RE
from .git_utils import get_repo_root, to_git_path
from .process_utils import run_process


@dataclass
class SizeCheck:
    too_large: bool
    reason: str = ""


class GitManager:
    SOURCE_EXTENSIONS = (".py", ".dart", ".swift", ".js", ".ts", ".java", ".cpp", ".c", ".cs")
    EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

    @staticmethod
    def get_repo_root() -> Path:
        return get_repo_root() or Path.cwd().resolve()

    @staticmethod
    def get_files_changed_by_push(pre_push_stdin: str) -> List[Path]:
        """
        Interpreta lo stdin del pre-push hook.

        Ogni riga ha forma:
        local_ref local_sha remote_ref remote_sha

        Se lo script viene lanciato manualmente e non riceve stdin, si ripiega
        sull'ultimo commit locale.
        """
        repo_root = GitManager.get_repo_root()
        changed: List[Path] = []

        for line in pre_push_stdin.splitlines():
            parts = line.strip().split()
            if len(parts) != 4:
                continue

            _local_ref, local_sha, _remote_ref, remote_sha = parts
            if ZERO_SHA_RE.match(local_sha):
                continue

            if ZERO_SHA_RE.match(remote_sha):
                names = GitManager._diff_names([GitManager.EMPTY_TREE, local_sha], repo_root)
            else:
                names = GitManager._diff_names([f"{remote_sha}..{local_sha}"], repo_root)

            for name in names:
                changed.append((repo_root / name).resolve())

        if not changed:
            changed = GitManager.get_files_from_last_commit()

        return GitManager._dedupe_existing_source_files(changed)

    @staticmethod
    def get_files_from_last_commit() -> List[Path]:
        repo_root = GitManager.get_repo_root()
        names = GitManager._diff_tree_names("HEAD", repo_root)
        return GitManager._dedupe_existing_source_files((repo_root / n).resolve() for n in names)

    @staticmethod
    def _diff_names(args: List[str], repo_root: Path) -> List[str]:
        res = run_process(["git", "diff", "--name-only", "--diff-filter=ACMR"] + args, cwd=repo_root)
        if res.returncode != 0:
            return []
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]

    @staticmethod
    def _diff_tree_names(commit: str, repo_root: Path) -> List[str]:
        res = run_process(
            [
                "git",
                "diff-tree",
                "--no-commit-id",
                "--root",
                "--name-only",
                "-r",
                "--diff-filter=ACMR",
                commit,
            ],
            cwd=repo_root,
        )
        if res.returncode != 0:
            return []
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]

    @staticmethod
    def _dedupe_existing_source_files(paths: Iterable[Path]) -> List[Path]:
        seen = set()
        result: List[Path] = []

        for path in paths:
            try:
                resolved = path.resolve()
            except Exception:
                continue

            key = str(resolved).lower() if os.name == "nt" else str(resolved)
            if key in seen:
                continue
            seen.add(key)

            if resolved.exists() and resolved.is_file() and GitManager.is_source_file(resolved):
                result.append(resolved)

        return result

    @staticmethod
    def is_source_file(path: Path) -> bool:
        return path.suffix.lower() in GitManager.SOURCE_EXTENSIONS

    @staticmethod
    def is_file_too_large(path: Path) -> SizeCheck:
        """Controlla dimensione, numero righe e presenza di byte nulli."""
        try:
            size = path.stat().st_size
        except OSError as exc:
            return SizeCheck(True, f"stat non riuscito: {exc}")

        if size > MAX_FILE_SIZE_BYTES:
            return SizeCheck(True, f"{size} byte > {MAX_FILE_SIZE_BYTES} byte")

        try:
            with path.open("rb") as f:
                first_chunk = f.read(4096)
                if b"\0" in first_chunk:
                    return SizeCheck(True, "file probabilmente binario")

                line_count = first_chunk.count(b"\n")
                for chunk in iter(lambda: f.read(8192), b""):
                    line_count += chunk.count(b"\n")
                    if line_count > MAX_FILE_LINES:
                        return SizeCheck(True, f"{line_count} righe > {MAX_FILE_LINES} righe")
        except OSError as exc:
            return SizeCheck(True, f"lettura non riuscita: {exc}")

        return SizeCheck(False, "")

    @staticmethod
    def get_context_files(target_file: Path, max_files: int = 3) -> List[Path]:
        """
        Prende pochi file vicini al target, con stessa estensione.

        Il contesto e utile, ma deve restare piccolo per non gonfiare il prompt.
        """
        target_dir = target_file.parent
        target_ext = target_file.suffix.lower()
        context_files: List[Path] = []

        if not target_dir.exists():
            return context_files

        for candidate in sorted(target_dir.iterdir(), key=lambda p: p.name.lower()):
            if candidate.resolve() == target_file.resolve():
                continue
            if not candidate.is_file() or candidate.suffix.lower() != target_ext:
                continue
            if GitManager.is_file_too_large(candidate).too_large:
                continue

            context_files.append(candidate)
            if len(context_files) >= max_files:
                break

        return context_files

    @staticmethod
    def read_files(file_list: Iterable[Path], repo_root: Optional[Path] = None) -> str:
        blocks: List[str] = []
        root = repo_root or GitManager.get_repo_root()

        for file_path in file_list:
            if not file_path.exists() or not file_path.is_file():
                continue
            if GitManager.is_file_too_large(file_path).too_large:
                continue

            text = GitManager._read_text_with_fallback(file_path)
            if text is None:
                continue

            try:
                label = to_git_path(file_path, root)
            except Exception:
                label = file_path.name

            blocks.append(f"\n\n--- FILE: {label} ---\n{text}\n")

        return "".join(blocks)

    @staticmethod
    def _read_text_with_fallback(file_path: Path) -> Optional[str]:
        for encoding in ("utf-8", "latin-1"):
            try:
                return file_path.read_text(encoding=encoding)
            except UnicodeDecodeError:
                continue
            except OSError:
                return None
        return None

    @staticmethod
    def has_worktree_changes(path: Path, repo_root: Path) -> bool:
        try:
            rel = to_git_path(path, repo_root)
        except Exception:
            rel = str(path)

        res = run_process(["git", "diff", "--quiet", "--", rel], cwd=repo_root)
        return res.returncode != 0
