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
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import __version__, presets, roblox
from .config import KIND_ASSET, KIND_FILE, KIND_URL, Swap, app_dir, swap_from_inputs
from .profiles import Store
from .proxy import ProxyRunner, ca_dir, ensure_ca
from .rules import Hit

REPATCH_MS = 20_000
POLL_MS = 150

MODE_LABELS = {KIND_ASSET: "ID", KIND_URL: "Link", KIND_FILE: "File"}
FILE_TYPES = [
    ("Roblox-ready files", "*.png *.jpg *.jpeg *.webp *.ogg *.mp3 *.wav *.mesh *.rbxm *.rbxmx"),
    ("All files", "*.*"),
]
JSON_TYPES = [("Preset (JSON)", "*.json"), ("All files", "*.*")]

PALETTE = {
    "bg": "#1b1b21",
    "panel": "#24242c",
    "field": "#141419",
    "text": "#e8e8ef",
    "muted": "#8b8b9a",
    "accent": "#7ce0a6",
    "accent_dark": "#5cc488",
    "warn": "#f2c46b",
    "off": "#e07c7c",
    "border": "#33333f",
    "select": "#2f4a3b",
}


class HintEntry(ttk.Entry):
    """An entry that shows grey hint text while it is empty."""

    def __init__(self, master, hint: str, **kw):
        self.var = tk.StringVar()
        super().__init__(master, textvariable=self.var, **kw)
        self.hint = hint
        self._showing = False
        self.bind("<FocusIn>", self._clear_hint)
        self.bind("<FocusOut>", self._show_hint)
        self._show_hint()

    def _show_hint(self, _=None) -> None:
        if not self.var.get():
            self._showing = True
            self.configure(style="Hint.TEntry")
            self.var.set(self.hint)

    def _clear_hint(self, _=None) -> None:
        if self._showing:
            self._showing = False
            self.var.set("")
            self.configure(style="TEntry")

    def value(self) -> str:
        return "" if self._showing else self.var.get()

    def set(self, text: str) -> None:
        self._clear_hint()
        self.var.set(text)
        if self.focus_get() is not self:
            self._show_hint()


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.home = app_dir()
        self.store = Store.load(self.home)
        self.events: queue.Queue = queue.Queue()
        self.runner = ProxyRunner(
            self.store.to_config(),
            self.home,
            on_hit=lambda hit: self.events.put(("hit", hit)),
            on_log=lambda level, msg: self.events.put(("log", (level, msg))),
        )
        self.busy = False
        self.ca_pem: str | None = None
        self.enabled_vars: dict[str, tk.BooleanVar] = {}

        root.title(f"Patchwork {__version__}")
        root.geometry("1040x760")
        root.minsize(860, 620)
        self._build()
        self._refresh_all()
        self._set_status("Stopped", "idle")
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.bind_all("<Control-z>", lambda e: self._undo())
        root.after(POLL_MS, self._poll)
        self._log("Patchwork swaps Roblox assets on this PC only. Add swaps, tick a profile under "
                  "Enabled, then press Start.")
        for warning in self.store.warnings:
            self._log(warning, "warn")

    # ----- layout -----------------------------------------------------------------

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=(14, 12, 14, 10))
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="Patchwork", style="Title.TLabel").pack(side="left")
        self.status = ttk.Label(header, style="Status.TLabel")
        self.status.pack(side="left", padx=16)
        self.start_btn = ttk.Button(header, text="Start", style="Accent.TButton", width=10, command=self._toggle)
        self.start_btn.pack(side="right")

        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill="both", expand=True, pady=(12, 0))
        replacer = ttk.Frame(self.tabs, padding=12)
        activity = ttk.Frame(self.tabs, padding=12)
        settings = ttk.Frame(self.tabs, padding=16)
        self.tabs.add(replacer, text="Replacer")
        self.tabs.add(activity, text="Activity")
        self.tabs.add(settings, text="Settings")
        self._build_replacer(replacer)
        self._build_activity(activity)
        self._build_settings(settings)

    def _build_replacer(self, tab: ttk.Frame) -> None:
        # Configuration row
        top = ttk.Frame(tab)
        top.pack(fill="x")
        ttk.Label(top, text="Configuration", style="Section.TLabel").pack(side="left", anchor="s")
        export_btn = ttk.Menubutton(top, text="Export…", style="Small.TMenubutton")
        export_menu = tk.Menu(export_btn, tearoff=False, background=PALETTE["panel"],
                              foreground=PALETTE["text"], activebackground=PALETTE["select"])
        export_menu.add_command(label="As a Patchwork preset…", command=lambda: self._export("patchwork"))
        export_menu.add_command(label="As a Fleasion preset…", command=lambda: self._export("fleasion"))
        export_btn["menu"] = export_menu
        export_btn.pack(side="right")
        ttk.Button(top, text="Import…", style="Small.TButton", command=self._import).pack(side="right", padx=6)
        conf = ttk.Frame(tab, style="Panel.TFrame", padding=10)
        conf.pack(fill="x", pady=(4, 10))
        ttk.Label(conf, text="Editing:", style="Panel.TLabel").pack(side="left")
        self.editing_var = tk.StringVar()
        self.editing_box = ttk.Combobox(conf, textvariable=self.editing_var, state="readonly", width=18)
        self.editing_box.pack(side="left", padx=(6, 16))
        self.editing_box.bind("<<ComboboxSelected>>", lambda e: self._switch_editing())
        ttk.Label(conf, text="Enabled:", style="Panel.TLabel").pack(side="left")
        self.enabled_btn = ttk.Menubutton(conf, width=22)
        self.enabled_menu = tk.Menu(self.enabled_btn, tearoff=False, background=PALETTE["panel"],
                                    foreground=PALETTE["text"], activebackground=PALETTE["select"],
                                    selectcolor=PALETTE["accent"])
        self.enabled_btn["menu"] = self.enabled_menu
        self.enabled_btn.pack(side="left", padx=(6, 16))
        for text, cmd in (("New", self._new_profile), ("Duplicate", self._duplicate_profile),
                          ("Rename", self._rename_profile), ("Delete", self._delete_profile)):
            ttk.Button(conf, text=text, command=cmd).pack(side="left", padx=(0, 6))

        # Swap table
        self.table_label = ttk.Label(tab, style="Section.TLabel")
        self.table_label.pack(anchor="w")
        table = ttk.Frame(tab)
        table.pack(fill="both", expand=True, pady=(4, 10))
        self.tree = ttk.Treeview(table, columns=("status", "name", "mode", "ids", "with"), show="headings",
                                 selectmode="extended")
        for col, label, width, stretch in (
            ("status", "Status", 70, False),
            ("name", "Name", 260, True),
            ("mode", "Mode", 70, False),
            ("ids", "Asset IDs", 200, True),
            ("with", "Replacement", 320, True),
        ):
            self.tree.heading(col, text=label, anchor="w")
            self.tree.column(col, width=width, stretch=stretch, anchor="w")
        self.tree.tag_configure("off", foreground=PALETTE["muted"])
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-1>", lambda e: self._load_selected())
        self.tree.bind("<space>", lambda e: self._toggle_selected())
        self.tree.bind("<Delete>", lambda e: self._remove_selected())

        # Add / edit form
        ttk.Label(tab, text="Add / edit swap", style="Section.TLabel").pack(anchor="w")
        form = ttk.Frame(tab, style="Panel.TFrame", padding=10)
        form.pack(fill="x", pady=(4, 8))
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="Name:", style="Panel.TLabel").grid(row=0, column=0, sticky="w", pady=3)
        self.name_entry = HintEntry(form, "Optional name")
        self.name_entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=3, padx=(10, 0))
        ttk.Label(form, text="Asset IDs:", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=3)
        self.ids_entry = HintEntry(form, "IDs to replace, separated by commas or spaces (links and rbxassetid:// work)")
        self.ids_entry.grid(row=1, column=1, columnspan=2, sticky="ew", pady=3, padx=(10, 0))
        ttk.Label(form, text="Replace with:", style="Panel.TLabel").grid(row=2, column=0, sticky="w", pady=3)
        self.with_entry = HintEntry(form, "Asset ID, web link, file path, or /Folder/file.png in the configs folder")
        self.with_entry.grid(row=2, column=1, sticky="ew", pady=3, padx=(10, 6))
        ttk.Button(form, text="Browse…", command=self._browse).grid(row=2, column=2, pady=3)
        buttons = ttk.Frame(form, style="Panel.TFrame")
        buttons.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Button(buttons, text="Add New", style="Accent.TButton", command=self._add_new).pack(side="left")
        ttk.Button(buttons, text="Load Selected", command=self._load_selected).pack(side="left", padx=6)
        ttk.Button(buttons, text="Update Selected", command=self._update_selected).pack(side="left")
        ttk.Button(buttons, text="Remove", command=self._remove_selected).pack(side="right")
        ttk.Button(buttons, text="On / Off", command=self._toggle_selected).pack(side="right", padx=6)

        # Footer
        footer = ttk.Frame(tab)
        footer.pack(fill="x")
        ttk.Label(footer, text=f"Configs: {self.store.configs_dir}", style="Hint.TLabel").pack(side="left")
        ttk.Button(footer, text="Undo (Ctrl+Z)", command=self._undo).pack(side="right")
        ttk.Button(footer, text="Open Configs", command=self._open_configs).pack(side="right", padx=6)
        ttk.Button(footer, text="Clear Cache", command=self._clear_cache).pack(side="right")

    def _build_activity(self, tab: ttk.Frame) -> None:
        self.logbox = tk.Text(tab, wrap="word", state="disabled", relief="flat",
                              background=PALETTE["field"], foreground=PALETTE["text"],
                              font=("Consolas", 10), padx=10, pady=8, borderwidth=0)
        self.logbox.pack(side="left", fill="both", expand=True)
        self.logbox.tag_configure("hit", foreground=PALETTE["accent"])
        self.logbox.tag_configure("warn", foreground=PALETTE["warn"])
        scroll = ttk.Scrollbar(tab, orient="vertical", command=self.logbox.yview)
        scroll.pack(side="right", fill="y")
        self.logbox.configure(yscrollcommand=scroll.set)

    def _build_settings(self, tab: ttk.Frame) -> None:
        def section(title: str, text: str) -> ttk.Frame:
            ttk.Label(tab, text=title, style="Section.TLabel").pack(anchor="w", pady=(0, 4))
            box = ttk.Frame(tab, style="Panel.TFrame", padding=12)
            box.pack(fill="x", pady=(0, 14))
            ttk.Label(box, text=text, style="PanelHint.TLabel", wraplength=760, justify="left").pack(anchor="w")
            return box

        studio = section("Roblox Studio", "Also swap assets in Roblox Studio. Takes effect next time you press Start.")
        self.studio_var = tk.BooleanVar(value="RobloxStudioBeta.exe" in self.store.processes)
        ttk.Checkbutton(studio, text="Swap in Roblox Studio too", variable=self.studio_var,
                        command=self._studio_changed).pack(anchor="w", pady=(8, 0))

        cert = section("Certificate", "Patchwork made a certificate for this PC and added it to Roblox's own "
                       "certificate list (not Windows'). Removing it stops Patchwork and takes it out of every "
                       "Roblox install. Your profiles are kept.")
        ttk.Button(cert, text="Remove certificate", command=self._uninstall).pack(anchor="w", pady=(8, 0))

        data = section("Data", f"Profiles, settings, downloaded files and the certificate are in {self.home}")
        row = ttk.Frame(data, style="Panel.TFrame")
        row.pack(anchor="w", pady=(8, 0))
        ttk.Button(row, text="Open data folder", command=lambda: _open_path(self.home)).pack(side="left")
        ttk.Button(row, text="Clear downloaded files", command=self._clear_downloads).pack(side="left", padx=6)

    # ----- helpers ------------------------------------------------------------------

    def _log(self, msg: str, tag: str | None = None) -> None:
        self.logbox.configure(state="normal")
        self.logbox.insert("end", msg + "\n", tag or ())
        lines = int(self.logbox.index("end-1c").split(".")[0])
        if lines > 1000:
            self.logbox.delete("1.0", f"{lines - 1000}.0")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def _set_status(self, text: str, state: str) -> None:
        color = {"idle": PALETTE["muted"], "on": PALETTE["accent"], "busy": PALETTE["warn"]}[state]
        self.status.configure(text=text, foreground=color)

    def _swaps(self) -> list[Swap]:
        return self.store.profiles[self.store.editing]

    def _selected(self) -> list[int]:
        return sorted(int(i) for i in self.tree.selection())

    def _display_value(self, swap: Swap) -> str:
        if swap.kind == KIND_FILE:
            portable = self.store.portable_path(swap.value)
            return "/" + portable if portable != swap.value else swap.value
        return swap.value

    def _refresh_all(self) -> None:
        self._refresh_profiles()
        self._refresh_swaps()

    def _refresh_profiles(self) -> None:
        names = list(self.store.profiles)
        self.editing_box.configure(values=names)
        self.editing_var.set(self.store.editing)
        self.enabled_menu.delete(0, "end")
        self.enabled_vars = {}
        for name in names:
            var = tk.BooleanVar(value=name in self.store.enabled)
            self.enabled_vars[name] = var
            self.enabled_menu.add_checkbutton(label=name, variable=var,
                                              command=lambda n=name: self._enabled_changed(n))
        on = [n for n in names if n in self.store.enabled]
        if not on:
            summary = "No profiles enabled"
        elif len(on) == 1:
            summary = on[0]
        else:
            summary = f"{len(on)} profiles enabled"
        self.enabled_btn.configure(text=summary)

    def _refresh_swaps(self) -> None:
        keep = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        for index, swap in enumerate(self._swaps()):
            self.tree.insert("", "end", iid=str(index), tags=() if swap.enabled else ("off",), values=(
                "On" if swap.enabled else "Off",
                swap.name,
                MODE_LABELS[swap.kind],
                ", ".join(map(str, swap.targets)),
                self._display_value(swap),
            ))
        keep = [i for i in keep if self.tree.exists(i)]
        if keep:
            self.tree.selection_set(keep)
        state = "enabled" if self.store.editing in self.store.enabled else "not enabled"
        self.table_label.configure(text=f"Swaps in {self.store.editing} ({len(self._swaps())}, {state})")

    def _changed(self, profile: str | None = None) -> None:
        """Save, redraw, and hand the new swap set to the proxy."""
        try:
            if profile:
                self.store.save_profile(profile)
            self.store.save_settings()
        except OSError as exc:
            messagebox.showerror("Patchwork", f"Couldn't save: {exc}")
        self._refresh_all()
        self.runner.update_config(self.store.to_config())
        if self.runner.running:
            self._log("Saved. Rejoin the game (Clear Cache first if the asset already loaded) to see changes.")

    # ----- profiles -----------------------------------------------------------------

    def _switch_editing(self) -> None:
        self.store.editing = self.editing_var.get()
        self.store.save_settings()
        self.tree.selection_set(())
        self._refresh_swaps()

    def _enabled_changed(self, name: str) -> None:
        self.store.checkpoint()
        self.store.set_enabled(name, self.enabled_vars[name].get())
        self._changed()

    def _ask_name(self, title: str, initial: str = "") -> str | None:
        name = simpledialog.askstring(title, "Profile name:", initialvalue=initial, parent=self.root)
        return name.strip() if name and name.strip() else None

    def _new_profile(self) -> None:
        if name := self._ask_name("New profile"):
            self.store.checkpoint()
            try:
                self.store.create(name)
            except ValueError as exc:
                messagebox.showerror("Patchwork", str(exc))
                return
            self._changed()

    def _duplicate_profile(self) -> None:
        self.store.checkpoint()
        self.store.duplicate(self.store.editing)
        self._changed()

    def _rename_profile(self) -> None:
        old = self.store.editing
        if (name := self._ask_name("Rename profile", old)) and name != old:
            self.store.checkpoint()
            try:
                self.store.rename(old, name)
            except ValueError as exc:
                messagebox.showerror("Patchwork", str(exc))
                return
            self._changed()

    def _delete_profile(self) -> None:
        name = self.store.editing
        count = len(self.store.profiles[name])
        if not messagebox.askyesno("Delete profile", f'Delete "{name}" and its {count} swap(s)?\n\n'
                                   "You can undo this with Ctrl+Z while Patchwork is open.", parent=self.root):
            return
        self.store.checkpoint()
        self.store.delete(name)
        self._changed()

    def _import(self) -> None:
        path = filedialog.askopenfilename(parent=self.root, title="Import preset", filetypes=JSON_TYPES)
        if not path:
            return
        try:
            result = presets.load_preset(Path(path))
        except (ValueError, OSError) as exc:
            messagebox.showerror("Can't import this file", str(exc), parent=self.root)
            return
        if not result.swaps:
            messagebox.showwarning("Nothing to import", "No usable swaps in this file.\n\n"
                                   + "\n".join(result.skipped[:15]), parent=self.root)
            return
        hosts = result.link_hosts()
        if hosts:
            links = sum(1 for s in result.swaps if s.kind == KIND_URL)
            if not messagebox.askyesno(
                "Import preset",
                f"This {result.format} preset has {len(result.swaps)} swap(s). {links} of them download "
                f"files from:\n\n  " + "\n  ".join(hosts) + "\n\nOnly import presets from people you trust. "
                "Import it?",
                parent=self.root,
            ):
                return
        self.store.checkpoint()
        name = self.store.create(Path(path).stem, result.swaps)
        self.store.set_enabled(name, True)
        self._changed()
        self._log(f'Imported {len(result.swaps)} swap(s) from {Path(path).name} as profile "{name}" (enabled).')
        for reason in result.skipped:
            self._log(f"Skipped {reason}", "warn")
        if result.skipped:
            messagebox.showinfo("Imported with some skipped",
                                f"Imported {len(result.swaps)} swap(s). Skipped {len(result.skipped)}:\n\n"
                                + "\n".join(result.skipped[:15]), parent=self.root)

    def _export(self, fmt: str) -> None:
        name = self.store.editing
        path = filedialog.asksaveasfilename(parent=self.root, title=f"Export {name}", defaultextension=".json",
                                            initialfile=f"{name}.json", filetypes=JSON_TYPES)
        if not path:
            return
        skipped: list[str] = []
        if fmt == "fleasion":
            text, skipped = presets.export_fleasion(self._swaps())
        else:
            text = presets.export_patchwork(self._swaps())
        try:
            Path(path).write_text(text, encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Patchwork", f"Couldn't save: {exc}")
            return
        self._log(f"Exported {name} to {path}.")
        if skipped:
            messagebox.showinfo("Exported", "Fleasion presets can't point at files on your PC, so these swaps "
                                "were left out:\n\n" + "\n".join(skipped), parent=self.root)

    # ----- swaps --------------------------------------------------------------------

    def _form_swap(self, enabled: bool = True) -> Swap | None:
        try:
            return swap_from_inputs(self.name_entry.value(), self.ids_entry.value(), self.with_entry.value(),
                                    self.store.configs_dir, enabled)
        except ValueError as exc:
            messagebox.showerror("Can't save this swap", str(exc), parent=self.root)
            return None

    def _clear_form(self) -> None:
        for entry in (self.name_entry, self.ids_entry, self.with_entry):
            entry.set("")

    def _add_new(self) -> None:
        if swap := self._form_swap():
            self.store.checkpoint()
            self._swaps().append(swap)
            self._clear_form()
            self._changed(self.store.editing)
            last = str(len(self._swaps()) - 1)
            self.tree.selection_set(last)
            self.tree.see(last)

    def _load_selected(self) -> None:
        sel = self._selected()
        if len(sel) != 1:
            if sel:
                messagebox.showinfo("Patchwork", "Select one swap to load.", parent=self.root)
            return
        swap = self._swaps()[sel[0]]
        self.name_entry.set(swap.name)
        self.ids_entry.set(", ".join(map(str, swap.targets)))
        self.with_entry.set(self._display_value(swap))

    def _update_selected(self) -> None:
        sel = self._selected()
        if len(sel) != 1:
            messagebox.showinfo("Patchwork", "Select the one swap to update.", parent=self.root)
            return
        old = self._swaps()[sel[0]]
        if swap := self._form_swap(old.enabled):
            self.store.checkpoint()
            self._swaps()[sel[0]] = swap
            self._changed(self.store.editing)

    def _toggle_selected(self) -> None:
        sel = self._selected()
        if not sel:
            return
        self.store.checkpoint()
        turn_on = not all(self._swaps()[i].enabled for i in sel)
        for i in sel:
            self._swaps()[i].enabled = turn_on
        self._changed(self.store.editing)

    def _remove_selected(self) -> None:
        sel = self._selected()
        if not sel:
            return
        self.store.checkpoint()
        for i in reversed(sel):
            del self._swaps()[i]
        self.tree.selection_set(())
        self._changed(self.store.editing)
        self._log(f"Removed {len(sel)} swap(s). Ctrl+Z brings them back.")

    def _browse(self) -> None:
        path = filedialog.askopenfilename(parent=self.root, filetypes=FILE_TYPES,
                                          initialdir=str(self.store.configs_dir))
        if not path:
            return
        portable = self.store.portable_path(path)
        self.with_entry.set("/" + portable if portable != path else path)
        if not self.name_entry.value():
            self.name_entry.set(Path(path).stem)

    def _undo(self) -> None:
        if self.store.undo():
            self._changed()
            self._log("Undid the last change.")

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
        if not self.store.active_swaps():
            self._log("No swaps are enabled yet: tick a profile under Enabled.", "warn")
        try:
            self._patch_roblox(quiet=False)
        except Exception as exc:
            messagebox.showerror("Patchwork", f"Couldn't prepare Roblox: {exc}")
            return
        self.runner.update_config(self.store.to_config())
        running = roblox.running_clients(tuple(self.store.processes))
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
                              "(Clear Cache to drop swapped ones it saved).")
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._poll)

    # ----- maintenance --------------------------------------------------------------

    def _studio_changed(self) -> None:
        procs = [p for p in self.store.processes if p != "RobloxStudioBeta.exe"]
        if self.studio_var.get():
            procs.append("RobloxStudioBeta.exe")
        self.store.processes = procs or ["RobloxPlayerBeta.exe"]
        self.store.save_settings()
        if self.runner.running:
            self._log("Studio setting saved. Stop and Start Patchwork to apply it.")

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

    def _clear_downloads(self) -> None:
        shutil.rmtree(self.home / "downloads", ignore_errors=True)
        self._log("Deleted downloaded replacement files. Patchwork downloads them again when needed.")

    def _open_configs(self) -> None:
        self.store.configs_dir.mkdir(parents=True, exist_ok=True)
        _open_path(self.store.configs_dir)
        self._log("Profiles in this folder are read when Patchwork starts.")

    def _uninstall(self) -> None:
        if not messagebox.askyesno(
            "Remove certificate",
            "Stop Patchwork, remove its certificate from every Roblox install, and delete this PC's "
            "Patchwork certificate?\n\nYour profiles are kept. Patchwork makes a new certificate next "
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

    def _on_close(self) -> None:
        if self.runner.running:
            self.runner.stop()
        self.root.destroy()


def _open_path(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _style(root: tk.Tk) -> None:
    p = PALETTE
    root.configure(background=p["bg"])
    style = ttk.Style(root)
    style.theme_use("clam")
    family = "Segoe UI" if sys.platform == "win32" else "DejaVu Sans"
    base = (family, 10)
    style.configure(".", background=p["bg"], foreground=p["text"], fieldbackground=p["field"],
                    bordercolor=p["border"], lightcolor=p["border"], darkcolor=p["border"],
                    troughcolor=p["panel"], selectbackground=p["select"], selectforeground=p["text"],
                    insertcolor=p["text"], font=base)
    style.configure("TFrame", background=p["bg"])
    style.configure("Panel.TFrame", background=p["panel"])
    style.configure("TLabel", background=p["bg"], foreground=p["text"])
    style.configure("Panel.TLabel", background=p["panel"])
    style.configure("Title.TLabel", font=(family, 17, "bold"))
    style.configure("Section.TLabel", foreground=p["muted"], font=(family, 9, "bold"))
    style.configure("Status.TLabel", font=base)
    style.configure("Hint.TLabel", foreground=p["muted"], font=(family, 9))
    style.configure("PanelHint.TLabel", background=p["panel"], foreground=p["muted"])
    style.configure("TButton", background=p["border"], foreground=p["text"], padding=(12, 5), relief="flat")
    style.map("TButton", background=[("disabled", p["bg"]), ("active", "#40404d")],
              foreground=[("disabled", p["muted"])])
    style.configure("Accent.TButton", background=p["accent"], foreground="#0d1a12", font=(family, 10, "bold"))
    style.map("Accent.TButton", background=[("disabled", p["border"]), ("active", p["accent_dark"])])
    style.configure("Small.TButton", padding=(10, 3))
    style.configure("Small.TMenubutton", background=p["border"], foreground=p["text"], padding=(10, 3),
                    arrowcolor=p["muted"], relief="flat")
    style.map("Small.TMenubutton", background=[("active", "#40404d")])
    style.configure("TMenubutton", background=p["field"], foreground=p["text"], padding=(10, 5),
                    arrowcolor=p["muted"], relief="flat")
    style.map("TMenubutton", background=[("active", p["border"])])
    style.configure("TEntry", fieldbackground=p["field"], foreground=p["text"], padding=6)
    style.configure("Hint.TEntry", fieldbackground=p["field"], foreground=p["muted"], padding=6)
    style.configure("TCombobox", fieldbackground=p["field"], background=p["field"], foreground=p["text"],
                    arrowcolor=p["muted"], padding=5)
    style.map("TCombobox", fieldbackground=[("readonly", p["field"])], foreground=[("readonly", p["text"])],
              selectbackground=[("readonly", p["field"])], selectforeground=[("readonly", p["text"])])
    root.option_add("*TCombobox*Listbox.background", p["field"])
    root.option_add("*TCombobox*Listbox.foreground", p["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", p["select"])
    style.configure("TCheckbutton", background=p["panel"], foreground=p["text"], indicatorbackground=p["field"],
                    indicatorforeground=p["accent"])
    style.map("TCheckbutton", background=[("active", p["panel"])])
    style.configure("TNotebook", background=p["bg"], borderwidth=0, tabmargins=(0, 0, 0, 0))
    style.configure("TNotebook.Tab", background=p["bg"], foreground=p["muted"], padding=(16, 7), borderwidth=0)
    style.map("TNotebook.Tab", background=[("selected", p["panel"])], foreground=[("selected", p["text"])],
              expand=[("selected", (0, 0, 0, 0))])
    style.configure("Treeview", background=p["field"], fieldbackground=p["field"], foreground=p["text"],
                    rowheight=26, borderwidth=0)
    style.map("Treeview", background=[("selected", p["select"])], foreground=[("selected", p["text"])])
    style.configure("Treeview.Heading", background=p["panel"], foreground=p["muted"], relief="flat",
                    font=(family, 9, "bold"), padding=(6, 5))
    style.map("Treeview.Heading", background=[("active", p["panel"])])
    style.configure("Vertical.TScrollbar", background=p["panel"], arrowcolor=p["muted"])


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
