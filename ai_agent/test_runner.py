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

        has_case_details = bool(re.search(r"\[(PASS|FAIL)\]", out_text, re.IGNORECASE))
        if not has_case_details:
            msg = (
                (out_text + "\n\n" if out_text else "")
                + "Il test deve stampare una riga [PASS] o [FAIL] per ogni caso eseguito."
            )
            with self._lock:
                self.test_status = "Fallito"
            return "failed", msg

        if res.returncode == 0 and self.tests_failed == "0":
            with self._lock:
                self.test_status = "Passato"
            return "passed", ""

        if self.tests_failed == "0":
            msg = out_text or "Il test ha stampato metriche positive ma e terminato con errore."
            with self._lock:
                self.test_status = "Fallito"
            return "failed", msg

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
