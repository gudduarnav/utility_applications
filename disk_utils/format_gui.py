#!/usr/bin/env python3
"""
format-gui - Windows 7 style formatting helper for removable drives on Linux.

This utility enumerates USB drives, allows choosing a filesystem,
supports quick/full formats, and shows progress/log output.
Run it with sudo/pkexec so it has permission to format block devices.
"""

import json
import os
import shlex
import shutil
import subprocess
import threading
from datetime import datetime
from typing import Dict, List, Optional

import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, scrolledtext, ttk


class OperationCancelled(Exception):
    """Raised when the user cancels the formatting process."""


class FormatGUI:
    FILESYSTEM_ORDER = ("exfat", "ntfs", "fat32", "ext4", "ext3", "fat16")
    FILESYSTEM_INFO = {
        "exfat": {"cmd": ["mkfs.exfat"], "label_flag": "-n", "label_max": 15},
        "ntfs": {"cmd": ["mkfs.ntfs", "-f"], "label_flag": "-L", "label_max": 32},
        "fat32": {"cmd": ["mkfs.vfat", "-F", "32"], "label_flag": "-n", "label_max": 11},
        "ext4": {"cmd": ["mkfs.ext4", "-F"], "label_flag": "-L", "label_max": 16},
        "ext3": {"cmd": ["mkfs.ext3", "-F"], "label_flag": "-L", "label_max": 16},
        "fat16": {"cmd": ["mkfs.vfat", "-F", "16"], "label_flag": "-n", "label_max": 11},
    }

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("format-gui - Format Removable Disk")

        style = ttk.Style()
        for theme in ("vista", "xpnative", "clam", "default"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        self.fixed_font: Optional[tkfont.Font] = None
        self._apply_large_fonts()

        self.devices: Dict[str, Dict[str, str]] = {}
        self.available_filesystems: List[str] = []
        self.operation_thread: Optional[threading.Thread] = None
        self.current_process: Optional[subprocess.Popen] = None
        self.cancel_requested = False

        self.device_var = tk.StringVar()
        self.fs_var = tk.StringVar()
        self.format_mode = tk.StringVar(value="quick")
        self.label_var = tk.StringVar()
        self.progress_var = tk.DoubleVar(value=0.0)

        self._build_ui()
        self._configure_window_geometry()
        self._update_filesystem_choices()
        if self._running_as_root():
            self.refresh_devices()
        else:
            self._prompt_for_admin_rights()

    # ------------------------------------------------------------------ UI ---
    def _apply_large_fonts(self) -> None:
        base_size = 16
        for family in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                font_obj = tkfont.nametofont(family)
            except tk.TclError:
                continue
            font_obj.configure(size=base_size)
        try:
            fixed = tkfont.nametofont("TkFixedFont")
            fixed.configure(size=base_size)
        except tk.TclError:
            fixed = tkfont.Font(family="Consolas", size=base_size)
        self.fixed_font = fixed

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=16)
        main.pack(fill="both", expand=True)

        # Device row
        device_frame = ttk.Frame(main)
        device_frame.pack(fill="x", pady=(0, 10))
        ttk.Label(device_frame, text="Device:").pack(side="left")
        self.device_combo = ttk.Combobox(
            device_frame,
            textvariable=self.device_var,
            state="readonly",
            width=45,
        )
        self.device_combo.pack(side="left", padx=8, fill="x", expand=True)
        ttk.Button(device_frame, text="Refresh", command=self.refresh_devices).pack(
            side="left"
        )

        # Filesystem row
        fs_frame = ttk.Frame(main)
        fs_frame.pack(fill="x", pady=(0, 10))
        ttk.Label(fs_frame, text="File system:").pack(side="left")
        self.fs_combo = ttk.Combobox(
            fs_frame,
            textvariable=self.fs_var,
            state="readonly",
            width=20,
        )
        self.fs_combo.pack(side="left", padx=8)

        # Label row
        label_frame = ttk.Frame(main)
        label_frame.pack(fill="x", pady=(0, 10))
        ttk.Label(label_frame, text="Volume label:").pack(side="left")
        ttk.Entry(label_frame, textvariable=self.label_var, width=30).pack(
            side="left", padx=8
        )

        # Format options
        options = ttk.Labelframe(main, text="Format options")
        options.pack(fill="x", pady=(0, 10))
        ttk.Radiobutton(
            options, text="Quick format", variable=self.format_mode, value="quick"
        ).pack(anchor="w", pady=2, padx=8)
        ttk.Radiobutton(
            options,
            text="Full format (zero entire drive)",
            variable=self.format_mode,
            value="full",
        ).pack(anchor="w", pady=2, padx=8)

        # Progress area
        progress_frame = ttk.Frame(main)
        progress_frame.pack(fill="x", pady=(0, 4))
        ttk.Label(progress_frame, text="Progress:").pack(anchor="w")
        self.progress_bar = ttk.Progressbar(
            progress_frame,
            variable=self.progress_var,
            orient="horizontal",
            length=400,
            mode="determinate",
            maximum=100,
        )
        self.progress_bar.pack(fill="x", expand=True, pady=(4, 0))

        # Log view
        log_frame = ttk.Frame(main)
        log_frame.pack(fill="both", expand=True, pady=(10, 8))
        ttk.Label(log_frame, text="Log:").pack(anchor="w")
        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            height=12,
            state="disabled",
            font=self.fixed_font or ("Consolas", 16),
        )
        self.log_text.pack(fill="both", expand=True, pady=(4, 0))

        # Buttons
        button_frame = ttk.Frame(main)
        button_frame.pack(fill="x", pady=(8, 0))
        self.format_button = ttk.Button(
            button_frame, text="Format", command=self.start_format
        )
        self.format_button.pack(side="left")
        self.cancel_button = ttk.Button(
            button_frame, text="Cancel", command=self.cancel_format, state="disabled"
        )
        self.cancel_button.pack(side="left", padx=8)
        ttk.Button(button_frame, text="Close", command=self.root.destroy).pack(
            side="right"
        )

    def _configure_window_geometry(self) -> None:
        """Make sure the window is tall enough for the enlarged widgets."""
        self.root.update_idletasks()
        padding = 32
        req_w = self.root.winfo_reqwidth()
        req_h = self.root.winfo_reqheight()
        width = max(780, req_w + padding)
        height = max(720, req_h + padding)
        self.root.minsize(width, height)
        self.root.geometry(f"{width}x{height}")

    def _update_filesystem_choices(self) -> None:
        available: List[str] = []
        missing: List[str] = []
        for fs_name in self.FILESYSTEM_ORDER:
            info = self.FILESYSTEM_INFO[fs_name]
            tool = info["cmd"][0]
            if shutil.which(tool):
                available.append(fs_name)
            else:
                missing.append(fs_name)
        self.available_filesystems = available
        if available:
            self.fs_combo["values"] = available
            current = self.fs_var.get()
            if current in available:
                self.fs_combo.set(current)
            else:
                self.fs_combo.current(0)
                self.fs_var.set(available[0])
            if missing:
                hidden = ", ".join(name.upper() for name in missing)
                self.log_message(f"Unavailable filesystem tools hidden: {hidden}")
        else:
            self.fs_combo["values"] = ()
            self.fs_var.set("")
            self.log_message(
                "No supported mkfs utilities detected. Install mkfs.exfat, mkfs.ntfs, mkfs.vfat, or mkfs.extX."
            )
            self._show_dialog_async(
                messagebox.showerror,
                "No format tools",
                "No supported mkfs utilities were found in PATH.\nInstall at least one filesystem tool and restart.",
            )
        self._set_controls_enabled(True)

    def _prompt_for_admin_rights(self) -> None:
        self._set_controls_enabled(False)
        self.cancel_button.config(state="disabled")
        self.log_message("Administrator privileges are required to format removable drives.")

        def alert_user() -> None:
            messagebox.showwarning(
                "Administrator privileges required",
                "This utility must be run with sudo or pkexec so it can access block devices.\n"
                "Close this window and restart the application with elevated privileges.",
            )

        self.root.after(0, alert_user)

    # ----------------------------------------------------------- Device logic -
    def refresh_devices(self) -> None:
        try:
            result = subprocess.check_output(
                [
                    "lsblk",
                    "-J",
                    "-o",
                    "NAME,KNAME,SIZE,TYPE,MOUNTPOINT,MODEL,TRAN,HOTPLUG,LABEL,FSTYPE",
                ],
                text=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            messagebox.showerror(
                "lsblk unavailable",
                f"Unable to query block devices via lsblk: {exc}",
            )
            return

        data = json.loads(result)
        device_names: List[str] = []
        self.devices.clear()

        for dev in data.get("blockdevices", []):
            if not self._is_usb_disk(dev):
                continue
            display = self._format_device_display(dev)
            self.devices[display] = {
                "path": f"/dev/{dev.get('kname') or dev.get('name')}",
                "size_str": dev.get("size") or "unknown",
                "model": dev.get("model") or "",
            }
            device_names.append(display)

        if device_names:
            self.device_combo["values"] = device_names
            self.device_combo.current(0)
        else:
            self.device_combo["values"] = ()
            self.device_var.set("")
            messagebox.showinfo(
                "No USB drives",
                "No removable USB drives were detected. Insert a drive and click Refresh.",
            )

    def _is_usb_disk(self, dev: Dict[str, object]) -> bool:
        if (dev.get("type") or "").lower() != "disk":
            return False
        tran = (dev.get("tran") or "").lower()
        hotplug = str(dev.get("hotplug") or "0")
        return tran == "usb" or hotplug == "1"

    def _format_device_display(self, dev: Dict[str, object]) -> str:
        name = dev.get("name") or "unknown"
        size = dev.get("size") or "unknown"
        model = (dev.get("model") or "").strip()
        label = dev.get("label") or ""
        fs = dev.get("fstype") or ""
        model_part = f"{model} " if model else ""
        label_part = f" [{label}]" if label else ""
        fs_part = f" ({fs})" if fs else ""
        return f"{name} - {model_part}{size}{label_part}{fs_part}".strip()

    # ----------------------------------------------------------- Format logic -
    def start_format(self) -> None:
        if self.operation_thread and self.operation_thread.is_alive():
            messagebox.showwarning(
                "Busy",
                "A format operation is already running. Cancel it before starting a new one.",
            )
            return

        selected = self.device_var.get()
        if not selected or selected not in self.devices:
            messagebox.showwarning("Select drive", "Please choose a USB drive to format.")
            return

        if not self._running_as_root():
            messagebox.showerror(
                "Root required",
                "Formatting block devices requires administrator privileges.\n"
                "Please run this utility with sudo or pkexec.",
            )
            return

        device_info = self.devices[selected]
        if not self.available_filesystems:
            messagebox.showerror(
                "No filesystem tools",
                "No supported mkfs utilities were detected. Install the necessary tools and restart this utility.",
            )
            return

        fs_choice = self.fs_var.get()
        label_text = self.label_var.get().strip()
        format_mode = self.format_mode.get()

        if not fs_choice:
            messagebox.showwarning("Select filesystem", "Choose a filesystem type before formatting.")
            return

        info = self.FILESYSTEM_INFO.get(fs_choice)
        if info is None:
            messagebox.showerror("Invalid filesystem", "Choose a valid filesystem type.")
            return

        tool = info["cmd"][0]
        if fs_choice not in self.available_filesystems or not shutil.which(tool):
            messagebox.showerror(
                "Filesystem unavailable",
                f"{fs_choice.upper()} formatting is not available right now.\n"
                f"Install {tool} and try again.",
            )
            self._update_filesystem_choices()
            return

        if format_mode not in ("quick", "full"):
            format_mode = "quick"

        if not messagebox.askyesno(
            "Confirm format",
            f"All data on {device_info['path']} will be permanently erased.\n"
            f"Format as {fs_choice.upper()}?",
        ):
            return

        self.cancel_requested = False
        self._set_controls_enabled(False)
        self.cancel_button.config(state="normal")
        self.progress_var.set(0.0)
        self.log_message("------------------------------------------------------------")
        self.log_message(
            f"Formatting {device_info['path']} as {fs_choice.upper()} "
            f"({'quick' if format_mode == 'quick' else 'full'})"
        )

        self.operation_thread = threading.Thread(
            target=self._format_worker,
            args=(device_info, fs_choice, label_text, format_mode),
            daemon=True,
        )
        self.operation_thread.start()

    def cancel_format(self) -> None:
        if not (self.operation_thread and self.operation_thread.is_alive()):
            return
        if self.cancel_requested:
            return
        self.cancel_requested = True
        self.log_message("Cancellation requested… attempting to stop current operation.")
        if self.current_process and self.current_process.poll() is None:
            try:
                self.current_process.terminate()
            except ProcessLookupError:
                pass

    def _format_worker(
        self,
        device_info: Dict[str, str],
        fs_choice: str,
        label_text: str,
        mode: str,
    ) -> None:
        device_path = device_info["path"]
        try:
            self._unmount_partitions(device_path)
            if mode == "full":
                self._perform_full_format(device_path)
            else:
                self.set_progress(40)
            self._make_filesystem(device_path, fs_choice, label_text)
            self.set_progress(100)
            self.log_message("Format completed successfully.")
            self._show_dialog_async(
                messagebox.showinfo,
                "Format complete",
                f"{device_path} formatted as {fs_choice.upper()}.",
            )
        except OperationCancelled:
            self.log_message("Operation cancelled by user.")
            self._show_dialog_async(messagebox.showinfo, "Cancelled", "Formatting cancelled.")
        except Exception as exc:
            self.log_message(f"Error: {exc}")
            self._show_dialog_async(
                messagebox.showerror, "Formatting failed", f"Formatting failed: {exc}"
            )
        finally:
            self.current_process = None
            self.operation_thread = None
            self.cancel_requested = False
            self.root.after(0, lambda: self.cancel_button.config(state="disabled"))
            self.root.after(0, lambda: self._set_controls_enabled(True))

    def _unmount_partitions(self, device_path: str) -> None:
        self.log_message("Checking for mounted partitions…")
        try:
            data = subprocess.check_output(
                ["lsblk", "-J", "-o", "NAME,MOUNTPOINT", device_path], text=True
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"lsblk failed while checking partitions: {exc}") from exc

        block = json.loads(data).get("blockdevices", [])
        if not block:
            self.set_progress(20)
            return
        partitions = block[0].get("children") or []
        for part in partitions:
            mountpoint = part.get("mountpoint")
            part_name = part.get("name")
            if not mountpoint:
                continue
            target = f"/dev/{part_name}"
            self.log_message(f"Unmounting {target} (mounted at {mountpoint})…")
            self._run_command(["umount", target], fail_ok=False)
        self.set_progress(20)

    def _perform_full_format(self, device_path: str) -> None:
        size_bytes = self._get_device_size(device_path)
        if not size_bytes:
            self.log_message("Unable to determine device size; falling back to quick format.")
            self.set_progress(40)
            return

        self.log_message("Full format selected – zeroing the entire device. This may take a while.")
        chunk_size = 4 * 1024 * 1024  # 4 MiB
        zero_chunk = b"\x00" * chunk_size
        written = 0
        fsync_interval = 256 * 1024 * 1024
        sync_progress = 0
        fd = os.open(device_path, os.O_WRONLY)
        try:
            while written < size_bytes:
                if self.cancel_requested:
                    raise OperationCancelled()
                remaining = size_bytes - written
                to_write = zero_chunk if remaining >= chunk_size else zero_chunk[:remaining]
                os.write(fd, to_write)
                written += len(to_write)
                sync_progress += len(to_write)
                if sync_progress >= fsync_interval or written >= size_bytes:
                    os.fsync(fd)
                    sync_progress = 0
                progressed = 20 + (written / size_bytes) * 55  # stays below mkfs stage
                self.set_progress(progressed)
                if written and (
                    written == size_bytes or written % (512 * 1024 * 1024) == 0
                ):
                    self.log_message(
                        f"Zeroed {self._format_bytes(written)} of {self._format_bytes(size_bytes)}"
                    )
        finally:
            os.close(fd)
        self.log_message("Zero fill completed.")

    def _make_filesystem(self, device_path: str, fs_choice: str, label_text: str) -> None:
        self.log_message(f"Creating {fs_choice.upper()} filesystem…")
        label_text = self._sanitize_label(label_text, fs_choice)
        info = self.FILESYSTEM_INFO.get(fs_choice)
        if info is None:
            raise RuntimeError(f"Unsupported filesystem: {fs_choice}")
        cmd: List[str] = list(info["cmd"])
        label_flag = info.get("label_flag")
        if label_text and label_flag:
            cmd.extend([label_flag, label_text])
        cmd.append(device_path)
        self.set_progress(80)
        self._run_command(cmd, fail_ok=False)

    def _run_command(self, cmd: List[str], fail_ok: bool = False) -> None:
        self.log_message(f"$ {' '.join(shlex.quote(part) for part in cmd)}")
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"Required tool not found: {cmd[0]}") from exc

        self.current_process = process
        for line in process.stdout or []:
            if self.cancel_requested:
                process.terminate()
                process.wait()
                self.current_process = None
                raise OperationCancelled()
            clean = line.rstrip()
            if clean:
                self.log_message(clean)
        return_code = process.wait()
        self.current_process = None
        if self.cancel_requested:
            raise OperationCancelled()
        if return_code != 0 and not fail_ok:
            raise RuntimeError(f"Command failed (exit code {return_code}).")

    # ----------------------------------------------------------- Utilities ---
    def _set_controls_enabled(self, enabled: bool) -> None:
        device_state = "normal" if enabled else "disabled"
        fs_state = "readonly" if (enabled and self.available_filesystems) else "disabled"
        format_state = "normal" if (enabled and self.available_filesystems) else "disabled"
        self.device_combo.config(state=device_state)
        self.fs_combo.config(state=fs_state)
        self.format_button.config(state=format_state)

    def log_message(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        text = f"[{timestamp}] {message}\n"

        def append() -> None:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", text)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        self.root.after(0, append)

    def set_progress(self, value: float) -> None:
        self.root.after(0, lambda: self.progress_var.set(min(max(value, 0.0), 100.0)))

    def _show_dialog_async(self, dialog, title: str, message: str) -> None:
        self.root.after(0, lambda: dialog(title, message))

    def _sanitize_label(self, label: str, fs_choice: str) -> str:
        if not label:
            return ""
        cleaned = "".join(ch for ch in label if ch.isalnum() or ch in ("-", "_"))
        info = self.FILESYSTEM_INFO.get(fs_choice)
        max_len = info.get("label_max") if info else None
        if max_len:
            cleaned = cleaned[:max_len]
        return cleaned

    def _get_device_size(self, device_path: str) -> int:
        try:
            out = subprocess.check_output(
                ["lsblk", "-b", "-dn", "-o", "SIZE", device_path], text=True
            )
        except subprocess.CalledProcessError:
            return 0
        try:
            return int(out.strip())
        except ValueError:
            return 0

    def _format_bytes(self, count: int) -> str:
        suffixes = ["B", "KiB", "MiB", "GiB", "TiB"]
        value = float(count)
        idx = 0
        while value >= 1024 and idx < len(suffixes) - 1:
            value /= 1024
            idx += 1
        return f"{value:.1f} {suffixes[idx]}"

    def _running_as_root(self) -> bool:
        if hasattr(os, "geteuid"):
            return os.geteuid() == 0
        return False


def main() -> None:
    root = tk.Tk()
    app = FormatGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
