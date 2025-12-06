"""Tkinter-based GUI for downloading YouTube videos at 480p with best audio."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

import yt_dlp


ACCELERATION_MODES: Dict[str, List[str]] = {
    "CPU": ["-vf", "scale=-2:480", "-c:v", "libx264", "-preset", "medium", "-crf", "23"],
    "GPU (CUDA)": [
        "-hwaccel",
        "cuda",
        "-hwaccel_output_format",
        "cuda",
        "-vf",
        "scale_cuda=-2:480",
        "-c:v",
        "h264_nvenc",
        "-preset",
        "fast",
        "-b:v",
        "3M",
    ],
    "OPENCL": [
        "-init_hw_device",
        "opencl=gpu:0",
        "-filter_hw_device",
        "gpu",
        "-vf",
        "scale_opencl=-2:480",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "23",
    ],
}


def slugify(value: str) -> str:
    """Return a filesystem-safe slug."""
    slug = re.sub(r"[^a-zA-Z0-9\\-]+", "_", value)
    slug = re.sub(r"__+", "_", slug).strip("_")
    return slug or "video"


def ensure_ffmpeg() -> str:
    """Return the ffmpeg executable path or raise an error."""
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise RuntimeError("ffmpeg executable not found in PATH.")
    return ffmpeg_path


def select_video_format(info: Dict[str, Any]) -> Tuple[str, Optional[int]]:
    """Choose the best 480p format if available, otherwise the closest above 480p."""
    formats: List[Dict[str, Any]] = [
        fmt
        for fmt in info.get("formats", [])
        if fmt.get("vcodec") != "none"
        and fmt.get("acodec") == "none"
        and fmt.get("height")
    ]
    if not formats:
        raise RuntimeError("No downloadable video formats were found.")

    exact_480 = [fmt for fmt in formats if fmt.get("height") == 480]
    if exact_480:
        best = max(exact_480, key=lambda f: (f.get("tbr") or 0))
        return best["format_id"], 480

    above_480 = [fmt for fmt in formats if fmt.get("height", 0) > 480]
    if above_480:
        closest = min(above_480, key=lambda f: f.get("height", 10_000))
        return closest["format_id"], closest.get("height")

    # Fallback to the best available format (even if below 480)
    fallback = max(formats, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
    return fallback["format_id"], fallback.get("height")


@dataclass
class DownloadResult:
    source_path: Path
    title: str
    height: Optional[int]


class YouTubeDownload480pApp(tk.Tk):
    """Main GUI application."""

    def __init__(self) -> None:
        super().__init__()
        self.title("youtube-download480p")
        self.geometry("960x820")
        self.minsize(900, 700)
        self.configure_fonts()
        self.style = ttk.Style(self)
        self.style.configure("TLabel", padding=4)

        self.output_dir = tk.StringVar(value=str(self.default_output_dir()))
        self.format_var = tk.StringVar(value="mp4")
        self.accel_var = tk.StringVar(value="CPU")
        self.current_progress = tk.DoubleVar(value=0.0)
        self.total_progress = tk.DoubleVar(value=0.0)
        self.current_status = tk.StringVar(value="Idle")

        self._worker: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._urls: List[str] = []
        self._total_videos = 0
        self._completed_videos = 0
        self._current_download_file: Optional[Path] = None

        self.create_widgets()

    @staticmethod
    def default_output_dir() -> Path:
        download_dir = Path.home() / "Downloads"
        return download_dir if download_dir.exists() else Path.cwd()

    def configure_fonts(self) -> None:
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                font_obj = tkfont.nametofont(name)
                font_obj.configure(size=16)
            except tk.TclError:
                continue

    def create_widgets(self) -> None:
        self.columnconfigure(0, weight=1)
        content = ttk.Frame(self)
        content.grid(row=0, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)

        url_label = ttk.Label(content, text="Video URLs (one per line):")
        url_label.grid(row=0, column=0, sticky="w")

        self.url_text = tk.Text(content, height=10, wrap="word", font=("Segoe UI", 16))
        self.url_text.grid(row=1, column=0, sticky="nsew", pady=(0, 10))

        dir_frame = ttk.Frame(content)
        dir_frame.grid(row=2, column=0, sticky="ew", pady=4)
        dir_frame.columnconfigure(1, weight=1)
        ttk.Label(dir_frame, text="Output directory:").grid(row=0, column=0, sticky="w")
        dir_entry = ttk.Entry(dir_frame, textvariable=self.output_dir)
        dir_entry.grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(dir_frame, text="Browse", command=self.choose_directory).grid(
            row=0, column=2, padx=4
        )

        format_frame = ttk.LabelFrame(content, text="Video Format")
        format_frame.grid(row=3, column=0, sticky="ew", pady=4)
        for idx, fmt in enumerate(("mp4", "mkv")):
            ttk.Radiobutton(
                format_frame, text=fmt.upper(), value=fmt, variable=self.format_var
            ).grid(row=0, column=idx, padx=10, sticky="w")

        accel_frame = ttk.LabelFrame(content, text="Conversion Device")
        accel_frame.grid(row=4, column=0, sticky="ew", pady=4)
        ttk.Label(accel_frame, text="Select engine:").grid(row=0, column=0, padx=4)
        self.accel_combo = ttk.Combobox(
            accel_frame,
            values=list(ACCELERATION_MODES.keys()),
            textvariable=self.accel_var,
            state="readonly",
            width=20,
        )
        self.accel_combo.grid(row=0, column=1, padx=6, pady=2, sticky="w")

        control_frame = ttk.Frame(content)
        control_frame.grid(row=5, column=0, sticky="ew", pady=8)
        ttk.Button(control_frame, text="Start Download", command=self.start_download).grid(
            row=0, column=0, padx=4
        )
        ttk.Button(control_frame, text="Stop", command=self.stop_download).grid(
            row=0, column=1, padx=4
        )

        progress_frame = ttk.LabelFrame(content, text="Progress")
        progress_frame.grid(row=6, column=0, sticky="ew", pady=8)
        ttk.Label(progress_frame, text="Current Video:").grid(row=0, column=0, sticky="w")
        self.current_bar = ttk.Progressbar(
            progress_frame, variable=self.current_progress, maximum=100
        )
        self.current_bar.grid(row=0, column=1, sticky="ew", padx=4, pady=2)
        progress_frame.columnconfigure(1, weight=1)
        self.current_message = ttk.Label(progress_frame, textvariable=self.current_status)
        self.current_message.grid(row=1, column=0, columnspan=2, sticky="w", padx=4)

        ttk.Label(progress_frame, text="Overall Queue:").grid(row=2, column=0, sticky="w")
        self.total_bar = ttk.Progressbar(
            progress_frame, variable=self.total_progress, maximum=100
        )
        self.total_bar.grid(row=2, column=1, sticky="ew", padx=4, pady=2)

        status_label = ttk.Label(content, text="Status Log:")
        status_label.grid(row=7, column=0, sticky="w")
        self.log_box = tk.Text(content, height=10, state=tk.DISABLED, wrap="word")
        self.log_box.grid(row=8, column=0, sticky="nsew")
        content.rowconfigure(8, weight=1)

    def choose_directory(self) -> None:
        directory = filedialog.askdirectory(initialdir=self.output_dir.get())
        if directory:
            self.output_dir.set(directory)

    def start_download(self) -> None:
        if self._worker and self._worker.is_alive():
            messagebox.showinfo("Download running", "A download is already in progress.")
            return

        urls = [line.strip() for line in self.url_text.get("1.0", tk.END).splitlines()]
        urls = [url for url in urls if url]
        if not urls:
            messagebox.showwarning("No URLs", "Provide at least one video URL.")
            return

        out_dir = Path(self.output_dir.get()).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)

        self._urls = urls
        self._total_videos = len(urls)
        self._completed_videos = 0
        self.current_progress.set(0.0)
        self.total_progress.set(0.0)
        self.set_status_message("Preparing downloads...")
        self._stop_event.clear()
        self.append_log(f"Starting batch of {len(urls)} videos.")

        self._worker = threading.Thread(
            target=self.run_queue, args=(urls, out_dir, self.format_var.get()), daemon=True
        )
        self._worker.start()

    def stop_download(self) -> None:
        if self._worker and self._worker.is_alive():
            self._stop_event.set()
            self.append_log("Stop requested. Current item will finish before exit.")

    def run_queue(self, urls: List[str], output_dir: Path, container: str) -> None:
        ffmpeg_path = None
        try:
            ffmpeg_path = ensure_ffmpeg()
        except RuntimeError as exc:
            self.append_log(str(exc))
            self.current_status.set("ffmpeg missing")
            return

        for idx, url in enumerate(urls, start=1):
            if self._stop_event.is_set():
                self.append_log("Download stopped by user.")
                break

            self.set_status_message(f"Downloading {idx}/{self._total_videos}")
            try:
                result = self.download_single(
                    url=url,
                    output_dir=output_dir,
                    container=container,
                    ffmpeg_path=ffmpeg_path,
                )
                self.append_log(
                    f"Saved '{result.title}' as {result.source_path.name} ({container.upper()})"
                )
            except Exception as exc:  # pylint: disable=broad-except
                self.append_log(f"Failed: {url} -> {exc}")
            finally:
                self._completed_videos += 1
                self.reset_current_progress()
                self.set_status_message("Idle")
                self.dispatch(self.update_total_progress, 0.0)

        self.append_log("All tasks finished.")

    def download_single(
        self, url: str, output_dir: Path, container: str, ffmpeg_path: str
    ) -> DownloadResult:
        temp_dir = Path(tempfile.mkdtemp(prefix="ytdlp480p_"))
        try:
            try:
                with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
                    info = ydl.extract_info(url, download=False)
            except Exception as exc:  # pylint: disable=broad-except
                raise RuntimeError(f"Metadata extraction failed: {exc}") from exc

            format_id, height = select_video_format(info)
            if height and height > 480:
                self.append_log(f"{info.get('title') or url}: using {height}p source (will downscale).")
            elif height and height < 480:
                self.append_log(
                    f"{info.get('title') or url}: highest quality is {height}p (480p unavailable)."
                )
            elif height is None:
                self.append_log(f"{info.get('title') or url}: unknown source height (forcing conversion).")
            ydl_opts = {
                "format": f"{format_id}+bestaudio/best",
                "outtmpl": str(temp_dir / "%(title)s.%(ext)s"),
                "merge_output_format": "mkv",
                "progress_hooks": [self.create_hook(info.get("title") or url)],
                "postprocessors": [
                    {"key": "FFmpegVideoConvertor", "preferedformat": "mkv"}
                ],
            }

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    result = ydl.extract_info(url, download=True)
            except Exception as exc:  # pylint: disable=broad-except
                raise RuntimeError(f"Download failed: {exc}") from exc

            downloaded_path = self.resolve_download_path(result)
            if not downloaded_path.exists():
                raise RuntimeError("Downloaded file path could not be determined.")

            final_name = slugify(info.get("title") or "video")
            desired_path = output_dir / f"{final_name}.{container}"

            if desired_path.exists():
                desired_path = self.disambiguate(desired_path)

            need_downscale = height is None or height > 480
            needs_conversion = need_downscale or downloaded_path.suffix.lstrip(".").lower() != container
            if needs_conversion:
                status = (
                    f"Converting {info.get('title') or url} to 480p"
                    if need_downscale
                    else f"Remuxing {info.get('title') or url} to {container.upper()}"
                )
                self.set_status_message(status)
                self.run_conversion(
                    source=downloaded_path,
                    destination=desired_path,
                    downscale=need_downscale,
                    ffmpeg_path=ffmpeg_path,
                )
            else:
                shutil.move(str(downloaded_path), desired_path)

            return DownloadResult(source_path=desired_path, title=info.get("title") or url, height=height)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def run_conversion(
        self,
        source: Path,
        destination: Path,
        downscale: bool,
        ffmpeg_path: str,
    ) -> None:
        accel = self.accel_var.get()
        accel_args = ACCELERATION_MODES.get(accel, ACCELERATION_MODES["CPU"])

        cmd = [ffmpeg_path, "-y", "-i", str(source)]
        if downscale:
            cmd.extend(accel_args)
        else:
            cmd.extend(["-c:v", "copy"])
        cmd.extend(["-c:a", "copy", str(destination)])

        self.append_log(f"Converting via {accel} -> {destination.name}")
        process = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if process.returncode != 0:
            raise RuntimeError(f"ffmpeg conversion failed: {process.stderr.strip() or 'unknown error'}")

    def resolve_download_path(self, result: Dict[str, Any]) -> Path:
        if "requested_downloads" in result and result["requested_downloads"]:
            file_path = result["requested_downloads"][0].get("filepath")
            if file_path:
                return Path(file_path)
        if "_filename" in result:
            return Path(result["_filename"])
        if "filename" in result:
            return Path(result["filename"])
        raise RuntimeError("yt-dlp did not report an output file.")

    def disambiguate(self, path: Path) -> Path:
        parent = path.parent
        stem = path.stem
        suffix = path.suffix
        counter = 1
        while True:
            candidate = parent / f"{stem}_{counter}{suffix}"
            if not candidate.exists():
                return candidate
            counter += 1

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
        total_percent = ((self._completed_videos + current_percent / 100.0) / total) * 100.0
        total_percent = max(0.0, min(total_percent, 100.0))
        self.total_progress.set(total_percent)

    def append_log(self, message: str) -> None:
        def writer() -> None:
            self.log_box.configure(state=tk.NORMAL)
            self.log_box.insert(tk.END, message + "\n")
            self.log_box.see(tk.END)
            self.log_box.configure(state=tk.DISABLED)

        self.after(0, writer)

    def set_status_message(self, message: str) -> None:
        self.dispatch(self.current_status.set, message)

    def reset_current_progress(self) -> None:
        self.dispatch(self.current_progress.set, 0.0)

    def dispatch(self, func, *args, **kwargs) -> None:
        self.after(0, lambda: func(*args, **kwargs))

def main() -> None:
    app = YouTubeDownload480pApp()
    app.mainloop()


if __name__ == "__main__":
    main()
