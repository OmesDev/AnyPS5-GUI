"""AnyPS5 Relinker — desktop frontend.

A native window (pywebview: WebView2 on Windows, WebKit on macOS, GTK/Qt on
Linux) around AnyPS5's `relinker`. Nothing runs in a browser and no network
server is opened; the UI talks to Python through pywebview's JS bridge.
The official AnyPS5 release ships in engine/ and is kept current from GitHub
releases (see engine.py); the only network access is that update check.

Run:    python main.py
Build:  build.bat / build.sh  (PyInstaller → dist/AnyPS5Relinker/)
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path

import webview

import builder
import engine

try:
    from webview.dom import DOMEventHandler
except Exception:  # very old pywebview: drag & drop falls back to Browse buttons
    DOMEventHandler = None

APP_NAME = "AnyPS5 Relinker"
VERSION = "2.1.0"

# Bundled resources (works both from source and from a PyInstaller build)
RES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
UI_FILE = RES / "ui" / "index.html"

# Per-user settings location
if platform.system() == "Windows":
    CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "AnyPS5Relinker"
elif platform.system() == "Darwin":
    CONFIG_DIR = Path.home() / "Library" / "Application Support" / "AnyPS5Relinker"
else:
    CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "anyps5-relinker"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
BUILD_ROOT = builder.default_root(CONFIG_DIR)  # where "Build latest" keeps tools, source and output

DEFAULTS = {
    "relinker": os.environ.get("ANYPS5_RELINKER", ""),
    "libs_windows": os.environ.get("ANYPS5_LIBS_WINDOWS", ""),
    "libs_linux": os.environ.get("ANYPS5_LIBS_LINUX", ""),
    "timeout": 1800,
    "theme": "system",
    "last_dir": "",
    # "auto": official AnyPS5 release (bundled or auto-updated); "custom": the paths above
    "engine_mode": "custom" if os.environ.get("ANYPS5_RELINKER") else "auto",
    "auto_update_check": True,
}

ELF_TYPES = ("Executables (*.elf;*.bin;*.self)", "All files (*.*)")
MODULE_TYPES = ("Modules (*.prx;*.sprx;*.elf)", "All files (*.*)")
EXIT_MEANING = {0: "Conversion succeeded", 1: "Invalid arguments", 2: "Conversion failed"}


def _dialog(kind: str) -> int:
    fd = getattr(webview, "FileDialog", None)
    if fd:
        return {"open": fd.OPEN, "folder": fd.FOLDER}[kind]
    return {"open": 10, "folder": 20}[kind]


_PRX_SUPPORT: dict[tuple[str, float], bool] = {}


def _supports_prx_dir(relinker: str) -> bool:
    """Newer relinker builds also convert a prx/ folder beside the executable (output goes to
    app0/prx/, where games load those modules from at runtime). v0.1.1 only knows sce_module(s)/.
    Detected from the binary itself, since the usage text names the supported folders."""
    try:
        key = (relinker, Path(relinker).stat().st_mtime)
    except OSError:
        return False
    if key not in _PRX_SUPPORT:
        try:
            _PRX_SUPPORT[key] = b"sce_modules/prx" in Path(relinker).read_bytes()
        except OSError:
            _PRX_SUPPORT[key] = False
    return _PRX_SUPPORT[key]


def _is_elf(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def _file_info(p: Path) -> dict:
    return {"path": str(p), "name": p.name, "size": p.stat().st_size, "elf": _is_elf(p)}


class Api:
    """Every public method here is callable from JS as `pywebview.api.<name>()`."""

    def __init__(self) -> None:
        self._window: webview.Window | None = None
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._job: dict = {"kind": None, "status": "idle", "log": []}
        self._builder: builder.Builder | None = None
        self._engine = engine.EngineManager(CONFIG_DIR / "engine", RES / "engine")
        if self._load().get("auto_update_check", True):
            threading.Thread(target=self._engine.check, daemon=True).start()

    # ------------------------------------------------------------ settings
    def _load(self) -> dict:
        s = dict(DEFAULTS)
        try:
            s.update(json.loads(SETTINGS_FILE.read_text("utf-8")))
        except Exception:
            pass
        return s

    def _save(self, s: dict) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(s, indent=2), "utf-8")

    def _resolve(self, s: dict) -> dict:
        """Paths actually used for a conversion: the official engine, or the user's own build."""
        if s.get("engine_mode") == "custom":
            return {"mode": "custom", "relinker": s["relinker"], "libs_windows": s["libs_windows"],
                    "libs_linux": s["libs_linux"], "label": "Custom build"}
        if s.get("engine_mode") == "latest":
            d = builder.read_dev_engine(BUILD_ROOT)
            if not d:
                return {"mode": "latest", "relinker": "", "libs_windows": "", "libs_linux": "",
                        "label": "Latest build not installed"}
            return {"mode": "latest", "relinker": d["relinker"], "libs_windows": d["libs_windows"],
                    "libs_linux": d["libs_linux"], "label": f"AnyPS5 main @ {d['commit']}" + (f" +{len(d['patches'])} fix" + ("es" if len(d['patches']) > 1 else "") if d.get('patches') else "")}
        a = self._engine.active()
        if not a:
            return {"mode": "auto", "relinker": "", "libs_windows": "", "libs_linux": "", "label": "Not installed"}
        return {"mode": "auto", "relinker": a["relinker"], "libs_windows": a["libs_windows"],
                "libs_linux": a["libs_linux"], "label": f"AnyPS5 {a['tag']}"}

    def _status(self, s: dict) -> dict:
        def libs(p: str) -> dict:
            d = Path(p) if p else None
            if not d or not d.is_dir():
                return {"set": bool(p), "ok": False, "count": 0}
            return {"set": True, "ok": True, "count": len(list(d.glob("*.prx")))}

        r = self._resolve(s)
        rp = Path(r["relinker"]) if r["relinker"] else None
        custom_rel = Path(s["relinker"]) if s["relinker"] else None
        return {
            "relinker_ok": bool(rp and rp.is_file() and (os.access(rp, os.X_OK) or platform.system() == "Windows")),
            "label": r["label"], "mode": r["mode"],
            "libs_windows": libs(r["libs_windows"]),
            "libs_linux": libs(r["libs_linux"]),
            "custom": {
                "relinker_ok": bool(custom_rel and custom_rel.is_file()),
                "libs_windows": libs(s["libs_windows"]),
                "libs_linux": libs(s["libs_linux"]),
            },
        }

    def get_state(self) -> dict:
        s = self._load()
        return {"settings": s, "status": self._status(s), "engine": self._engine.info(),
                "dev": builder.read_dev_engine(BUILD_ROOT), "job": self._job_summary(),
                "version": VERSION, "os": platform.system(), "dnd": DOMEventHandler is not None}

    def _job_summary(self) -> dict:
        with self._lock:
            return {k: self._job.get(k) for k in ("kind", "status", "progress", "error")}

    # ------------------------------------------------------------ build latest AnyPS5
    def start_build(self) -> dict:
        with self._lock:
            if self._job["status"] == "running":
                return {"error": "Something is already running. Wait for it or cancel it first."}
            self._job = {"kind": "build", "status": "running", "log": [], "started": time.time(),
                         "progress": {"step": "Starting", "pct": 0}}

        def progress(p: dict) -> None:
            with self._lock:
                self._job["progress"] = {**self._job.get("progress", {}), **p}

        self._builder = builder.Builder(BUILD_ROOT, self._log, progress, patches_dir=RES / "patches")
        threading.Thread(target=self._run_build, daemon=True).start()
        return {"ok": True}

    def _run_build(self) -> None:
        try:
            info = self._builder.run()
            s = self._load()
            s["engine_mode"] = "latest"  # switch conversions to the fresh build
            self._save(s)
            self._finish(status="done", info=info)
        except builder.BuildCancelled:
            self._log("› Build cancelled")
            self._finish(status="cancelled")
        except Exception as e:  # noqa: BLE001
            msg = str(e).splitlines()[0] if str(e) else type(e).__name__
            self._log(f"error: {e}")
            self._finish(status="failed", error=msg)
        finally:
            self._builder = None

    # ------------------------------------------------------------ engine updates
    def engine_info(self) -> dict:
        return self.get_state()

    def check_updates(self) -> dict:
        self._engine.check()
        return self.get_state()

    def install_update(self) -> dict:
        return self._engine.install_latest()

    def open_release_page(self) -> None:
        info = self._engine.info()
        url = (info.get("latest") or {}).get("url") or f"https://github.com/{engine.REPO}/releases"
        webbrowser.open(url)

    def save_settings(self, data: dict) -> dict:
        s = self._load()
        for k in ("relinker", "libs_windows", "libs_linux", "theme", "engine_mode"):
            if k in data:
                s[k] = str(data[k]).strip().strip('"')
        if "auto_update_check" in data:
            s["auto_update_check"] = bool(data["auto_update_check"])
        if "timeout" in data:
            try:
                s["timeout"] = max(30, int(data["timeout"]))
            except (TypeError, ValueError):
                pass
        self._save(s)
        return self.get_state()

    # ------------------------------------------------------------ dialogs
    def _start_dir(self) -> str:
        d = self._load().get("last_dir", "")
        return d if d and Path(d).is_dir() else str(Path.home())

    def _remember_dir(self, path: str) -> None:
        s = self._load()
        s["last_dir"] = str(Path(path).parent if Path(path).is_file() else path)
        self._save(s)

    def pick_executable(self) -> dict | None:
        r = self._window.create_file_dialog(_dialog("open"), directory=self._start_dir(),
                                            file_types=ELF_TYPES)
        if not r:
            return None
        self._remember_dir(r[0])
        return self._executable(Path(r[0]))

    def pick_modules(self) -> list[dict]:
        r = self._window.create_file_dialog(_dialog("open"), directory=self._start_dir(),
                                            allow_multiple=True, file_types=MODULE_TYPES)
        return [_file_info(Path(p)) for p in (r or []) if Path(p).is_file()]

    def pick_module_folder(self) -> dict | None:
        r = self._window.create_file_dialog(_dialog("folder"), directory=self._start_dir())
        if not r:
            return None
        d = Path(r[0] if isinstance(r, (list, tuple)) else r)
        return {"dir_name": d.name, "files": [_file_info(p) for p in sorted(d.iterdir()) if p.is_file()]}

    def pick_folder(self) -> str | None:
        r = self._window.create_file_dialog(_dialog("folder"), directory=self._start_dir())
        if not r:
            return None
        return str(r[0] if isinstance(r, (list, tuple)) else r)

    def pick_relinker(self) -> str | None:
        r = self._window.create_file_dialog(_dialog("open"), directory=self._start_dir())
        return r[0] if r else None

    def _executable(self, p: Path) -> dict:
        """Describe a chosen ELF and auto-detect a sibling sce_module(s) folder."""
        info = _file_info(p)
        found = [n for n in ("sce_module", "sce_modules") if (p.parent / n).is_dir()]
        info["modules_conflict"] = len(found) == 2
        info["modules"] = None
        if len(found) == 1:
            d = p.parent / found[0]
            info["modules"] = {"dir_name": found[0],
                               "files": [_file_info(f) for f in sorted(d.iterdir()) if f.is_file()]}
            prx = p.parent / "prx"  # game modules some titles load by path at runtime (e.g. DOOM)
            if prx.is_dir():
                info["modules"]["files"] += [_file_info(f) for f in sorted(prx.iterdir())
                                             if f.is_file() and f.suffix.lower() in (".prx", ".sprx")]
                info["modules"]["label"] = f"{found[0]}/ + prx/"
            unity = p.parent / "Media" / "Modules"  # Unity: game code (Il2CppUserAssemblies) lives here
            if unity.is_dir():
                info["modules"]["files"] += [_file_info(f) for f in sorted(unity.iterdir())
                                             if f.is_file() and f.suffix.lower() in (".prx", ".sprx")]
                info["modules"]["label"] = info["modules"].get("label", found[0] + "/") .rstrip("/") + "/ + Media/Modules/"
        info["suggested_out"] = str(p.parent / f"{p.stem}-port")
        return info

    def inspect_paths(self, paths: list[str]) -> list[dict]:
        out = []
        for raw in paths:
            p = Path(raw)
            if p.is_dir():
                for f in sorted(p.iterdir()):
                    if f.is_file():
                        out.append(_file_info(f) | {"from_dir": p.name})
            elif p.is_file():
                out.append(_file_info(p))
        return out

    def describe_executable(self, path: str) -> dict | None:
        p = Path(path)
        return self._executable(p) if p.is_file() else None

    def open_folder(self, path: str) -> None:
        if not Path(path).exists():
            return
        if platform.system() == "Windows":
            os.startfile(path)  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    # ------------------------------------------------------------ conversion
    def convert(self, o: dict) -> dict:
        with self._lock:
            if self._job["status"] == "running":
                return {"error": "A conversion is already running."}
        s = self._load()
        if not self._status(s)["relinker_ok"]:
            return {"error": "The AnyPS5 engine isn't installed. Open Settings to download it or choose your own build."}
        r = self._resolve(s)

        elf = Path(o.get("elf", ""))
        if not elf.is_file():
            return {"error": "Choose an executable first."}
        if not _is_elf(elf):
            return {"error": "That file has no ELF header. AnyPS5 needs a clean, decrypted executable."}
        out_dir = Path(o.get("out_dir", "").strip() or elf.parent / f"{elf.stem}-port")
        target = "linux" if o.get("target") == "linux" else "windows"
        name = "".join(c for c in (o.get("output_name") or "app") if c.isalnum() or c in "-_.").strip(".") or "app"
        name = Path(name).stem
        out_file = out_dir / (name + (".exe" if target == "windows" else ".elf"))

        rpath = (o.get("rpath") or "").strip()
        if target == "windows" and rpath and not rpath.isascii():
            return {"error": "On Windows the library path (rpath) must be ASCII."}

        args = [r["relinker"]]
        if target == "windows":
            args.append("--windows")
            if o.get("windows_gui"):
                args.append("--windows-gui")
            if o.get("windows_diagnostics"):
                args.append("--windows-diagnostics")
        if o.get("to_intel"):
            args.append("--to-intel")
        if str(o.get("unused_filter", "0")) in ("1", "2"):
            args.append(f"unused-filter={o['unused_filter']}")
        if o.get("registry"):
            args.append("--registry")
        if rpath and rpath != "$ORIGIN/libs":
            args += ["--rpath", rpath]  # argv list, no shell: $ORIGIN stays literal

        modules = [Path(m) for m in o.get("modules", [])]
        missing = [m.name for m in modules if not m.is_file()]
        if missing:
            return {"error": f"Module not found: {missing[0]}"}

        with self._lock:
            self._job = {"kind": "convert", "status": "running", "log": [], "started": time.time(),
                         "target": target, "out_dir": str(out_dir), "output": out_file.name}
        threading.Thread(target=self._run, daemon=True,
                         args=(args, elf, modules, o.get("module_dir") or "sce_module",
                               out_dir, out_file, target, s, r)).start()
        return {"ok": True}

    def _log(self, line: str) -> None:
        with self._lock:
            self._job["log"].append(line)

    def _finish(self, **kw) -> None:
        with self._lock:
            self._job.update(kw, finished=time.time())
            self._proc = None

    def _stage(self, src: Path, elf: Path, modules: list[Path], mod_dir_name: str, r: dict,
               plugins: list[Path] | None = None) -> tuple[Path, dict]:
        """Lay out input exactly as relinker requires: input.elf plus sce_module(s)/ (and prx/) beside it.
        Plugins go back under their original folder (Media/Plugins) for --module-dir."""
        mdir = src / ("sce_modules" if mod_dir_name == "sce_modules" else "sce_module")
        mdir.mkdir(parents=True)
        # Modules picked from a game's prx/ folder go back into prx/ when relinker supports it:
        # games load those by path at runtime (e.g. /app0/prx/libcohtml.Prospero.prx).
        prx_dir = src / "prx"
        use_prx = _supports_prx_dir(r["relinker"])
        staged_elf = src / elf.name
        shutil.copy2(elf, staged_elf)
        counts: dict[str, int] = {}
        for m in modules:
            dest = prx_dir if use_prx and m.parent.name.lower() == "prx" else mdir
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(m, dest / m.name)
            counts[dest.name] = counts.get(dest.name, 0) + 1
        root = elf.parent.resolve()
        for p in plugins or []:
            rel = p.resolve().parent.relative_to(root)
            (src / rel).mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, src / rel / p.name)
            counts[rel.as_posix() + "/"] = counts.get(rel.as_posix() + "/", 0) + 1
        return staged_elf, counts

    def _relink(self, args: list[str], staged_elf: Path, out_file: Path, s: dict, r: dict,
                quiet: bool = False) -> tuple[int, list[str]]:
        full = args + [str(staged_elf), str(out_file)]
        if not quiet:
            self._log("$ " + subprocess.list2cmdline(full))
        flags = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
        env = dict(os.environ)
        # let relinker find its MinGW runtime DLLs when they sit beside it
        env["PATH"] = str(Path(r["relinker"]).parent) + os.pathsep + env.get("PATH", "")
        proc = subprocess.Popen(full, cwd=out_file.parent, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace",
                                bufsize=1, creationflags=flags)
        with self._lock:
            self._proc = proc
        timer = threading.Timer(int(s["timeout"]), proc.kill)
        timer.start()
        lines: list[str] = []
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.rstrip("\n")
                lines.append(line)
                if not quiet:
                    self._log(line)
            code = proc.wait()
        finally:
            timer.cancel()
        return code, lines

    def _run(self, args, elf, modules, mod_dir_name, out_dir, out_file, target, s, r) -> None:
        stage = Path(tempfile.mkdtemp(prefix="anyps5-"))
        try:
            plugins = self._runtime_plugins(elf, modules)
            out_dir.mkdir(parents=True, exist_ok=True)
            self._log(f"› Engine: {r['label']}")
            code = -1
            plugins_linked = False
            if plugins:
                # The game's own app.exe writes each module's imports at startup, so a plugin has to be
                # relinked together with the game it belongs to; a plugin converted in a pass of its own
                # keeps null imports and crashes the first time it calls into the system.
                staged_elf, counts = self._stage(stage / "source-plugins", elf, modules, mod_dir_name, r, plugins)
                where = ", ".join(f"{n} into {d}" if d.endswith("/") else f"{n} into {d}/" for d, n in counts.items())
                self._log(f"› Staging {elf.name} with its runtime plugins; {where}")
                folders = sorted({p.resolve().parent.relative_to(elf.parent.resolve()).as_posix() for p in plugins})
                extra = [a for f in folders for a in ("--module-dir", f)]
                code, lines = self._relink(args + extra, staged_elf, out_file, s, r)
                plugins_linked = code == 0
                if not plugins_linked and self._job.get("status") != "cancelled":
                    reason = next((l for l in lines if "FAIL" in l or "error" in l.lower()), f"exit code {code}")
                    self._log(f"› Plugins can't be linked into the game together ({reason}); "
                              "converting them one by one instead. Their imports will not be bound, "
                              "so a plugin that calls into the system can crash.")
            if not plugins_linked:
                staged_elf, counts = self._stage(stage / "source", elf, modules, mod_dir_name, r)
                where = ", ".join(f"{n} into {d}/" for d, n in counts.items()) or "no modules"
                self._log(f"› Staging {elf.name}; {where}")
                code, _ = self._relink(args, staged_elf, out_file, s, r)

            if self._job.get("status") == "cancelled":
                self._log("› Cancelled")
                self._finish()
                return
            self._log(f"› Exit code {code}: {EXIT_MEANING.get(code, 'terminated (timeout?)')}")
            if code != 0:
                self._finish(status="failed", exit_code=code,
                             error=EXIT_MEANING.get(code, "relinker exited abnormally"))
                return

            libs_src = r["libs_windows"] if target == "windows" else r["libs_linux"]
            libs_count = 0
            if libs_src and Path(libs_src).is_dir():
                (out_dir / "libs").mkdir(exist_ok=True)
                for lib in Path(libs_src).iterdir():
                    # *.prx system libraries, plus the MinGW runtime DLLs they need on Windows
                    if lib.is_file() and lib.suffix.lower() in (".prx", ".dll"):
                        shutil.copy2(lib, out_dir / "libs" / lib.name)
                        libs_count += lib.suffix.lower() == ".prx"
                self._log(f"› Copied {libs_count} system libraries into libs/")
            else:
                self._log("› No system libraries folder configured; libs/ must be filled manually")
            if r["mode"] == "auto" and libs_src and Path(libs_src).is_dir():
                self._link_guest_modules(out_dir, Path(libs_src))
            (out_dir / "app0").mkdir(exist_ok=True)
            self._mirror_runtime_modules(elf, modules, out_dir)
            if plugins and not plugins_linked:
                self._convert_plugins(args, elf, modules, mod_dir_name, plugins, out_dir, stage, s, r)
                if self._job.get("status") == "cancelled":
                    self._log("› Cancelled")
                    self._finish()
                    return
            if target == "linux" and out_file.exists():
                try:
                    out_file.chmod(out_file.stat().st_mode | 0o111)
                except OSError:  # exFAT/FAT drives have no permission bits
                    self._log("› Couldn't mark the output executable (drive has no permissions, e.g. exFAT); "
                              "copy it to a Linux filesystem and chmod +x before running")

            files = sorted(p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*") if p.is_file())
            self._finish(status="done", exit_code=0, files=files[:300], file_count=len(files),
                         libs_count=libs_count)
        except FileNotFoundError as e:
            self._finish(status="failed", error=f"File not found: {e.filename}")
        except Exception as e:  # noqa: BLE001
            self._log(f"error: {e}")
            self._finish(status="failed", error=str(e))
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    @staticmethod
    def _runtime_plugins(elf: Path, modules: list[Path]) -> list[Path]:
        """Unity loads every module in Media/Plugins by itself at startup. They aren't linked into
        the game, and several export identical names (all Wwise plugins share one entry point),
        so converting them together fails with "Duplicate guest export". They're converted one
        pass each instead. Plugins the user already added as normal modules are left alone."""
        folder = elf.parent / "Media" / "Plugins"
        if not folder.is_dir():
            return []
        chosen = {m.name.lower() for m in modules}
        return [p for p in sorted(folder.iterdir())
                if p.is_file() and p.suffix.lower() in (".prx", ".sprx") and p.name.lower() not in chosen]

    def _convert_plugins(self, args, elf, modules, mod_dir_name, plugins, out_dir, stage, s, r) -> None:
        self._log(f"› Converting {len(plugins)} runtime plugin(s) from Media/Plugins, one pass each")
        done, failed = 0, []
        root = elf.parent.resolve()
        for i, plugin in enumerate(plugins, 1):
            if self._job.get("status") == "cancelled":
                return
            work = stage / f"plugin{i}"
            staged_elf, _ = self._stage(work / "source", elf, modules + [plugin], mod_dir_name, r)
            tmp_out = work / "out"
            tmp_out.mkdir()
            code, lines = self._relink(args, staged_elf, tmp_out / ("app.exe" if "--windows" in args else "app.elf"),
                                       s, r, quiet=True)
            guest = next(iter((tmp_out / "app0").rglob(plugin.name + ".guest.prx")), None) if code == 0 else None
            if not guest:
                reason = next((l for l in lines if "FAIL" in l or "error" in l.lower()), f"exit code {code}")
                failed.append(plugin.name)
                self._log(f"  ✗ {plugin.name}: {reason}")
                continue
            target = out_dir / "app0" / plugin.resolve().parent.relative_to(root) / guest.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(guest, target)
            done += 1
            self._log(f"  ✓ {plugin.name}")
            shutil.rmtree(work, ignore_errors=True)
        self._log(f"› Runtime plugins: {done} converted" + (f", {len(failed)} failed" if failed else ""))

    def _mirror_runtime_modules(self, elf: Path, modules: list[Path], out_dir: Path) -> None:
        """Some engines load their modules by path while running, e.g. Unity's
        /app0/Media/Modules/Il2CppUserAssemblies.prx. relinker only writes converted modules to
        app0/sce_module (or app0/prx), and AnyPS5's loader looks for <path>.guest.prx next to the
        requested file. So put the converted module at the original location too, as a hard link
        to the same file (Windows then recognises it as the module already loaded at startup);
        a plain copy is the fallback when linking isn't possible."""
        game_root = elf.parent.resolve()
        app0 = out_dir / "app0"
        mirrored = 0
        for m in modules:
            try:
                rel = m.resolve().parent.relative_to(game_root)
            except ValueError:
                continue  # module picked from outside the game folder
            if rel.parts and rel.parts[0].lower() in ("sce_module", "sce_modules", "prx"):
                continue  # relinker already places these where the game expects them
            if not rel.parts:
                continue
            name = m.name + ".guest.prx"
            converted = next((d / name for d in (app0 / "sce_module", app0 / "sce_modules", app0 / "prx")
                              if (d / name).is_file()), None)
            if not converted:
                continue
            target = app0 / rel / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.unlink(missing_ok=True)
            try:
                os.link(converted, target)
            except OSError:
                shutil.copy2(converted, target)
            mirrored += 1
        if mirrored:
            self._log(f"› Placed {mirrored} converted module(s) at their original paths under app0/ "
                      "(for engines that load them at runtime)")

    def _link_guest_modules(self, out_dir: Path, system_libs: Path) -> None:
        """Workaround for the v0.1.1 release: it resolves the executable's imports against
        bundled modules, but one bundled module importing another (and differently-cased
        names) end up looked for in libs/. A copy of each converted module there under its
        original name satisfies those lookups. Newer AnyPS5 builds link these properly."""
        system = {p.name.lower() for p in system_libs.iterdir()}
        linked = 0
        for guest in (out_dir / "app0").rglob("*.guest.prx"):
            name = guest.name[: -len(".guest.prx")]
            if name.lower() in system:
                continue  # never shadow an AnyPS5 system library
            shutil.copy2(guest, out_dir / "libs" / name)
            linked += 1
        if linked:
            self._log(f"› Linked {linked} game module(s) into libs/ (needed with the v0.1.1 release)")

    def poll(self, since: int = 0) -> dict:
        with self._lock:
            j = dict(self._job)
            j["log_total"] = len(self._job["log"])
            j["log"] = self._job["log"][since:]
        return j

    def cancel(self) -> None:
        if self._job.get("kind") == "build" and self._builder:
            self._builder.cancel()
            return
        with self._lock:
            if self._job["status"] == "running":
                self._job["status"] = "cancelled"
            proc = self._proc
        if proc and proc.poll() is None:
            proc.kill()

    # ------------------------------------------------------------ drag & drop
    def _on_drop(self, e: dict) -> None:
        files = (e.get("dataTransfer") or {}).get("files", [])
        paths = [f["pywebviewFullPath"] for f in files if f.get("pywebviewFullPath")]
        if self._window:
            self._window.evaluate_js(f"window.onNativeDrop && window.onNativeDrop({json.dumps(paths)})")

    def _bind(self, window: webview.Window) -> None:
        if DOMEventHandler is None:
            return
        noop = lambda e: None  # noqa: E731
        doc = window.dom.document
        doc.events.dragenter += DOMEventHandler(noop, True, True)
        doc.events.dragstart += DOMEventHandler(noop, True, True)
        doc.events.dragover += DOMEventHandler(noop, True, True, debounce=500)
        doc.events.drop += DOMEventHandler(self._on_drop, True, True)


def main() -> None:
    api = Api()
    window = webview.create_window(
        APP_NAME, str(UI_FILE), js_api=api,
        width=1200, height=800, min_size=(940, 640), background_color="#09090b",
    )
    api._window = window
    webview.start(api._bind, window, debug=bool(os.environ.get("ANYPS5_DEBUG")))


if __name__ == "__main__":
    main()
