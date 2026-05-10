import ast
import re
from typing import Optional, Tuple


class ResponseParserMixin:
    def _handle_ai_response(self, response_text: str) -> Tuple[str, str]:
        no_bug = bool(re.search(r"\bnessun\s+bug\b", response_text, re.IGNORECASE))

        fixed_code = self._extract_block_after_heading(response_text, r"codice\s+corretto")
        candidate_code = fixed_code.strip() if fixed_code and not no_bug else ""
        if candidate_code:
            state_error = self._find_removed_instance_state(candidate_code)
            if state_error:
                return "failed", state_error
            with self._lock:
                self.fixed_code = candidate_code

        cmd = self._extract_metadata(response_text, "RUN_COMMAND")
        t_file = self._extract_metadata(response_text, "TEST_FILE_NAME")

        if not cmd or not t_file:
            return (
                "failed",
                "Risposta AI incompleta: mancano TEST_FILE_NAME e/o RUN_COMMAND.",
            )

        if candidate_code:
            run_result, err_log = self._run_tests_against_candidate(
                response_text,
                cmd,
                t_file,
                candidate_code,
            )
        else:
            run_result, err_log = self._run_tests(response_text, cmd, t_file)

        if no_bug:
            if run_result == "passed":
                return "clean", ""
            return "failed", err_log

        if candidate_code:
            if run_result == "passed":
                with self._lock:
                    self.fixed_code = candidate_code
                return "bug", ""
            with self._lock:
                self.fixed_code = ""
            details = err_log or self.test_output_log or "La patch proposta non ha superato la validazione locale."
            return "failed", details

        if run_result in ("passed", "structured_failed"):
            return "bug", ""

        return "failed", err_log

    def _find_removed_instance_state(self, candidate_code: str) -> str:
        if not self.target_file or self.target_file.suffix.lower() != ".py":
            return ""

        try:
            original_tree = ast.parse(self.target_file.read_text(encoding="utf-8-sig"))
            candidate_tree = ast.parse(candidate_code.lstrip("\ufeff"))
        except (OSError, SyntaxError):
            return ""

        original_attrs = self._collect_self_attrs(original_tree)
        candidate_attrs = self._collect_self_attrs(candidate_tree)
        removed = original_attrs - candidate_attrs
        if not removed:
            return ""

        names = ", ".join(sorted(removed))
        return (
            "Patch non valida: rimuove attributi di stato esistenti "
            f"({names}). Mantieni lo stato originale e proponi una correzione minima."
        )

    @staticmethod
    def _collect_self_attrs(tree: ast.AST) -> set[str]:
        attrs: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
            ):
                attrs.add(node.attr)
        return attrs
    def _run_tests_against_candidate(
        self,
        response_text: str,
        cmd: str,
        t_file: str,
        candidate_code: str,
    ) -> Tuple[str, str]:
        if not self.target_file:
            return self._run_tests(response_text, cmd, t_file)

        try:
            original_bytes = self.target_file.read_bytes()
        except OSError as exc:
            return "failed", f"Impossibile leggere il file originale prima della validazione: {exc}"

        try:
            self.target_file.write_text(candidate_code, encoding="utf-8")
            return self._run_tests(response_text, cmd, t_file)
        except OSError as exc:
            return "failed", f"Impossibile validare temporaneamente la patch: {exc}"
        finally:
            try:
                self.target_file.write_bytes(original_bytes)
            except OSError:
                pass

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

