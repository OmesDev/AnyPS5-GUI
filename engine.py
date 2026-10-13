"""AnyPS5 engine manager.

The "engine" is an official AnyPS5 release: the relinker binary for this OS plus
the patched system libraries (libs/*.prx and, on Windows, the MinGW runtime
DLLs) for each target OS. Engines are looked up in this order:

1. A newer engine downloaded into the user's config folder (engine/<tag>/)
2. The engine bundled with the app (engine/<tag>/ next to main.py or in the exe)

Each engine folder:
    relinker(.exe) [+ runtime DLLs on Windows]
    libs-windows/*.prx, *.dll
    libs-linux/*.prx
    VERSION
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import threading
import urllib.request
import zipfile
from pathlib import Path

REPO = "boykopovar/AnyPS5"
API_LATEST = os.environ.get("ANYPS5_RELEASES_API", f"https://api.github.com/repos/{REPO}/releases/latest")
UA = {"User-Agent": "AnyPS5-Relinker-GUI", "Accept": "application/vnd.github+json"}
IS_WINDOWS = platform.system() == "Windows"
RELINKER_NAME = "relinker.exe" if IS_WINDOWS else "relinker"


def parse_tag(tag: str) -> tuple:
    nums = re.findall(r"\d+", tag or "")
    return tuple(int(n) for n in nums) if nums else (-1,)


def engine_ok(d: Path) -> bool:
    return (d / RELINKER_NAME).is_file() and (d / "libs-windows").is_dir()


def list_engines(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    found = [d for d in root.iterdir() if d.is_dir() and not d.name.endswith(".part") and engine_ok(d)]
    return sorted(found, key=lambda d: parse_tag(d.name), reverse=True)


def _get_json(url: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_latest() -> dict:
    """Return {'tag', 'url', 'assets': {name: (url, size)}} for the latest release."""
    data = _get_json(API_LATEST)
    return {
        "tag": data["tag_name"],
        "url": data.get("html_url", f"https://github.com/{REPO}/releases"),
        "published": data.get("published_at", ""),
        "assets": {a["name"]: (a["browser_download_url"], a.get("size", 0)) for a in data.get("assets", [])},
    }


class EngineManager:
    def __init__(self, user_root: Path, bundled_root: Path):
        self.user_root = user_root
        self.bundled_root = bundled_root
        self._lock = threading.Lock()
        self.latest: dict | None = None
        self.check_error: str | None = None
        self.checking = False
        self.progress: dict = {"state": "idle"}  # idle | downloading | done | error

    # ---------------------------------------------------------- resolution
    def active(self) -> dict | None:
        user = list_engines(self.user_root)
        bundled = list_engines(self.bundled_root)
        best = None
        for d, src in [(e, "downloaded") for e in user] + [(e, "bundled") for e in bundled]:
            if best is None or parse_tag(d.name) > parse_tag(best[0].name):
                best = (d, src)
        if not best:
            return None
        d, src = best
        return {
            "tag": d.name, "source": src, "dir": str(d),
            "relinker": str(d / RELINKER_NAME),
            "libs_windows": str(d / "libs-windows"),
            "libs_linux": str(d / "libs-linux") if (d / "libs-linux").is_dir() else "",
        }

    def info(self) -> dict:
        a = self.active()
        latest = self.latest
        upd = bool(latest and (not a or parse_tag(latest["tag"]) > parse_tag(a["tag"])))
        with self._lock:
            prog = dict(self.progress)
        return {"active": a, "latest": {k: latest[k] for k in ("tag", "url", "published")} if latest else None,
                "update_available": upd, "checking": self.checking, "check_error": self.check_error,
                "progress": prog}

    # ---------------------------------------------------------- updates
    def check(self) -> dict:
        self.checking = True
        try:
            self.latest = fetch_latest()
            self.check_error = None
        except Exception as e:  # noqa: BLE001  (offline, rate limited, …)
            self.check_error = f"Couldn't reach GitHub ({e.__class__.__name__})."
        finally:
            self.checking = False
        return self.info()

    def install_latest(self) -> dict:
        with self._lock:
            if self.progress.get("state") == "downloading":
                return {"error": "Already downloading."}
            self.progress = {"state": "downloading", "pct": 0, "step": "Contacting GitHub"}
        threading.Thread(target=self._install, daemon=True).start()
        return {"ok": True}

    def _set(self, **kw) -> None:
        with self._lock:
            self.progress.update(kw)

    def _download(self, url: str, dest: Path, base: float, span: float, label: str) -> None:
        req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                frac = done / total if total else 0
                self._set(pct=round(base + span * frac), step=label)

    def _install(self) -> None:
        part = None
        try:
            if not self.latest:
                self.check()
            if not self.latest:
                raise RuntimeError(self.check_error or "No release information.")
            tag, assets = self.latest["tag"], self.latest["assets"]
            relinker_asset = f"relinker-{tag}.exe" if IS_WINDOWS else f"relinker-{tag}"
            needed = [relinker_asset, f"prx-windows-{tag}.zip"]
            missing = [n for n in needed if n not in assets]
            if missing:
                raise RuntimeError(f"Release {tag} is missing {missing[0]}.")

            self.user_root.mkdir(parents=True, exist_ok=True)
            part = self.user_root / f"{tag}.part"
            shutil.rmtree(part, ignore_errors=True)
            part.mkdir()

            self._download(assets[relinker_asset][0], part / RELINKER_NAME, 0, 30, "Downloading relinker")
            self._download(assets[f"prx-windows-{tag}.zip"][0], part / "prx-windows.zip", 30, 50,
                           "Downloading Windows libraries")
            linux_zip = f"prx-linux-{tag}.zip"
            if linux_zip in assets:
                self._download(assets[linux_zip][0], part / "prx-linux.zip", 80, 15, "Downloading Linux libraries")

            self._set(pct=96, step="Installing")
            self._extract(part / "prx-windows.zip", part / "libs-windows")
            if (part / "prx-linux.zip").exists():
                self._extract(part / "prx-linux.zip", part / "libs-linux")
            for z in part.glob("*.zip"):
                z.unlink()
            if IS_WINDOWS:  # relinker.exe needs the MinGW runtime beside it
                for dll in (part / "libs-windows").glob("*.dll"):
                    shutil.copy2(dll, part / dll.name)
            (part / RELINKER_NAME).chmod(0o755)
            (part / "VERSION").write_text(tag)
            if not engine_ok(part):
                raise RuntimeError("Downloaded release is incomplete.")

            final = self.user_root / tag
            shutil.rmtree(final, ignore_errors=True)
            part.rename(final)
            part = None
            for old in list_engines(self.user_root):  # keep only the newest download
                if old != final:
                    shutil.rmtree(old, ignore_errors=True)
            self._set(state="done", pct=100, step=f"Installed {tag}", tag=tag)
        except Exception as e:  # noqa: BLE001
            self._set(state="error", error=str(e))
        finally:
            if part:
                shutil.rmtree(part, ignore_errors=True)

    @staticmethod
    def _extract(zip_path: Path, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                name = Path(info.filename).name  # archives store libs/<file>; flatten safely
                if not name or name in (".", ".."):
                    continue
                with z.open(info) as src, open(dest / name, "wb") as out:
                    shutil.copyfileobj(src, out)
