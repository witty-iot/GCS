"""
GCS_UI -- small desktop control panel for the ESP32/Pixhawk mission bridge.

Talks the same UDP JSON mission protocol (192.168.4.1:14551) as the
Missions/mission_hover_guided_test.py / mission_hover_loiter_test.py scripts,
and reuses Missions/flight_logger.py for detailed run logging into the same
logs/ folder. Nothing in Missions/ or Arduino_esp_code/ is modified by this
app -- it is purely another client of the existing protocol.

Run with:  uv run python GCS_UI/app.py
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MISSIONS_DIR = os.path.join(REPO_ROOT, "Missions")
LOGS_DIR = os.path.join(REPO_ROOT, "logs")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, MISSIONS_DIR)

from drone_client import DroneClient
from mission_actions import ACTIONS, format_params
from flight_logger import FlightLogger, MAV_SEVERITY_NAMES  # reused as-is, not modified

STATUS_POLL_SECONDS = 1.0


class GCSApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("GCS UI")
        self.geometry("1080x720")
        self.minsize(860, 600)

        self.client = DroneClient()
        self.steps = []  # list of {"action": ..., <params>}

        self.status_queue = queue.Queue()
        self.script_queue = queue.Queue()
        self.script_proc = None

        self.logger = None
        self._start_session_logger()

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._poll_stop = threading.Event()
        self._poll_thread = threading.Thread(target=self._status_poll_loop, daemon=True)
        self._poll_thread.start()

        self.after(150, self._drain_queues)
        self._refresh_log_list()
        self._refresh_script_list()

    # ─── Session-level flight logger (reused from Missions/, unmodified) ──
    def _start_session_logger(self):
        # 14552 is the dedicated always-broadcast logger port added to the ESP32
        # firmware, independent of whatever Mission Planner/QGC is doing on 14550.
        self.logger = FlightLogger(14552, run_name="gcs_ui", mission=None)
        self.logger.start()

    def _log(self, line):
        if self.logger:
            self.logger.note(line)
        self._append_console(f"[app] {line}")

    def _on_close(self):
        self._poll_stop.set()
        if self.script_proc and self.script_proc.poll() is None:
            try:
                self.script_proc.terminate()
            except Exception:
                pass
        if self.logger:
            path = self.logger.stop()
            print(f"[Log] Session log written: {path}")
        self.destroy()

    # ─── UI construction ───────────────────────────────────────────────────
    def _build_ui(self):
        conn = ttk.Frame(self, padding=6)
        conn.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(conn, text="ESP32 IP:").pack(side=tk.LEFT)
        self.ip_var = tk.StringVar(value=self.client.ip)
        ttk.Entry(conn, textvariable=self.ip_var, width=15).pack(side=tk.LEFT, padx=(2, 10))

        ttk.Label(conn, text="Mission port:").pack(side=tk.LEFT)
        self.port_var = tk.IntVar(value=self.client.mission_port)
        ttk.Entry(conn, textvariable=self.port_var, width=7).pack(side=tk.LEFT, padx=(2, 10))

        ttk.Button(conn, text="Apply", command=self._apply_connection).pack(side=tk.LEFT, padx=(0, 20))

        self.conn_indicator = ttk.Label(conn, text="● no status yet", foreground="gray")
        self.conn_indicator.pack(side=tk.LEFT)

        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        self.mission_tab = ttk.Frame(notebook)
        self.status_tab = ttk.Frame(notebook)
        self.logs_tab = ttk.Frame(notebook)
        self.scripts_tab = ttk.Frame(notebook)

        notebook.add(self.mission_tab, text="Mission Builder")
        notebook.add(self.status_tab, text="Status / Telemetry")
        notebook.add(self.logs_tab, text="Logs")
        notebook.add(self.scripts_tab, text="Run Scripts")

        self._build_mission_tab(self.mission_tab)
        self._build_status_tab(self.status_tab)
        self._build_logs_tab(self.logs_tab)
        self._build_scripts_tab(self.scripts_tab)

    # ─── Mission Builder tab ───────────────────────────────────────────────
    def _build_mission_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(0, weight=1)

        # -- Add-step form --
        form = ttk.LabelFrame(parent, text="Add Step", padding=8)
        form.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        ttk.Label(form, text="Action:").grid(row=0, column=0, sticky="w")
        self.action_var = tk.StringVar(value="arm")
        action_combo = ttk.Combobox(form, textvariable=self.action_var, state="readonly",
                                     values=list(ACTIONS.keys()), width=18)
        action_combo.grid(row=0, column=1, sticky="w", pady=4)
        action_combo.bind("<<ComboboxSelected>>", lambda e: self._rebuild_param_fields())

        self.action_desc_var = tk.StringVar()
        ttk.Label(form, textvariable=self.action_desc_var, wraplength=220, foreground="#555"
                  ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self.param_frame = ttk.Frame(form)
        self.param_frame.grid(row=2, column=0, columnspan=2, sticky="w")
        self.param_vars = {}

        ttk.Button(form, text="Add Step", command=self._add_step).grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        ttk.Separator(form).grid(row=4, column=0, columnspan=2, sticky="ew", pady=10)
        ttk.Label(form, text="Quick commands (send + run immediately,\nbypassing the queued mission below):",
                  foreground="#555", justify="left").grid(row=5, column=0, columnspan=2, sticky="w")
        quick = ttk.Frame(form)
        quick.grid(row=6, column=0, columnspan=2, sticky="ew", pady=6)
        ttk.Button(quick, text="Quick ARM", command=lambda: self._quick_run([{"action": "arm"}])
                   ).pack(fill=tk.X, pady=2)
        ttk.Button(quick, text="Quick DISARM", command=lambda: self._quick_run([{"action": "disarm"}])
                   ).pack(fill=tk.X, pady=2)
        ttk.Button(quick, text="Quick RTL", command=lambda: self._quick_run([{"action": "rtl"}])
                   ).pack(fill=tk.X, pady=2)
        ttk.Button(quick, text="STOP / LAND now", command=self._stop_mission
                   ).pack(fill=tk.X, pady=2)

        ttk.Label(form, text="Bench-test warning: 'arm'/'takeoff' steps command\n"
                              "a real climb. Never run them without propellers\n"
                              "removed and the vehicle restrained.",
                  foreground="#a33", wraplength=220, justify="left").grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(10, 0))

        self._rebuild_param_fields()

        # -- Step list --
        list_frame = ttk.LabelFrame(parent, text="Mission Steps (in order)", padding=8)
        list_frame.grid(row=0, column=1, sticky="nsew")
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)

        columns = ("#", "action", "params")
        self.step_tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=14)
        for col, width in zip(columns, (30, 100, 260)):
            self.step_tree.heading(col, text=col.capitalize() if col != "#" else "#")
            self.step_tree.column(col, width=width, anchor="w")
        self.step_tree.grid(row=0, column=0, sticky="nsew")

        step_btns = ttk.Frame(list_frame)
        step_btns.grid(row=1, column=0, sticky="ew", pady=6)
        ttk.Button(step_btns, text="Up", command=lambda: self._move_step(-1)).pack(side=tk.LEFT)
        ttk.Button(step_btns, text="Down", command=lambda: self._move_step(1)).pack(side=tk.LEFT, padx=4)
        ttk.Button(step_btns, text="Remove", command=self._remove_step).pack(side=tk.LEFT)
        ttk.Button(step_btns, text="Clear All", command=self._clear_steps).pack(side=tk.LEFT, padx=4)

        action_btns = ttk.Frame(list_frame)
        action_btns.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(action_btns, text="Upload Mission", command=self._upload_mission
                   ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        ttk.Button(action_btns, text="Start Mission", command=self._start_mission
                   ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        ttk.Button(action_btns, text="STOP / LAND", command=self._stop_mission
                   ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

    def _rebuild_param_fields(self):
        for child in self.param_frame.winfo_children():
            child.destroy()
        self.param_vars = {}

        action = self.action_var.get()
        desc, fields = ACTIONS[action]
        self.action_desc_var.set(desc)

        for i, (name, kind, default) in enumerate(fields):
            ttk.Label(self.param_frame, text=f"{name}:").grid(row=i, column=0, sticky="w", pady=2)
            if kind.startswith("choice:"):
                options = kind.split(":", 1)[1].split(",")
                var = tk.StringVar(value=default)
                ttk.Combobox(self.param_frame, textvariable=var, state="readonly",
                             values=options, width=14).grid(row=i, column=1, sticky="w")
            else:
                var = tk.StringVar(value=str(default))
                ttk.Entry(self.param_frame, textvariable=var, width=16).grid(row=i, column=1, sticky="w")
            self.param_vars[name] = (var, kind)

    def _read_step_from_form(self):
        action = self.action_var.get()
        _, fields = ACTIONS[action]
        step = {"action": action}
        for name, kind, _default in fields:
            var, kind = self.param_vars[name]
            raw = var.get().strip()
            if kind == "float":
                try:
                    step[name] = float(raw)
                except ValueError:
                    messagebox.showerror("Invalid value", f"'{name}' must be a number, got {raw!r}")
                    return None
            elif kind == "int":
                try:
                    step[name] = int(raw)
                except ValueError:
                    messagebox.showerror("Invalid value", f"'{name}' must be a whole number, got {raw!r}")
                    return None
            else:
                step[name] = raw
        return step

    def _add_step(self):
        step = self._read_step_from_form()
        if step is None:
            return
        self.steps.append(step)
        self._refresh_step_tree()

    def _refresh_step_tree(self):
        self.step_tree.delete(*self.step_tree.get_children())
        for i, step in enumerate(self.steps, start=1):
            self.step_tree.insert("", "end", iid=str(i - 1),
                                   values=(i, step["action"], format_params(step)))

    def _selected_step_index(self):
        sel = self.step_tree.selection()
        if not sel:
            return None
        return int(sel[0])

    def _move_step(self, direction):
        i = self._selected_step_index()
        if i is None:
            return
        j = i + direction
        if 0 <= j < len(self.steps):
            self.steps[i], self.steps[j] = self.steps[j], self.steps[i]
            self._refresh_step_tree()
            self.step_tree.selection_set(str(j))

    def _remove_step(self):
        i = self._selected_step_index()
        if i is None:
            return
        del self.steps[i]
        self._refresh_step_tree()

    def _clear_steps(self):
        if self.steps and not messagebox.askyesno("Clear all steps", "Remove all steps from the mission?"):
            return
        self.steps = []
        self._refresh_step_tree()

    def _apply_connection(self):
        self.client.ip = self.ip_var.get().strip()
        try:
            self.client.mission_port = int(self.port_var.get())
        except (tk.TclError, ValueError):
            messagebox.showerror("Invalid port", "Mission port must be a number")
            return
        self._log(f"Connection target set to {self.client.ip}:{self.client.mission_port}")

    def _upload_mission(self):
        if not self.steps:
            messagebox.showwarning("No steps", "Add at least one step before uploading.")
            return
        ok, reply = self.client.upload_mission(self.steps)
        if ok:
            self._log(f"Mission uploaded ({len(self.steps)} steps)")
            messagebox.showinfo("Upload", "Mission uploaded and stored on ESP32.")
        else:
            self._log(f"Mission upload FAILED: {reply}")
            messagebox.showerror("Upload failed", f"ESP32 did not confirm storage.\nReply: {reply}")

    def _start_mission(self):
        if not messagebox.askyesno("Confirm START", "Start the uploaded mission now?"):
            return
        ok, detail = self.client.start()
        if ok:
            self._log(f"Mission started: {detail}")
        else:
            self._log(f"START failed: {detail}")
            messagebox.showerror("START failed", f"ESP32 refused START.\nReply: {detail}")

    def _stop_mission(self):
        ok, detail = self.client.stop()
        self._log(f"STOP sent: {detail}" if ok else f"STOP got no reply — {detail}. "
                  f"Do not assume the vehicle is landing; use manual RC override if available.")
        if not ok:
            messagebox.showwarning("No reply", "No reply from ESP32 for STOP. "
                                    "Do not assume the vehicle is landing — use manual RC override.")

    def _quick_run(self, one_step_mission):
        label = one_step_mission[0]["action"].upper()
        if not messagebox.askyesno(f"Confirm {label}", f"Send a 1-step mission ({label}) and start it now?"):
            return
        ok, reply = self.client.upload_mission(one_step_mission)
        if not ok:
            self._log(f"Quick {label} upload FAILED: {reply}")
            messagebox.showerror("Failed", f"Upload failed: {reply}")
            return
        ok, detail = self.client.start()
        if ok:
            self._log(f"Quick {label}: {detail}")
        else:
            self._log(f"Quick {label} START failed: {detail}")
            messagebox.showerror("START failed", f"ESP32 refused START.\nReply: {detail}")

    # ─── Status tab ─────────────────────────────────────────────────────
    def _build_status_tab(self, parent):
        grid = ttk.Frame(parent, padding=12)
        grid.pack(fill=tk.X)

        self.status_vars = {}
        fields = [
            ("running", "Mission running"), ("armed", "Armed"),
            ("gps", "GPS"), ("gps_fix_type", "GPS fix type"), ("gps_satellites", "Satellites"),
            ("alt", "Altitude (m)"), ("lat", "Latitude"), ("lon", "Longitude"),
            ("mode", "Mode (raw)"), ("step", "Step"), ("total_steps", "Total steps"),
            ("completed", "Completed"), ("aborted", "Aborted"),
            ("last_abort", "Last abort (ESP32)"), ("last_important_text", "Last Pixhawk PreArm/important msg"),
        ]
        for i, (key, label) in enumerate(fields):
            ttk.Label(grid, text=label + ":", font=("Segoe UI", 9, "bold")).grid(
                row=i, column=0, sticky="w", pady=2, padx=(0, 10))
            var = tk.StringVar(value="—")
            ttk.Label(grid, textvariable=var).grid(row=i, column=1, sticky="w")
            self.status_vars[key] = var

        ttk.Button(parent, text="Refresh Now", command=self._refresh_status_once).pack(
            anchor="w", padx=12, pady=8)

    def _refresh_status_once(self):
        status = self.client.request_status(timeout=2)
        if status:
            self.status_queue.put(status)
        else:
            messagebox.showwarning("No reply", "No STATUS reply from ESP32.")

    def _status_poll_loop(self):
        while not self._poll_stop.is_set():
            try:
                status = self.client.request_status(timeout=1.5)
                if status:
                    self.status_queue.put(status)
            except Exception:
                pass
            self._poll_stop.wait(STATUS_POLL_SECONDS)

    # ─── Logs tab ───────────────────────────────────────────────────────
    def _build_logs_tab(self, parent):
        parent.rowconfigure(0, weight=3)
        parent.rowconfigure(2, weight=2)
        parent.columnconfigure(0, weight=1)

        ttk.Label(parent, text="Live console (this session)").grid(row=0, column=0, sticky="w", padx=8)
        self.console = scrolledtext.ScrolledText(parent, height=14, state="disabled", wrap="word")
        self.console.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        parent.rowconfigure(1, weight=3)

        past = ttk.Frame(parent)
        past.grid(row=2, column=0, sticky="nsew", padx=8, pady=(0, 8))
        past.columnconfigure(1, weight=1)
        past.rowconfigure(1, weight=1)

        header = ttk.Frame(past)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(header, text="Past run logs (logs/ folder)").pack(side=tk.LEFT)
        ttk.Button(header, text="Refresh", command=self._refresh_log_list).pack(side=tk.RIGHT)

        self.log_listbox = tk.Listbox(past, width=36)
        self.log_listbox.grid(row=1, column=0, sticky="nsew")
        self.log_listbox.bind("<<ListboxSelect>>", self._on_log_selected)

        self.log_viewer = scrolledtext.ScrolledText(past, state="disabled", wrap="none")
        self.log_viewer.grid(row=1, column=1, sticky="nsew", padx=(8, 0))

    def _append_console(self, line):
        self.console.configure(state="normal")
        self.console.insert(tk.END, line + "\n")
        self.console.see(tk.END)
        self.console.configure(state="disabled")

    def _refresh_log_list(self):
        self.log_listbox.delete(0, tk.END)
        if not os.path.isdir(LOGS_DIR):
            return
        files = sorted((f for f in os.listdir(LOGS_DIR) if f.endswith(".log")),
                       key=lambda f: os.path.getmtime(os.path.join(LOGS_DIR, f)), reverse=True)
        for f in files:
            self.log_listbox.insert(tk.END, f)

    def _on_log_selected(self, _event):
        sel = self.log_listbox.curselection()
        if not sel:
            return
        name = self.log_listbox.get(sel[0])
        path = os.path.join(LOGS_DIR, name)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as exc:
            content = f"Could not read {path}: {exc}"
        self.log_viewer.configure(state="normal")
        self.log_viewer.delete("1.0", tk.END)
        self.log_viewer.insert(tk.END, content)
        self.log_viewer.configure(state="disabled")

    # ─── Run Scripts tab ────────────────────────────────────────────────
    def _build_scripts_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        top = ttk.Frame(parent, padding=8)
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="Python scripts in Missions/:").pack(side=tk.LEFT)
        ttk.Button(top, text="Refresh", command=self._refresh_script_list).pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="Browse...", command=self._browse_script).pack(side=tk.LEFT)

        mid = ttk.Frame(parent, padding=(8, 0))
        mid.grid(row=1, column=0, sticky="ew")
        self.script_listbox = tk.Listbox(mid, height=6)
        self.script_listbox.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.script_listbox.bind("<<ListboxSelect>>", self._on_script_selected)

        args_frame = ttk.Frame(parent, padding=8)
        args_frame.grid(row=2, column=0, sticky="ew")
        ttk.Label(args_frame, text="Selected script:").grid(row=0, column=0, sticky="w")
        self.selected_script_var = tk.StringVar(value="(none)")
        ttk.Label(args_frame, textvariable=self.selected_script_var, foreground="#555").grid(
            row=0, column=1, sticky="w")
        ttk.Label(args_frame, text="Extra args:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.script_args_var = tk.StringVar(value="")
        ttk.Entry(args_frame, textvariable=self.script_args_var, width=40).grid(
            row=1, column=1, sticky="w", pady=(6, 0))

        btns = ttk.Frame(parent, padding=8)
        btns.grid(row=3, column=0, sticky="ew")
        self.run_btn = ttk.Button(btns, text="Run", command=self._run_script)
        self.run_btn.pack(side=tk.LEFT)
        self.stop_script_btn = ttk.Button(btns, text="Stop", command=self._stop_script, state="disabled")
        self.stop_script_btn.pack(side=tk.LEFT, padx=6)

        parent.rowconfigure(4, weight=1)
        self.script_console = scrolledtext.ScrolledText(parent, state="disabled", wrap="word")
        self.script_console.grid(row=4, column=0, sticky="nsew", padx=8, pady=8)

        self.selected_script_path = None

    def _refresh_script_list(self):
        self.script_listbox.delete(0, tk.END)
        if not os.path.isdir(MISSIONS_DIR):
            return
        for f in sorted(os.listdir(MISSIONS_DIR)):
            if f.endswith(".py"):
                self.script_listbox.insert(tk.END, f)

    def _on_script_selected(self, _event):
        sel = self.script_listbox.curselection()
        if not sel:
            return
        name = self.script_listbox.get(sel[0])
        self.selected_script_path = os.path.join(MISSIONS_DIR, name)
        self.selected_script_var.set(name)

    def _browse_script(self):
        path = filedialog.askopenfilename(title="Choose a Python script",
                                           filetypes=[("Python files", "*.py"), ("All files", "*.*")])
        if path:
            self.selected_script_path = path
            self.selected_script_var.set(path)

    def _append_script_console(self, line):
        self.script_console.configure(state="normal")
        self.script_console.insert(tk.END, line + "\n")
        self.script_console.see(tk.END)
        self.script_console.configure(state="disabled")

    def _run_script(self):
        if self.script_proc and self.script_proc.poll() is None:
            messagebox.showwarning("Already running", "A script is already running. Stop it first.")
            return
        if not self.selected_script_path or not os.path.isfile(self.selected_script_path):
            messagebox.showwarning("No script", "Select or browse to a .py script first.")
            return

        import shlex
        args = shlex.split(self.script_args_var.get())
        cmd = [sys.executable, self.selected_script_path] + args
        self._append_script_console(f"$ {' '.join(cmd)}")
        try:
            self.script_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, cwd=os.path.dirname(self.selected_script_path) or None,
            )
        except OSError as exc:
            messagebox.showerror("Failed to start", str(exc))
            return

        self.run_btn.configure(state="disabled")
        self.stop_script_btn.configure(state="normal")
        self._log(f"Running script: {' '.join(cmd)}")
        threading.Thread(target=self._pump_script_output, daemon=True).start()

    def _pump_script_output(self):
        proc = self.script_proc
        for line in proc.stdout:
            self.script_queue.put(line.rstrip("\n"))
        proc.wait()
        self.script_queue.put(f"[process exited with code {proc.returncode}]")
        self.script_queue.put("__DONE__")

    def _stop_script(self):
        if self.script_proc and self.script_proc.poll() is None:
            self.script_proc.terminate()
            self._append_script_console("[stop requested]")

    # ─── Queue draining (runs on the Tk main thread) ────────────────────
    def _drain_queues(self):
        try:
            while True:
                status = self.status_queue.get_nowait()
                self._apply_status(status)
        except queue.Empty:
            pass

        try:
            while True:
                line = self.script_queue.get_nowait()
                if line == "__DONE__":
                    self.run_btn.configure(state="normal")
                    self.stop_script_btn.configure(state="disabled")
                else:
                    self._append_script_console(line)
        except queue.Empty:
            pass

        if self.logger:
            for severity, text in self.logger.drain_statustext():
                sev_name = MAV_SEVERITY_NAMES.get(severity, str(severity))
                self._append_console(f"[Pixhawk {sev_name}] {text}")

        self.after(150, self._drain_queues)

    def _apply_status(self, status):
        self.conn_indicator.configure(text="● live", foreground="green")
        for key, var in self.status_vars.items():
            if key in ("running", "armed", "completed", "aborted"):
                var.set("YES" if status.get(key) else "NO")
            elif key == "gps":
                var.set("OK" if status.get(key) else "NO")
            elif key in ("lat", "lon"):
                var.set(f"{status.get(key, 0):.6f}")
            elif key == "alt":
                var.set(f"{status.get(key, 0):.2f}")
            else:
                var.set(str(status.get(key, "—")))


def main():
    os.makedirs(LOGS_DIR, exist_ok=True)
    app = GCSApp()
    app.mainloop()


if __name__ == "__main__":
    main()
