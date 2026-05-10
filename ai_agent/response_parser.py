import re
from typing import Optional, Tuple


class ResponseParserMixin:
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
