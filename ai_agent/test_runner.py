import ast
import os
import re
import shlex
import subprocess
import sys
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

from .config import TEST_TIMEOUT_SECONDS
from .process_utils import run_process


class TestRunnerMixin:
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
            return self._record_test_failure("Blocco UNIT TEST non trovato nella risposta AI.")

        test_code = re.sub(
            r"(?im)^(DEPENDENCIES|TEST_FILE_NAME|RUN_COMMAND):.*$",
            "",
            test_code,
        ).strip()

        if "sys.exit" in test_code and "import sys" not in test_code:
            test_code = "import sys\n" + test_code

        syntax_error = self._validate_python_test_syntax(test_code)
        if syntax_error:
            return self._record_test_failure(syntax_error)

        stdout_error = self._find_stdout_capture(test_code)
        if stdout_error:
            return self._record_test_failure(stdout_error)

        shadow_error = self._find_shadowed_target_symbols(test_code)
        if shadow_error:
            return self._record_test_failure(shadow_error)

        test_code = self._prepare_python_test_code(test_code)

        safe_name = self._safe_test_file_name(t_file_name)
        test_path = self._make_temp_test_path(safe_name)

        try:
            test_path.write_text(test_code, encoding="utf-8")
        except Exception as exc:
            return self._record_test_failure(f"Impossibile scrivere il test: {exc}")

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

        display_text = out_text
        if has_metrics:
            display_text = self._format_test_output(
                out_text,
                m_pass.group(1),
                m_fail.group(1),
            )

        with self._lock:
            self.test_output_log = display_text
            self.tests_passed = m_pass.group(1) if m_pass else "0"
            self.tests_failed = m_fail.group(1) if m_fail else "0"

        if not has_metrics:
            msg = out_text or "Il test non ha stampato Passed/Failed nel formato richiesto."
            with self._lock:
                self.test_status = "Fallito"
                self.test_output_log = msg
            return "failed", msg

        if res.returncode == 0 and self.tests_failed == "0":
            with self._lock:
                self.test_status = "Passato"
            return "passed", ""

        if self.tests_failed == "0":
            msg = out_text or "Il test ha stampato metriche positive ma e terminato con errore."
            with self._lock:
                self.test_status = "Fallito"
                self.test_output_log = msg
            return "failed", msg

        with self._lock:
            self.test_status = "Fallito"
        return "structured_failed", ""

    @staticmethod
    def _validate_python_test_syntax(test_code: str) -> str:
        try:
            ast.parse(test_code)
            return ""
        except SyntaxError as exc:
            location = f"linea {exc.lineno}" if exc.lineno else "posizione sconosciuta"
            return (
                "Test non valido: il codice Python generato non e sintatticamente valido "
                f"({location}: {exc.msg}). Correggi il test usando funzioni normali; "
                "non usare assert dentro lambda."
            )
    @staticmethod
    def _find_stdout_capture(test_code: str) -> str:
        blocked_patterns = (
            "sys.stdout =",
            "sys.stdout=",
            "sys.stdout.getvalue",
            "StringIO(",
            "from io import StringIO",
        )
        if any(pattern in test_code for pattern in blocked_patterns):
            return (
                "Test non valido: non catturare o sostituire sys.stdout. "
                "Stampa direttamente su console le righe [PASS], [FAIL], Passed e Failed."
            )
        return ""
    def _find_shadowed_target_symbols(self, test_code: str) -> str:
        if not self.target_file or self.target_file.suffix.lower() != ".py":
            return ""

        try:
            target_tree = ast.parse(self.target_file.read_text(encoding="utf-8-sig"))
            test_tree = ast.parse(test_code)
        except (OSError, SyntaxError):
            return ""

        target_symbols = {
            node.name
            for node in target_tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and not node.name.startswith("_")
        }
        if not target_symbols:
            return ""

        test_symbols = {
            node.name
            for node in test_tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in target_symbols
        }
        if not test_symbols:
            return ""

        names = ", ".join(sorted(test_symbols))
        return (
            "Test non valido: ridefinisce simboli del file target "
            f"({names}) invece di usare il codice reale da validare."
        )

    def _prepare_python_test_code(self, test_code: str) -> str:
        if not self.target_file or self.target_file.suffix.lower() != ".py":
            return test_code

        target_path = self.target_file.resolve()
        preload = (
            "import importlib.util as _ai_importlib_util\n"
            "from pathlib import Path as _AiPath\n\n"
            f"_ai_target_path = _AiPath(r\"{target_path}\")\n"
            "_ai_spec = _ai_importlib_util.spec_from_file_location(\"_ai_agent_target_module\", _ai_target_path)\n"
            "_ai_module = _ai_importlib_util.module_from_spec(_ai_spec)\n"
            "_ai_spec.loader.exec_module(_ai_module)\n"
            "for _ai_name in dir(_ai_module):\n"
            "    if not _ai_name.startswith(\"_\"):\n"
            "        globals().setdefault(_ai_name, getattr(_ai_module, _ai_name))"
        )
        return preload + "\n\n" + test_code
    @staticmethod
    def _format_test_output(out_text: str, passed: str, failed: str) -> str:
        has_detail_rows = bool(re.search(r"^\s*\[(PASS|FAIL)\]", out_text, re.IGNORECASE | re.MULTILINE))
        if has_detail_rows:
            return out_text

        summary = []
        try:
            passed_count = int(passed)
            failed_count = int(failed)
        except ValueError:
            return out_text

        if passed_count > 0:
            summary.append(f"[PASS] {passed_count} controlli superati")
        if failed_count > 0:
            summary.append(f"[FAIL] {failed_count} controlli falliti: dettagli nei messaggi precedenti")

        if not summary:
            return out_text

        base = out_text.rstrip()
        return base + "\n\nDettaglio sintetico:\n" + "\n".join(summary)

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

