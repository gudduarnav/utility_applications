"""GUI for downloading best-quality YouTube audio and exporting as MP3."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

import yt_dlp


BITRATE_OPTIONS = ["320k", "256k", "192k", "160k", "128k"]


def slugify(value: str) -> str:
    """Return a filesystem-safe slug."""
    slug = re.sub(r"[^a-zA-Z0-9\\-]+", "_", value or "")
    slug = re.sub(r"__+", "_", slug).strip("_")
    return slug or "audio"


def ensure_ffmpeg() -> str:
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise RuntimeError("ffmpeg executable not found on PATH.")
    return ffmpeg_path


def select_audio_format(info: Dict[str, Any]) -> Tuple[str, Optional[int]]:
    """Return the format_id of the highest bitrate audio-only stream."""
    formats: List[Dict[str, Any]] = [
        fmt
        for fmt in info.get("formats", [])
        if fmt.get("acodec") not in (None, "none") and fmt.get("vcodec") == "none"
    ]
    if not formats:
        raise RuntimeError("No standalone audio stream was found.")

    preferred = max(
        formats,
        key=lambda f: (
            f.get("abr") or 0,
            f.get("tbr") or 0,
            f.get("filesize") or 0,
        ),
    )
    abr = preferred.get("abr")
    return preferred["format_id"], abr


class YouTubeAudioMp3App(tk.Tk):
    """Main Tkinter application for downloading MP3 audio."""

    def __init__(self) -> None:
        super().__init__()
        self.title("youtube-best-audio-mp3")
        self.geometry("920x760")
        self.minsize(860, 640)
        self.configure_fonts()

        self.output_dir = tk.StringVar(value=str(self.default_output_dir()))
        self.bitrate_var = tk.StringVar(value=BITRATE_OPTIONS[0])
        self.current_progress = tk.DoubleVar(value=0.0)
        self.total_progress = tk.DoubleVar(value=0.0)
        self.current_status = tk.StringVar(value="Idle")

        self._worker: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._total_videos = 0
        self._completed = 0

        self.create_widgets()

    @staticmethod
    def default_output_dir() -> Path:
        download_dir = Path.home() / "Music"
        return download_dir if download_dir.exists() else Path.cwd()

    def configure_fonts(self) -> None:
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(size=16)
            except tk.TclError:
                continue

    def create_widgets(self) -> None:
        self.columnconfigure(0, weight=1)
        container = ttk.Frame(self)
        container.grid(row=0, column=0, sticky="nsew")
        container.columnconfigure(0, weight=1)
        container.rowconfigure(7, weight=1)

        ttk.Label(container, text="Video URLs (one per line):").grid(row=0, column=0, sticky="w")
        self.url_text = tk.Text(container, height=10, font=("Segoe UI", 16))
        self.url_text.grid(row=1, column=0, sticky="nsew", pady=(0, 10))

        dir_frame = ttk.Frame(container)
        dir_frame.grid(row=2, column=0, sticky="ew", pady=4)
        dir_frame.columnconfigure(1, weight=1)
        ttk.Label(dir_frame, text="Output directory:").grid(row=0, column=0, sticky="w")
        ttk.Entry(dir_frame, textvariable=self.output_dir).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(dir_frame, text="Browse", command=self.choose_directory).grid(row=0, column=2, padx=4)

        bitrate_frame = ttk.LabelFrame(container, text="MP3 Settings")
        bitrate_frame.grid(row=3, column=0, sticky="ew")
        ttk.Label(bitrate_frame, text="Audio bitrate:").grid(row=0, column=0, padx=4, pady=4)
        bitrate_combo = ttk.Combobox(
            bitrate_frame,
            state="readonly",
            textvariable=self.bitrate_var,
            values=BITRATE_OPTIONS,
            width=10,
        )
        bitrate_combo.grid(row=0, column=1, padx=6, pady=4, sticky="w")

        actions = ttk.Frame(container)
        actions.grid(row=4, column=0, pady=10, sticky="ew")
        ttk.Button(actions, text="Start Download", command=self.start_download).grid(row=0, column=0, padx=4)
        ttk.Button(actions, text="Stop", command=self.stop_download).grid(row=0, column=1, padx=4)

        progress = ttk.LabelFrame(container, text="Progress")
        progress.grid(row=5, column=0, sticky="ew")
        progress.columnconfigure(1, weight=1)
        ttk.Label(progress, text="Current Item:").grid(row=0, column=0, sticky="w")
        self.current_bar = ttk.Progressbar(progress, variable=self.current_progress, maximum=100)
        self.current_bar.grid(row=0, column=1, sticky="ew", padx=4, pady=2)
        ttk.Label(progress, text="Overall Queue:").grid(row=1, column=0, sticky="w")
        self.total_bar = ttk.Progressbar(progress, variable=self.total_progress, maximum=100)
        self.total_bar.grid(row=1, column=1, sticky="ew", padx=4, pady=2)
        ttk.Label(progress, textvariable=self.current_status).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=4
        )

        ttk.Label(container, text="Status Log:").grid(row=6, column=0, sticky="w")
        self.log_box = tk.Text(container, state=tk.DISABLED, height=10, wrap="word")
        self.log_box.grid(row=7, column=0, sticky="nsew")

    def choose_directory(self) -> None:
        directory = filedialog.askdirectory(initialdir=self.output_dir.get())
        if directory:
            self.output_dir.set(directory)

    def start_download(self) -> None:
        if self._worker and self._worker.is_alive():
            messagebox.showinfo("In Progress", "A download batch is already running.")
            return

        try:
            urls = [line.strip() for line in self.url_text.get("1.0", tk.END).splitlines() if line.strip()]
            if not urls:
                messagebox.showwarning("Missing URLs", "Provide at least one video URL.")
                return

            output_dir = Path(self.output_dir.get()).expanduser()
            output_dir.mkdir(parents=True, exist_ok=True)

            ffmpeg_path = ensure_ffmpeg()
        except Exception as exc:  # pylint: disable=broad-except
            messagebox.showerror("Unable to start", str(exc))
            self.append_log(f"Startup error: {exc}")
            return

        self._total_videos = len(urls)
        self._completed = 0
        self.current_progress.set(0.0)
        self.total_progress.set(0.0)
        self.set_status("Preparing downloads...")
        self._stop_event.clear()
        self.append_log(f"Queue size: {len(urls)}")

        self._worker = threading.Thread(
            target=self.run_queue,
            args=(urls, output_dir, self.bitrate_var.get(), ffmpeg_path),
            daemon=True,
        )
        self._worker.start()

    def stop_download(self) -> None:
        if self._worker and self._worker.is_alive():
            self._stop_event.set()
            self.append_log("Stop requested; finishing current item...")

    def run_queue(self, urls: List[str], output_dir: Path, bitrate: str, ffmpeg_path: str) -> None:
        for idx, url in enumerate(urls, start=1):
            if self._stop_event.is_set():
                break

            self.set_status(f"Downloading {idx}/{self._total_videos}")
            self.append_log(f"Starting download {idx}/{self._total_videos}: {url}")
            try:
                result = self.download_single(
                    url=url,
                    output_dir=output_dir,
                    bitrate=bitrate,
                    ffmpeg_path=ffmpeg_path,
                )
                self.append_log(f"Saved '{result.name}' ({bitrate} MP3).")
            except Exception as exc:  # pylint: disable=broad-except
                self.append_log(f"{url} failed: {exc}")
            finally:
                self._completed += 1
                self.reset_current_progress()
                self.dispatch(self.update_total_progress, 0.0)

        self.set_status("Idle")
        self.append_log("Queue finished.")
        self._worker = None

    def download_single(
        self,
        url: str,
        output_dir: Path,
        bitrate: str,
        ffmpeg_path: str,
    ) -> Path:
        temp_dir = Path(tempfile.mkdtemp(prefix="ytdlp_mp3_"))
        try:
            self.append_log(f"Fetching metadata for: {url}")
            info = self.fetch_metadata(url)
            fmt_id, abr = select_audio_format(info)
            if abr:
                self.append_log(f"{info.get('title') or url}: best audio {abr} kbps stream.")

            ydl_opts = {
                "format": fmt_id,
                "outtmpl": str(temp_dir / "%(title)s.%(ext)s"),
                "progress_hooks": [self.create_hook(info.get("title") or url)],
                "quiet": True,
                "no_warnings": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                download_result = ydl.extract_info(url, download=True)

            source = self.resolve_path(download_result)
            title = info.get("title") or url
            target_name = self.disambiguate(output_dir / f"{slugify(title)}.mp3")

            self.set_status(f"Converting '{title}' to MP3")
            self.convert_to_mp3(source=source, destination=target_name, bitrate=bitrate, ffmpeg_path=ffmpeg_path)
            return target_name
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def fetch_metadata(self, url: str) -> Dict[str, Any]:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            return ydl.extract_info(url, download=False)

    def resolve_path(self, result: Dict[str, Any]) -> Path:
        if "requested_downloads" in result and result["requested_downloads"]:
            filepath = result["requested_downloads"][0].get("filepath")
            if filepath:
                return Path(filepath)

        if "_filename" in result:
            return Path(result["_filename"])
        if "filename" in result:
            return Path(result["filename"])
        raise RuntimeError("yt-dlp did not report the output path.")

    def disambiguate(self, path: Path) -> Path:
        if not path.exists():
            return path
        stem, suffix = path.stem, path.suffix
        counter = 1
        while True:
            candidate = path.parent / f"{stem}_{counter}{suffix}"
            if not candidate.exists():
                return candidate
            counter += 1

    def convert_to_mp3(self, source: Path, destination: Path, bitrate: str, ffmpeg_path: str) -> None:
        cmd = [
            ffmpeg_path,
            "-y",
            "-i",
            str(source),
            "-vn",
            "-c:a",
            "libmp3lame",
            "-b:a",
            bitrate,
            str(destination),
        ]
        process = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if process.returncode != 0:
            raise RuntimeError(process.stderr.strip() or "ffmpeg conversion failed.")

    def create_hook(self, title: str):
        def hook(status: Dict[str, Any]) -> None:
            if status["status"] == "downloading":
                downloaded = status.get("downloaded_bytes") or 0
                total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
                percent = (downloaded / total * 100) if total else 0
                self.update_current_progress(percent, f"Downloading {title}")
            elif status["status"] == "finished":
                self.update_current_progress(100.0, f"Downloaded {title}")

        return hook

    def update_current_progress(self, percent: float, message: str) -> None:
        percent = max(0.0, min(percent, 100.0))

        def apply() -> None:
            self.current_progress.set(percent)
            self.current_status.set(message)
            self.update_total_progress(percent)

        self.dispatch(apply)

    def update_total_progress(self, current_percent: float) -> None:
        total = self._total_videos or 1
        total_percent = ((self._completed + current_percent / 100.0) / total) * 100.0
        total_percent = max(0.0, min(total_percent, 100.0))
        self.total_progress.set(total_percent)

    def append_log(self, message: str) -> None:
        def writer() -> None:
            print(message)
            self.log_box.configure(state=tk.NORMAL)
            self.log_box.insert(tk.END, message + "\n")
            self.log_box.see(tk.END)
            self.log_box.configure(state=tk.DISABLED)

        self.after(0, writer)

    def set_status(self, message: str) -> None:
        self.dispatch(self.current_status.set, message)

    def reset_current_progress(self) -> None:
        self.dispatch(self.current_progress.set, 0.0)

    def dispatch(self, func, *args, **kwargs) -> None:
        self.after(0, lambda: func(*args, **kwargs))


def main() -> None:
    app = YouTubeAudioMp3App()
    app.mainloop()


if __name__ == "__main__":
    main()
