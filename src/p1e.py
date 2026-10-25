
# --------------------------------------------------------------------------- crash detection and error reports
# Three kinds of trouble are caught and turned into a report file under ~/.cache/kestrel/reports/:
#   error  — an unhandled Python exception (GTK callback, worker thread). Kestrel keeps running; the window
#            shows a bar with "Review & report…". Each distinct failure is reported once per session.
#   crash  — the process died (segfault in a C library, abort, killed). faulthandler writes the stacks of
#            every thread to a per-process file on fatal signals; a per-process session marker that is only
#            removed on a clean exit tells the next start that the previous run ended abnormally.
#   freeze — the stall watchdog's >4 s lock-up dump (p1c), picked up the same way.
# Nothing is ever sent automatically: the user reviews (and can edit) the report, then opens it as a
# pre-filled GitHub issue (or copies it). Home folder and user name are replaced with "~" / "<user>" before anything is shown.

REPORT_ISSUES_URL = "https://github.com/KCArrow/kestrel/issues/new"
REPORT_DIR = os.path.join(GLib.get_user_cache_dir(), "kestrel", "reports")


class CrashReporter:
    KEEP = 40                      # archived (sent / dismissed) reports kept

    def __init__(self, directory: str = REPORT_DIR):
        self.dir = directory
        self.archive_dir = os.path.join(directory, "archive")
        self.pid = os.getpid()
        self.started = time.time()
        self.seen: set = set()
        self.previous: List[str] = []          # reports about the last run that deserve a prompt
        self.on_report: Optional[Callable[[str], object]] = None   # GUI hook, called on the GTK thread
        self._fault_fh = None
        self._prev_hook = sys.excepthook
        self._prev_thread_hook = getattr(threading, "excepthook", None)
        self.installed = False

    # ---- lifecycle
    def _session_file(self, pid: int) -> str:
        return os.path.join(self.dir, f"session-{pid}.json")

    def _fault_file(self, pid: int) -> str:
        return os.path.join(self.dir, f"fault-{pid}.txt")

    def install(self):
        """Hook exceptions and fatal signals and mark this process as running. Call once, early."""
        try:
            os.makedirs(self.archive_dir, exist_ok=True)
            self.previous = self._check_previous()
            with open(self._session_file(self.pid), "w") as f:
                json.dump({"pid": self.pid, "started": self.started, "version": VERSION}, f)
            import faulthandler
            self._fault_fh = open(self._fault_file(self.pid), "w")
            faulthandler.enable(file=self._fault_fh, all_threads=True)
        except Exception as ex:
            sys.stderr.write(f"kestrel: crash reporter unavailable: {ex}\n")
        sys.excepthook = self._excepthook
        if self._prev_thread_hook is not None:
            threading.excepthook = self._thread_hook
        self.installed = True

    def shutdown(self):
        """Clean exit: this run did not crash."""
        if not self.installed:
            return
        try:
            import faulthandler
            faulthandler.disable()
            if self._fault_fh:
                self._fault_fh.close()
            for p in (self._fault_file(self.pid), self._session_file(self.pid)):
                if os.path.exists(p):
                    os.remove(p)
        except Exception:
            pass
        self.installed = False

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return b"kestrel" in f.read()            # a recycled pid belongs to something else
        except OSError:
            return True

    def _check_previous(self) -> List[str]:
        """Turn the leftovers of runs that ended abnormally into reports. Returns those worth prompting for
        (a stack trace or a freeze dump exists); a run that simply vanished (SIGKILL, OOM killer, power
        loss) is recorded but not prompted for, since there's nothing to diagnose."""
        prompt = []
        for name in os.listdir(self.dir):
            m = re.match(r"session-(\d+)\.json$", name)
            if not m:
                continue
            pid = int(m.group(1))
            if pid == self.pid or self._alive(pid):
                continue                                 # another Kestrel window process, still running
            try:
                with open(self._session_file(pid)) as f:
                    info = json.load(f)
            except Exception:
                info = {}
            fault = ""
            try:
                with open(self._fault_file(pid)) as f:
                    fault = f.read().strip()
            except OSError:
                pass
            started = float(info.get("started", 0))
            freeze = ""
            cache = os.path.dirname(self.dir)
            done = set(info.get("reported_freezes", []))
            for fz in sorted(glob.glob(os.path.join(cache, "freeze-*.txt"))):
                if os.path.getmtime(fz) >= started and os.path.basename(fz) not in done:
                    try:
                        with open(fz) as f:
                            freeze = f.read()
                    except OSError:
                        pass
            when = _dt.datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M") if started else "?"
            if fault:
                summary = f"Kestrel {info.get('version', '?')} crashed (run started {when})"
                body = "Fatal signal — stacks of every thread at the moment of the crash:\n\n" + fault
                if freeze:
                    body += "\n\nThe interface had frozen before the crash:\n\n" + freeze
                prompt.append(self._write("crash", summary, body))
            elif freeze:
                summary = f"Kestrel {info.get('version', '?')} froze and was closed (run started {when})"
                prompt.append(self._write("freeze", summary, freeze))
            else:
                self._write("ended", f"Kestrel {info.get('version', '?')} ended unexpectedly (run started {when})",
                            "No stack trace: the process was killed (out of memory, SIGKILL, logout without "
                            "a clean quit, power loss).")
            for p in (self._session_file(pid), self._fault_file(pid)):
                try:
                    os.remove(p)
                except OSError:
                    pass
        return prompt

    def freeze_report(self, dump_path: str):
        """Called by the stall watchdog (its own thread) after a >4 s freeze dump. One report per session;
        the bar appears once the interface is responsive again (idle_add runs on the GTK thread)."""
        if "freeze" in self.seen:
            return
        self.seen.add("freeze")
        try:
            with open(dump_path) as f:
                text = f.read()
            path = self._write("freeze", "Kestrel's window stopped responding", text, "MainThread")
            sess = self._session_file(self.pid)
            with open(sess) as f:
                info = json.load(f)
            info["reported_freezes"] = info.get("reported_freezes", []) + [os.path.basename(dump_path)]
            with open(sess, "w") as f:
                json.dump(info, f)
            if self.on_report is not None:
                GLib.idle_add(lambda: (self.on_report(path), False)[1])
        except Exception:
            pass

    # ---- exceptions
    def _excepthook(self, etype, value, tb):
        try:
            self._prev_hook(etype, value, tb)            # still print it to the terminal
        except Exception:
            pass
        if not issubclass(etype, (KeyboardInterrupt, SystemExit)):
            self.record(etype, value, tb, threading.current_thread().name)

    def _thread_hook(self, args):
        try:
            self._prev_thread_hook(args)
        except Exception:
            pass
        if args.exc_type is not None and not issubclass(args.exc_type, SystemExit):
            self.record(args.exc_type, args.exc_value, args.exc_traceback, args.thread.name if args.thread else "?")

    def record(self, etype, value, tb, thread: str = "MainThread") -> Optional[str]:
        """Write a report for an exception (once per distinct failure site) and notify the GUI."""
        import traceback
        try:
            frames = traceback.extract_tb(tb)
            sig = (etype.__name__, tuple((f.name, f.lineno) for f in frames[-3:]))
            if sig in self.seen:
                return None
            self.seen.add(sig)
            text = "".join(traceback.format_exception(etype, value, tb))
            where = frames[-1].name if frames else "?"
            path = self._write("error", f"{etype.__name__} in {where}(): {value}"[:160], text, thread)
            if self.on_report is not None:
                GLib.idle_add(lambda: (self.on_report(path), False)[1])
            return path
        except Exception:
            return None

    # ---- report files
    @staticmethod
    def sanitize(text: str) -> str:
        """Replace the home folder and the user name, so a report doesn't identify the machine's owner."""
        home = os.path.expanduser("~")
        if home and home != "/":
            text = text.replace(home, "~")
        try:
            user = pwd.getpwuid(os.getuid()).pw_name
        except Exception:
            user = os.environ.get("USER", "")
        if user and len(user) >= 3:
            text = re.sub(r"(?<![\w])" + re.escape(user) + r"(?![\w])", "<user>", text)
        return text

    @staticmethod
    def environment() -> str:
        rows = [("Kestrel", VERSION), ("Python", sys.version.split()[0]),
                ("PyGObject", getattr(gi, "__version__", "?")),
                ("GTK", f"{Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}"),
                ("GLib", ".".join(str(x) for x in GLib.glib_version))]
        distro = "?"
        try:
            with open("/etc/os-release") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        distro = line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
        rows.append(("OS", distro))
        rows.append(("Kernel", os.uname().release))
        rows.append(("Desktop", f"{os.environ.get('XDG_CURRENT_DESKTOP', '?')} · {os.environ.get('XDG_SESSION_TYPE', '?')}"
                                + (f" · GDK_BACKEND={os.environ['GDK_BACKEND']}" if os.environ.get("GDK_BACKEND") else "")))
        mem = "?"
        try:
            with open("/proc/meminfo") as f:
                kb = int(f.readline().split()[1])
                mem = f"{kb / 1048576:.1f} GB"
        except Exception:
            pass
        rows.append(("CPU / RAM", f"{os.cpu_count()} threads · {mem} · UI cores {UI_CORES}"))
        rows.append(("numpy", np.__version__ if np is not None else "not installed"))
        try:
            cfg = load_config()
            rows.append(("Settings", f"OpenGL preview {'on' if cfg.get('gl_preview') else 'off'} · index "
                                     f"{'on' if cfg.get('index_enabled') else 'off'} · layout {cfg.get('layout')}"))
        except Exception:
            pass
        return "\n".join(f"{k:<11}{v}" for k, v in rows)

    def _write(self, kind: str, summary: str, body: str, thread: str = "") -> str:
        now = _dt.datetime.now()
        lines = [f"Kestrel {kind} report", f"summary  {summary}", f"time     {now:%Y-%m-%d %H:%M:%S}"]
        if thread:
            lines.append(f"thread   {thread}")
        lines += ["", "== Environment", self.environment(), "", "== Details", body.rstrip()]
        try:
            stalls = WATCHDOG.report()
            if stalls and "no stalls" not in stalls.lower():
                lines += ["", "== Interface stalls this session", stalls.rstrip()]
        except Exception:
            pass
        if RECENT_LOG:
            lines += ["", "== Recent activity", *list(RECENT_LOG)[-40:]]
        text = self.sanitize("\n".join(lines) + "\n")
        os.makedirs(self.dir, exist_ok=True)
        path = os.path.join(self.dir, f"report-{now:%Y%m%d-%H%M%S}-{kind}-{self.pid}-{len(os.listdir(self.dir))}.txt")
        with open(path, "w") as f:
            f.write(text)
        return path

    def manual_report(self, note: str = "") -> str:
        """Help → Report a problem: environment, optional tools, stalls, recent activity, plus a blank to fill."""
        body = "What happened (please describe):\n\n\n\nSteps to reproduce:\n\n\n"
        try:
            deps = optional_deps()
            body += "\n== Optional tools\n" + "\n".join(f"{'✓' if d.get('found') else '·'} {d.get('name', '?')}" for d in deps)
        except Exception:
            pass
        if note:
            body += "\n\n" + note
        return self._write("manual", "Problem report", body)

    def pending(self) -> List[str]:
        """Reports not yet sent or dismissed, newest first (the 'ended' kind is informational only)."""
        try:
            names = [n for n in os.listdir(self.dir) if n.startswith("report-") and n.endswith(".txt")]
        except OSError:
            return []
        return [os.path.join(self.dir, n) for n in sorted(names, reverse=True) if "-ended-" not in n]

    def archive(self, path: str, state: str):
        """Move a report out of the pending list (state: sent | dismissed). Keeps the newest KEEP."""
        try:
            os.makedirs(self.archive_dir, exist_ok=True)
            base = os.path.basename(path)
            os.replace(path, os.path.join(self.archive_dir, base.replace(".txt", f".{state}.txt")))
            old = sorted(os.listdir(self.archive_dir))
            for n in old[:-self.KEEP]:
                os.remove(os.path.join(self.archive_dir, n))
        except OSError:
            pass

    @staticmethod
    def summary_of(path: str) -> str:
        try:
            with open(path) as f:
                for line in f:
                    if line.startswith("summary"):
                        return line.split(None, 1)[1].strip()
        except OSError:
            pass
        return "Error report"

    # ---- sending (always user-initiated)
    @staticmethod
    def github_issue(path: str, text: str) -> Optional[str]:
        """Open a new issue on the Kestrel repository in the browser, title and body pre-filled. Returns an
        error or None. Long reports are truncated to fit a URL; the user can paste the rest from Copy."""
        import urllib.parse
        title = CrashReporter.summary_of(path)[:120]
        body = "```\n" + (text if len(text) <= 6000 else text[:6000] + "\n[… truncated]") + "\n```\n"
        uri = f"{REPORT_ISSUES_URL}?title={urllib.parse.quote(title)}&body={urllib.parse.quote(body)}&labels=bug"
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
            return None
        except Exception as ex:
            return f"Couldn't open the browser ({ex}). Use Copy and paste the report at {REPORT_ISSUES_URL}"


CRASH = CrashReporter()
