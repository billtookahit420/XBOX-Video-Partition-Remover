#!/usr/bin/env python3
"""XBOX Video Partition Remover - GUI.

Strips the 387 MiB video partition from full Redump Xbox ISOs
(the in-process equivalent of `dd skip=387 bs=1M`), leaving the
~6.9 GiB game ISO. Queue-based like the rest of the converter
family: Add Game / Add Folder / Network Share, sequential
processing, a status bar that always says what is happening,
and a queue + results log that persist to disk.

All the real work lives in xvpr_core (GUI-free); network shares
are netshare (the family's proven GVFS/UNC implementation).
"""
import os
import queue as queue_mod
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))

import netshare  # noqa: E402
import xvpr_core as core  # noqa: E402

APP_NAME = "XBOX Video Partition Remover"
APP_ROOT = Path(__file__).resolve().parent.parent


def fmt_size(n):
    if n is None:
        return ""
    for unit, f in (("GiB", 1024 ** 3), ("MiB", 1024 ** 2),
                    ("KiB", 1024)):
        if abs(n) >= f:
            return f"{n / f:.2f} {unit}"
    return f"{n} B"


class App:
    def __init__(self, root):
        self.root = root
        self.home = core.app_home(APP_ROOT)
        self.queue_file = self.home / "queue.json"
        self.settings_file = self.home / "settings.json"
        self.results_file = self.home / "results.log"
        self.scratch_dir = self.home / "archive-scratch"
        self.scratch_dir.mkdir(parents=True, exist_ok=True)

        self.settings = core.load_json(self.settings_file, {})
        self.items = []            # queue item dicts
        self._next_id = 1
        self.busy = False
        self.cancel_event = threading.Event()
        self._ui = queue_mod.Queue()
        self._progress = {}        # item id -> (done, total, t, speed)
        self._ask_result = None
        self._ask_event = threading.Event()
        # Tests/drives may replace these with scripted answers.
        self.confirm_hook = None
        self.info_hook = None

        root.title(APP_NAME)
        root.geometry("880x620")
        root.minsize(720, 480)
        self._build_ui()
        self._load_queue()
        self._refresh_rows()
        self._set_status("Ready.")
        self.root.after(100, self._drain_ui)
        self.root.after(300, self._maybe_first_run_prompt)

    # ------------------------------------------------------------ UI

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        top.pack(fill="x")
        ttk.Button(top, text="Add Game...",
                   command=self._add_game_dialog).pack(side="left")
        ttk.Button(top, text="Add Folder...",
                   command=self._add_folder_dialog).pack(
                       side="left", padx=(6, 0))
        ttk.Button(top, text="Network Share...",
                   command=self._share_dialog).pack(side="left",
                                                    padx=(6, 0))
        ttk.Button(top, text="Remove",
                   command=self._remove_selected).pack(
                       side="left", padx=(6, 0))
        ttk.Button(top, text="Clear Finished",
                   command=self._clear_finished).pack(
                       side="left", padx=(6, 0))
        ttk.Button(top, text="7-Zip Setup...",
                   command=self._setup_dialog).pack(side="right")

        out = ttk.Frame(self.root, padding=(8, 0, 8, 4))
        out.pack(fill="x")
        ttk.Label(out, text="Output folder:").pack(side="left")
        self.output_var = tk.StringVar(
            value=self.settings.get("output_dir", ""))
        ttk.Entry(out, textvariable=self.output_var).pack(
            side="left", fill="x", expand=True, padx=(6, 6))
        ttk.Button(out, text="Browse...",
                   command=self._browse_output).pack(side="left")

        mid = ttk.Frame(self.root, padding=(8, 0, 8, 4))
        mid.pack(fill="both", expand=True)
        cols = ("name", "size", "status", "detail")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings",
                                 selectmode="extended")
        for col, title, width in (
                ("name", "Game", 300), ("size", "Size", 90),
                ("status", "Status", 110),
                ("detail", "Detail", 340)):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True)

        ctl = ttk.Frame(self.root, padding=(8, 0, 8, 4))
        ctl.pack(fill="x")
        self.process_btn = ttk.Button(
            ctl, text="Process Queue", command=self.start_processing)
        self.process_btn.pack(side="left")
        self.cancel_btn = ttk.Button(
            ctl, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=(6, 0))
        self.progress = ttk.Progressbar(ctl, mode="determinate",
                                        maximum=1000)
        self.progress.pack(side="left", fill="x", expand=True,
                           padx=(10, 6))
        self.progress_label = ttk.Label(ctl, text="")
        self.progress_label.pack(side="left")

        logf = ttk.LabelFrame(self.root, text="Log",
                              padding=(6, 4, 6, 6))
        logf.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self.log_text = tk.Text(logf, height=8, wrap="word",
                                state="disabled")
        self.log_text.pack(fill="both", expand=True)

        self.status_var = tk.StringVar(value="")
        status = ttk.Label(self.root, textvariable=self.status_var,
                           relief="sunken", anchor="w",
                           padding=(6, 3))
        status.pack(fill="x", side="bottom")

    # ------------------------------------------------------------ helpers

    def _set_status(self, text):
        self.status_var.set(text)

    def log(self, msg):
        line = f"[{core.stamp()}] {msg}"
        try:
            with open(self.results_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        self._ui.put(("log", line))

    def _drain_ui(self):
        try:
            while True:
                kind, payload = self._ui.get_nowait()
                if kind == "log":
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", payload + "\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "row":
                    self._refresh_row(payload)
                elif kind == "status":
                    self._set_status(payload)
                elif kind == "progress":
                    self._show_progress()
                elif kind == "ask":
                    self._do_ask(payload)
                elif kind == "done":
                    self._processing_done(payload)
                elif kind == "mounted":
                    cb, path, err = payload
                    cb(path, err)
                elif kind == "setup_done":
                    ok, msg = payload
                    self._setup_finished(ok, msg)
        except queue_mod.Empty:
            pass
        self.root.after(100, self._drain_ui)

    def _show_progress(self):
        cur = self._progress.get("current")
        if not cur:
            self.progress["value"] = 0
            self.progress_label.configure(text="")
            return
        done, total, speed = cur
        pct = (done / total * 100) if total else 0
        self.progress["value"] = int(done / total * 1000) if total \
            else 0
        spd = f" - {fmt_size(int(speed))}/s" if speed else ""
        self.progress_label.configure(
            text=f"{fmt_size(done)} / {fmt_size(total)} "
                 f"({pct:.0f}%){spd}")

    def notify(self, message, warning=False, error=False):
        """Show an informational dialog (scripted in tests)."""
        if self.info_hook is not None:
            self.info_hook(message)
            return
        if error:
            messagebox.showerror(APP_NAME, message, parent=self.root)
        elif warning:
            messagebox.showwarning(APP_NAME, message,
                                   parent=self.root)
        else:
            messagebox.showinfo(APP_NAME, message, parent=self.root)

    def ask_user(self, kind, message):
        """Ask a yes/no question. Safe to call from worker threads:
        the dialog itself is shown on the UI thread."""
        if self.confirm_hook is not None:
            return bool(self.confirm_hook(kind, message))
        self._ask_result = None
        self._ask_event.clear()
        self._ui.put(("ask", (kind, message)))
        self._ask_event.wait()
        return bool(self._ask_result)

    def _do_ask(self, payload):
        kind, message = payload
        title = {"size_warning": "Unexpected size",
                 "overwrite": "Output exists",
                 "scratch_warning": "Low disk space"}.get(
                     kind, APP_NAME)
        self._ask_result = messagebox.askyesno(title, message,
                                               parent=self.root)
        self._ask_event.set()

    # ------------------------------------------------------------ queue

    def _new_item(self, name, input_path, size, source="",
                  archive="", member=""):
        item = {
            "id": self._next_id,
            "name": name,
            "input": str(input_path),
            "source": source or str(input_path),
            "archive": archive,
            "member": member,
            "size": size,
            "status": "Queued",
            "detail": "",
        }
        self._next_id += 1
        self.items.append(item)
        return item

    def _save_queue(self):
        core.save_json_atomic(
            self.queue_file,
            {"next_id": self._next_id, "items": self.items})

    def _load_queue(self):
        data = core.load_json(self.queue_file, {})
        self._next_id = data.get("next_id", 1)
        recovered = False
        for item in data.get("items", []):
            if item.get("status") in ("Processing", "Extracting"):
                item["status"] = "Queued"
                item["detail"] = "recovered after an app restart"
                recovered = True
            self.items.append(item)
        if recovered:
            self.log("Queue recovered after a restart; in-flight "
                     "items were re-queued.")

    def _refresh_rows(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for item in self.items:
            self.tree.insert("", "end", iid=str(item["id"]),
                             values=self._row_values(item))

    def _refresh_row(self, item_id):
        for item in self.items:
            if item["id"] == item_id:
                if self.tree.exists(str(item_id)):
                    self.tree.item(str(item_id),
                                   values=self._row_values(item))
                return

    @staticmethod
    def _row_values(item):
        return (item["name"], fmt_size(item["size"]), item["status"],
                item["detail"])

    def _remove_selected(self):
        sel = {int(i) for i in self.tree.selection()}
        if not sel:
            return
        keep = []
        for item in self.items:
            if item["id"] in sel:
                self._cleanup_item_scratch(item)
            else:
                keep.append(item)
        self.items = keep
        self._save_queue()
        self._refresh_rows()

    def _clear_finished(self):
        keep = []
        for item in self.items:
            if item["status"] in ("Done", "Failed", "Blocked",
                                  "Skipped"):
                self._cleanup_item_scratch(item)
            else:
                keep.append(item)
        self.items = keep
        self._save_queue()
        self._refresh_rows()

    def _cleanup_item_scratch(self, item):
        if item.get("archive"):
            p = Path(item["input"])
            if str(p).startswith(str(self.scratch_dir)):
                core._silent_unlink(p)

    # ------------------------------------------------------------ adding

    def _add_game_dialog(self):
        path = filedialog.askopenfilename(
            title="Add Game",
            filetypes=[("Xbox images and archives",
                        "*.iso *.zip *.7z"),
                       ("All files", "*.*")])
        if path:
            self.add_path(Path(path))

    def _add_folder_dialog(self):
        path = filedialog.askdirectory(title="Add Folder")
        if path:
            self.add_folder(Path(path))

    def _browse_output(self):
        path = filedialog.askdirectory(title="Output folder",
                                       initialdir=self.output_var.get()
                                       or None)
        if path:
            self.output_var.set(path)
            self._save_settings()

    def _save_settings(self):
        self.settings["output_dir"] = self.output_var.get()
        core.save_json_atomic(self.settings_file, self.settings)

    def add_path(self, path):
        """Add one file: an .iso, or an archive whose ISOs are
        extracted to local scratch and queued individually."""
        path = Path(path)
        if core.is_archive(path):
            self._add_archive(path)
        elif path.suffix.lower() == ".iso":
            self._add_isos([path])
        else:
            self.log(f"Skipped {path.name}: not an .iso, .zip or .7z")

    def add_folder(self, folder):
        isos = core.find_isos(folder)
        if not isos:
            self.log(f"No .iso files found under {folder}")
            return
        self._add_isos(isos)

    def _add_isos(self, paths):
        refused, warned, ready = [], [], []
        for p in paths:
            try:
                size = p.stat().st_size
            except OSError as ex:
                refused.append((p, f"cannot read: {ex}"))
                continue
            verdict, msg = core.check_input_size(size)
            if verdict == "refuse":
                refused.append((p, msg))
            elif verdict == "warn":
                warned.append((p, size, msg))
            else:
                ready.append((p, size))
        for p, why in refused:
            self.log(f"Refused {p.name}: {why}")
        if refused:
            names = "\n".join(f" - {p.name}: {why}"
                              for p, why in refused[:8])
            self.notify("These files cannot be processed:\n"
                        + names, warning=True)
        if warned:
            listing = "\n".join(f" - {p.name} ({msg})"
                                for p, size, msg in warned[:8])
            more = (f"\n - ...and {len(warned) - 8} more"
                    if len(warned) > 8 else "")
            if self.ask_user(
                    "size_warning",
                    "These are not the expected full-dump size "
                    f"({core.EXPECTED_FULL_SIZE:,} bytes):\n"
                    f"{listing}{more}\n\nAdd them anyway?"):
                ready.extend((p, size) for p, size, msg in warned)
            else:
                for p, size, msg in warned:
                    self.log(f"Skipped {p.name}: {msg}")
        added = 0
        for p, size in ready:
            self._new_item(p.name, p, size)
            added += 1
        if added:
            where = netshare.describe_path(paths[0]) or "local disk"
            self.log(f"Queued {added} game(s) from {where}.")
            self._save_queue()
            self._refresh_rows()
            self._set_status(f"{added} game(s) queued.")

    def _add_archive(self, path):
        warn = core.scratch_space_warning(path, self.scratch_dir)
        if warn and not self.ask_user("scratch_warning",
                                      warn + "\n\nAdd it anyway?"):
            self.log(f"Skipped {path.name}: {warn}")
            return
        try:
            extracted = core.extract_archive(path, self.scratch_dir)
        except core.StripError as ex:
            self.log(f"Could not add {path.name}: {ex}")
            self.notify(str(ex), error=True)
            return
        added = 0
        for iso_path, member in extracted:
            size = iso_path.stat().st_size
            verdict, msg = core.check_input_size(size)
            if verdict == "refuse":
                self.log(f"Refused {member} from {path.name}: {msg}")
                core._silent_unlink(iso_path)
                continue
            item = self._new_item(
                iso_path.name, iso_path, size,
                source=f"{path} [{member}]", archive=str(path),
                member=member)
            item["detail"] = f"from archive {path.name}"
            added += 1
        if added:
            self.log(f"Queued {added} game(s) from archive "
                     f"{path.name} (extracted to local scratch).")
            self._save_queue()
            self._refresh_rows()

    # ------------------------------------------------------------ processing

    def start_processing(self):
        if self.busy:
            return
        outdir = self.output_var.get().strip()
        if not outdir:
            self.notify("Pick an output folder first.")
            return
        pending = [i for i in self.items
                   if i["status"] in ("Queued", "Failed", "Blocked",
                                      "Skipped")]
        if not pending:
            self.notify("Nothing queued to process.")
            return
        self._save_settings()
        self.busy = True
        self.cancel_event.clear()
        self.process_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        threading.Thread(target=self._process_worker,
                         args=(Path(outdir), pending),
                         daemon=True).start()

    def _cancel(self):
        self.cancel_event.set()
        self._set_status("Cancelling...")

    def _process_worker(self, outdir, pending):
        done = failed = 0
        total = len(pending)
        try:
            for n, item in enumerate(pending, 1):
                if self.cancel_event.is_set():
                    self.log("Processing cancelled.")
                    break
                self._ui.put(("status",
                              f"Processing {n} of {total}: "
                              f"{item['name']}"))
                ok = self._process_item(item, outdir)
                if ok:
                    done += 1
                else:
                    failed += 1
                self._save_queue()
        finally:
            self._ui.put(("done", (done, failed, total)))

    def _process_item(self, item, outdir):
        src = Path(item["input"])
        dst = core.output_path_for(src, outdir)
        item["status"] = "Processing"
        item["detail"] = ""
        self._ui.put(("row", item["id"]))
        # An archive item whose extracted copy vanished (restart)
        # is re-extracted from its source archive.
        if item.get("archive") and not src.exists():
            try:
                extracted = core.extract_archive(
                    Path(item["archive"]), self.scratch_dir)
                for p, member in extracted:
                    if member == item["member"]:
                        item["input"] = str(p)
                        src = p
                        break
            except core.StripError as ex:
                item["status"] = "Failed"
                item["detail"] = str(ex)
                self._ui.put(("row", item["id"]))
                self.log(f"FAILED {item['name']}: {ex}")
                return False
        if core.paths_equal(src, dst):
            item["status"] = "Blocked"
            item["detail"] = ("output would overwrite the source "
                              "file itself - pick a different "
                              "output folder")
            self._ui.put(("row", item["id"]))
            self.log(f"BLOCKED {item['name']}: {item['detail']}")
            return False
        if dst.exists():
            if not self.ask_user(
                    "overwrite",
                    f"{dst} already exists.\n\nOverwrite it?"):
                item["status"] = "Skipped"
                item["detail"] = "output exists; overwrite declined"
                self._ui.put(("row", item["id"]))
                self.log(f"Skipped {item['name']}: output exists "
                         "and overwrite was declined.")
                return False
        state = {"t0": time.time(), "done0": 0, "speed": 0.0}

        def progress(done, total_bytes):
            now = time.time()
            dt = now - state["t0"]
            if dt >= 0.25:
                state["speed"] = (done - state["done0"]) / dt
                state["t0"] = now
                state["done0"] = done
            self._progress["current"] = (done, total_bytes,
                                         state["speed"])
            self._ui.put(("progress", None))

        try:
            in_size, out_size = core.strip_file(
                src, dst, progress=progress,
                cancel=self.cancel_event.is_set)
        except core.StripCancelled:
            item["status"] = "Queued"
            item["detail"] = "cancelled"
            self._ui.put(("row", item["id"]))
            self.log(f"Cancelled while processing {item['name']}.")
            return False
        except core.StripError as ex:
            item["status"] = "Failed"
            item["detail"] = str(ex)
            self._ui.put(("row", item["id"]))
            self.log(f"FAILED {item['name']}: {ex}")
            return False
        finally:
            self._progress.pop("current", None)
            self._ui.put(("progress", None))
        item["status"] = "Done"
        item["detail"] = f"{in_size:,} -> {out_size:,} bytes"
        self._ui.put(("row", item["id"]))
        self.log(f"Done {item['name']}: {in_size:,} bytes in, "
                 f"{out_size:,} bytes out -> {dst}")
        self._cleanup_item_scratch(item)
        return True

    def _processing_done(self, payload):
        done, failed, total = payload
        self.busy = False
        self.process_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self._refresh_rows()
        self._set_status(f"Finished: {done} done, {failed} not "
                         f"completed, of {total} queued.")

    # ------------------------------------------------------------ network shares

    def mount_share_async(self, server, share, username, password,
                          workgroup, callback):
        """Mount on a worker thread; callback(path, err) runs on
        the UI thread. Remembers the share (never the password)."""
        def work():
            path, err = netshare.mount_share(server, share, username,
                                             password, workgroup)
            if path:
                recent = [r for r in
                          self.settings.get("recent_shares", [])
                          if not (r.get("server") == server
                                  and r.get("share") == share)]
                recent.insert(0, {"server": server, "share": share,
                                  "username": username})
                self.settings["recent_shares"] = recent[:8]
                self.settings["last_server"] = server
                core.save_json_atomic(self.settings_file,
                                      self.settings)
            self._ui.put(("mounted", (callback, path, err)))
        threading.Thread(target=work, daemon=True).start()

    def _share_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("Network Share")
        dlg.transient(self.root)
        dlg.grab_set()
        frm = ttk.Frame(dlg, padding=10)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Server:").grid(row=0, column=0,
                                            sticky="e", pady=2)
        ttk.Label(frm, text="Share:").grid(row=1, column=0,
                                           sticky="e", pady=2)
        ttk.Label(frm, text="Username:").grid(row=2, column=0,
                                              sticky="e", pady=2)
        ttk.Label(frm, text="Password:").grid(row=3, column=0,
                                              sticky="e", pady=2)
        ttk.Label(frm, text="Workgroup:").grid(row=4, column=0,
                                               sticky="e", pady=2)
        server_v = tk.StringVar(
            value=self.settings.get("last_server", ""))
        share_v = tk.StringVar()
        user_v = tk.StringVar()
        pass_v = tk.StringVar()
        wg_v = tk.StringVar(value="WORKGROUP")
        ttk.Entry(frm, textvariable=server_v, width=34).grid(
            row=0, column=1, pady=2)
        ttk.Entry(frm, textvariable=share_v, width=34).grid(
            row=1, column=1, pady=2)
        ttk.Entry(frm, textvariable=user_v, width=34).grid(
            row=2, column=1, pady=2)
        ttk.Entry(frm, textvariable=pass_v, width=34,
                  show="*").grid(row=3, column=1, pady=2)
        ttk.Entry(frm, textvariable=wg_v, width=34).grid(
            row=4, column=1, pady=2)
        recent = self.settings.get("recent_shares", [])
        if recent:
            ttk.Label(frm, text="Recent:").grid(row=5, column=0,
                                                sticky="e", pady=2)
            labels = [f"{r['server']}/{r['share']}"
                      for r in recent]
            combo = ttk.Combobox(frm, values=labels, width=32,
                                 state="readonly")
            combo.grid(row=5, column=1, pady=2, sticky="w")

            def pick_recent(_evt):
                r = recent[combo.current()]
                server_v.set(r["server"])
                share_v.set(r["share"])
                user_v.set(r.get("username", ""))
            combo.bind("<<ComboboxSelected>>", pick_recent)
        note = ttk.Label(
            frm, text="The password is used for this mount only "
                      "and is never saved.")
        note.grid(row=6, column=0, columnspan=2, pady=(6, 2))
        status = ttk.Label(frm, text="")
        status.grid(row=7, column=0, columnspan=2)
        btns = ttk.Frame(frm)
        btns.grid(row=8, column=0, columnspan=2, pady=(8, 0))

        def connect(after):
            server, share = server_v.get().strip(), \
                share_v.get().strip()
            if not server or not share:
                status.configure(text="Server and share are both "
                                      "required.")
                return
            status.configure(text="Connecting...")
            for b in btns.winfo_children():
                b.configure(state="disabled")

            def done(path, err):
                if err:
                    status.configure(text=err)
                    for b in btns.winfo_children():
                        b.configure(state="normal")
                    return
                dlg.destroy()
                after(path)
            self.mount_share_async(server, share, user_v.get(),
                                   pass_v.get(), wg_v.get(), done)

        ttk.Button(btns, text="Connect && Add Game...",
                   command=lambda: connect(self._share_add_game)
                   ).pack(side="left")
        ttk.Button(btns, text="Connect && Add Folder...",
                   command=lambda: connect(self._share_add_folder)
                   ).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Use as Output Folder",
                   command=lambda: connect(self._share_set_output)
                   ).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Cancel",
                   command=dlg.destroy).pack(side="left",
                                             padx=(6, 0))

    def _share_add_game(self, mount_path):
        path = filedialog.askopenfilename(
            title="Add Game from share", initialdir=mount_path,
            filetypes=[("Xbox images and archives",
                        "*.iso *.zip *.7z"), ("All files", "*.*")])
        if path:
            self.add_path(Path(path))

    def _share_add_folder(self, mount_path):
        path = filedialog.askdirectory(
            title="Add Folder from share", initialdir=mount_path)
        if path:
            self.add_folder(Path(path))

    def _share_set_output(self, mount_path):
        path = filedialog.askdirectory(
            title="Output folder on share", initialdir=mount_path)
        if path:
            self.output_var.set(path)
            self._save_settings()

    # ------------------------------------------------------------ first-run setup

    def _maybe_first_run_prompt(self):
        if self.settings.get("setup_prompted"):
            return
        self.settings["setup_prompted"] = True
        core.save_json_atomic(self.settings_file, self.settings)
        if core.find_7z_tool(self.home) is not None:
            return
        if messagebox.askyesno(
                APP_NAME,
                "First-run setup: this app can download 7-Zip "
                "(about 1.6 MB, from the official 7-Zip release, "
                "SHA-256 verified) so it can open .7z archives.\n\n"
                "Plain .iso and .zip files work without it.\n\n"
                "Download it now?",
                parent=self.root):
            self.run_setup()

    def _setup_dialog(self):
        if core.find_7z_tool(self.home) is not None:
            self.notify("7-Zip is already available: "
                        + str(core.find_7z_tool(self.home)))
            return
        if messagebox.askyesno(
                APP_NAME,
                "Download 7-Zip from the official release "
                "(SHA-256 verified) into the app's tools folder?",
                parent=self.root):
            self.run_setup()

    def run_setup(self):
        self._set_status("First-run setup: downloading 7-Zip...")
        self.log("First-run setup started.")

        def work():
            ok, msg = core.run_first_run_setup(
                self.home, log=self.log)
            self._ui.put(("setup_done", (ok, msg)))
        threading.Thread(target=work, daemon=True).start()

    def _setup_finished(self, ok, msg):
        self._set_status(msg)
        self.log(f"First-run setup: {msg}")
        if not ok:
            self.notify("First-run setup did not complete:\n"
                        + msg + "\n\n.iso and .zip files still "
                                "work; .7z archives need 7-Zip.",
                        warning=True)


def main():
    root = tk.Tk()
    app = App(root)
    if os.environ.get("XVPR_SMOKE"):
        root.update()
        print(f"smoke: home={app.home} items={len(app.items)} "
              f"7z={core.find_7z_tool(app.home)}")
        root.destroy()
        return 0
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
