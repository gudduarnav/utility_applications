#!/usr/bin/env python3
"""Beautiful Tkinter based Kodi remote control application.

This module contains a fully featured GUI client that can discover Kodi
instances on the local network, connect to a selected host, and act as a remote
control using Kodi's JSON-RPC interface. The interface focuses on clarity and
simplicity with sizeable controls suitable for touch or pointer devices.
"""
from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import subprocess
import socket
import sys
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from tkinter import messagebox, simpledialog, ttk
from typing import Any, Dict, List, Optional, Sequence, Tuple
import urllib.error
import urllib.request


DISCOVERY_MAX_TARGETS = 768
MANUAL_SCAN_LIMIT = 1024
KNOWN_KODI_HOSTS = ("localhost", "kodi", "kodi.local", "osmc", "libreelec", "libreelec.local")


class KodiConnectionError(RuntimeError):
    """Raised when Kodi cannot be reached."""


class KodiCommandError(RuntimeError):
    """Raised when Kodi responds with an error."""


@dataclass
class KodiVersion:
    """Simple representation of a Kodi instance version."""

    major: int
    minor: int
    revision: str

    def label(self) -> str:
        return f"Kodi {self.major}.{self.minor}{self.revision}".strip()


def _format_timecode(time_dict: Optional[Dict[str, int]]) -> str:
    """Convert Kodi's time structure to HH:MM:SS."""

    if not time_dict:
        return "00:00:00"
    hours = int(time_dict.get("hours", 0))
    minutes = int(time_dict.get("minutes", 0))
    seconds = int(time_dict.get("seconds", 0))
    total_minutes, seconds = divmod(seconds, 60)
    minutes += total_minutes
    total_hours, minutes = divmod(minutes, 60)
    hours += total_hours
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


class KodiClient:
    """Lightweight Kodi JSON-RPC client built on urllib."""

    def __init__(
        self,
        host: str,
        port: int = 8080,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.username = username or ""
        self.password = password or ""
        self.url = f"http://{self.host}:{self.port}/jsonrpc"
        if self.username:
            token = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8"))
            self._auth_header = f"Basic {token.decode('ascii')}"
        else:
            self._auth_header = None

    def _perform_request(self, method: str, params: Optional[Dict] = None, timeout: float = 3.0) -> Dict:
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode(
            "utf-8"
        )
        headers = {"Content-Type": "application/json", "User-Agent": "KodiRemoteGUI"}
        request = urllib.request.Request(self.url, data=payload, headers=headers)
        if self._auth_header:
            request.add_header("Authorization", self._auth_header)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:  # pragma: no cover - UI handles
            raise KodiConnectionError(f"HTTP error {exc.code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:  # pragma: no cover - UI handles
            raise KodiConnectionError(str(exc.reason)) from exc
        if not body:
            raise KodiConnectionError("Empty response from Kodi")
        data = json.loads(body)
        if "error" in data:
            raise KodiCommandError(data["error"])
        return data

    def get_version(self, timeout: float = 2.0) -> KodiVersion:
        result = self._perform_request("JSONRPC.Version", timeout=timeout)
        version = result.get("result", {}).get("version", {})
        return KodiVersion(major=version.get("major", 0), minor=version.get("minor", 0), revision=str(version.get("revision", "")))

    def ping(self) -> bool:
        try:
            self._perform_request("JSONRPC.Ping")
            return True
        except KodiConnectionError:
            return False

    def send_input(self, method: str) -> None:
        self._perform_request(method)

    def send_text(self, text: str) -> None:
        self._perform_request("Input.SendText", {"text": text, "done": True})

    def execute_action(self, action: str) -> None:
        self._perform_request("Input.ExecuteAction", {"action": action})

    def toggle_play_pause(self) -> str:
        player_id = self._get_first_player_id()
        result = self._perform_request("Player.PlayPause", {"playerid": player_id})
        speed = result.get("result", {}).get("speed")
        return "Playing" if speed else "Paused"

    def stop_playback(self) -> None:
        player_id = self._get_first_player_id()
        self._perform_request("Player.Stop", {"playerid": player_id})

    def set_fullscreen(self) -> None:
        self._perform_request("GUI.SetFullscreen", {"fullscreen": "toggle"})

    def mute(self) -> bool:
        result = self._perform_request("Application.SetMute", {"mute": "toggle"})
        return bool(result.get("result"))

    def volume_step(self, increase: bool) -> None:
        self.execute_action("volumeup" if increase else "volumedown")

    def get_playback_status(self) -> Optional[Dict[str, Any]]:
        response = self._perform_request("Player.GetActivePlayers").get("result", [])
        if not response:
            return None
        player_id = response[0]["playerid"]
        item_request = {
            "playerid": player_id,
            "properties": ["title", "showtitle", "season", "episode", "artist", "album", "file", "label"],
        }
        props_request = {
            "playerid": player_id,
            "properties": ["speed", "time", "totaltime", "percentage", "type"],
        }
        item_result = self._perform_request("Player.GetItem", item_request).get("result", {}).get("item", {})
        props_result = self._perform_request("Player.GetProperties", props_request).get("result", {})

        label = item_result.get("label") or item_result.get("title") or ""
        show = item_result.get("showtitle")
        if show and label:
            label = f"{show} - {label}"
        elif not label and show:
            label = show
        file_path = item_result.get("file") or ""
        state = "Playing" if props_result.get("speed", 0) > 0 else "Paused"
        position = _format_timecode(props_result.get("time"))
        total = _format_timecode(props_result.get("totaltime"))
        percentage = float(props_result.get("percentage", 0.0))
        return {
            "label": label or "Unknown media",
            "file": file_path or "Unknown location",
            "state": state if props_result else "Idle",
            "position": position,
            "total": total,
            "percentage": percentage,
        }

    def _get_first_player_id(self) -> int:
        players = self._perform_request("Player.GetActivePlayers").get("result", [])
        if not players:
            raise KodiCommandError("No active players")
        return players[0]["playerid"]


def fetch_local_ipv4_addresses() -> List[str]:
    """Return a list of IPv4 addresses belonging to the machine."""

    addresses: set[str] = set()
    try:
        hostname = socket.gethostname()
        host_ips = socket.gethostbyname_ex(hostname)[2]
        addresses.update(ip for ip in host_ips if _is_valid_ipv4(ip))
    except socket.gaierror:
        pass

    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        addresses.update(info[4][0] for info in infos if _is_valid_ipv4(info[4][0]))
    except socket.gaierror:
        pass

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        addresses.add(sock.getsockname()[0])
    except OSError:
        pass
    finally:
        try:
            sock.close()
        except Exception:
            pass

    return sorted(addresses)


def fetch_local_networks(local_ips: Optional[Sequence[str]] = None) -> List[ipaddress.IPv4Network]:
    """Return IPv4 networks associated with local interfaces."""

    local_ips = list(local_ips or fetch_local_ipv4_addresses())
    networks: List[ipaddress.IPv4Network] = []
    networks.extend(_fetch_networks_from_system())
    for ip in local_ips:
        try:
            interface = ipaddress.IPv4Interface(f"{ip}/24")
        except ValueError:
            continue
        if not interface.network.is_loopback:
            networks.append(interface.network)
    return _dedupe_networks(networks)


def _fetch_networks_from_system() -> List[ipaddress.IPv4Network]:
    networks: List[ipaddress.IPv4Network] = []
    try:
        output = subprocess.check_output(["ip", "-o", "-4", "addr", "show"], text=True, timeout=2)
    except (FileNotFoundError, subprocess.SubprocessError):
        return networks
    for line in output.splitlines():
        parts = line.split()
        if "inet" not in parts:
            continue
        try:
            cidr = parts[parts.index("inet") + 1]
        except (ValueError, IndexError):
            continue
        if "/" not in cidr:
            continue
        try:
            interface = ipaddress.IPv4Interface(cidr)
        except ValueError:
            continue
        if interface.network.is_loopback:
            continue
        networks.append(interface.network)
    return networks


def _dedupe_networks(networks: Sequence[ipaddress.IPv4Network]) -> List[ipaddress.IPv4Network]:
    unique: List[ipaddress.IPv4Network] = []
    seen: set[Tuple[int, int]] = set()
    for network in networks:
        key = (int(network.network_address), network.prefixlen)
        if key in seen:
            continue
        seen.add(key)
        unique.append(network)
    return unique


def _iter_network_hosts(
    network: ipaddress.IPv4Network, preferred: Optional[ipaddress.IPv4Address] = None
):
    """Yield hosts from a network, prioritizing addresses near the preferred host."""

    first_host = int(network.network_address) + 1
    last_host = int(network.broadcast_address) - 1
    host_count = last_host - first_host + 1
    if host_count <= 0:
        return
    if preferred and preferred not in network:
        preferred = None
    start_index = 0
    if preferred:
        start_index = int(preferred) - first_host
    seen: set[int] = set()

    def emit(idx: int) -> Optional[str]:
        if 0 <= idx < host_count and idx not in seen:
            seen.add(idx)
            return str(ipaddress.IPv4Address(first_host + idx))
        return None

    first_value = emit(start_index)
    if first_value:
        yield first_value
    for offset in range(1, host_count):
        idx = (start_index + offset) % host_count
        value = emit(idx)
        if value:
            yield value


def parse_manual_targets(text: str, limit: int = MANUAL_SCAN_LIMIT) -> List[str]:
    """Parse manual scan instructions into a list of target IP addresses."""

    tokens = [token.strip() for token in text.replace(";", ",").split(",") if token.strip()]
    targets: List[str] = []
    seen: set[str] = set()

    def add_target(ip_text: str) -> bool:
        if ip_text in seen:
            return len(targets) >= limit
        seen.add(ip_text)
        targets.append(ip_text)
        return len(targets) >= limit

    for token in tokens:
        if len(targets) >= limit:
            break
        if "/" in token:
            try:
                network = ipaddress.IPv4Network(token, strict=False)
            except ValueError as exc:
                raise ValueError(f"Invalid CIDR block: {token}") from exc
            for host in network.hosts():
                if add_target(str(host)):
                    break
            continue
        if "-" in token:
            start_str, end_str = token.split("-", 1)
            try:
                start_ip = ipaddress.IPv4Address(start_str.strip())
                end_ip = ipaddress.IPv4Address(end_str.strip())
            except ipaddress.AddressValueError as exc:
                raise ValueError(f"Invalid IP range: {token}") from exc
            start_int = int(start_ip)
            end_int = int(end_ip)
            if end_int < start_int:
                start_int, end_int = end_int, start_int
            for value in range(start_int, end_int + 1):
                if add_target(str(ipaddress.IPv4Address(value))):
                    break
            continue
        if not _is_valid_ipv4(token):
            raise ValueError(f"Invalid IP address: {token}")
        add_target(token)
    return targets


def _is_valid_ipv4(ip: str) -> bool:
    try:
        ipaddress.IPv4Address(ip)
        return True
    except ipaddress.AddressValueError:
        return False


def build_scan_targets(max_targets: int = DISCOVERY_MAX_TARGETS) -> List[str]:
    """Construct IP addresses and hostnames to scan for Kodi instances."""

    local_ips = fetch_local_ipv4_addresses()
    local_ip_objs = []
    for ip in local_ips:
        try:
            local_ip_objs.append(ipaddress.IPv4Address(ip))
        except ipaddress.AddressValueError:
            continue
    networks = fetch_local_networks(local_ips)
    candidates: List[str] = []
    seen: set[str] = set()

    def add_candidate(value: str) -> bool:
        if value in seen:
            return False
        seen.add(value)
        candidates.append(value)
        return len(candidates) >= max_targets

    if add_candidate("127.0.0.1"):
        return candidates
    for host in KNOWN_KODI_HOSTS:
        if add_candidate(host):
            return candidates
    for ip in local_ips:
        if add_candidate(ip):
            return candidates

    for network in networks:
        preferred = next((ip for ip in local_ip_objs if ip in network), None)
        for host in _iter_network_hosts(network, preferred):
            if add_candidate(host):
                return candidates
    return candidates


def discover_kodi_instances(
    port: int = 8080,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout: float = 1.0,
    max_targets: int = DISCOVERY_MAX_TARGETS,
    targets: Optional[Sequence[str]] = None,
) -> List[Tuple[str, KodiVersion]]:
    """Scan the network for reachable Kodi instances."""

    if targets:
        deduped: List[str] = []
        seen: set[str] = set()
        for target in targets:
            if target not in seen:
                seen.add(target)
                deduped.append(target)
            if len(deduped) >= max_targets:
                break
        scan_targets = deduped
    else:
        scan_targets = build_scan_targets(max_targets=max_targets)
    if not scan_targets:
        return []
    found: List[Tuple[str, KodiVersion]] = []

    def probe(ip: str) -> Optional[Tuple[str, KodiVersion]]:
        client = KodiClient(ip, port=port, username=username, password=password)
        try:
            version = client.get_version(timeout=timeout)
        except (KodiConnectionError, KodiCommandError):
            return None
        return ip, version

    worker_total = min(64, max(4, len(scan_targets)))
    with ThreadPoolExecutor(max_workers=worker_total) as executor:
        futures = {executor.submit(probe, ip): ip for ip in scan_targets}
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception:
                continue
            if result:
                found.append(result)
    return found


class KodiRemoteApp:
    """GUI application wrapping the Kodi client."""

    BG_COLOR = "#040d21"
    CARD_COLOR = "#0b1a36"
    ACCENT_COLOR = "#ffb703"
    TEXT_COLOR = "#f4f4f4"
    FONT_FAMILY = "Helvetica"
    BASE_FONT = (FONT_FAMILY, 16)
    BOLD_FONT = (FONT_FAMILY, 16, "bold")
    PLAYBACK_POLL_MS = 3500

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Kodi Remote Control")
        self.root.configure(bg=self.BG_COLOR)
        self.root.option_add("*Font", f"{self.FONT_FAMILY} 16")
        self.client: Optional[KodiClient] = None
        self.discovery_thread: Optional[threading.Thread] = None
        self.discovery_active = False
        self.discovery_mode = "auto"
        self.playback_job: Optional[str] = None
        self.manual_search_btn: Optional[ttk.Button] = None
        self._build_styles()

        self.host_var = tk.StringVar(value="localhost")
        self.port_var = tk.StringVar(value="8080")
        self.username_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Not connected")
        self.discovery_var = tk.StringVar(value="Use Auto Search to locate Kodi on your network.")
        self.manual_scan_var = tk.StringVar()
        self.now_playing_title = tk.StringVar(value="No media playing.")
        self.now_playing_state = tk.StringVar(value="State: Idle")
        self.now_playing_position = tk.StringVar(value="Position: -- / -- (0%)")
        self.now_playing_file = tk.StringVar(value="Source: --")

        self._build_layout()

    def _build_styles(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Remote.TFrame", background=self.CARD_COLOR)
        style.configure("Remote.TLabelframe", background=self.CARD_COLOR, foreground=self.TEXT_COLOR, padding=20)
        style.configure("Remote.TLabelframe.Label", font=self.BOLD_FONT)
        style.configure("Remote.TLabel", background=self.CARD_COLOR, foreground=self.TEXT_COLOR, font=self.BASE_FONT)
        style.configure("Subtle.TLabel", background=self.CARD_COLOR, foreground="#a3b0d0", font=(self.FONT_FAMILY, 14))
        style.configure(
            "Remote.TButton",
            font=self.BOLD_FONT,
            padding=12,
            background="#13254b",
            foreground=self.TEXT_COLOR,
            borderwidth=0,
            focusthickness=3,
            focustcolor=self.ACCENT_COLOR,
        )
        style.map(
            "Remote.TButton",
            background=[("active", "#1e3874"), ("disabled", "#1d2b4a")],
            foreground=[("disabled", "#7c88a8")],
        )
        style.configure("Accent.TButton", background=self.ACCENT_COLOR, foreground="#0b1a36")
        style.map("Accent.TButton", background=[("active", "#ffd166")])
        style.configure("Status.TLabel", background=self.BG_COLOR, foreground=self.TEXT_COLOR, font=(self.FONT_FAMILY, 15))
        style.configure("Remote.TEntry", fieldbackground="#0e244d", foreground=self.TEXT_COLOR)

    def _build_layout(self) -> None:
        main = ttk.Frame(self.root, padding=20, style="Remote.TFrame")
        main.pack(fill=tk.BOTH, expand=True, padx=24, pady=24)

        connection = ttk.LabelFrame(main, text="Connection", style="Remote.TLabelframe")
        connection.grid(row=0, column=0, sticky="nsew", padx=(0, 20))

        controls = ttk.LabelFrame(main, text="Remote", style="Remote.TLabelframe")
        controls.grid(row=0, column=1, sticky="nsew")

        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)
        main.rowconfigure(1, weight=0)

        self._build_connection_panel(connection)
        self._build_remote_controls(controls)

        now_playing = ttk.LabelFrame(main, text="Now Playing", style="Remote.TLabelframe")
        now_playing.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(20, 0))
        self._build_now_playing(now_playing)

        status = ttk.Label(self.root, textvariable=self.status_var, style="Status.TLabel")
        status.pack(side=tk.BOTTOM, fill=tk.X, pady=(0, 12))

        self._fit_window()

    def _build_connection_panel(self, parent: ttk.LabelFrame) -> None:
        parent.columnconfigure(1, weight=1)
        ttk.Label(parent, text="Host", style="Remote.TLabel").grid(row=0, column=0, sticky="w", pady=4)
        host_entry = ttk.Entry(parent, textvariable=self.host_var, font=self.BASE_FONT, width=18)
        host_entry.grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(parent, text="Port", style="Remote.TLabel").grid(row=1, column=0, sticky="w", pady=4)
        port_entry = ttk.Entry(parent, textvariable=self.port_var, font=self.BASE_FONT, width=10)
        port_entry.grid(row=1, column=1, sticky="w", pady=4)

        ttk.Label(parent, text="Username", style="Remote.TLabel").grid(row=2, column=0, sticky="w", pady=4)
        user_entry = ttk.Entry(parent, textvariable=self.username_var, font=self.BASE_FONT, width=18)
        user_entry.grid(row=2, column=1, sticky="ew", pady=4)

        ttk.Label(parent, text="Password", style="Remote.TLabel").grid(row=3, column=0, sticky="w", pady=4)
        pass_entry = ttk.Entry(parent, textvariable=self.password_var, font=self.BASE_FONT, width=18, show="*")
        pass_entry.grid(row=3, column=1, sticky="ew", pady=4)

        action_row = ttk.Frame(parent, style="Remote.TFrame")
        action_row.grid(row=4, column=0, columnspan=2, pady=(20, 8), sticky="ew")
        action_row.columnconfigure(1, weight=1)

        connect_btn = ttk.Button(action_row, text="Connect", style="Accent.TButton", command=self._connect)
        connect_btn.grid(row=0, column=0, sticky="ew", padx=(0, 10))

        self.search_btn = ttk.Button(action_row, text="Auto Search", style="Remote.TButton", command=self._start_discovery)
        self.search_btn.grid(row=0, column=1, sticky="ew")

        manual_label = ttk.Label(parent, text="Manual scan targets (IP, range, or CIDR)", style="Subtle.TLabel")
        manual_label.grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))

        manual_frame = ttk.Frame(parent, style="Remote.TFrame")
        manual_frame.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        manual_frame.columnconfigure(0, weight=1)
        manual_entry = ttk.Entry(manual_frame, textvariable=self.manual_scan_var, font=self.BASE_FONT)
        manual_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.manual_search_btn = ttk.Button(manual_frame, text="Manual Scan", style="Remote.TButton", command=self._manual_scan)
        self.manual_search_btn.grid(row=0, column=1, sticky="ew")

        self.progress = ttk.Progressbar(parent, mode="indeterminate")
        self.progress.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(10, 6))

        discovery_label = ttk.Label(parent, textvariable=self.discovery_var, style="Subtle.TLabel", wraplength=320)
        discovery_label.grid(row=8, column=0, columnspan=2, pady=(6, 0), sticky="w")

    def _build_remote_controls(self, parent: ttk.LabelFrame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)
        parent.columnconfigure(2, weight=1)

        nav_frame = ttk.Frame(parent, style="Remote.TFrame")
        nav_frame.grid(row=0, column=0, columnspan=3, pady=(0, 20))
        for i in range(3):
            nav_frame.columnconfigure(i, weight=1)
        for i in range(3):
            nav_frame.rowconfigure(i, weight=1)

        self._make_button(nav_frame, "Up", lambda: self._sendInput("Input.Up"), 0, 1)
        self._make_button(nav_frame, "Down", lambda: self._sendInput("Input.Down"), 2, 1)
        self._make_button(nav_frame, "Left", lambda: self._sendInput("Input.Left"), 1, 0)
        self._make_button(nav_frame, "Right", lambda: self._sendInput("Input.Right"), 1, 2)
        self._make_button(nav_frame, "OK", lambda: self._sendInput("Input.Select"), 1, 1)

        extra_frame = ttk.Frame(parent, style="Remote.TFrame")
        extra_frame.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 20))
        extra_frame.columnconfigure((0, 1, 2), weight=1)

        self._make_button(extra_frame, "Back", lambda: self._sendInput("Input.Back"), 0, 0)
        self._make_button(extra_frame, "Home", lambda: self._sendInput("Input.Home"), 0, 1)
        self._make_button(extra_frame, "Info", lambda: self._sendInput("Input.Info"), 0, 2)
        self._make_button(extra_frame, "Context", lambda: self._sendInput("Input.ContextMenu"), 1, 0)
        self._make_button(extra_frame, "OSD", self._show_osd, 1, 1)
        self._make_button(extra_frame, "Keyboard", lambda: self._sendInput("Input.SendText"), 1, 2)

        playback = ttk.Frame(parent, style="Remote.TFrame")
        playback.grid(row=2, column=0, columnspan=3, sticky="ew")
        for idx in range(4):
            playback.columnconfigure(idx, weight=1)

        self._make_button(playback, "Prev", lambda: self._execute_action("skipminus"), 0, 0)
        self._make_button(playback, "Play/Pause", self._toggle_play_pause, 0, 1)
        self._make_button(playback, "Next", lambda: self._execute_action("skipplus"), 0, 2)
        self._make_button(playback, "Stop", self._stop_playback, 0, 3)

        volume_frame = ttk.Frame(parent, style="Remote.TFrame")
        volume_frame.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(20, 0))
        for idx in range(3):
            volume_frame.columnconfigure(idx, weight=1)
        self._make_button(volume_frame, "Vol -", lambda: self._volume(False), 0, 0)
        self._make_button(volume_frame, "Mute", self._mute, 0, 1)
        self._make_button(volume_frame, "Vol +", lambda: self._volume(True), 0, 2)
        self._make_button(volume_frame, "Fullscreen", self._fullscreen, 1, 0, columnspan=3)

    def _build_now_playing(self, parent: ttk.LabelFrame) -> None:
        parent.columnconfigure(0, weight=1)
        ttk.Label(parent, textvariable=self.now_playing_title, style="Remote.TLabel", wraplength=760).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(parent, textvariable=self.now_playing_state, style="Remote.TLabel").grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )
        ttk.Label(parent, textvariable=self.now_playing_position, style="Remote.TLabel").grid(
            row=2, column=0, sticky="w", pady=(6, 0)
        )
        ttk.Label(parent, textvariable=self.now_playing_file, style="Subtle.TLabel", wraplength=760).grid(
            row=3, column=0, sticky="w", pady=(6, 0)
        )

    def _fit_window(self) -> None:
        self.root.update_idletasks()
        width = self.root.winfo_reqwidth()
        height = self.root.winfo_reqheight()
        self.root.minsize(width, height)
        self.root.geometry(f"{width}x{height}")

    def _make_button(
        self,
        parent: ttk.Frame,
        text: str,
        command,
        row: int,
        column: int,
        columnspan: int = 1,
    ) -> None:
        button = ttk.Button(parent, text=text, style="Remote.TButton", command=command)
        button.grid(row=row, column=column, columnspan=columnspan, padx=6, pady=6, sticky="nsew")

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _connect(self) -> None:
        host = self.host_var.get().strip()
        port = self.port_var.get().strip() or "8080"
        username = self.username_var.get().strip() or None
        password = self.password_var.get().strip() or None
        if not host:
            messagebox.showwarning("Kodi Remote", "Please provide a host name or IP address.")
            return

        self._cancel_playback_monitor()
        self.client = None
        self._update_now_playing(None)
        self._set_status(f"Connecting to {host}:{port}...")
        thread = threading.Thread(
            target=self._connect_thread, args=(host, port, username, password), daemon=True
        )
        thread.start()

    def _connect_thread(self, host: str, port: str, username: Optional[str], password: Optional[str]) -> None:
        try:
            client = KodiClient(host, int(port), username, password)
            version = client.get_version()
        except (KodiConnectionError, KodiCommandError) as exc:
            self.root.after(0, lambda: self._set_status(f"Connection failed: {exc}"))
            return
        self.root.after(0, lambda: self._on_connected(client, version, host, port))

    def _on_connected(self, client: KodiClient, version: KodiVersion, host: str, port: str) -> None:
        self.client = client
        self._set_status(f"Connected to {version.label()} @ {host}:{port}")
        self._start_playback_monitor()

    def _sendInput(self, method: str) -> None:
        if method == "Input.SendText":
            self._send_text()
            return
        self._async_call(lambda: self.client.send_input(method) if self.client else None, method)

    def _send_text(self) -> None:
        if not self.client:
            self._set_status("Connect to Kodi before sending text.")
            return
        text = simpledialog.askstring("Send Text", "Text to send to Kodi:", parent=self.root)
        if text:
            self._async_call(lambda: self.client.send_text(text), "SendText")

    def _execute_action(self, action: str) -> None:
        self._async_call(lambda: self.client.execute_action(action) if self.client else None, action)

    def _toggle_play_pause(self) -> None:
        def run() -> Optional[str]:
            if not self.client:
                return None
            return self.client.toggle_play_pause()

        self._async_call(run, "Play/Pause")

    def _stop_playback(self) -> None:
        self._async_call(lambda: self.client.stop_playback() if self.client else None, "Stop")

    def _volume(self, increase: bool) -> None:
        self._async_call(
            lambda: self.client.volume_step(increase) if self.client else None,
            "VolumeUp" if increase else "VolumeDown",
        )

    def _mute(self) -> None:
        self._async_call(lambda: self.client.mute() if self.client else None, "Mute")

    def _fullscreen(self) -> None:
        self._async_call(lambda: self.client.set_fullscreen() if self.client else None, "Fullscreen")

    def _show_osd(self) -> None:
        self._async_call(lambda: self.client.send_input("Input.ShowOSD") if self.client else None, "OSD")

    def _async_call(self, func, label: str) -> None:
        if not self.client:
            self._set_status("Not connected to any Kodi instance.")
            return

        def runner() -> None:
            try:
                result = func()
            except KodiCommandError as exc:
                self.root.after(0, lambda: self._set_status(f"Kodi error: {exc}"))
            except KodiConnectionError as exc:
                self.root.after(0, lambda: self._handle_connection_lost(str(exc)))
            else:
                self.root.after(0, lambda: self._set_status(f"{label} command sent." if result is None else f"{label}: {result}"))

        threading.Thread(target=runner, daemon=True).start()

    def _start_discovery(self, manual_targets: Optional[List[str]] = None, mode: str = "auto") -> None:
        if self.discovery_active:
            return
        self.discovery_active = True
        self.discovery_mode = mode
        self.progress.start(10)
        self.search_btn.config(state=tk.DISABLED)
        if self.manual_search_btn:
            self.manual_search_btn.config(state=tk.DISABLED)
        message = "Manual search in progress..." if manual_targets else "Searching for Kodi devices..."
        self._set_status(message)
        thread = threading.Thread(target=self._discovery_thread, args=(manual_targets,), daemon=True)
        thread.start()
        self.discovery_thread = thread

    def _discovery_thread(self, manual_targets: Optional[Sequence[str]]) -> None:
        try:
            port = int(self.port_var.get() or 8080)
        except ValueError:
            port = 8080
        username = self.username_var.get().strip() or None
        password = self.password_var.get().strip() or None
        devices = discover_kodi_instances(
            port=port,
            username=username,
            password=password,
            max_targets=DISCOVERY_MAX_TARGETS,
            targets=manual_targets,
        )
        self.root.after(0, lambda: self._finish_discovery(devices))

    def _finish_discovery(self, devices: Sequence[Tuple[str, KodiVersion]]) -> None:
        self.discovery_active = False
        self.progress.stop()
        self.search_btn.config(state=tk.NORMAL)
        if self.manual_search_btn:
            self.manual_search_btn.config(state=tk.NORMAL)
        if not devices:
            mode_label = "Manual scan" if self.discovery_mode == "manual" else "Auto search"
            self.discovery_var.set(f"{mode_label} found no devices. Ensure Kodi's web server is enabled and reachable.")
            self._set_status("Discovery finished with no results.")
            return
        options = [f"{host} - {version.label()}" for host, version in devices]
        best_host = devices[0][0]
        self.host_var.set(best_host)
        mode_label = "Manual scan" if self.discovery_mode == "manual" else "Auto search"
        self.discovery_var.set(f"{mode_label} found: " + "; ".join(options[:3]))
        self._set_status(f"Found {len(devices)} Kodi device(s). First selection applied.")

    def _manual_scan(self) -> None:
        manual_text = self.manual_scan_var.get().strip()
        if not manual_text:
            messagebox.showinfo("Kodi Remote", "Enter at least one IP address, range, or CIDR block to scan.")
            return
        try:
            targets = parse_manual_targets(manual_text, limit=DISCOVERY_MAX_TARGETS)
        except ValueError as exc:
            messagebox.showerror("Kodi Remote", str(exc))
            return
        if not targets:
            messagebox.showinfo("Kodi Remote", "No valid targets were parsed from the manual scan input.")
            return
        self._start_discovery(targets, mode="manual")

    def _start_playback_monitor(self) -> None:
        self._cancel_playback_monitor()
        self._poll_playback_status()

    def _cancel_playback_monitor(self) -> None:
        if self.playback_job is not None:
            self.root.after_cancel(self.playback_job)
            self.playback_job = None

    def _poll_playback_status(self) -> None:
        if not self.client:
            self._update_now_playing(None)
            self.playback_job = None
            return

        def worker() -> None:
            try:
                status = self.client.get_playback_status() if self.client else None
            except KodiConnectionError as exc:
                self.root.after(0, lambda: self._handle_connection_lost(str(exc)))
                return
            except KodiCommandError:
                status = None
            self.root.after(0, lambda: self._update_now_playing(status))
            self.root.after(0, self._schedule_next_playback_poll)

        threading.Thread(target=worker, daemon=True).start()

    def _schedule_next_playback_poll(self) -> None:
        if not self.client:
            self.playback_job = None
            return
        self.playback_job = self.root.after(self.PLAYBACK_POLL_MS, self._poll_playback_status)

    def _update_now_playing(self, status: Optional[Dict[str, Any]]) -> None:
        if not status:
            self.now_playing_title.set("No media playing.")
            self.now_playing_state.set("State: Idle")
            self.now_playing_position.set("Position: -- / -- (0%)")
            self.now_playing_file.set("Source: --")
            return
        percentage = float(status.get("percentage", 0.0))
        self.now_playing_title.set(status.get("label", "Unknown media"))
        self.now_playing_state.set(f"State: {status.get('state', 'Idle')}")
        self.now_playing_position.set(
            f"Position: {status.get('position', '--')} / {status.get('total', '--')} ({percentage:.1f}%)"
        )
        self.now_playing_file.set(f"Source: {status.get('file', 'Unknown location')}")

    def _handle_connection_lost(self, message: str) -> None:
        self._cancel_playback_monitor()
        self.client = None
        self._update_now_playing(None)
        self._set_status(f"Lost connection: {message}")

    def run(self) -> None:
        self.root.mainloop()


def do_environment_check() -> int:
    """Verify that Tkinter and required modules are available."""

    print("Python", sys.version)
    try:
        import tkinter  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        print("Tkinter is not available:", exc)
        return 1
    print("Tkinter available. This app is ready for use in the current environment.")
    return 0


def parse_args(args: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Elegant Kodi remote control GUI")
    parser.add_argument("--env-check", action="store_true", help="Verify that the environment supports Tkinter")
    return parser.parse_args(args)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.env_check:
        return do_environment_check()
    app = KodiRemoteApp()
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
