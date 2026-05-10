import csv
from datetime import datetime
from pathlib import Path


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
