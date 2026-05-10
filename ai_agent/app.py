import threading
from pathlib import Path
from typing import List, Optional

import customtkinter as ctk

from .app_ui import AppUiMixin
from .experiment_logger import ExperimentLogger
from .git_manager import GitManager
from .git_utils import get_git_dir
from .response_parser import ResponseParserMixin
from .review_dialog import ReviewDialogMixin
from .test_runner import TestRunnerMixin
from .workflow import AgentWorkflowMixin


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class GitAgentApp(
    AppUiMixin,
    ReviewDialogMixin,
    AgentWorkflowMixin,
    ResponseParserMixin,
    TestRunnerMixin,
    ctk.CTk,
):
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
