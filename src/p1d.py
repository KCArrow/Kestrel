

# =========================================================================== encrypted file index
# An optional, opt-in map of the user's folders (name, kind, size, mtime per entry, keyed by directory) that
# makes two things instant: painting a folder the moment you navigate to it (the real listing replaces the
# indexed one a moment later, like the progressive listing does today), and name search across whole trees
# without walking the disk. Because such a map is a complete inventory of the machine, it never touches the
# disk in the clear: the index file is AES-256-CBC (via the system `openssl`, PBKDF2-stretched key) with an
# HMAC-SHA256 over the ciphertext (encrypt-then-MAC), and the key lives in the desktop keyring through
# libsecret's `secret-tool` when available, otherwise in a 0600 file in ~/.config/kestrel. Plaintext exists
# only in this process's memory.
INDEX_FILE = os.path.join(GLib.get_user_cache_dir(), "kestrel", "index.enc")
INDEX_KEYFILE = os.path.join(CONFIG_DIR, "index.key")
INDEX_EXCLUDE_DIRS = {".cache", ".git", ".svn", "node_modules", "__pycache__", ".Trash", ".local/share/Trash", "proc", "sys", "dev", "run",
                      ".thumbnails", ".mozilla", ".var", "snap", "lost+found", ".npm", ".cargo", ".rustup", ".conda", ".pyenv", "site-packages"}
INDEX_SYSTEM_PREFIXES = ("/proc", "/sys", "/dev", "/run", "/tmp", "/var/tmp", "/var/cache", "/var/lib", "/snap")


class IndexKey:
    """32-byte key for the index. Keyring first (secret-tool / libsecret), file fallback. `source` tells the UI
    where it ended up so the user knows what the protection actually is."""

    def __init__(self):
        self.source = ""

    def get(self, create: bool = True) -> Optional[bytes]:
        st = shutil.which("secret-tool")
        if st:
            try:
                r = subprocess.run([st, "lookup", "application", "kestrel", "purpose", "index-key"], capture_output=True, text=True, timeout=15)
                if r.returncode == 0 and len(r.stdout.strip()) == 64:
                    self.source = "desktop keyring"
                    return bytes.fromhex(r.stdout.strip())
                if create:
                    key = os.urandom(32)
                    r = subprocess.run([st, "store", "--label=Kestrel file index key", "application", "kestrel", "purpose", "index-key"],
                                       input=key.hex(), capture_output=True, text=True, timeout=30)
                    if r.returncode == 0:
                        self.source = "desktop keyring"
                        return key
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            if os.path.exists(INDEX_KEYFILE):
                with open(INDEX_KEYFILE, "rb") as f:
                    data = f.read().strip()
                if len(data) == 64:
                    self.source = "key file (install libsecret-tools / a keyring daemon for keyring storage)"
                    return bytes.fromhex(data.decode())
            if create:
                key = os.urandom(32)
                os.makedirs(CONFIG_DIR, exist_ok=True)
                fd = os.open(INDEX_KEYFILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(key.hex())
                self.source = "key file (install libsecret-tools / a keyring daemon for keyring storage)"
                return key
        except OSError:
            pass
        return None

    def forget(self):
        st = shutil.which("secret-tool")
        if st:
            subprocess.run([st, "clear", "application", "kestrel", "purpose", "index-key"], capture_output=True, timeout=15)
        try:
            os.remove(INDEX_KEYFILE)
        except OSError:
            pass


def _openssl(args: List[str], data: bytes, key: bytes) -> bytes:
    """Run openssl enc with the key passed over a pipe fd (never argv/env, which other users can read)."""
    rfd, wfd = os.pipe()
    try:
        os.write(wfd, key.hex().encode())
        os.close(wfd)
        r = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "100000", "-salt", "-pass", f"fd:{rfd}"] + args,
                           input=data, capture_output=True, pass_fds=(rfd,), timeout=600)
    finally:
        try:
            os.close(rfd)
        except OSError:
            pass
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip().splitlines()[-1:] or "openssl failed")
    return r.stdout


def _mac(key: bytes, blob: bytes) -> bytes:
    import hmac, hashlib
    return hmac.new(hashlib.sha256(key + b"kestrel-index-mac").digest(), blob, hashlib.sha256).digest()


class FileIndex:
    MAGIC = b"KSTRLIDX1"

    def __init__(self):
        self.data: Dict[str, Tuple[float, list]] = {}      # dir → (dir mtime, [(name, is_dir, size, mtime), …])
        self.lock = threading.RLock()
        self.key = IndexKey()
        self._k: Optional[bytes] = None
        self.loaded = False
        self.dirty = False
        self.crawling = False
        self.cancel = threading.Event()
        self.progress = {"dirs": 0, "files": 0, "current": "", "started": 0.0, "finished": 0.0}
        self.last_error = ""
        self._flat: Optional[List[Tuple[str, str, int]]] = None    # (lower name, dir, idx) for search
        self.on_status: Optional[Callable] = None

    # ---- persistence
    def _key(self) -> Optional[bytes]:
        if self._k is None:
            self._k = self.key.get()
        return self._k

    def load(self) -> bool:
        if not os.path.exists(INDEX_FILE):
            self.loaded = True
            return False
        k = self._key()
        if not k:
            self.last_error = "no key available"
            return False
        try:
            with open(INDEX_FILE, "rb") as f:
                blob = f.read()
            if not blob.startswith(self.MAGIC):
                raise ValueError("not a Kestrel index")
            mac, body = blob[len(self.MAGIC):len(self.MAGIC) + 32], blob[len(self.MAGIC) + 32:]
            import hmac as _h
            if not _h.compare_digest(mac, _mac(k, body)):
                raise ValueError("index authentication failed (wrong key or tampered file) — it will be rebuilt")
            import zlib
            raw = zlib.decompress(_openssl(["-d"], body, k))
            obj = json.loads(raw.decode("utf-8"))
            with self.lock:
                self.data = {d: (m, [tuple(x) for x in ents]) for d, (m, ents) in obj["dirs"].items()}
                self._flat = None
            self.progress["finished"] = obj.get("finished", 0.0)
            self.loaded = True
            return True
        except Exception as ex:
            self.last_error = str(ex)
            self.loaded = True
            return False

    def save(self) -> bool:
        k = self._key()
        if not k:
            return False
        try:
            import zlib
            with self.lock:
                obj = {"version": 1, "finished": self.progress["finished"], "dirs": {d: [m, ents] for d, (m, ents) in self.data.items()}}
            raw = zlib.compress(json.dumps(obj, separators=(",", ":")).encode("utf-8"), 6)
            body = _openssl([], raw, k)
            blob = self.MAGIC + _mac(k, body) + body
            os.makedirs(os.path.dirname(INDEX_FILE), exist_ok=True)
            tmp = INDEX_FILE + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
            os.replace(tmp, INDEX_FILE)
            self.dirty = False
            return True
        except Exception as ex:
            self.last_error = f"save: {ex}"
            return False

    def wipe(self):
        with self.lock:
            self.data = {}
            self._flat = None
        try:
            os.remove(INDEX_FILE)
        except OSError:
            pass
        self.key.forget()
        self._k = None

    # ---- queries
    def lookup(self, path: str) -> Optional[list]:
        with self.lock:
            hit = self.data.get(path.rstrip("/") or "/")
        return hit[1] if hit else None

    def update_dir(self, path: str, entries: List["Entry"]):
        """Called with a freshly listed folder: keep the index current for places the user actually visits."""
        try:
            mt = os.stat(path).st_mtime
        except OSError:
            return
        with self.lock:
            self.data[path.rstrip("/") or "/"] = (mt, [(e.name, e.is_dir, e.size or 0, e.mtime or 0.0) for e in entries])
            self._flat = None
            self.dirty = True
        self._schedule_save()

    def _schedule_save(self, delay: float = 20.0):
        """Write the encrypted file a little after the last change (coalesces a burst of visits)."""
        t = getattr(self, "_save_timer", None)
        if t is not None:
            t.cancel()

        def go():
            self._save_timer = None
            if self.dirty and not self.crawling:
                pin_worker()
                self.save()
        self._save_timer = threading.Timer(delay, go)
        self._save_timer.daemon = True
        self._save_timer.start()

    def search(self, pattern: str, root: Optional[str] = None, limit: int = 5000) -> List[Tuple[str, bool, int, float]]:
        """Case-insensitive substring or glob match over every indexed name. ~1M names in well under a second."""
        pat = pattern.lower()
        globby = any(c in pat for c in "*?[")
        with self.lock:
            if self._flat is None:
                self._flat = [(ents, d) for d, (m, ents) in self.data.items()]
            flat = self._flat
        out = []
        rootp = (root.rstrip("/") + "/") if root and root != "/" else None
        for ents, d in flat:
            if rootp and not (d == root.rstrip("/") or d.startswith(rootp)):
                continue
            for name, is_dir, size, mtime in ents:
                low = name.lower()
                if (fnmatch.fnmatch(low, pat) if globby else pat in low):
                    out.append((os.path.join(d, name), is_dir, size, mtime))
                    if len(out) >= limit:
                        return out
        return out

    def stats(self) -> dict:
        with self.lock:
            dirs = len(self.data)
            files = sum(len(v[1]) for v in self.data.values())
        try:
            size = os.path.getsize(INDEX_FILE)
        except OSError:
            size = 0
        return {"dirs": dirs, "files": files, "size": size, "finished": self.progress["finished"], "key": self.key.source or "—",
                "crawling": self.crawling, "error": self.last_error}

    # ---- crawling
    def crawl(self, roots: List[str], include_network: bool, include_hidden: bool, full: bool = False):
        """Walk the roots in a low-priority thread. Directories whose mtime hasn't changed since they were
        indexed are skipped (full=False), so re-crawls touch only what moved. Saves every 60 s and at the end."""
        if self.crawling:
            return
        self.crawling = True
        self.cancel.clear()
        self.progress.update(dirs=0, files=0, current="", started=time.time())

        def work():
            pin_worker()
            try:
                os.nice(10)
            except OSError:
                pass
            last_save = time.time()
            seen_dev = {}
            for root in roots:
                root = os.path.abspath(os.path.expanduser(root))
                if not os.path.isdir(root):
                    continue
                stack = [root]
                while stack and not self.cancel.is_set():
                    d = stack.pop()
                    if d != root and d.startswith(INDEX_SYSTEM_PREFIXES) and not root.startswith(INDEX_SYSTEM_PREFIXES):
                        continue            # never wander into /proc, /sys, /run… unless that IS the chosen root
                    if not include_network and is_network_path(d):
                        continue
                    try:
                        st = os.stat(d)
                    except OSError:
                        continue
                    self.progress["current"] = d
                    with self.lock:
                        old = self.data.get(d)
                    if old is not None and not full and abs(old[0] - st.st_mtime) < 1e-6:
                        # unchanged directory: still descend (subfolders may have changed)
                        for name, is_dir, _, _ in old[1]:
                            if is_dir and (include_hidden or not name.startswith(".")) and name not in INDEX_EXCLUDE_DIRS:
                                stack.append(os.path.join(d, name))
                        self.progress["dirs"] += 1
                        continue
                    ents = []
                    try:
                        with os.scandir(d) as it:
                            for de in it:
                                if not include_hidden and de.name.startswith("."):
                                    continue
                                try:
                                    is_dir = de.is_dir(follow_symlinks=False)
                                    s = de.stat(follow_symlinks=False)
                                except OSError:
                                    continue
                                ents.append((de.name, is_dir, 0 if is_dir else s.st_size, s.st_mtime))
                                if is_dir and de.name not in INDEX_EXCLUDE_DIRS:
                                    stack.append(de.path)
                    except OSError:
                        continue
                    with self.lock:
                        self.data[d] = (st.st_mtime, ents)
                        self._flat = None
                    self.dirty = True
                    self.progress["dirs"] += 1
                    self.progress["files"] += len(ents)
                    if time.time() - last_save > 60:
                        self.save()
                        last_save = time.time()
                        if self.on_status:
                            GLib.idle_add(lambda: (self.on_status(), False)[1])
            self.progress["finished"] = time.time()
            self.progress["current"] = ""
            self.save()
            self.crawling = False
            if self.on_status:
                GLib.idle_add(lambda: (self.on_status(), False)[1])
        threading.Thread(target=work, daemon=True, name="kestrel-indexer").start()

    def stop(self):
        self.cancel.set()


INDEX = FileIndex()


def index_entries_for(path: str, rows: list) -> List["Entry"]:
    """Index tuples → Entry objects (icons/types from the extension cache) for the instant first paint."""
    out = []
    for name, is_dir, size, mtime in rows:
        e = Entry()
        e.name, e.path, e.is_dir = name, os.path.join(path, name), bool(is_dir)
        e.size = None if is_dir else int(size)
        e.mtime = float(mtime)
        e.hidden = name.startswith(".")
        if is_dir:
            e.content_type, e.type, e.icon = "inode/directory", "File folder", _FOLDER_ICON
        else:
            e.content_type, e.type, e.icon = _ct_info(name)
        out.append(e)
    return out
