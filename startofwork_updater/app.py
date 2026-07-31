"""독립형 업데이터 GUI: 다운로드 → (실패 시 수동) → 메인 종료 → 설치 → 재시작."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Optional

from startofwork_updater.core import (
    ReleaseInfo,
    UpdateError,
    download_and_prepare_update,
    get_installed_exe_path,
    get_update_download_dir,
    prepare_setup_for_install,
    verify_download,
)
from startofwork_updater.bootstrap import relaunch_from_temp
from startofwork_updater.install import (
    restart_main_app,
    run_setup_installer,
    terminate_main_app,
    unblock_file,
    write_update_log,
)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="StartOfWork Updater")
    parser.add_argument("--version", required=True)
    parser.add_argument("--download-url", required=True)
    parser.add_argument("--asset-name", required=True)
    parser.add_argument("--sha256", default="")
    parser.add_argument("--html-url", default="")
    parser.add_argument("--pid", type=int, default=0)
    parser.add_argument("--install-exe", default="")
    parser.add_argument("--bootstrapped", action="store_true")
    return parser.parse_args(argv)


def release_from_args(args: argparse.Namespace) -> ReleaseInfo:
    sha = (args.sha256 or "").strip() or None
    return ReleaseInfo(
        version=args.version,
        tag_name=f"v{args.version}",
        html_url=args.html_url or "",
        asset_name=args.asset_name,
        download_url=args.download_url,
        body="",
        expected_sha256=sha,
    )


def resolve_install_exe(args: argparse.Namespace) -> Path:
    if args.install_exe:
        return Path(args.install_exe)
    return get_installed_exe_path()


class UpdaterApp(tk.Tk):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__()
        self.args = args
        self.release = release_from_args(args)
        self.install_exe = resolve_install_exe(args)
        self.log_path = get_update_download_dir() / "update.log"
        self.setup_path: Optional[Path] = None
        self._busy = False

        self.title(f"StartOfWork 업데이트 — v{self.release.version}")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        frame = ttk.Frame(self, padding=(16, 14, 16, 14))
        frame.grid(row=0, column=0, sticky="nsew")

        ttk.Label(
            frame,
            text=f"새 버전 {self.release.version} 설치",
            font=("Segoe UI", 11, "bold"),
        ).grid(row=0, column=0, columnspan=3, sticky="w")

        self.status_var = tk.StringVar(value="업데이트를 준비하는 중…")
        ttk.Label(frame, textvariable=self.status_var, wraplength=420).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(8, 6)
        )

        self.progress = ttk.Progressbar(
            frame, mode="determinate", maximum=100, length=420
        )
        self.progress.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 4))

        self.progress_label = tk.StringVar(value="대기 중")
        ttk.Label(frame, textvariable=self.progress_label).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )

        log_frame = ttk.LabelFrame(frame, text="로그", padding=(6, 4, 6, 6))
        log_frame.grid(row=4, column=0, columnspan=3, sticky="nsew", pady=(0, 10))
        self.log_text = tk.Text(log_frame, height=10, width=58, wrap="word")
        scroll = ttk.Scrollbar(
            log_frame, orient="vertical", command=self.log_text.yview
        )
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        log_frame.columnconfigure(0, weight=1)

        btn = ttk.Frame(frame)
        btn.grid(row=5, column=0, columnspan=3, sticky="e")
        self.retry_btn = ttk.Button(
            btn, text="다시 다운로드", command=self._start_download, state="disabled"
        )
        self.github_btn = ttk.Button(
            btn, text="GitHub 릴리스 열기", command=self._open_github, state="disabled"
        )
        self.browse_btn = ttk.Button(
            btn, text="다운로드 파일 선택", command=self._browse_setup, state="disabled"
        )
        self.close_btn = ttk.Button(btn, text="닫기", command=self._on_close)
        self.retry_btn.pack(side="left", padx=(0, 6))
        self.github_btn.pack(side="left", padx=(0, 6))
        self.browse_btn.pack(side="left", padx=(0, 6))
        self.close_btn.pack(side="left")

        self.after(200, self._start_download)

    def _append_log(self, message: str) -> None:
        write_update_log(self.log_path, message)
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")

    def _set_manual_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.retry_btn.configure(state=state)
        self.github_btn.configure(state=state)
        self.browse_btn.configure(state=state)

    def _format_bytes(self, n: int) -> str:
        if n < 1024:
            return f"{n} B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f} KB"
        return f"{n / (1024 * 1024):.1f} MB"

    def _on_progress(self, downloaded: int, total: int) -> None:
        def _ui() -> None:
            if total > 0:
                pct = min(100, int(downloaded * 100 / total))
                self.progress.configure(mode="determinate", maximum=100, value=pct)
                self.progress_label.set(
                    f"다운로드 중… {pct}% "
                    f"({self._format_bytes(downloaded)} / {self._format_bytes(total)})"
                )
            else:
                if str(self.progress.cget("mode")) != "indeterminate":
                    self.progress.configure(mode="indeterminate")
                    self.progress.start(12)
                self.progress_label.set(
                    f"다운로드 중… {self._format_bytes(downloaded)}"
                )

        self.after(0, _ui)

    def _start_download(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._set_manual_enabled(False)
        self.status_var.set("설치 파일을 다운로드하는 중입니다.")
        self.progress.configure(mode="determinate", value=0)
        self.progress_label.set("다운로드 시작…")
        self._append_log(
            f"download start version={self.release.version} "
            f"url={self.release.download_url}"
        )

        def worker() -> None:
            try:
                path = download_and_prepare_update(
                    self.release, progress_callback=self._on_progress
                )
                self.after(0, lambda: self._on_download_ok(path))
            except UpdateError as exc:
                self.after(0, lambda: self._on_download_fail(str(exc)))
            except Exception as exc:
                logging.exception("업데이트 다운로드 실패")
                self.after(
                    0,
                    lambda: self._on_download_fail(f"다운로드 오류: {exc}"),
                )

        threading.Thread(target=worker, name="updater-download", daemon=True).start()

    def _on_download_ok(self, path: Path) -> None:
        self._busy = False
        try:
            self.progress.stop()
        except tk.TclError:
            pass
        self.progress.configure(mode="determinate", value=100)
        self.progress_label.set(
            f"다운로드 완료 ({self._format_bytes(path.stat().st_size)})"
        )
        self.status_var.set("다운로드 완료. 설치를 진행합니다.")
        self._append_log(f"download ok path={path}")
        self._begin_install(path)

    def _on_download_fail(self, message: str) -> None:
        self._busy = False
        try:
            self.progress.stop()
        except tk.TclError:
            pass
        self.progress.configure(mode="determinate", value=0)
        self.progress_label.set("실패")
        self.status_var.set(
            "자동 다운로드에 실패했습니다. 로그를 확인하거나 "
            "GitHub에서 Setup을 받아 직접 선택하세요."
        )
        self._append_log(f"download fail: {message}")
        self._set_manual_enabled(True)

    def _open_github(self) -> None:
        url = self.release.html_url or (
            "https://github.com/JeongwooPark/StartOfWork/releases/latest"
        )
        self._append_log(f"open github url={url}")
        webbrowser.open(url)

    def _browse_setup(self) -> None:
        path_str = filedialog.askopenfilename(
            parent=self,
            title="StartOfWork Setup 선택",
            filetypes=[
                ("Setup", "StartOfWorkSetup-*.exe"),
                ("Executable", "*.exe"),
                ("All files", "*.*"),
            ],
        )
        if not path_str:
            return
        path = Path(path_str)
        self._append_log(f"manual setup selected={path}")
        try:
            if self.release.expected_sha256:
                verify_download(path, self.release.expected_sha256)
        except UpdateError as exc:
            self._append_log(f"manual verify fail: {exc}")
            self.status_var.set(str(exc))
            return
        self._set_manual_enabled(False)
        self._begin_install(path)

    def _begin_install(self, setup_path: Path) -> None:
        if self._busy:
            return
        self._busy = True
        self.status_var.set("메인 프로그램을 종료한 뒤 설치합니다…")
        self.progress_label.set("설치 준비 중…")
        self._append_log(f"install begin setup={setup_path}")

        def worker() -> None:
            try:
                pending = prepare_setup_for_install(setup_path)
                unblock_file(pending)
                terminate_main_app(
                    target_pid=int(self.args.pid or 0),
                    log_path=self.log_path,
                )
                code = run_setup_installer(pending, log_path=self.log_path)
                if code != 0:
                    raise UpdateError(f"설치 프로그램이 실패했습니다 (exit={code})")
                restart_main_app(self.install_exe, log_path=self.log_path)
                write_update_log(self.log_path, "done")
                self.after(0, self._on_install_ok)
            except Exception as exc:
                logging.exception("업데이트 설치 실패")
                self.after(0, lambda: self._on_install_fail(str(exc)))

        threading.Thread(target=worker, name="updater-install", daemon=True).start()

    def _on_install_ok(self) -> None:
        self._busy = False
        self.status_var.set("설치가 완료되었습니다. 업데이터를 종료합니다.")
        self.progress_label.set("완료")
        self._append_log("install ok — exiting updater")
        self.after(800, self.destroy)

    def _on_install_fail(self, message: str) -> None:
        self._busy = False
        self.status_var.set(f"설치 실패: {message}")
        self.progress_label.set("실패")
        self._append_log(f"install fail: {message}")
        self._set_manual_enabled(True)

    def _on_close(self) -> None:
        if self._busy:
            self.status_var.set("업데이트 진행 중입니다. 잠시만 기다려 주세요.")
            return
        self.destroy()


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    raw = list(sys.argv[1:] if argv is None else argv)
    # TEMP 재기동 (frozen 설치본)
    if "--bootstrapped" not in raw:
        code = relaunch_from_temp(raw)
        if code == 0:
            return 0

    args = parse_args(raw)
    # LOCALAPPDATA 없이 개발 실행 시 대비
    if not args.install_exe and not os.environ.get("LOCALAPPDATA"):
        logging.error("LOCALAPPDATA 또는 --install-exe 가 필요합니다.")
        return 2

    app = UpdaterApp(args)
    app.mainloop()
    return 0
