"""Patchwork's desktop window (Tkinter, so it needs nothing beyond Python)."""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__, roblox
from .config import KIND_ASSET, KIND_FILE, Config, Swap, app_dir, parse_asset_ids
from .proxy import ProxyRunner, ca_dir, ensure_ca
from .rules import Hit

REPATCH_MS = 20_000
POLL_MS = 150

FILE_TYPES = [
    ("Roblox-ready files", "*.png *.jpg *.jpeg *.ogg *.mp3 *.wav *.mesh *.rbxm *.rbxmx"),
    ("All files", "*.*"),
]


class SwapDialog(tk.Toplevel):
    """Add or edit one swap."""

    def __init__(self, parent: tk.Misc, swap: Swap | None = None):
        super().__init__(parent)
        self.title("Edit swap" if swap else "Add swap")
        self.configure(background=PALETTE["bg"])
        self.resizable(False, False)
        self.transient(parent)
        self.result: Swap | None = None

        self.name_var = tk.StringVar(value=swap.name if swap else "")
        self.targets_var = tk.StringVar(value=" ".join(map(str, swap.targets)) if swap else "")
        self.kind_var = tk.StringVar(value=swap.kind if swap else KIND_ASSET)
        self.value_var = tk.StringVar(value=swap.value if swap else "")
        self.enabled = swap.enabled if swap else True

        body = ttk.Frame(self, padding=16)
        body.grid(sticky="nsew")
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text="Name").grid(row=0, column=0, sticky="w", pady=4)
        name = ttk.Entry(body, textvariable=self.name_var, width=44)
        name.grid(row=0, column=1, columnspan=2, sticky="ew", pady=4)

        ttk.Label(body, text="Asset IDs to replace").grid(row=1, column=0, sticky="w", pady=4, padx=(0, 12))
        ttk.Entry(body, textvariable=self.targets_var).grid(row=1, column=1, columnspan=2, sticky="ew", pady=4)
        ttk.Label(
            body,
            text="Separate several IDs with spaces or commas. Links and rbxassetid:// work too.",
            style="Hint.TLabel",
        ).grid(row=2, column=1, columnspan=2, sticky="w")

        ttk.Label(body, text="Replace with").grid(row=3, column=0, sticky="w", pady=(12, 4))
        kinds = ttk.Frame(body)
        kinds.grid(row=3, column=1, columnspan=2, sticky="w", pady=(12, 4))
        ttk.Radiobutton(kinds, text="Another asset ID", value=KIND_ASSET, variable=self.kind_var,
                        command=self._kind_changed).pack(side="left")
        ttk.Radiobutton(kinds, text="A file on this PC", value=KIND_FILE, variable=self.kind_var,
                        command=self._kind_changed).pack(side="left", padx=(12, 0))

        ttk.Entry(body, textvariable=self.value_var).grid(row=4, column=1, sticky="ew", pady=4)
        self.browse = ttk.Button(body, text="Browse…", command=self._browse)
        self.browse.grid(row=4, column=2, padx=(6, 0), pady=4)
        self.value_hint = ttk.Label(body, style="Hint.TLabel")
        self.value_hint.grid(row=5, column=1, columnspan=2, sticky="w")

        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, columnspan=3, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Save", style="Accent.TButton", command=self._save).pack(side="right", padx=6)

        self._kind_changed()
        self.bind("<Return>", lambda e: self._save())
        self.bind("<Escape>", lambda e: self.destroy())
        name.focus_set()
        self.grab_set()

    def _kind_changed(self) -> None:
        if self.kind_var.get() == KIND_FILE:
            self.browse.state(["!disabled"])
            self.value_hint.configure(
                text="Use the asset's own format: PNG/JPG for images, OGG/MP3 for sounds, .mesh for meshes."
            )
        else:
            self.browse.state(["disabled"])
            self.value_hint.configure(text="Roblox will download this asset in place of the originals.")

    def _browse(self) -> None:
        path = filedialog.askopenfilename(parent=self, filetypes=FILE_TYPES)
        if path:
            self.value_var.set(path)
            if not self.name_var.get().strip():
                self.name_var.set(Path(path).stem)

    def _save(self) -> None:
        try:
            targets = parse_asset_ids(self.targets_var.get())
            value = self.value_var.get().strip()
            if self.kind_var.get() == KIND_ASSET and value:
                value = str(parse_asset_ids(value)[0])
            if self.kind_var.get() == KIND_FILE and value and not Path(value).is_file():
                raise ValueError("That replacement file doesn't exist.")
            name = self.name_var.get().strip() or f"Swap {targets[0] if targets else ''}".strip()
            self.result = Swap(name=name, targets=targets, kind=self.kind_var.get(), value=value,
                               enabled=self.enabled)
        except ValueError as exc:
            messagebox.showerror("Can't save this swap", str(exc), parent=self)
            return
        self.destroy()


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.home = app_dir()
        self.config = self._load_config()
        self.events: queue.Queue = queue.Queue()
        self.runner = ProxyRunner(
            self.config,
            self.home,
            on_hit=lambda hit: self.events.put(("hit", hit)),
            on_log=lambda level, msg: self.events.put(("log", (level, msg))),
        )
        self.busy = False
        self.ca_pem: str | None = None

        root.title(f"Patchwork {__version__}")
        root.minsize(760, 520)
        self._build()
        self._refresh_swaps()
        self._set_status("Stopped", "idle")
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(POLL_MS, self._poll)
        self._log("Patchwork swaps Roblox assets on this PC only. Add a swap, then press Start.")

    # ----- layout -----------------------------------------------------------------

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=3)
        outer.rowconfigure(5, weight=2)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="Patchwork", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.status = ttk.Label(header, style="Status.TLabel")
        self.status.grid(row=0, column=1, sticky="w", padx=14)
        self.start_btn = ttk.Button(header, text="Start", style="Accent.TButton", command=self._toggle)
        self.start_btn.grid(row=0, column=2, sticky="e")

        ttk.Label(outer, text="Swaps", style="Section.TLabel").grid(row=1, column=0, sticky="w", pady=(14, 4))
        table = ttk.Frame(outer)
        table.grid(row=2, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=("on", "name", "targets", "with"), show="headings",
                                 selectmode="extended")
        for col, label, width, stretch in (
            ("on", "", 50, False),
            ("name", "Name", 180, True),
            ("targets", "Replaces", 200, True),
            ("with", "With", 220, True),
        ):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, stretch=stretch, anchor="center" if col == "on" else "w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-1>", lambda e: self._edit())
        self.tree.bind("<space>", lambda e: self._toggle_enabled())
        self.tree.bind("<Delete>", lambda e: self._remove())

        actions = ttk.Frame(outer)
        actions.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(actions, text="Add swap", command=self._add).pack(side="left")
        ttk.Button(actions, text="Edit", command=self._edit).pack(side="left", padx=6)
        ttk.Button(actions, text="On / off", command=self._toggle_enabled).pack(side="left")
        ttk.Button(actions, text="Remove", command=self._remove).pack(side="left", padx=6)
        ttk.Button(actions, text="Remove certificate", command=self._uninstall).pack(side="right")
        ttk.Button(actions, text="Clear Roblox cache", command=self._clear_cache).pack(side="right", padx=6)
        ttk.Button(actions, text="Open data folder", command=self._open_folder).pack(side="right")

        ttk.Label(outer, text="Activity", style="Section.TLabel").grid(row=4, column=0, sticky="w", pady=(14, 4))
        logframe = ttk.Frame(outer)
        logframe.grid(row=5, column=0, sticky="nsew")
        logframe.columnconfigure(0, weight=1)
        logframe.rowconfigure(0, weight=1)
        self.logbox = tk.Text(logframe, height=8, wrap="word", state="disabled", relief="flat",
                              background=PALETTE["field"], foreground=PALETTE["text"],
                              font=("Consolas", 9), padx=8, pady=6, borderwidth=0)
        self.logbox.grid(row=0, column=0, sticky="nsew")
        self.logbox.tag_configure("hit", foreground=PALETTE["accent"])
        self.logbox.tag_configure("warn", foreground=PALETTE["warn"])
        ls = ttk.Scrollbar(logframe, orient="vertical", command=self.logbox.yview)
        ls.grid(row=0, column=1, sticky="ns")
        self.logbox.configure(yscrollcommand=ls.set)

    # ----- helpers ------------------------------------------------------------------

    def _load_config(self) -> Config:
        try:
            return Config.load()
        except (ValueError, TypeError, OSError) as exc:
            backup = app_dir() / "config.broken.json"
            try:
                shutil.copy(app_dir() / "config.json", backup)
            except OSError:
                pass
            messagebox.showwarning("Patchwork", f"Your swap list couldn't be read ({exc}).\n"
                                   f"A copy was saved to {backup} and Patchwork started with an empty list.")
            return Config()

    def _save(self) -> None:
        try:
            self.config.save()
        except OSError as exc:
            messagebox.showerror("Patchwork", f"Couldn't save your swaps: {exc}")
        self.runner.update_config(self.config)

    def _log(self, msg: str, tag: str | None = None) -> None:
        self.logbox.configure(state="normal")
        self.logbox.insert("end", msg + "\n", tag or ())
        lines = int(self.logbox.index("end-1c").split(".")[0])
        if lines > 500:
            self.logbox.delete("1.0", f"{lines - 500}.0")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def _set_status(self, text: str, state: str) -> None:
        color = {"idle": PALETTE["muted"], "on": PALETTE["accent"], "busy": PALETTE["warn"]}[state]
        self.status.configure(text=text, foreground=color)

    def _selected_indexes(self) -> list[int]:
        return sorted(int(i) for i in self.tree.selection())

    def _refresh_swaps(self) -> None:
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        for index, swap in enumerate(self.config.swaps):
            targets = ", ".join(map(str, swap.targets))
            self.tree.insert("", "end", iid=str(index),
                             values=("On" if swap.enabled else "Off", swap.name, targets, swap.describe()))
        keep = [i for i in selected if self.tree.exists(i)]
        if keep:
            self.tree.selection_set(keep)

    def _apply_hint(self) -> None:
        if self.runner.running:
            self._log("Saved. Rejoin the game (and clear the cache if the asset already loaded) to see the change.")

    # ----- swap list actions ------------------------------------------------------

    def _add(self) -> None:
        dialog = SwapDialog(self.root)
        self.root.wait_window(dialog)
        if dialog.result:
            self.config.swaps.append(dialog.result)
            self._save()
            self._refresh_swaps()
            self._apply_hint()

    def _edit(self) -> None:
        indexes = self._selected_indexes()
        if len(indexes) != 1:
            return
        dialog = SwapDialog(self.root, self.config.swaps[indexes[0]])
        self.root.wait_window(dialog)
        if dialog.result:
            self.config.swaps[indexes[0]] = dialog.result
            self._save()
            self._refresh_swaps()
            self._apply_hint()

    def _toggle_enabled(self) -> None:
        indexes = self._selected_indexes()
        if not indexes:
            return
        turn_on = not all(self.config.swaps[i].enabled for i in indexes)
        for i in indexes:
            self.config.swaps[i].enabled = turn_on
        self._save()
        self._refresh_swaps()
        self._apply_hint()

    def _remove(self) -> None:
        indexes = self._selected_indexes()
        if not indexes:
            return
        names = ", ".join(self.config.swaps[i].name for i in indexes)
        if not messagebox.askyesno("Remove swaps", f"Remove {names}?", parent=self.root):
            return
        for i in reversed(indexes):
            del self.config.swaps[i]
        self.tree.selection_set(())
        self._save()
        self._refresh_swaps()

    # ----- proxy --------------------------------------------------------------------

    def _toggle(self) -> None:
        if self.busy:
            return
        if self.runner.running:
            self._stop()
        else:
            self._start()

    def _patch_roblox(self, quiet: bool) -> None:
        if self.ca_pem is None:
            self.ca_pem = ensure_ca(self.home)
        report = roblox.patch_all(self.ca_pem)
        for label in report.patched:
            self._log(f"Trusted Patchwork's certificate in {label}.")
        for label, reason in report.failed:
            if not quiet:
                self._log(f"Couldn't update {label}: {reason}", "warn")
        if not quiet and report.trusted_count == 0:
            self._log("No Roblox install found. Install Roblox, then press Start again "
                      "(the Microsoft Store version isn't supported).", "warn")

    def _start(self) -> None:
        try:
            self._patch_roblox(quiet=False)
        except Exception as exc:
            messagebox.showerror("Patchwork", f"Couldn't prepare Roblox: {exc}")
            return
        running = roblox.running_clients(tuple(self.config.processes))
        self.busy = True
        self.start_btn.state(["disabled"])
        self._set_status("Starting… accept the Windows prompt if one appears", "busy")

        def work() -> None:
            try:
                self.runner.start()
                self.events.put(("started", running))
            except Exception as exc:
                self.events.put(("failed", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _stop(self) -> None:
        self.busy = True
        self.start_btn.state(["disabled"])
        self._set_status("Stopping…", "busy")

        def work() -> None:
            self.runner.stop()
            self.events.put(("stopped", None))

        threading.Thread(target=work, daemon=True).start()

    def _repatch(self) -> None:
        # Roblox installs updates into a new folder, so keep trusting new versions.
        if self.runner.running:
            try:
                self._patch_roblox(quiet=True)
            except Exception:
                pass
            self.root.after(REPATCH_MS, self._repatch)

    def _poll(self) -> None:
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "hit":
                    hit: Hit = data
                    self._log(f"Swapped {hit.asset_id} → {hit.replacement}  ({hit.swap_name})", "hit")
                elif kind == "log":
                    level, msg = data
                    self._log(msg, "warn" if level in ("WARNING", "ERROR", "CRITICAL") else None)
                elif kind == "started":
                    self.busy = False
                    self.start_btn.state(["!disabled"])
                    self.start_btn.configure(text="Stop")
                    self._set_status("Running: swapping Roblox assets", "on")
                    self._log("Patchwork is running.")
                    if data:
                        self._log("Roblox was already open. Close it and open it again so it uses Patchwork.",
                                  "warn")
                    self.root.after(REPATCH_MS, self._repatch)
                elif kind == "failed":
                    self.busy = False
                    self.start_btn.state(["!disabled"])
                    self._set_status("Stopped", "idle")
                    self._log(f"Couldn't start: {data}", "warn")
                    messagebox.showerror("Patchwork couldn't start", data)
                elif kind == "stopped":
                    self.busy = False
                    self.start_btn.state(["!disabled"])
                    self.start_btn.configure(text="Start")
                    self._set_status("Stopped", "idle")
                    self._log("Patchwork stopped. Roblox now loads its normal assets "
                              "(clear the cache to drop swapped ones it saved).")
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._poll)

    # ----- maintenance --------------------------------------------------------------

    def _clear_cache(self) -> None:
        running = roblox.running_clients()
        if running:
            messagebox.showinfo("Close Roblox first",
                                f"{', '.join(running)} is running. Close Roblox, then clear the cache.")
            return
        removed, failed = roblox.clear_cache()
        if removed:
            self._log(f"Cleared Roblox's asset cache ({len(removed)} item(s)).")
        else:
            self._log("Roblox's asset cache was already empty.")
        for path, reason in failed:
            self._log(f"Couldn't delete {path}: {reason}", "warn")

    def _uninstall(self) -> None:
        if not messagebox.askyesno(
            "Remove certificate",
            "Stop Patchwork, remove its certificate from every Roblox install, and delete this PC's "
            "Patchwork certificate?\n\nYour swap list is kept. Patchwork makes a new certificate next "
            "time you press Start.",
            parent=self.root,
        ):
            return
        if self.runner.running:
            self.runner.stop()
            self.events.put(("stopped", None))
        report = roblox.unpatch_all()
        for label in report.patched:
            self._log(f"Removed the certificate from {label}.")
        for label, reason in report.failed:
            self._log(f"Couldn't update {label}: {reason}", "warn")
        shutil.rmtree(ca_dir(self.home), ignore_errors=True)
        self.ca_pem = None
        self._log("Deleted Patchwork's certificate from this PC.")

    def _open_folder(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(self.home)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(self.home)])

    def _on_close(self) -> None:
        if self.runner.running:
            self.runner.stop()
        self.root.destroy()


PALETTE = {
    "bg": "#16161c",
    "panel": "#1f1f27",
    "field": "#111116",
    "text": "#e8e8ef",
    "muted": "#8b8b9a",
    "accent": "#7ce0a6",
    "accent_dark": "#5cc488",
    "warn": "#f2c46b",
    "border": "#2c2c37",
}


def _style(root: tk.Tk) -> None:
    p = PALETTE
    root.configure(background=p["bg"])
    style = ttk.Style(root)
    style.theme_use("clam")
    base = ("Segoe UI", 10) if sys.platform == "win32" else ("TkDefaultFont", 10)
    style.configure(".", background=p["bg"], foreground=p["text"], fieldbackground=p["field"],
                    bordercolor=p["border"], lightcolor=p["border"], darkcolor=p["border"],
                    troughcolor=p["panel"], font=base)
    style.configure("TFrame", background=p["bg"])
    style.configure("TLabel", background=p["bg"], foreground=p["text"])
    style.configure("Title.TLabel", font=(base[0], 18, "bold"))
    style.configure("Section.TLabel", foreground=p["muted"], font=(base[0], 9, "bold"))
    style.configure("Status.TLabel", font=(base[0], 10))
    style.configure("Hint.TLabel", foreground=p["muted"], font=(base[0], 9))
    style.configure("TButton", background=p["panel"], foreground=p["text"], padding=(12, 6), relief="flat")
    style.map("TButton", background=[("disabled", p["bg"]), ("active", p["border"])],
              foreground=[("disabled", p["muted"])])
    style.configure("Accent.TButton", background=p["accent"], foreground="#0d1a12",
                    font=(base[0], 10, "bold"))
    style.map("Accent.TButton", background=[("disabled", p["border"]), ("active", p["accent_dark"])])
    style.configure("TEntry", fieldbackground=p["field"], foreground=p["text"], insertcolor=p["text"],
                    padding=5)
    style.configure("TRadiobutton", background=p["bg"], foreground=p["text"])
    style.map("TRadiobutton", background=[("active", p["bg"])])
    style.configure("Treeview", background=p["panel"], fieldbackground=p["panel"], foreground=p["text"],
                    rowheight=26, borderwidth=0)
    style.map("Treeview", background=[("selected", "#2f4a3b")], foreground=[("selected", p["text"])])
    style.configure("Treeview.Heading", background=p["bg"], foreground=p["muted"], relief="flat",
                    font=(base[0], 9, "bold"))
    style.map("Treeview.Heading", background=[("active", p["bg"])])
    style.configure("Vertical.TScrollbar", background=p["panel"], arrowcolor=p["muted"])
    root.option_add("*TCombobox*Listbox.background", p["field"])


def main() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    _style(root)
    icon = Path(__file__).with_name("assets") / "patchwork.png"
    if icon.exists():
        try:
            root.iconphoto(True, tk.PhotoImage(file=str(icon)))
        except tk.TclError:
            pass
    App(root)
    root.mainloop()
