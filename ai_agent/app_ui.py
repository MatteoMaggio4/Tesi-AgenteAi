import threading

import tkinter as tk

import customtkinter as ctk


class AppUiMixin:
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
