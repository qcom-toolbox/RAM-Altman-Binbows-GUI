"""
ram_altman_gui.py - the window for Ram-Altman. Plain tkinter, so it needs
nothing beyond a normal Python install (and uses very little RAM, which
would otherwise be embarrassing).
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import ram_altman_core as core

REFRESH_MS = 2000
CHECK_EVERY_MS = 5 * 60 * 1000

USED = "#c0392b"
AVAILABLE = "#1e8449"
CACHE = "#7dcea0"
MUTED = "#6b6b6b"


class App(ttk.Frame):
    def __init__(self, root: tk.Tk):
        super().__init__(root, padding=14)
        self.root = root
        self.results: queue.Queue = queue.Queue()
        self.cfg = core.load_saved_config()
        self.mem: core.Memory | None = None
        self.check_job = None

        root.title("Ram-Altman")
        root.minsize(620, 560)
        self.pack(fill="both", expand=True)
        self._style()
        self._build_memory()
        self._build_tabs()

        self.refresh_memory()
        self.refresh_schedule()
        self.refresh_log()
        self.new_preview()
        self.root.after(100, self._drain)

    # ----- look ---------------------------------------------------------

    def _style(self):
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Big.TLabel", font=("Segoe UI", 20, "bold"))
        style.configure("Head.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Verdict.TLabel", font=("Segoe UI", 10, "italic"))

    # ----- memory panel -------------------------------------------------

    def _build_memory(self):
        box = ttk.LabelFrame(self, text=" The evidence against you ", padding=12)
        box.pack(fill="x")

        nums = ttk.Frame(box)
        nums.pack(fill="x")
        self.vars = {}
        for col, (key, label) in enumerate(
                [("total", "Total"), ("used", "Used"), ("available", "Available")]):
            cell = ttk.Frame(nums)
            cell.grid(row=0, column=col, sticky="w", padx=(0, 36))
            ttk.Label(cell, text=label, style="Muted.TLabel").pack(anchor="w")
            self.vars[key] = tk.StringVar(value="…")
            ttk.Label(cell, textvariable=self.vars[key], style="Big.TLabel").pack(anchor="w")

        self.bar = tk.Canvas(box, height=26, highlightthickness=0, bg="#e5e5e5")
        self.bar.pack(fill="x", pady=(10, 4))
        self.bar.bind("<Configure>", lambda e: self._draw_bar())

        legend = ttk.Frame(box)
        legend.pack(anchor="w")
        self.legend = {}
        for key, color in [("used", USED), ("cache", CACHE), ("free", AVAILABLE)]:
            tk.Label(legend, text="■", fg=color, font=("Segoe UI", 11)).pack(side="left")
            self.legend[key] = tk.StringVar()
            ttk.Label(legend, textvariable=self.legend[key], style="Muted.TLabel").pack(
                side="left", padx=(2, 14))
        self.vars["verdict"] = tk.StringVar()
        ttk.Label(box, textvariable=self.vars["verdict"], style="Verdict.TLabel",
                  wraplength=560).pack(anchor="w", pady=(6, 0))

    def _draw_bar(self):
        c = self.bar
        c.delete("all")
        mem = self.mem
        if not mem:
            return
        w, h = c.winfo_width(), c.winfo_height()
        used_w = w * mem.used_kb / mem.total_kb
        c.create_rectangle(0, 0, used_w, h, fill=USED, width=0)
        if mem.free_kb is not None:
            cache_w = w * (mem.cache_kb or 0) / mem.total_kb
            c.create_rectangle(used_w, 0, used_w + cache_w, h, fill=CACHE, width=0)
            c.create_rectangle(used_w + cache_w, 0, w, h, fill=AVAILABLE, width=0)
        else:
            c.create_rectangle(used_w, 0, w, h, fill=AVAILABLE, width=0)
        # Threshold marker: the email goes out while available sits to the right of it.
        try:
            threshold = int(self.cfg.get("threshold", 10))
        except (TypeError, ValueError):
            threshold = 10
        x = w * (100 - threshold) / 100
        c.create_line(x, 0, x, h, fill="black", width=2, dash=(3, 2))
        c.create_text(x - 4, h / 2, text=f"{threshold}%", anchor="e", fill="white",
                      font=("Segoe UI", 8, "bold"))

    def refresh_memory(self):
        try:
            self.mem = mem = core.read_memory()
        except core.RamAltmanError as exc:
            self.vars["verdict"].set(str(exc))
        else:
            self.vars["total"].set(core.to_gib(mem.total_kb))
            self.vars["used"].set(core.to_gib(mem.used_kb))
            self.vars["available"].set(f"{core.to_gib(mem.available_kb)}  ({mem.available_pct}%)")
            self.legend["used"].set("used")
            if mem.free_kb is not None:
                self.legend["cache"].set(
                    f"cache, counts as available: {core.to_gib(mem.cache_kb)}")
                self.legend["free"].set(f"free: {core.to_gib(mem.free_kb)}")
            else:
                self.legend["cache"].set("")
                self.legend["free"].set("available")
            try:
                threshold = int(self.cfg.get("threshold", 10))
            except (TypeError, ValueError):
                threshold = 10
            if mem.is_over_threshold(threshold):
                verdict = (f"Over {threshold}% available. Sam would want to know about this "
                           f"{core.to_gib(mem.available_kb)}.")
            else:
                verdict = (f"Not above {threshold}% available. Your machine has suffered "
                           "enough; no email today.")
            self.vars["verdict"].set(verdict)
            self._draw_bar()
        self.root.after(REFRESH_MS, self.refresh_memory)

    # ----- tabs ---------------------------------------------------------

    def _build_tabs(self):
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, pady=(12, 0))
        self._build_settings_tab()
        self._build_preview_tab()
        self._build_schedule_tab()
        self._build_log_tab()

    def _build_settings_tab(self):
        tab = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(tab, text="Email settings")
        self.fields = {}

        def row(r, key, label, width=36, show=None, hint=None):
            ttk.Label(tab, text=label).grid(row=r, column=0, sticky="w", pady=3)
            var = tk.StringVar(value=str(self.cfg.get(key, "")))
            ttk.Entry(tab, textvariable=var, width=width, show=show).grid(
                row=r, column=1, sticky="we", pady=3)
            if hint:
                ttk.Label(tab, text=hint, style="Muted.TLabel").grid(
                    row=r, column=2, sticky="w", padx=8)
            self.fields[key] = var

        row(0, "to", "To", hint="who gets the bad news")
        row(1, "from", "From", hint="a real address you send from")
        row(2, "from_name", "Your name", hint="or your nom de RAM")
        row(3, "threshold", "Threshold %", width=6, hint="email when available is above this")
        ttk.Separator(tab).grid(row=4, column=0, columnspan=3, sticky="we", pady=8)
        row(5, "smtp_host", "SMTP server", hint="e.g. smtp.gmail.com")
        row(6, "smtp_port", "Port", width=6)

        ttk.Label(tab, text="Security").grid(row=7, column=0, sticky="w", pady=3)
        self.fields["smtp_security"] = tk.StringVar(value=self.cfg.get("smtp_security", "starttls"))
        ttk.Combobox(tab, textvariable=self.fields["smtp_security"], state="readonly",
                     values=["starttls", "ssl", "none"], width=10).grid(
            row=7, column=1, sticky="w", pady=3)

        row(8, "smtp_user", "Username")
        ttk.Label(tab, text="Password").grid(row=9, column=0, sticky="w", pady=3)
        self.password = tk.StringVar(value=core.get_password())
        ttk.Entry(tab, textvariable=self.password, show="•", width=36).grid(
            row=9, column=1, sticky="we", pady=3)
        ttk.Label(tab, text="kept in Windows Credential Manager", style="Muted.TLabel").grid(
            row=9, column=2, sticky="w", padx=8)
        tab.columnconfigure(1, weight=1)

        buttons = ttk.Frame(tab)
        buttons.grid(row=10, column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Button(buttons, text="Save", command=self.save).pack(side="left")
        ttk.Button(buttons, text="Send a test email to myself",
                   command=self.send_test).pack(side="left", padx=8)
        self.settings_status = tk.StringVar()
        ttk.Label(tab, textvariable=self.settings_status, style="Muted.TLabel",
                  wraplength=560).grid(row=11, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(tab, style="Muted.TLabel", wraplength=560, text=(
            "Gmail and Outlook need an app password, not your normal one. "
            "Use your own real address, and if anyone asks you to stop, stop.")).grid(
            row=12, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _build_preview_tab(self):
        tab = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(tab, text="Preview")
        top = ttk.Frame(tab)
        top.pack(fill="x")
        ttk.Label(top, text="Today's correspondence (dry run, nothing is sent)",
                  style="Head.TLabel").pack(side="left")
        ttk.Button(top, text="Another one", command=self.new_preview).pack(side="right")
        self.preview = tk.Text(tab, wrap="word", font=("Consolas", 10), height=14,
                               relief="flat", padx=10, pady=10, bg="#fbfbf8")
        self.preview.pack(fill="both", expand=True, pady=(8, 0))

    def _build_schedule_tab(self):
        tab = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(tab, text="Schedule")
        ttk.Label(tab, text="Background checks", style="Head.TLabel").pack(anchor="w")
        ttk.Label(tab, style="Muted.TLabel", wraplength=560, text=(
            "A Windows scheduled task samples your RAM every 5 minutes and sends at most one "
            "email a day, even while this window is closed. If a send fails, it tries again "
            "on the next run.")).pack(anchor="w", pady=(2, 8))

        self.task_status = tk.StringVar(value="Checking…")
        ttk.Label(tab, textvariable=self.task_status).pack(anchor="w")
        row = ttk.Frame(tab)
        row.pack(anchor="w", pady=8)
        self.task_button = ttk.Button(row, text="…", command=self.toggle_task, state="disabled")
        self.task_button.pack(side="left")
        ttk.Button(row, text="Check now", command=self.check_now).pack(side="left", padx=8)

        self.watch = tk.BooleanVar(value=False)
        ttk.Checkbutton(tab, text="Also check every 5 minutes while this window is open",
                        variable=self.watch, command=self.toggle_watch).pack(anchor="w")

        self.sent_status = tk.StringVar()
        ttk.Label(tab, textvariable=self.sent_status, style="Muted.TLabel").pack(
            anchor="w", pady=(12, 0))
        self.run_status = tk.StringVar()
        ttk.Label(tab, textvariable=self.run_status, wraplength=560).pack(anchor="w", pady=(4, 0))

    def _build_log_tab(self):
        tab = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(tab, text="Audit")
        self.audit_text = tk.StringVar()
        ttk.Label(tab, textvariable=self.audit_text, style="Head.TLabel",
                  wraplength=560).pack(anchor="w")
        ttk.Label(tab, style="Muted.TLabel", wraplength=560, text=(
            "Yes, share of memory in use isn't the same as share of time spent in use. "
            "Welcome to Hacker News.")).pack(anchor="w", pady=(2, 8))

        cols = ("time", "used", "available", "pct")
        self.log_view = ttk.Treeview(tab, columns=cols, show="headings", height=8)
        for col, title, width in [("time", "When", 170), ("used", "Used", 90),
                                  ("available", "Available", 90), ("pct", "Available %", 90)]:
            self.log_view.heading(col, text=title)
            self.log_view.column(col, width=width, anchor="w")
        self.log_view.pack(fill="both", expand=True)

        row = ttk.Frame(tab)
        row.pack(anchor="w", pady=(8, 0))
        ttk.Button(row, text="Refresh", command=self.refresh_log).pack(side="left")
        ttk.Button(row, text="Open data folder", command=self.open_folder).pack(side="left", padx=8)

    # ----- actions ------------------------------------------------------

    def _form_config(self) -> dict:
        cfg = {k: v.get().strip() for k, v in self.fields.items()}
        for key in ("threshold", "smtp_port"):
            try:
                cfg[key] = int(cfg[key])
            except ValueError:
                raise core.RamAltmanError(f"{key.replace('_', ' ')} must be a whole number")
        if not 0 <= cfg["threshold"] <= 100:
            raise core.RamAltmanError("threshold must be between 0 and 100")
        return cfg

    def save(self) -> bool:
        try:
            cfg = self._form_config()
            core.save_config(cfg)
            if core.IS_WINDOWS:
                core.set_password(self.password.get(), cfg["smtp_user"])
        except core.RamAltmanError as exc:
            messagebox.showerror("Ram-Altman", str(exc), parent=self.root)
            return False
        self.cfg = cfg
        self.settings_status.set("Saved.")
        self._draw_bar()
        return True

    def send_test(self):
        if not self.save():
            return
        cfg = core.load_config()
        if not cfg["from"]:
            self.settings_status.set("Fill in From first; the test goes to that address.")
            return
        self.settings_status.set(f"Sending a test to {cfg['from']}…")

        def work():
            mem = self.mem or core.read_memory()
            subject, body = core.compose_email(mem, cfg["from_name"], dry_run=True)
            msg = core.build_message(cfg, "[Ram-Altman test] " + subject, body, to=cfg["from"])
            core.send_email(cfg, msg)
            return f"Test email sent to {cfg['from']}. Check your inbox."

        self._background(work, self.settings_status.set)

    def new_preview(self):
        mem = self.mem or core.read_memory()
        cfg = dict(core.DEFAULTS, **self.cfg)
        try:
            threshold = int(cfg["threshold"])
        except (TypeError, ValueError):
            threshold = 10
        subject, body = core.compose_email(mem, cfg["from_name"], dry_run=True)
        text = core.render_preview(cfg, subject, body)
        if not mem.is_over_threshold(threshold):
            text = (f"(Right now available RAM is not above {threshold}%, so no email would go "
                    "out. Here's what it would say if it did.)\n\n") + text
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text)
        self.preview.configure(state="disabled")

    def refresh_schedule(self):
        sent = core.last_sent()
        text = f"Last email sent: {sent}" if sent else "No email sent yet."
        run = core.last_run()
        if run:
            text += f"\nLast scheduled run: {run}"
        self.sent_status.set(text)
        if not core.IS_WINDOWS:
            self.task_status.set("Scheduled tasks are Windows only. On Linux, use cron "
                                 "with: ram_altman.py --run")
            return
        self._background(core.task_installed, self._show_task)

    def _show_task(self, installed):
        if isinstance(installed, str):     # an error message
            self.task_status.set(installed)
            return
        self.task_installed = installed
        if installed:
            self.task_status.set("✔ Scheduled task is on: checking every 5 minutes.")
            self.task_button.configure(text="Turn off", state="normal")
        else:
            self.task_status.set("Scheduled task is off.")
            self.task_button.configure(text="Turn on", state="normal")

    def toggle_task(self):
        self.task_button.configure(state="disabled")
        if getattr(self, "task_installed", False):
            self.task_status.set("Removing the scheduled task…")
            work = lambda: (core.uninstall_task(), core.task_installed())[1]
        else:
            if not self.save():
                self.task_button.configure(state="normal")
                return
            cfg = core.load_config()
            if not cfg["to"] or not cfg["from"]:
                self.task_status.set("Fill in To and From on the Email settings tab first.")
                self.task_button.configure(state="normal")
                return
            self.task_status.set("Creating the scheduled task…")
            work = lambda: (core.install_task(), core.task_installed())[1]
        self._background(work, self._show_task)

    def check_now(self):
        if not self.save():
            return
        self.run_status.set("Checking…")

        def done(text):
            self.run_status.set(text)
            self.refresh_schedule()
            self.refresh_log()

        self._background(lambda: core.run_once().capitalize() + ".", done)

    def toggle_watch(self):
        if self.check_job:
            self.root.after_cancel(self.check_job)
            self.check_job = None
        if self.watch.get():
            self._watch_tick()

    def _watch_tick(self):
        self.check_now()
        self.check_job = self.root.after(CHECK_EVERY_MS, self._watch_tick)

    def refresh_log(self):
        count, pct = core.audit()
        if pct is None:
            self.audit_text.set("No samples yet. Turn on the schedule, or press Check now.")
        else:
            self.audit_text.set(f"Your RAM in use: {pct:.1f}% across {count} samples "
                                "(the claim: maybe 5%).")
        self.log_view.delete(*self.log_view.get_children())
        for r in reversed(core.read_log(limit=200)):
            self.log_view.insert("", "end", values=(
                r["timestamp"].replace("T", " ")[:19], core.to_gib(r["used_kb"]),
                core.to_gib(r["available_kb"]), f"{r['available_pct']:.1f}%"))

    def open_folder(self):
        folder = core.state_dir()
        folder.mkdir(parents=True, exist_ok=True)
        if core.IS_WINDOWS:
            os.startfile(folder)
        else:
            subprocess.Popen(["xdg-open", str(folder)])

    # ----- threading ----------------------------------------------------

    def _background(self, work, on_done):
        def runner():
            try:
                result = work()
            except core.RamAltmanError as exc:
                result = f"⚠ {exc}"
            except Exception as exc:     # keep the window alive whatever happens
                result = f"⚠ {type(exc).__name__}: {exc}"
            self.results.put((on_done, result))
        threading.Thread(target=runner, daemon=True).start()

    def _drain(self):
        try:
            while True:
                on_done, result = self.results.get_nowait()
                on_done(result)
        except queue.Empty:
            pass
        self.root.after(100, self._drain)


def main():
    if core.IS_WINDOWS:
        try:   # crisp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (OSError, AttributeError):
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
