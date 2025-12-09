#!/usr/bin/env python3
"""Kodi Stream Launcher GUI.

A single-file Tkinter application that accepts one or more YouTube URLs,
forces playback at 480p or 720p HD, and pushes the media to a reachable Kodi
instance.

Features
--------
* Beautiful themed interface with 16pt typography and accent colors.
* Auto-discovery of Kodi instances via Zeroconf (if installed) plus local
  subnet probing; manual host entry is always available.
* Enforces 480p or 720p playback. If YouTube already exposes a matching
  progressive stream, that direct URL is sent to Kodi. Otherwise, the video is
  downloaded from the next-higher quality variant (e.g., 720p->1080p) and
  ffmpeg transcodes it in the background using CPU, NVIDIA NVENC, or OpenCL
  acceleration before streaming locally.
* Local threaded HTTP server shares the download directory so Kodi can reach
  converted assets without extra setup.
* Built-in logging, connection testing, and friendly status indicators.

Dependencies
------------
Python 3.9+, `requests`, `yt_dlp`, and `ffmpeg` must be available. Zeroconf is
optional but improves auto-detection accuracy. Usage inside the "torch" conda
environment:

    conda activate torch
    pip install requests yt-dlp zeroconf

"""

from __future__ import annotations

import concurrent.futures
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

try:
    import requests
except ImportError:  # pragma: no cover - handled at runtime
    requests = None

try:
    from yt_dlp import YoutubeDL
except ImportError:  # pragma: no cover - handled at runtime
    YoutubeDL = None

try:
    from zeroconf import ServiceBrowser, Zeroconf
except ImportError:  # pragma: no cover - handled at runtime
    Zeroconf = None


BASE_FONT = ("Segoe UI", 16)
ACCENT_COLOR = "#2EC4B6"
BG_DARK = "#1B1F3B"
BG_CARD = "#2D325A"
FG_PRIMARY = "#ECF9FF"
FG_SECONDARY = "#A5B4CE"
LOG_SUCCESS = "#6EE7B7"
LOG_WARN = "#F7B267"
LOG_ERROR = "#F25F5C"

RESOLUTION_OPTIONS: Dict[str, int] = {"480p": 480, "720p HD": 720}


def _cpu_args(height: int) -> Sequence[str]:
    return [
        "-vf",
        f"scale=-2:{height}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
    ]


def _nvenc_args(height: int) -> Sequence[str]:
    return [
        "-hwaccel",
        "cuda",
        "-hwaccel_output_format",
        "cuda",
        "-vf",
        f"scale_cuda=-2:{height}",
        "-c:v",
        "h264_nvenc",
        "-preset",
        "p4",
        "-b:v",
        "2500k" if height >= 720 else "1800k",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
    ]


def _opencl_args(height: int) -> Sequence[str]:
    return [
        "-init_hw_device",
        "opencl=gpu:0",
        "-filter_hw_device",
        "gpu",
        "-vf",
        f"scale_opencl=-2:{height}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
    ]


GPU_PROFILES: Dict[str, Callable[[int], Sequence[str]]] = {
    "CPU (x264)": _cpu_args,
    "NVIDIA NVENC": _nvenc_args,
    "OpenCL": _opencl_args,
}


@dataclass
class KodiEndpoint:
    host: str
    port: int = 8080
    name: str = ""

    def label(self) -> str:
        desc = f"{self.host}:{self.port}"
        if self.name:
            desc = f"{self.name} ({desc})"
        return desc


def determine_local_ip() -> str:
    """Best-effort way to grab a LAN IP for the embedded HTTP server."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            return ip
    except OSError:
        return "127.0.0.1"


class LocalHTTPServer:
    """Threaded static file server rooted at the selected download directory."""

    def __init__(self, directory: Path, preferred_port: int = 8765) -> None:
        self.directory = Path(directory)
        self.port = preferred_port
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._server:
            return
        directory = self.directory
        directory.mkdir(parents=True, exist_ok=True)
        handler_cls = partial(StyledHTTPRequestHandler, directory=str(directory))
        for port in range(self.port, self.port + 20):
            try:
                server = ThreadingHTTPServer(("0.0.0.0", port), handler_cls)
                server.daemon_threads = True
                self._server = server
                self.port = port
                break
            except OSError:
                continue
        if not self._server:
            raise RuntimeError("Unable to bind local HTTP server to any port in range.")
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._server:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1)
        self._thread = None

    def restart(self, directory: Path) -> None:
        self.stop()
        self.directory = Path(directory)
        self.start()

    def base_url(self) -> str:
        ip = determine_local_ip()
        return f"http://{ip}:{self.port}"


class StyledHTTPRequestHandler(SimpleHTTPRequestHandler):
    """Quiet request handler with disabled console spam."""

    def log_message(self, format: str, *args: object) -> None:  # pragma: no cover - silence
        return


class KodiRPCClient:
    def __init__(
        self,
        endpoint: KodiEndpoint,
        username: str = "",
        password: str = "",
        timeout: float = 15.0,
        retries: int = 2,
    ) -> None:
        if not requests:
            raise RuntimeError("The requests package is required for Kodi RPC calls.")
        self.endpoint = endpoint
        self.auth = (username, password) if username or password else None
        self.timeout = timeout
        self.retries = max(1, retries)
        self._session = requests.Session()

    def _url(self) -> str:
        return f"http://{self.endpoint.host}:{self.endpoint.port}/jsonrpc"

    def call(self, method: str, params: Optional[dict] = None) -> dict:
        payload = {"jsonrpc": "2.0", "method": method, "id": str(uuid.uuid4())}
        if params:
            payload["params"] = params
        last_exc: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                response = self._session.post(self._url(), json=payload, timeout=self.timeout, auth=self.auth)
                response.raise_for_status()
                data = response.json()
                if "error" in data:
                    raise RuntimeError(f"Kodi error: {data['error']}")
                return data
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_exc = exc
                if attempt >= self.retries - 1:
                    break
                time.sleep(1.5 * (attempt + 1))
            except Exception:
                raise
        raise RuntimeError(f"Kodi RPC request failed after retries: {last_exc}")

    def ping(self) -> bool:
        reply = self.call("JSONRPC.Ping")
        return reply.get("result") == "pong"

    def play_stream(self, url: str) -> None:
        params = {"item": {"file": url}}
        self.call("Player.Open", params=params)


class KodiLocator:
    COMMON_PORTS = (8080, 5000, 80, 9090)

    def __init__(self, logger: Callable[[str], None]) -> None:
        self.logger = logger

    def discover(
        self,
        timeout: float = 4.0,
        stop_event: Optional[threading.Event] = None,
        manual_host: Optional[str] = None,
    ) -> List[KodiEndpoint]:
        endpoints: Dict[str, KodiEndpoint] = {}
        manual_host = (manual_host or "").strip()
        if manual_host:
            host, ports = self._parse_manual_host(manual_host)
            manual_endpoint = self._probe_host(host, ports=ports)
            if manual_endpoint:
                endpoints[f"{manual_endpoint.host}:{manual_endpoint.port}"] = manual_endpoint
        zc_hosts = self._discover_with_zeroconf(timeout, stop_event)
        endpoints.update({f"{ep.host}:{ep.port}": ep for ep in zc_hosts})
        probe_hosts = self._probe_local_networks(limit_hosts=64, stop_event=stop_event)
        for ep in probe_hosts:
            endpoints.setdefault(f"{ep.host}:{ep.port}", ep)
        if not endpoints:
            self.logger("No Kodi hosts discovered. Use manual entry or adjust network scanning range.")
        return list(endpoints.values())

    def _discover_with_zeroconf(self, timeout: float, stop_event: Optional[threading.Event]) -> List[KodiEndpoint]:
        if not Zeroconf:
            return []
        results: List[KodiEndpoint] = []
        class _Listener:
            def __init__(self, collector: List[KodiEndpoint]) -> None:
                self.collector = collector

            def add_service(self, zeroconf: Zeroconf, type_: str, name: str) -> None:
                info = zeroconf.get_service_info(type_, name)
                if info and info.addresses:
                    host = socket.inet_ntoa(info.addresses[0])
                    port = info.port or 8080
                    self.collector.append(KodiEndpoint(host=host, port=port, name=info.server.rstrip(".")))

            def remove_service(self, *_: object) -> None:
                return

            def update_service(self, *_: object) -> None:
                return

        zc = Zeroconf()
        listener = _Listener(results)
        browser = ServiceBrowser(zc, "_xbmc-jsonrpc._tcp.local.", listener)
        start = time.time()
        try:
            while time.time() - start < timeout:
                if stop_event and stop_event.is_set():
                    break
                time.sleep(0.2)
        finally:
            browser.cancel()
            zc.close()
        if results:
            self.logger(f"Zeroconf discovered {len(results)} candidate(s).")
        return results

    def _probe_local_networks(self, limit_hosts: int = 64, stop_event: Optional[threading.Event] = None) -> List[KodiEndpoint]:
        if not requests:
            return []
        cidrs = self._candidate_cidrs()
        if not cidrs:
            cidrs = ["127.0.0.1/32"]
        to_probe: List[str] = []
        for cidr in cidrs:
            try:
                network = ip_network_safe(cidr)
            except ValueError:
                continue
            for idx, host in enumerate(network.hosts()):
                if len(to_probe) >= limit_hosts:
                    break
                to_probe.append(str(host))
            if len(to_probe) >= limit_hosts:
                break
        endpoints: List[KodiEndpoint] = []
        if not to_probe:
            return endpoints
        self.logger(f"Scanning {len(to_probe)} host(s) for Kodi JSON-RPC...")
        max_workers = min(16, max(1, len(to_probe)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for host in to_probe:
                if stop_event and stop_event.is_set():
                    break
                futures.append(executor.submit(self._probe_host, host))
            for future in concurrent.futures.as_completed(futures):
                if stop_event and stop_event.is_set():
                    break
                try:
                    endpoint = future.result()
                except Exception:
                    continue
                if endpoint:
                    endpoints.append(endpoint)
        if endpoints:
            self.logger(f"Active Kodi endpoint(s) detected: {', '.join(ep.label() for ep in endpoints)}")
        return endpoints

    def _candidate_cidrs(self) -> List[str]:
        cidrs: List[str] = []
        for iface in socket.getaddrinfo(socket.gethostname(), None, proto=socket.IPPROTO_TCP):
            addr = iface[4][0]
            if addr.startswith("127."):
                continue
            octets = addr.split(".")
            if len(octets) == 4:
                cidrs.append(".".join(octets[:3]) + ".0/24")
        return list(dict.fromkeys(cidrs))

    def _probe_host(self, host: str, ports: Optional[Sequence[int]] = None) -> Optional[KodiEndpoint]:
        port_list = list(ports) if ports else list(self.COMMON_PORTS)
        for port in port_list:
            url = f"http://{host}:{port}/jsonrpc"
            try:
                resp = requests.post(url, json={"jsonrpc": "2.0", "method": "JSONRPC.Ping", "id": "scan"}, timeout=0.7)
                if resp.ok:
                    data = resp.json()
                    if data.get("result") == "pong":
                        name = resp.headers.get("Server", "Kodi")
                        return KodiEndpoint(host=host, port=port, name=name)
            except Exception:
                continue
        return None

    def _parse_manual_host(self, manual_host: str) -> tuple[str, Sequence[int]]:
        host = manual_host
        ports: Sequence[int] = self.COMMON_PORTS
        if ":" in manual_host:
            raw_host, raw_port = manual_host.rsplit(":", 1)
            host = raw_host.strip()
            try:
                ports = (int(raw_port),)
            except ValueError:
                ports = self.COMMON_PORTS
        return host, ports


def ip_network_safe(cidr: str):  # type: ignore[no-any-unimported]
    import ipaddress

    return ipaddress.ip_network(cidr, strict=False)


class StreamPlanner:
    def __init__(self, download_dir: Path, logger: Callable[[str, str], None], http_server: LocalHTTPServer) -> None:
        self.download_dir = Path(download_dir)
        self.logger = logger
        self.http_server = http_server

    def ensure_dependencies(self) -> None:
        missing = []
        if not YoutubeDL:
            missing.append("yt_dlp")
        if not shutil.which("ffmpeg"):
            missing.append("ffmpeg")
        if missing:
            raise RuntimeError(f"Missing dependencies: {', '.join(missing)}")

    def prepare(self, url: str, gpu_profile: str, target_height: int) -> Dict[str, str]:
        self.ensure_dependencies()
        info = self._extract_info(url)
        title = info.get("title") or url
        video_id = info.get("id") or uuid.uuid4().hex
        direct = self._find_direct_stream(info, target_height)
        if direct:
            self.logger(f"Found native {target_height}p stream for '{title}'.", "success")
            return {"mode": "direct", "url": direct, "title": title}
        self.logger(
            f"Transcoding '{title}' to {target_height}p using {gpu_profile} (sourcing next-best quality)...",
            "warn",
        )
        source_fmt = self._select_source_format(info, target_height)
        fmt_label = f"{source_fmt.get('height')}p" if source_fmt and source_fmt.get("height") else "best available"
        self.logger(f"Using {fmt_label} source for conversion", "warn")
        source_url, headers = self._resolve_source_stream(info, source_fmt)
        stream_path = self._transcode(source_url, headers, video_id, gpu_profile, target_height)
        rel = os.path.relpath(stream_path, self.http_server.directory)
        http_url = f"{self.http_server.base_url()}/{rel.replace(os.sep, '/')}"
        return {"mode": "local", "url": http_url, "title": title}

    def _extract_info(self, url: str) -> dict:
        if not YoutubeDL:
            raise RuntimeError("yt_dlp is required for extracting YouTube metadata.")
        opts = {"quiet": True, "skip_download": True, "nocheckcertificate": True}
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    def _find_direct_stream(self, info: dict, target_height: int) -> Optional[str]:
        formats = info.get("formats") or []
        for fmt in formats:
            height = fmt.get("height") or 0
            acodec = fmt.get("acodec")
            vcodec = fmt.get("vcodec")
            if (
                height == target_height
                and acodec
                and acodec != "none"
                and vcodec
                and vcodec != "none"
                and fmt.get("url")
            ):
                return fmt.get("url")
        return None

    def _select_source_format(self, info: dict, target_height: int) -> Optional[dict]:
        def _with_audio(fmt: dict) -> bool:
            acodec = fmt.get("acodec")
            return bool(acodec and acodec != "none")

        formats = [fmt for fmt in info.get("formats") or [] if fmt.get("height") and fmt.get("url")]
        higher = [fmt for fmt in formats if (fmt.get("height") or 0) > target_height and _with_audio(fmt)]
        higher.sort(key=lambda fmt: fmt.get("height") or 0)
        if higher:
            return higher[0]
        with_audio = [fmt for fmt in formats if _with_audio(fmt)]
        with_audio.sort(key=lambda fmt: fmt.get("height") or 0, reverse=True)
        if with_audio:
            return with_audio[0]
        formats.sort(key=lambda fmt: fmt.get("height") or 0, reverse=True)
        return formats[0] if formats else None

    def _resolve_source_stream(self, info: dict, fmt: Optional[dict]) -> Tuple[str, Dict[str, str]]:
        candidates = [fmt] if fmt else []
        if not candidates:
            candidates = [info]
        for candidate in candidates:
            if not candidate:
                continue
            url = candidate.get("url")
            if url:
                headers = candidate.get("http_headers") or info.get("http_headers") or {}
                return url, headers
        raise RuntimeError("Unable to resolve a source stream URL for conversion.")

    def _transcode(self, source_url: str, headers: Dict[str, str], video_id: str, gpu_profile: str, target_height: int) -> Path:
        profile_factory = GPU_PROFILES.get(gpu_profile)
        if not profile_factory:
            raise ValueError(f"Unknown GPU profile: {gpu_profile}")
        profile_args = list(profile_factory(target_height))
        target_dir = self.download_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        output = target_dir / f"{video_id}_{target_height}p.mp4"
        ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
        header_args: List[str] = []
        if headers:
            header_block = "\r\n".join(f"{k}: {v}" for k, v in headers.items()) + "\r\n"
            header_args = ["-headers", header_block]
        cmd = [ffmpeg_bin, "-y", *header_args, "-i", source_url, *profile_args, str(output)]
        self.logger(f"Running ffmpeg: {' '.join(cmd)}")
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self._stream_ffmpeg_output(process)
        if process.wait() != 0:
            raise RuntimeError("ffmpeg failed to transcode the media")
        self.logger(f"Transcode finished: {output}", "success")
        return output

    def _stream_ffmpeg_output(self, process: subprocess.Popen) -> None:
        assert process.stdout
        start = time.time()
        for line in process.stdout:
            line = line.strip()
            if line and time.time() - start > 0.5:
                self.logger(f"ffmpeg: {line}")
                start = time.time()


class KodiStreamGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Kodi Stream Launcher")
        self.root.configure(bg=BG_DARK)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._configure_style()

        self.url_text = scrolledtext.ScrolledText(root, font=BASE_FONT, height=5, wrap=tk.WORD, bg=BG_CARD, fg=FG_PRIMARY, insertbackground=FG_PRIMARY)
        self.url_text.insert("1.0", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.url_text.grid(row=0, column=0, columnspan=4, sticky="nsew", padx=18, pady=(18, 12))

        self.gpu_var = tk.StringVar(value=list(GPU_PROFILES.keys())[0])
        self.download_dir = tk.StringVar(value=str(Path(tempfile.gettempdir()) / "kodi_streams"))
        self.resolution_var = tk.StringVar(value=list(RESOLUTION_OPTIONS.keys())[0])
        self.host_var = tk.StringVar(value="127.0.0.1")
        self.port_var = tk.StringVar(value="8080")
        self.user_var = tk.StringVar(value="")
        self.password_var = tk.StringVar(value="")

        self._build_option_cards()
        self._build_host_section()
        self._build_action_buttons()
        self._build_logger()

        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_rowconfigure(5, weight=2)
        self.root.grid_columnconfigure(0, weight=1)

        self.log_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self.discovered: List[KodiEndpoint] = []
        self.stop_event = threading.Event()
        self.discovery_threads: Set[threading.Thread] = set()
        self.http_server = LocalHTTPServer(Path(self.download_dir.get()))
        try:
            self.http_server.start()
        except Exception as exc:
            self._log(f"Failed to start local HTTP server: {exc}", "error")
        if not self.stop_event.is_set():
            self.root.after(200, self._process_logs)
        self._snap_to_content()

    def _configure_style(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background=BG_DARK)
        style.configure("Card.TFrame", background=BG_CARD)
        style.configure("TLabel", background=BG_DARK, foreground=FG_PRIMARY, font=BASE_FONT)
        style.configure("Card.TLabel", background=BG_CARD, foreground=FG_PRIMARY)
        style.configure("TButton", font=BASE_FONT, padding=10, background=ACCENT_COLOR, foreground=BG_DARK)
        style.map("TButton", background=[("active", "#4ADEDE")])
        style.configure("TCombobox", fieldbackground=BG_CARD, background=BG_CARD, foreground=FG_PRIMARY)
        style.configure("Horizontal.TProgressbar", troughcolor=BG_CARD, background=ACCENT_COLOR)

    def _build_option_cards(self) -> None:
        card = ttk.Frame(self.root, style="Card.TFrame")
        card.grid(row=1, column=0, columnspan=4, sticky="ew", padx=18, pady=12)
        card.columnconfigure(1, weight=1)

        ttk.Label(card, text="GPU / Accelerator:", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=12, pady=12)
        gpu_combo = ttk.Combobox(card, textvariable=self.gpu_var, values=list(GPU_PROFILES.keys()), state="readonly", font=BASE_FONT)
        gpu_combo.grid(row=0, column=1, sticky="ew", padx=12, pady=12)

        ttk.Label(card, text="Output Resolution:", style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=12, pady=12)
        res_combo = ttk.Combobox(
            card,
            textvariable=self.resolution_var,
            values=list(RESOLUTION_OPTIONS.keys()),
            state="readonly",
            font=BASE_FONT,
        )
        res_combo.grid(row=1, column=1, sticky="ew", padx=12, pady=12)

        ttk.Label(card, text="Download / Cache Directory:", style="Card.TLabel").grid(row=2, column=0, sticky="w", padx=12, pady=12)
        dir_entry = ttk.Entry(card, textvariable=self.download_dir, font=BASE_FONT)
        dir_entry.grid(row=2, column=1, sticky="ew", padx=12, pady=12)
        ttk.Button(card, text="Browse", command=self._choose_directory).grid(row=2, column=2, padx=12, pady=12)

        ttk.Label(card, text="Kodi HTTP Base:", style="Card.TLabel").grid(row=3, column=0, sticky="w", padx=12, pady=12)
        host_entry = ttk.Entry(card, textvariable=self.host_var, font=BASE_FONT)
        host_entry.grid(row=3, column=1, sticky="ew", padx=12, pady=12)
        port_entry = ttk.Entry(card, textvariable=self.port_var, width=6, font=BASE_FONT)
        port_entry.grid(row=3, column=2, sticky="ew", padx=12, pady=12)

        ttk.Label(card, text="Kodi Credentials (optional):", style="Card.TLabel").grid(row=4, column=0, sticky="w", padx=12, pady=12)
        user_entry = ttk.Entry(card, textvariable=self.user_var, font=BASE_FONT)
        user_entry.grid(row=4, column=1, sticky="ew", padx=12, pady=12)
        pass_entry = ttk.Entry(card, textvariable=self.password_var, show="*", font=BASE_FONT)
        pass_entry.grid(row=4, column=2, sticky="ew", padx=12, pady=12)

    def _build_host_section(self) -> None:
        host_frame = ttk.Frame(self.root, style="Card.TFrame")
        host_frame.grid(row=2, column=0, columnspan=4, sticky="nsew", padx=18, pady=12)
        host_frame.columnconfigure(0, weight=1)

        ttk.Label(host_frame, text="Discovered Kodi Hosts", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=12, pady=(12, 6))
        self.host_list = tk.Listbox(host_frame, font=BASE_FONT, height=4, selectmode=tk.SINGLE, bg=BG_DARK, fg=FG_PRIMARY, activestyle="none")
        self.host_list.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self.host_list.bind("<<ListboxSelect>>", self._on_host_select)

    def _build_action_buttons(self) -> None:
        button_frame = ttk.Frame(self.root, style="Card.TFrame")
        button_frame.grid(row=3, column=0, columnspan=4, sticky="ew", padx=18, pady=12)
        button_frame.columnconfigure((0, 1, 2), weight=1)

        ttk.Button(button_frame, text="Auto-Detect Kodi", command=self._start_detection).grid(row=0, column=0, padx=12, pady=12, sticky="ew")
        ttk.Button(button_frame, text="Test Connection", command=self._test_connection).grid(row=0, column=1, padx=12, pady=12, sticky="ew")
        ttk.Button(button_frame, text="Stream to Kodi", command=self._start_stream).grid(row=0, column=2, padx=12, pady=12, sticky="ew")

    def _build_logger(self) -> None:
        log_frame = ttk.Frame(self.root, style="Card.TFrame")
        log_frame.grid(row=4, column=0, columnspan=4, sticky="nsew", padx=18, pady=(0, 18))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_widget = scrolledtext.ScrolledText(log_frame, font=BASE_FONT, bg=BG_DARK, fg=FG_SECONDARY, height=10, wrap=tk.WORD, state=tk.DISABLED)
        self.log_widget.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        for tag, color in {"success": LOG_SUCCESS, "warn": LOG_WARN, "error": LOG_ERROR}.items():
            self.log_widget.tag_config(tag, foreground=color)

    def _snap_to_content(self) -> None:
        self.root.update_idletasks()
        width = self.root.winfo_reqwidth()
        height = self.root.winfo_reqheight()
        self.root.minsize(width, height)
        self.root.geometry(f"{width}x{height}")

    def _choose_directory(self) -> None:
        directory = filedialog.askdirectory(initialdir=self.download_dir.get())
        if directory:
            self.download_dir.set(directory)
            self.http_server.restart(Path(directory))
            self._log(f"HTTP server root set to {directory}", "success")

    def _on_host_select(self, _: tk.Event) -> None:
        selection = self.host_list.curselection()
        if not selection:
            return
        idx = selection[0]
        if 0 <= idx < len(self.discovered):
            endpoint = self.discovered[idx]
            self.host_var.set(endpoint.host)
            self.port_var.set(str(endpoint.port))

    def _start_detection(self) -> None:
        if self.stop_event.is_set():
            return
        locator = KodiLocator(lambda msg: self._log(msg, "warn"))
        host_hint = self.host_var.get().strip()
        thread = threading.Thread(target=self._run_detection, args=(locator, host_hint), daemon=True)
        self.discovery_threads.add(thread)
        thread.start()

    def _run_detection(self, locator: KodiLocator, host_hint: str) -> None:
        try:
            try:
                endpoints = locator.discover(stop_event=self.stop_event, manual_host=host_hint)
            except Exception as exc:
                self._log(f"Auto-detect failed: {exc}", "error")
                endpoints = []
            if self.stop_event.is_set():
                return
            self.discovered = endpoints
            values = [ep.label() for ep in endpoints]
            self.root.after(0, lambda: self._refresh_host_list(values))
        finally:
            self.discovery_threads.discard(threading.current_thread())

    def _refresh_host_list(self, values: List[str]) -> None:
        self.host_list.delete(0, tk.END)
        for value in values:
            self.host_list.insert(tk.END, value)
        if values and self.discovered:
            self.host_list.selection_set(0)
            first = self.discovered[0]
            self.host_var.set(first.host)
            self.port_var.set(str(first.port))

    def _test_connection(self) -> None:
        endpoint = self._current_endpoint()
        if not endpoint:
            messagebox.showwarning("Kodi Streamer", "Provide a Kodi host first.")
            return
        thread = threading.Thread(target=self._run_ping, args=(endpoint,), daemon=True)
        thread.start()

    def _run_ping(self, endpoint: KodiEndpoint) -> None:
        try:
            client = KodiRPCClient(endpoint, self.user_var.get(), self.password_var.get())
            if client.ping():
                self._log(f"Connected to Kodi @ {endpoint.label()}", "success")
            else:
                self._log("Kodi responded but did not return 'pong'.", "warn")
        except Exception as exc:
            self._log(f"Connection failed: {exc}", "error")

    def _start_stream(self) -> None:
        urls = [line.strip() for line in self.url_text.get("1.0", tk.END).splitlines() if line.strip()]
        if not urls:
            messagebox.showinfo("Kodi Streamer", "Enter at least one YouTube URL.")
            return
        endpoint = self._current_endpoint()
        if not endpoint:
            messagebox.showwarning("Kodi Streamer", "Provide Kodi host and port before streaming.")
            return
        planner = StreamPlanner(Path(self.download_dir.get()), self._log, self.http_server)
        target_height = RESOLUTION_OPTIONS.get(self.resolution_var.get(), 480)
        args = (
            urls,
            endpoint,
            planner,
            self.gpu_var.get(),
            target_height,
            self.user_var.get(),
            self.password_var.get(),
        )
        thread = threading.Thread(target=self._run_stream, args=args, daemon=True)
        thread.start()

    def _run_stream(
        self,
        urls: List[str],
        endpoint: KodiEndpoint,
        planner: StreamPlanner,
        gpu_profile: str,
        target_height: int,
        username: str,
        password: str,
    ) -> None:
        try:
            client = KodiRPCClient(endpoint, username, password)
        except Exception as exc:
            self._log(f"Cannot initialize Kodi RPC client: {exc}", "error")
            return
        try:
            if not client.ping():
                self._log("Kodi responded but did not return 'pong' during preflight ping.", "warn")
                return
        except Exception as exc:
            self._log(f"Unable to reach Kodi before streaming: {exc}", "error")
            return
        for url in urls:
            try:
                plan = planner.prepare(url, gpu_profile, target_height)
                client.play_stream(plan["url"])
                self._log(f"Streaming {plan['title']} via {plan['mode']} mode", "success")
            except Exception as exc:
                self._log(f"Failed to stream {url}: {exc}", "error")

    def _current_endpoint(self) -> Optional[KodiEndpoint]:
        host = self.host_var.get().strip()
        port_str = self.port_var.get().strip()
        if not host or not port_str:
            return None
        try:
            port = int(port_str)
        except ValueError:
            self._log("Port must be an integer", "error")
            return None
        return KodiEndpoint(host=host, port=port, name="Manual")

    def _log(self, message: str, level: str = "info") -> None:
        timestamp = time.strftime("%H:%M:%S")
        formatted = f"[{timestamp}] {message}\n"
        self.log_queue.put((formatted, level))

    def _process_logs(self) -> None:
        while not self.log_queue.empty():
            message, level = self.log_queue.get()
            self.log_widget.configure(state=tk.NORMAL)
            if level in ("success", "warn", "error"):
                self.log_widget.insert(tk.END, message, level)
            else:
                self.log_widget.insert(tk.END, message)
            self.log_widget.configure(state=tk.DISABLED)
            self.log_widget.see(tk.END)
        if not self.stop_event.is_set():
            self.root.after(200, self._process_logs)

    def _on_close(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        for thread in list(self.discovery_threads):
            thread.join(timeout=1)
        self.http_server.stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    KodiStreamGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
