import csv
import difflib
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog

import customtkinter as ctk
from google import genai


# ============================================================================
# CONFIGURAZIONE GENERALE
# ============================================================================

BYPASS_ENV_VAR = "AI_AGENT_BYPASS"
BYPASS_TTL_SECONDS = 120
DIFF_CONTEXT_LINES = 8
API_KEY_FALLBACK_FILE = ".api_key"
MAX_FILE_SIZE_BYTES = 150 * 1024
MAX_FILE_LINES = 2000
MAX_RETRY_ATTEMPTS = 3
MAX_FILES_TO_ANALYZE = 10
TEST_TIMEOUT_SECONDS = 30

ZERO_SHA_RE = re.compile(r"^0+$")


# ============================================================================
# FUNZIONI DI SUPPORTO PER GIT E PROCESSI
# ============================================================================


def run_process(
    args: List[str],
    cwd: Optional[Path] = None,
    timeout: Optional[int] = None,
    env: Optional[dict] = None,
) -> subprocess.CompletedProcess:
    """Esegue un comando senza shell, cosi da ridurre il rischio di injection."""
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        shell=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


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


# ============================================================================
# GESTIONE DEL BYPASS HOOK
# ============================================================================


def check_and_clear_bypass() -> bool:
    """
    Consuma un eventuale flag di bypass scritto nella directory .git.

    Il timestamp evita che un bypass rimasto sul disco per errore faccia saltare
    controlli futuri in modo silenzioso.
    """
    git_dir = get_git_dir()
    if git_dir:
        bypass_file = git_dir / "ai_agent_bypass"
        if bypass_file.exists():
            try:
                written_at = float(bypass_file.read_text(encoding="utf-8").strip())
                bypass_file.unlink(missing_ok=True)

                age = time.time() - written_at
                if age <= BYPASS_TTL_SECONDS:
                    return True

                print(
                    "[Agente AI] Bypass ignorato perche scaduto "
                    f"({int(age)}s > {BYPASS_TTL_SECONDS}s)."
                )
            except Exception:
                try:
                    bypass_file.unlink(missing_ok=True)
                except Exception:
                    pass

    if os.environ.get(BYPASS_ENV_VAR) == "1":
        return True

    return False


def set_bypass_flag() -> None:
    """Scrive il flag usato per evitare il loop dopo un commit automatico."""
    git_dir = get_git_dir()
    if not git_dir:
        return
    try:
        (git_dir / "ai_agent_bypass").write_text(str(time.time()), encoding="utf-8")
    except Exception:
        pass


# ============================================================================
# RISOLUZIONE API KEY
# ============================================================================


def resolve_api_key(repo_root: Optional[Path] = None) -> Optional[str]:
    """
    Cerca la chiave API in tre posti:
    1. variabile GOOGLE_API_KEY;
    2. file .api_key nella directory corrente;
    3. file .api_key nella root del repository.
    """
    key = os.getenv("GOOGLE_API_KEY")
    if key and key.strip():
        return key.strip()

    candidates = [Path.cwd() / API_KEY_FALLBACK_FILE]
    if repo_root:
        repo_key = repo_root / API_KEY_FALLBACK_FILE
        if repo_key not in candidates:
            candidates.append(repo_key)

    for candidate in candidates:
        try:
            if candidate.exists():
                key = candidate.read_text(encoding="utf-8").strip()
                if key:
                    return key
        except Exception:
            continue

    return None


# ============================================================================
# GESTORE REPOSITORY GIT
# ============================================================================


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


# ============================================================================
# CLIENT AI
# ============================================================================


class GenAIClient:
    MODELS = ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-2.5-pro"]

    def __init__(self, api_key: str):
        self.client = genai.Client(api_key=api_key)

    def analyze_code(
        self,
        target_file: Path,
        source_code: str,
        context_code: str = "",
        error_feedback: str = "",
    ) -> str:
        prompt = self._build_prompt(target_file, source_code, context_code, error_feedback)

        for model_name in self.MODELS:
            print(f"[API] Connessione al modello: {model_name}")
            for attempt in range(2):
                try:
                    response = self.client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                    )
                    return response.text or ""
                except Exception as exc:
                    err = str(exc)
                    if "404" in err:
                        print(f"[API] Modello non disponibile: {model_name}.")
                        break
                    if any(marker in err for marker in ("503", "UNAVAILABLE", "429")):
                        if attempt == 0:
                            print("[API] Servizio temporaneamente saturo. Riprovo tra 5 secondi.")
                            time.sleep(5)
                            continue
                        print(f"[API] Modello non raggiungibile: {model_name}.")
                        break

                    print(f"[API] Errore su {model_name}: {exc}")
                    break

        raise RuntimeError(
            "Nessun modello Gemini disponibile. Controllare connessione e API key."
        )

    @staticmethod
    def _build_prompt(
        target_file: Path,
        source_code: str,
        context_code: str,
        error_feedback: str,
    ) -> str:
        prompt = (
            "Sei un revisore automatico di codice per un progetto universitario.\n"
            "Analizza solo difetti logici, bug reali e casi limite rilevanti. "
            "Ignora stile, formattazione e preferenze personali.\n\n"
            f"FILE TARGET: {target_file}\n"
            f"CODICE TARGET:\n{source_code}\n\n"
            f"CONTESTO ARCHITETTURALE:\n{context_code}\n\n"
            "Formato obbligatorio della risposta:\n"
            "- Se trovi un bug, usa queste sezioni:\n"
            "  ## ANALISI DELL'ERRORE\n"
            "  ## CODICE CORRETTO\n"
            "  ```linguaggio\n"
            "  <file target completo corretto>\n"
            "  ```\n"
            "  ## UNIT TEST\n"
            "  ```linguaggio\n"
            "  <test flat e autonomo>\n"
            "  ```\n"
            "- Se non trovi bug, scrivi chiaramente 'Nessun bug' e fornisci comunque "
            "un test basilare di convalida.\n\n"
            "Vincoli sui test:\n"
            "1. Niente framework esterni: no pytest, junit, database o servizi remoti.\n"
            "2. Il test deve stampare esattamente queste metriche:\n"
            "   Passed: <numero>\n"
            "   Failed: <numero>\n"
            "3. Il test deve terminare con exit code 0 se passa e non-zero se fallisce.\n"
            "4. Non proporre comandi distruttivi o comandi che non eseguono il test.\n\n"
            "Concludi sempre fuori dai blocchi di codice con:\n"
            "DEPENDENCIES: NONE\n"
            "TEST_FILE_NAME: <nome_file_test>\n"
            "RUN_COMMAND: <comando_per_eseguire_il_test>\n"
        )

        if error_feedback:
            prompt += (
                "\n\n[FEEDBACK ESECUZIONE PRECEDENTE]\n"
                "Il test precedente non e stato eseguibile o non ha rispettato il formato.\n"
                "Correggi risposta, test o patch mantenendo i vincoli sopra.\n"
                f"Output ricevuto:\n```\n{error_feedback}\n```\n"
            )

        return prompt


# ============================================================================
# LOGGER PER LA TESI
# ============================================================================


class ExperimentLogger:
    LOG_FILE = "thesis_metrics.csv"

    @classmethod
    def initialize(cls, repo_root: Path) -> None:
        log_path = repo_root / cls.LOG_FILE
        if log_path.exists():
            return

        try:
            with log_path.open(mode="w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        "Timestamp",
                        "File Analizzato",
                        "Esito LLM",
                        "Stato Test",
                        "Passati",
                        "Falliti",
                        "Azione Utente",
                        "Tempo API (s)",
                        "Tempo Sessione (s)",
                    ]
                )
        except Exception:
            pass

    @classmethod
    def log_run(
        cls,
        repo_root: Path,
        target_file: Path,
        llm_status: str,
        test_status: str,
        passed: str,
        failed: str,
        human_action: str,
        api_time: float,
        session_time: float,
    ) -> None:
        try:
            with (repo_root / cls.LOG_FILE).open(mode="a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        target_file.name,
                        llm_status,
                        test_status,
                        passed,
                        failed,
                        human_action,
                        round(api_time, 2),
                        round(session_time, 2),
                    ]
                )
        except Exception:
            pass


# ============================================================================
# INTERFACCIA GRAFICA E LOGICA PRINCIPALE
# ============================================================================


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class GitAgentApp(ctk.CTk):
    def __init__(self, pre_push_stdin: str):
        super().__init__()
        self.repo_root = GitManager.get_repo_root()
        self.git_dir = get_git_dir() or (self.repo_root / ".git")
        self.pre_push_stdin = pre_push_stdin
        self.exit_code = 0
        self._closing = False
        self._shutdown_lock = threading.Lock()
        self._shutdown_requested = False

        self.title("Git Pre-Push AI Reviewer")
        self.geometry("860x620")
        self.protocol("WM_DELETE_WINDOW", self.bypass_hook)

        self.fixed_code = ""
        self.target_file: Optional[Path] = None
        self.generated_test_code = ""
        self.test_output_log = ""
        self.tests_passed = "0"
        self.tests_failed = "0"
        self.test_status = "N/A"
        self.action_taken = "Nessuna azione"
        self._current_has_patch = False

        self._decision_event = threading.Event()
        self.force_push_requested = False
        self.patched_files_list: List[Path] = []
        self.backup_files: List[Path] = []

        self._lock = threading.Lock()
        self._total_files = 0
        self._analyzed_files = 0
        self._patched_count = 0

        ExperimentLogger.initialize(self.repo_root)
        self._build_ui()
        self.after(100, self._poll_shutdown)

        self.safe_log("Avvio analisi dei file inclusi nel push...")
        threading.Thread(target=self.run_agent_logic, daemon=True).start()

    def _build_ui(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(15, 5))

        ctk.CTkLabel(
            header,
            text="Code Review Automatica",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(side="left")

        self.status_label = ctk.CTkLabel(
            header,
            text="[in esecuzione]",
            font=ctk.CTkFont(size=13),
            text_color="#4CAF50",
        )
        self.status_label.pack(side="right")

        self.log_box = ctk.CTkTextbox(
            self,
            width=820,
            height=480,
            font=("Courier New", 12),
        )
        self.log_box.pack(padx=20, pady=10)

    def safe_log(self, text: str) -> None:
        def append() -> None:
            if self._closing:
                return
            try:
                self.log_box.insert("end", text + "\n")
                self.log_box.see("end")
            except tk.TclError:
                pass

        try:
            self.after(0, append)
        except tk.TclError:
            pass

    def _set_status(self, text: str, color: str) -> None:
        def update() -> None:
            try:
                self.status_label.configure(text=text, text_color=color)
            except tk.TclError:
                pass

        try:
            self.after(0, update)
        except tk.TclError:
            pass

    def _request_exit(self, exit_code: int, reason: str = "") -> None:
        if reason:
            print(f"[Sistema] {reason}")

        with self._shutdown_lock:
            if self._shutdown_requested:
                return
            self._shutdown_requested = True
            self.exit_code = exit_code

        self._decision_event.set()

        try:
            self.after(0, self._finish_shutdown)
        except tk.TclError:
            pass

    def _poll_shutdown(self) -> None:
        if self._closing:
            return

        with self._shutdown_lock:
            requested = self._shutdown_requested

        if requested:
            self._finish_shutdown()
            return

        try:
            self.after(100, self._poll_shutdown)
        except tk.TclError:
            pass

    def _finish_shutdown(self) -> None:
        if self._closing:
            return

        self._closing = True
        self._decision_event.set()

        try:
            self.quit()
        except tk.TclError:
            pass

        try:
            self.destroy()
        except tk.TclError:
            pass

    def _should_stop(self) -> bool:
        with self._shutdown_lock:
            return self._shutdown_requested or self._closing

    def bypass_hook(self) -> None:
        self._request_exit(
            0,
            "Finestra chiusa dall'utente. Il push originale proseguira.",
        )

    # ------------------------------------------------------------------
    # Diff viewer
    # ------------------------------------------------------------------

    def show_diff_viewer(self) -> None:
        try:
            self._show_diff_viewer_impl()
        except Exception as exc:
            self.safe_log(f"[!] Impossibile aprire la revisione visuale: {exc}")
            with self._lock:
                self.action_taken = "Errore popup, file saltato"
            self._decision_event.set()

    def _show_diff_viewer_impl(self) -> None:
        if not self.target_file:
            self._decision_event.set()
            return

        old_text = GitManager._read_text_with_fallback(self.target_file) or ""
        old_lines = old_text.splitlines(keepends=True)

        with self._lock:
            fixed_code = self.fixed_code
            t_passed = self.tests_passed
            t_failed = self.tests_failed
            t_status = self.test_status
            t_log = self.test_output_log
            test_code = self.generated_test_code

        has_patch = bool(fixed_code) and fixed_code != old_text
        self._current_has_patch = has_patch
        if not fixed_code:
            fixed_code = old_text

        new_lines = fixed_code.splitlines(keepends=True)
        diff_lines = list(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile="Originale",
                tofile="Patch AI",
                n=DIFF_CONTEXT_LINES,
            )
        )

        popup = ctk.CTkToplevel(self)
        popup.title(f"Revisione file: {self.target_file.name}")
        popup.geometry("940x820")
        popup.transient(self)
        popup.grab_set()
        popup.protocol("WM_DELETE_WINDOW", lambda: self._handle_decision(popup, "skip"))

        all_passed = t_failed == "0" and t_status == "Passato"
        badge_color = "#4CAF50" if all_passed else "#FF6B35"
        badge_text = (
            f"{t_passed} test passati - patch validata"
            if all_passed and has_patch
            else f"{t_passed} passati, {t_failed} falliti - revisione consigliata"
        )

        ctk.CTkLabel(
            popup,
            text=badge_text,
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=badge_color,
        ).pack(pady=(15, 5))

        tabs = ctk.CTkTabview(popup, width=900, height=560)
        tabs.pack(pady=5, padx=10)

        tab_diff = tabs.add("Diff patch")
        tab_test = tabs.add("Script test")
        tab_log_name = "Output test" if all_passed else "Output test - attenzione"
        tabs.add(tab_log_name)

        diff_frame = ctk.CTkFrame(tab_diff, fg_color="transparent")
        diff_frame.pack(fill="both", expand=True)

        txt_diff = tk.Text(
            diff_frame,
            font=("Courier New", 11),
            bg="#1e1e1e",
            fg="#d4d4d4",
            insertbackground="white",
            relief="flat",
            bd=0,
            wrap="none",
        )
        sb_y = tk.Scrollbar(diff_frame, orient="vertical", command=txt_diff.yview)
        sb_x = tk.Scrollbar(diff_frame, orient="horizontal", command=txt_diff.xview)
        txt_diff.configure(yscrollcommand=sb_y.set, xscrollcommand=sb_x.set)
        sb_y.pack(side="right", fill="y")
        sb_x.pack(side="bottom", fill="x")
        txt_diff.pack(fill="both", expand=True)

        txt_diff.tag_configure("added", background="#1e3a1e", foreground="#6fcf6f")
        txt_diff.tag_configure("removed", background="#3a1e1e", foreground="#f47676")
        txt_diff.tag_configure("header", foreground="#569cd6", font=("Courier New", 11, "bold"))
        txt_diff.tag_configure("hunk", foreground="#c586c0")
        txt_diff.tag_configure("neutral", foreground="#9a9a9a")
        txt_diff.tag_configure("notice", foreground="#FFA726", font=("Courier New", 11, "italic"))

        if diff_lines:
            for line in diff_lines:
                if line.startswith("+++") or line.startswith("---"):
                    txt_diff.insert("end", line, "header")
                elif line.startswith("@@"):
                    txt_diff.insert("end", line, "hunk")
                elif line.startswith("+"):
                    txt_diff.insert("end", line, "added")
                elif line.startswith("-"):
                    txt_diff.insert("end", line, "removed")
                else:
                    txt_diff.insert("end", line, "neutral")
        else:
            msg = (
                "L'AI non ha proposto modifiche al file target.\n\n"
                "Puoi leggere il test e l'output per capire se serve un controllo manuale."
            )
            txt_diff.insert("end", msg, "notice")

        txt_diff.configure(state="disabled")

        txt_test = ctk.CTkTextbox(tab_test, width=880, height=500, font=("Courier New", 11))
        txt_test.pack(fill="both", expand=True)
        txt_test.insert("0.0", test_code or "Script di test non generato.")

        txt_log = ctk.CTkTextbox(
            tabs.tab(tab_log_name),
            width=880,
            height=500,
            font=("Courier New", 11),
        )
        txt_log.pack(fill="both", expand=True)
        txt_log.insert("0.0", t_log or "Nessun output disponibile.")

        if not all_passed:
            tabs.set(tab_log_name)

        btn_frame = ctk.CTkFrame(popup, fg_color="transparent")
        btn_frame.pack(pady=15)

        apply_label = "Applica patch" if has_patch else "Nessuna patch da applicare"
        apply_color = "#2e7d32" if all_passed and has_patch else "#8a6d1d"

        ctk.CTkButton(
            btn_frame,
            text=apply_label,
            fg_color=apply_color,
            hover_color="#1b5e20" if all_passed and has_patch else "#6f5614",
            width=220,
            command=lambda: self._handle_decision(popup, "queue"),
        ).pack(side="left", padx=10)

        ctk.CTkButton(
            btn_frame,
            text="Scarta",
            fg_color="#795500",
            hover_color="#5c3d00",
            width=120,
            command=lambda: self._handle_decision(popup, "skip"),
        ).pack(side="left", padx=10)

        ctk.CTkButton(
            btn_frame,
            text="Forza push",
            fg_color="#b71c1c",
            hover_color="#7f0000",
            width=140,
            command=lambda: self._handle_decision(popup, "force"),
        ).pack(side="right", padx=10)

    def _handle_decision(self, popup: ctk.CTkToplevel, decision: str) -> None:
        if decision == "queue":
            self._apply_current_patch()
        elif decision == "skip":
            with self._lock:
                self.action_taken = "Scartato"
            if self.target_file:
                self.safe_log(f"[-] Modifiche scartate per: {self.target_file.name}")
        elif decision == "force":
            self.force_push_requested = True
            with self._lock:
                self.action_taken = "Forza push"
            self.safe_log("[!] Forza push richiesto. Le analisi successive saranno saltate.")

        try:
            popup.destroy()
        except tk.TclError:
            pass

        self._decision_event.set()

    def _apply_current_patch(self) -> None:
        if not self.target_file:
            return

        if not self._current_has_patch:
            with self._lock:
                self.action_taken = "Nessuna patch applicabile"
            self.safe_log("[i] Nessuna patch applicabile: il file rimane invariato.")
            return

        abs_path = self.target_file.resolve()
        try:
            backup_dir = self.git_dir / "ai_agent_backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"{int(time.time())}_{abs_path.name}.bak"
            shutil.copy2(abs_path, backup_path)

            abs_path.write_text(self.fixed_code, encoding="utf-8")
            self.patched_files_list.append(abs_path)
            self.backup_files.append(backup_path)

            with self._lock:
                self.action_taken = "Patch applicata"
                self._patched_count += 1

            self.safe_log(f"[+] Patch salvata su disco per: {abs_path.name}")
        except Exception as exc:
            with self._lock:
                self.action_taken = "Errore scrittura patch"
            self.safe_log(f"[!] Impossibile scrivere la patch: {exc}")

    # ------------------------------------------------------------------
    # Logica principale
    # ------------------------------------------------------------------

    def run_agent_logic(self) -> None:
        try:
            api_key = resolve_api_key(self.repo_root)
            if not api_key:
                self.safe_log("[!] API key non trovata.")
                self.safe_log("    Usa GOOGLE_API_KEY oppure un file .api_key nella root del repository.")
                self.safe_log("    Il push viene autorizzato senza analisi.")
                time.sleep(2)
                return self._request_exit(0)

            target_files = GitManager.get_files_changed_by_push(self.pre_push_stdin)

            if len(target_files) > MAX_FILES_TO_ANALYZE:
                self.safe_log(
                    f"[!] Rilevati {len(target_files)} file. "
                    f"Analizzo solo i primi {MAX_FILES_TO_ANALYZE}."
                )
                target_files = target_files[:MAX_FILES_TO_ANALYZE]

            if not target_files:
                self.safe_log("Nessun file sorgente rilevante nel push. Push autorizzato.")
                time.sleep(1)
                return self._request_exit(0)

            self._total_files = len(target_files)
            self._set_status(f"[analisi: {self._total_files} file]", "#FFA726")
            ai_client = GenAIClient(api_key)

            for idx, file_path in enumerate(target_files, start=1):
                if self.force_push_requested or self._should_stop():
                    break

                self.target_file = file_path
                api_time = 0.0
                file_start = time.time()

                self.safe_log("\n" + "=" * 60)
                self.safe_log(f"[{idx}/{self._total_files}] Analisi file: {file_path.name}")

                size_check = GitManager.is_file_too_large(file_path)
                if size_check.too_large:
                    self.safe_log(f"  [!] File saltato: {size_check.reason}.")
                    ExperimentLogger.log_run(
                        self.repo_root,
                        file_path,
                        "Saltato per dimensione",
                        "N/A",
                        "0",
                        "0",
                        "Saltato automaticamente",
                        0.0,
                        round(time.time() - file_start, 2),
                    )
                    continue

                self._reset_file_state()
                error_feedback = ""
                bug_or_risk_found = False
                llm_status = "Non conclusivo"

                for attempt in range(MAX_RETRY_ATTEMPTS):
                    if self._should_stop():
                        break

                    self.safe_log(
                        f"  [Iterazione {attempt + 1}/{MAX_RETRY_ATTEMPTS}] "
                        "Richiesta analisi AI..."
                    )

                    t0 = time.time()
                    response_text = self._fetch_ai_response(ai_client, file_path, error_feedback)
                    api_time += time.time() - t0

                    if not response_text:
                        self.safe_log("  [!] Risposta AI vuota.")
                        llm_status = "Risposta vuota"
                        break

                    result, err_log = self._handle_ai_response(response_text)

                    if result == "bug":
                        bug_or_risk_found = True
                        llm_status = "Bug o rischio segnalato"
                        self.safe_log(
                            f"  [test] Passed: {self.tests_passed} | Failed: {self.tests_failed}"
                        )
                        break

                    if result == "clean":
                        llm_status = "Nessun bug"
                        self.safe_log("  [ok] Nessuna criticita logica segnalata.")
                        break

                    if result == "failed":
                        llm_status = "Test non eseguibile"
                        error_feedback = err_log or "Esecuzione fallita senza output utile."
                        self.safe_log("  [!] Test non eseguibile. Invio feedback all'AI.")
                        continue

                    llm_status = "Formato non valido"
                    error_feedback = err_log or (
                        "La risposta AI non rispetta il formato richiesto: "
                        "mancano metadati o blocchi di test validi."
                    )
                    self.safe_log("  [!] Risposta AI incompleta. Invio feedback all'AI.")
                    continue

                self._analyzed_files += 1

                if self._should_stop():
                    break

                needs_review = bug_or_risk_found or self.test_status == "Fallito"
                if needs_review:
                    self.safe_log("  [review] Richiesta decisione manuale.")
                    self._decision_event.clear()
                    self.after(0, self.show_diff_viewer)
                    self._decision_event.wait()
                    if self._should_stop():
                        break
                else:
                    self.safe_log("  [ok] File concluso senza interventi.")

                session_time = round(time.time() - file_start, 2)
                with self._lock:
                    ExperimentLogger.log_run(
                        self.repo_root,
                        file_path,
                        llm_status,
                        self.test_status,
                        self.tests_passed,
                        self.tests_failed,
                        self.action_taken,
                        round(api_time, 2),
                        session_time,
                    )

            self.safe_log("\n" + "=" * 60)
            self._finalize()

        except Exception as exc:
            self.safe_log(f"\n[Eccezione interna] {exc}")
            time.sleep(2)
            self._request_exit(0)

    def _reset_file_state(self) -> None:
        with self._lock:
            self.tests_passed = "0"
            self.tests_failed = "0"
            self.test_status = "N/A"
            self.test_output_log = ""
            self.action_taken = "Nessuna azione"
            self.fixed_code = ""
            self.generated_test_code = ""
            self._current_has_patch = False

    def _finalize(self) -> None:
        if self.force_push_requested:
            self.safe_log("Forza push attivo. Il push originale viene autorizzato.")
            self._set_status("[push forzato]", "#FF6B35")
            time.sleep(2)
            return self._request_exit(0)

        changed_files = [
            path for path in self.patched_files_list
            if path.exists() and GitManager.has_worktree_changes(path, self.repo_root)
        ]

        if changed_files:
            self.safe_log(
                f"Riepilogo: {self._total_files} file considerati, "
                f"{self._analyzed_files} analizzati, {self._patched_count} patch applicate."
            )
            self.safe_log(f"Creo un commit separato per {len(changed_files)} file patchati.")

            rel_paths = [to_git_path(path, self.repo_root) for path in changed_files]
            commit_msg = f"Auto-patch AI: correzioni su {len(changed_files)} file"

            commit_res = run_process(
                ["git", "commit", "--only", "-m", commit_msg, "--"] + rel_paths,
                cwd=self.repo_root,
            )

            if commit_res.returncode != 0:
                self.safe_log("[!] Commit automatico non riuscito.")
                self.safe_log(commit_res.stderr.strip() or commit_res.stdout.strip())
                self.safe_log("    Il push viene bloccato per permettere un controllo manuale.")
                time.sleep(4)
                return self._request_exit(1)

            set_bypass_flag()
            self._cleanup_backups()
            self._set_status("[validazione terminata]", "#4CAF50")
            self.safe_log("Commit automatico creato correttamente.")
            self.safe_log("Riesegui git push: il bypass temporaneo evita un doppio controllo immediato.")
            time.sleep(4)
            return self._request_exit(1)

        self._cleanup_backups()
        self.safe_log(
            f"Riepilogo: {self._total_files} file considerati, "
            f"{self._analyzed_files} analizzati, nessuna patch salvata."
        )
        self._set_status("[validazione ok]", "#4CAF50")
        self.safe_log("Validazione conclusa. Push autorizzato.")
        time.sleep(2)
        return self._request_exit(0)

    # ------------------------------------------------------------------
    # Parsing risposta AI e test
    # ------------------------------------------------------------------

    def _fetch_ai_response(
        self,
        ai_client: GenAIClient,
        target_file: Path,
        error_feedback: str = "",
    ) -> Optional[str]:
        source_code = GitManager.read_files([target_file], self.repo_root)
        context_code = GitManager.read_files(
            GitManager.get_context_files(target_file),
            self.repo_root,
        )
        try:
            return ai_client.analyze_code(target_file, source_code, context_code, error_feedback)
        except Exception as exc:
            self.safe_log(f"  [!] Errore API: {exc}")
            return None

    def _handle_ai_response(self, response_text: str) -> Tuple[str, str]:
        no_bug = bool(re.search(r"\bnessun\s+bug\b", response_text, re.IGNORECASE))

        fixed_code = self._extract_block_after_heading(response_text, r"codice\s+corretto")
        if fixed_code and not no_bug:
            with self._lock:
                self.fixed_code = fixed_code.strip()

        cmd = self._extract_metadata(response_text, "RUN_COMMAND")
        t_file = self._extract_metadata(response_text, "TEST_FILE_NAME")

        if not cmd or not t_file:
            return (
                "failed",
                "Risposta AI incompleta: mancano TEST_FILE_NAME e/o RUN_COMMAND.",
            )

        run_result, err_log = self._run_tests(response_text, cmd, t_file)

        if no_bug:
            if run_result == "passed":
                return "clean", ""
            return "failed", err_log

        if run_result in ("passed", "structured_failed"):
            return "bug", ""

        return "failed", err_log

    @staticmethod
    def _extract_metadata(response_text: str, key: str) -> Optional[str]:
        match = re.search(rf"^{re.escape(key)}:\s*(.+)$", response_text, re.IGNORECASE | re.MULTILINE)
        return match.group(1).strip() if match else None

    @staticmethod
    def _extract_block_after_heading(response_text: str, heading_pattern: str) -> Optional[str]:
        heading = re.search(rf"##\s*{heading_pattern}.*?$", response_text, re.IGNORECASE | re.MULTILINE)
        if not heading:
            return None

        block = re.search(r"```[^\n]*\n(.*?)\n```", response_text[heading.end():], re.DOTALL)
        return block.group(1) if block else None

    def _extract_test_block(self, response_text: str) -> Optional[str]:
        unit_heading = re.search(r"##\s*unit\s+test.*?$", response_text, re.IGNORECASE | re.MULTILINE)
        if unit_heading:
            block = re.search(r"```[^\n]*\n(.*?)\n```", response_text[unit_heading.end():], re.DOTALL)
            if block:
                return block.group(1).strip()

        blocks = [
            (m.start(), m.group(1).strip())
            for m in re.finditer(r"```[^\n]*\n(.*?)\n```", response_text, re.DOTALL)
        ]
        if not blocks:
            return None

        meta_match = re.search(r"^TEST_FILE_NAME:", response_text, re.IGNORECASE | re.MULTILINE)
        if meta_match:
            before_meta = [item for item in blocks if item[0] < meta_match.start()]
            if before_meta:
                return before_meta[-1][1]

        return blocks[-1][1]

    def _run_tests(self, response_text: str, cmd: str, t_file_name: str) -> Tuple[str, str]:
        test_code = self._extract_test_block(response_text)
        if not test_code:
            return "failed", "Blocco UNIT TEST non trovato nella risposta AI."

        test_code = re.sub(
            r"(?im)^(DEPENDENCIES|TEST_FILE_NAME|RUN_COMMAND):.*$",
            "",
            test_code,
        ).strip()

        if "sys.exit" in test_code and "import sys" not in test_code:
            test_code = "import sys\n" + test_code

        safe_name = self._safe_test_file_name(t_file_name)
        test_path = self._make_temp_test_path(safe_name)

        try:
            test_path.write_text(test_code, encoding="utf-8")
        except Exception as exc:
            return "failed", f"Impossibile scrivere il test: {exc}"

        with self._lock:
            self.generated_test_code = test_code

        exec_args, setup_error, cleanup_paths = self._build_test_command(cmd, test_path, safe_name)
        if setup_error:
            self._cleanup_test_file(test_path)
            with self._lock:
                self.test_status = "Fallito"
                self.test_output_log = setup_error
            return "failed", setup_error

        try:
            res = run_process(
                exec_args,
                cwd=self.repo_root,
                timeout=TEST_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            msg = f"Timeout: il test ha superato {TEST_TIMEOUT_SECONDS} secondi."
            self._cleanup_test_file(test_path)
            with self._lock:
                self.test_status = "Fallito"
                self.test_output_log = msg
            return "failed", msg
        except Exception as exc:
            msg = f"Esecuzione test non riuscita: {exc}"
            self._cleanup_test_file(test_path)
            with self._lock:
                self.test_status = "Fallito"
                self.test_output_log = msg
            return "failed", msg
        finally:
            for path in cleanup_paths:
                self._cleanup_test_file(path)
            self._cleanup_test_file(test_path)

        out_text = (res.stdout + "\n" + res.stderr).strip()
        m_pass = re.search(r"Passed:\s*(\d+)", out_text, re.IGNORECASE)
        m_fail = re.search(r"Failed:\s*(\d+)", out_text, re.IGNORECASE)
        has_metrics = bool(m_pass and m_fail)

        with self._lock:
            self.test_output_log = out_text
            self.tests_passed = m_pass.group(1) if m_pass else "0"
            self.tests_failed = m_fail.group(1) if m_fail else "0"

        if not has_metrics:
            msg = out_text or "Il test non ha stampato Passed/Failed nel formato richiesto."
            with self._lock:
                self.test_status = "Fallito"
            return "failed", msg

        if res.returncode == 0 and self.tests_failed == "0":
            with self._lock:
                self.test_status = "Passato"
            return "passed", ""

        with self._lock:
            self.test_status = "Fallito"
        return "structured_failed", ""

    def _safe_test_file_name(self, name: str) -> str:
        base = Path(name.strip().strip('"').strip("'")).name
        base = re.sub(r"[^A-Za-z0-9_.-]", "_", base)

        if not base or "." not in base:
            ext = ".py"
            if self.target_file:
                ext = {
                    ".js": ".js",
                    ".ts": ".js",
                    ".dart": ".dart",
                    ".swift": ".swift",
                }.get(self.target_file.suffix.lower(), ".py")
            base = f"test_ai_fix{ext}"

        return base

    def _make_temp_test_path(self, safe_name: str) -> Path:
        """
        Crea il test temporaneo nella root del repo.

        In questo modo Python/Node risolvono gli import come farebbe un test
        lanciato manualmente dal progetto, senza modificare PYTHONPATH.
        Il file viene rimosso subito dopo l'esecuzione.
        """
        return self.repo_root / f".ai_agent_test_{uuid.uuid4().hex}_{safe_name}"

    def _build_test_command(
        self,
        cmd: str,
        test_path: Path,
        safe_name: str,
    ) -> Tuple[List[str], str, List[Path]]:
        if not self.target_file:
            return [], "File target non impostato.", []

        target_ext = self.target_file.suffix.lower()
        tokens = self._split_command(cmd)
        if not tokens:
            return [], "RUN_COMMAND vuoto.", []

        executable = Path(tokens[0]).name.lower()

        if target_ext == ".py" or executable in ("python", "python3", "py"):
            return [sys.executable, str(test_path)], "", []

        if target_ext in (".js", ".ts") or executable == "node":
            return ["node", str(test_path)], "", []

        if target_ext == ".dart" or executable == "dart":
            return ["dart", str(test_path)], "", []

        if target_ext == ".swift":
            return self._compile_swift(test_path)

        allowed = {"java", "javac", "dotnet"}
        if executable not in allowed:
            return [], f"Comando non consentito per sicurezza: {cmd}", []

        normalized = [str(test_path) if Path(tok).name == safe_name else tok for tok in tokens]
        if str(test_path) not in normalized:
            return [], "Il comando di test non fa riferimento al file generato.", []

        return normalized, "", []

    @staticmethod
    def _split_command(cmd: str) -> List[str]:
        try:
            return shlex.split(cmd, posix=(os.name != "nt"))
        except ValueError:
            return []

    def _compile_swift(self, test_path: Path) -> Tuple[List[str], str, List[Path]]:
        if not self.target_file:
            return [], "File target non impostato.", []

        target_dir = self.target_file.parent
        swift_files = sorted(str(path) for path in target_dir.glob("*.swift"))
        test_abs = str(test_path.resolve())
        if test_abs not in swift_files:
            swift_files.append(test_abs)

        exe = self.git_dir / "ai_agent_tests" / ("TestExe.exe" if os.name == "nt" else "TestExe")
        comp = run_process(["swiftc"] + swift_files + ["-o", str(exe)], cwd=self.repo_root)
        if comp.returncode != 0:
            return [], comp.stderr.strip() or comp.stdout.strip(), []

        return [str(exe)], "", [exe]

    def _cleanup_test_file(self, t_file: Optional[Path]) -> None:
        if not t_file:
            return
        try:
            if t_file.exists():
                t_file.unlink()
        except Exception:
            pass

    def _cleanup_backups(self) -> None:
        for path in self.backup_files:
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                pass


# ============================================================================
# INSTALLAZIONE HOOK
# ============================================================================


def install_hook() -> None:
    root = tk.Tk()
    root.withdraw()
    target_dir = filedialog.askdirectory(
        title="Seleziona la root del repository Git da controllare"
    )

    if not target_dir:
        sys.exit(1)

    hooks_dir = Path(target_dir) / ".git" / "hooks"
    if not hooks_dir.exists():
        print("Errore: directory .git/hooks non trovata.")
        sys.exit(1)

    pre_push_path = hooks_dir / "pre-push"
    script_path = Path(__file__).resolve().as_posix()
    python_exe = Path(sys.executable).resolve().as_posix()

    bash_hook = f'#!/bin/sh\n"{python_exe}" "{script_path}"\nexit $?\n'

    try:
        pre_push_path.write_text(bash_hook, encoding="utf-8")
        current_mode = pre_push_path.stat().st_mode
        pre_push_path.chmod(current_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        print("Hook pre-push installato correttamente.")
        print(f"Percorso: {pre_push_path}")
        print("")
        print("Configurazione API key:")
        print("  export GOOGLE_API_KEY=la_tua_chiave")
        print("  oppure crea un file .api_key nella root del repository")
        print("  e aggiungi .api_key al .gitignore.")
    except Exception as exc:
        print(f"Installazione hook non riuscita: {exc}")
        sys.exit(1)

    sys.exit(0)


# ============================================================================
# ENTRY POINT
# ============================================================================


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--install":
        install_hook()
        return 0

    if check_and_clear_bypass():
        print("[Agente AI] Bypass temporaneo riconosciuto. Push autorizzato.")
        return 0

    pre_push_stdin = ""
    try:
        if not sys.stdin.closed:
            pre_push_stdin = sys.stdin.read()
    except Exception:
        pre_push_stdin = ""

    app = GitAgentApp(pre_push_stdin)
    app.mainloop()
    return app.exit_code


if __name__ == "__main__":
    sys.exit(main())
