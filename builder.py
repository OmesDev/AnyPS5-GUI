"""Build the newest AnyPS5 code on this PC.

Windows: downloads the exact toolchain AnyPS5 supports (WinLibs GCC 15.2.0 UCRT,
which also ships CMake and Ninja) and, if Git isn't installed, portable MinGit.
Linux/macOS: uses git, cmake, ninja and gcc/g++ from the system.

Then: clone/update AnyPS5 main (+ submodules) → configure → build → build libs →
package relinker + system libraries into <root>/engine-dev/current/.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

REPO_URL = "https://github.com/boykopovar/AnyPS5.git"
IS_WINDOWS = platform.system() == "Windows"

WINLIBS_RELEASE = "15.2.0posix-14.0.0-ucrt-r7"
WINLIBS_ZIP = "winlibs-x86_64-posix-seh-gcc-15.2.0-mingw-w64ucrt-14.0.0-r7.zip"
WINLIBS_URL = f"https://github.com/brechtsanders/winlibs_mingw/releases/download/{WINLIBS_RELEASE}/{WINLIBS_ZIP}"
MINGIT_VERSION = "2.47.1"
MINGIT_URL = (f"https://github.com/git-for-windows/git/releases/download/v{MINGIT_VERSION}.windows.1/"
              f"MinGit-{MINGIT_VERSION}-64-bit.zip")
RUNTIME_DLLS = ("libgcc_s_seh-1.dll", "libstdc++-6.dll", "libwinpthread-1.dll")
NINJA_PROGRESS = re.compile(r"^\[(\d+)/(\d+)\]")


def default_root(config_dir: Path) -> Path:
    """Where builds live. On Windows a short path under %LOCALAPPDATA% keeps AnyPS5's deeply
    nested submodule and object-file paths well clear of the 260-character MAX_PATH limit."""
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "A5build"
    return config_dir


class BuildCancelled(Exception):
    pass


class Builder:
    def __init__(self, root: Path, log: Callable[[str], None], progress: Callable[[dict], None],
                 patches_dir: Path | None = None):
        self.root = root
        self.tools = root / "toolchain"
        self.src = root / "source" / "AnyPS5"
        self.build = root / "source" / "build"
        self.dest = root / "engine-dev" / "current"
        self.log = log
        self.progress = progress
        self.cancelled = threading.Event()
        self._proc: subprocess.Popen | None = None
        self.env = dict(os.environ)
        self.mingw_bin: Path | None = None
        self.patches_dir = patches_dir

    # -------------------------------------------------------------- public
    def cancel(self) -> None:
        self.cancelled.set()
        p = self._proc
        if p and p.poll() is None:
            p.kill()

    def run(self) -> dict:
        t0 = time.time()
        self._step("Preparing tools", 0)
        self._prepare_tools()
        self._step("Fetching the newest AnyPS5 code", 15)
        info = self._fetch_source()
        info["patches"] = self._apply_patches()
        self._step("Configuring", 25)
        self._cmake(["-S", str(self.src), "-B", str(self.build), "-G", "Ninja",
                     "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_C_COMPILER=gcc", "-DCMAKE_CXX_COMPILER=g++"])
        self._step("Building AnyPS5", 30)
        self._cmake(["--build", str(self.build), "--parallel"], span=(30, 85))
        self._step("Building system libraries", 85)
        self._cmake(["--build", str(self.build), "--target", "libs", "--parallel"], span=(85, 97))
        self._step("Installing", 97)
        info["built_at"] = time.strftime("%Y-%m-%d %H:%M")
        info["minutes"] = round((time.time() - t0) / 60, 1)
        self._package(info)
        self._step("Done", 100)
        return info

    # -------------------------------------------------------------- steps
    def _step(self, name: str, pct: int) -> None:
        self._check()
        self.log(f"› {name}")
        self.progress({"step": name, "pct": pct})

    def _check(self) -> None:
        if self.cancelled.is_set():
            raise BuildCancelled()

    def _which(self, name: str) -> str | None:
        return shutil.which(name, path=self.env.get("PATH"))

    def _prepare_tools(self) -> None:
        if IS_WINDOWS:
            mingw = self.tools / f"winlibs-{WINLIBS_RELEASE}"
            if not (mingw / ".complete").exists():
                self._download_extract(WINLIBS_URL, mingw, "compiler (WinLibs GCC 15.2, ~256 MB)", (1, 11))
            self.mingw_bin = mingw / "mingw64" / "bin"
            path = [str(self.mingw_bin)]
            if not shutil.which("git"):
                mingit = self.tools / f"mingit-{MINGIT_VERSION}"
                if not (mingit / ".complete").exists():
                    self._download_extract(MINGIT_URL, mingit, "Git (MinGit, ~46 MB)", (11, 14))
                path.append(str(mingit / "cmd"))
            self.env["PATH"] = os.pathsep.join(path + [self.env.get("PATH", "")])
            self._setup_ca_bundle()
        missing = [t for t in ("git", "cmake", "ninja", "gcc", "g++") if not self._which(t)]
        if missing:
            raise RuntimeError("Missing build tools: " + ", ".join(missing) +
                               (". Install them with your package manager." if not IS_WINDOWS else "."))
        for tool, args in (("gcc", ["--version"]), ("cmake", ["--version"])):
            out = subprocess.run([self._which(tool)] + args, capture_output=True, text=True, env=self.env)
            self.log(out.stdout.splitlines()[0] if out.stdout else f"{tool}: ?")

    def _setup_ca_bundle(self) -> None:
        """WinLibs' CMake downloads over TLS (AnyPS5 fetches FFmpeg during configure) but doesn't
        know where Windows keeps trusted certificates. Point it at a CA bundle, as AnyPS5's own
        CI does with SSL_CERT_FILE: Git's bundle first, else the Windows certificate store.
        Verification stays on."""
        if self.env.get("SSL_CERT_FILE") and Path(self.env["SSL_CERT_FILE"]).is_file():
            return
        bundle: Path | None = None
        git = self._which("git")
        if git:
            for parent in Path(git).resolve().parents:  # …\Git\cmd\git.exe → …\Git\mingw64\etc\ssl\certs
                cand = parent / "mingw64" / "etc" / "ssl" / "certs" / "ca-bundle.crt"
                if cand.is_file():
                    bundle = cand
                    break
        if bundle is None:
            bundle = self._export_windows_roots()
        if bundle:
            self.env["SSL_CERT_FILE"] = str(bundle)
            self.env["CURL_CA_BUNDLE"] = str(bundle)
            self.log(f"› Using certificates from {bundle}")
        else:
            self.log("› Warning: no certificate bundle found; downloads during configure may fail")

    def _export_windows_roots(self) -> Path | None:
        import ssl
        pems = []
        for store in ("ROOT", "CA"):
            try:
                for cert, enc, trust in ssl.enum_certificates(store):  # type: ignore[attr-defined]
                    if enc == "x509_asn" and (trust is True or "1.3.6.1.5.5.7.3.1" in trust):
                        pems.append(ssl.DER_cert_to_PEM_cert(cert))
            except Exception:
                pass
        if not pems:
            return None
        out = self.tools / "ca-bundle.pem"
        self.tools.mkdir(parents=True, exist_ok=True)
        out.write_text("".join(pems))
        return out

    def _download_extract(self, url: str, dest: Path, label: str, span: tuple[int, int]) -> None:
        self.log(f"› Downloading {label}")
        self.tools.mkdir(parents=True, exist_ok=True)
        tmp = self.tools / (dest.name + ".zip.part")
        req = urllib.request.Request(url, headers={"User-Agent": "AnyPS5-Relinker-GUI"})
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done, last = 0, -1
            while chunk := r.read(1 << 20):
                self._check()
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = span[0] + (span[1] - span[0]) * done / total
                    if int(pct) != last:
                        last = int(pct)
                        self.progress({"step": f"Downloading {label.split(' (')[0]}", "pct": last})
        self.log(f"› Extracting {label.split(' (')[0]}")
        shutil.rmtree(dest, ignore_errors=True)
        with zipfile.ZipFile(tmp) as z:
            root = dest.resolve()
            for info in z.infolist():
                target = (dest / info.filename).resolve()
                if root != target and root not in target.parents:
                    continue  # skip anything that would escape the folder
                z.extract(info, dest)
        tmp.unlink()
        (dest / ".complete").write_text("ok")

    def _git(self, *args: str, cwd: Path | None = None, echo: bool = True) -> str:
        return self._exec([self._which("git"), "-c", "core.longpaths=true", *args], cwd=cwd, capture=True, echo=echo)

    def _fetch_source(self) -> dict:
        self.src.parent.mkdir(parents=True, exist_ok=True)
        if (self.src / ".git").exists():
            self._git("fetch", "--depth", "1", "origin", "main", cwd=self.src)
            self._git("reset", "--hard", "FETCH_HEAD", cwd=self.src)
        else:
            shutil.rmtree(self.src, ignore_errors=True)
            self._git("clone", "--depth", "1", "--branch", "main", REPO_URL, str(self.src))
        self._git("submodule", "sync", "--recursive", cwd=self.src)
        try:
            self._git("submodule", "update", "--init", "--recursive", "--depth", "1", cwd=self.src)
        except RuntimeError:
            self.log("› Shallow submodule fetch failed; fetching full history instead")
            self._git("submodule", "update", "--init", "--recursive", cwd=self.src)
        out = self._git("log", "-1", "--format=%h%x1f%H%x1f%cI%x1f%s", cwd=self.src, echo=False).strip().splitlines()[-1]
        short, full, date, subject = out.split("\x1f")
        self.log(f"› AnyPS5 main @ {short} ({date[:10]}): {subject}")
        return {"commit": short, "commit_full": full, "date": date, "subject": subject}

    def _apply_patches(self) -> list[str]:
        """Apply the app's fixes for functions AnyPS5 doesn't provide yet. The fetch above resets
        the source, so they're reapplied on every build. A patch AnyPS5 has since merged is
        skipped, and one that no longer fits the newer code is skipped with a note."""
        applied: list[str] = []
        if not self.patches_dir or not self.patches_dir.is_dir():
            return applied
        for patch in sorted(self.patches_dir.glob("*.patch")):
            def ok(*args: str) -> bool:
                try:
                    self._git("apply", *args, str(patch), cwd=self.src)
                    return True
                except RuntimeError:
                    return False
            if self._already_defines(patch):
                self.log(f"› Fix {patch.name} adds a function AnyPS5 already defines; skipping")
            elif ok("--check"):
                ok()
                applied.append(patch.stem)
                self.log(f"› Applied fix {patch.name}")
            elif ok("--reverse", "--check"):
                self.log(f"› Fix {patch.name} is already part of AnyPS5 now; skipping")
            else:
                self.log(f"› Fix {patch.name} no longer fits the newest AnyPS5 code; skipped")
        return applied

    def _already_defines(self, patch: Path) -> bool:
        """True when a patch adds a function the source already defines. `git apply` only looks at
        surrounding lines, so a function merged upstream would otherwise be added twice and the
        build fails with a redefinition error."""
        file: Path | None = None
        for line in patch.read_text(encoding="utf-8").splitlines():
            if line.startswith("+++ b/"):
                file = self.src / line[6:]
                continue
            m = re.match(r"\+[^\s/].*\bAPS5_VABI\s+\**(\w+)\s*\(", line)
            if m and file is not None and file.is_file():
                text = file.read_text(encoding="utf-8", errors="replace")
                if re.search(rf"^[^\n/]*\bAPS5_VABI\s+\**{m.group(1)}\s*\(", text, re.M):
                    return True
        return False

    def _cmake(self, args: list[str], span: tuple[int, int] | None = None) -> None:
        self._exec([self._which("cmake"), *args], span=span)

    def _exec(self, cmd: list[str], cwd: Path | None = None, capture: bool = False,
              span: tuple[int, int] | None = None, echo: bool = True) -> str:
        self._check()
        if echo:
            self.log("$ " + " ".join(Path(cmd[0]).name if i == 0 else c for i, c in enumerate(cmd)))
        flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
        self._proc = subprocess.Popen(cmd, cwd=cwd, env=self.env, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True, errors="replace",
                                      bufsize=1, creationflags=flags)
        captured, last = [], -1
        for line in self._proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip("\n")
            captured.append(line)
            m = NINJA_PROGRESS.match(line)
            if m and span:
                n, total = int(m[1]), int(m[2])
                pct = span[0] + (span[1] - span[0]) * n / max(total, 1)
                if int(pct) != last:
                    last = int(pct)
                    self.progress({"pct": last, "detail": f"{n}/{total}"})
                if n % 25 and n != total and "FAILED" not in line:
                    continue  # keep the log readable: show every 25th compile step
            if echo:
                self.log(line)
        code = self._proc.wait()
        self._proc = None
        self._check()
        if code != 0:
            tail = "\n".join(captured[-15:])
            raise RuntimeError(f"{Path(cmd[0]).name} {cmd[1] if len(cmd) > 1 else ''} failed (exit {code})."
                               + (f"\n{tail}" if capture else ""))
        return "\n".join(captured)

    def _package(self, info: dict) -> None:
        exe = "relinker.exe" if IS_WINDOWS else "relinker"
        relinker = self.build / "core" / "relinker" / exe
        libs = sorted((self.build / "core" / "libs" / "libs").glob("*.prx"))
        if not relinker.is_file():
            raise RuntimeError(f"Build finished but {exe} was not found in build/core/relinker.")
        if not libs:
            raise RuntimeError("Build finished but no system libraries were produced.")
        staging = self.dest.parent / "current.part"
        shutil.rmtree(staging, ignore_errors=True)
        host_libs = staging / ("libs-windows" if IS_WINDOWS else "libs-linux")
        host_libs.mkdir(parents=True)
        shutil.copy2(relinker, staging / exe)
        for lib in libs:
            shutil.copy2(lib, host_libs / lib.name)
        if IS_WINDOWS and self.mingw_bin:
            for dll in RUNTIME_DLLS:
                shutil.copy2(self.mingw_bin / dll, host_libs / dll)
                shutil.copy2(self.mingw_bin / dll, staging / dll)
        (staging / exe).chmod(0o755)
        info["libs"] = len(libs)
        (staging / "VERSION.json").write_text(json.dumps(info, indent=2))
        shutil.rmtree(self.dest, ignore_errors=True)
        staging.rename(self.dest)
        self.log(f"› Installed relinker and {len(libs)} system libraries")


def read_dev_engine(root: Path) -> dict | None:
    d = root / "engine-dev" / "current"
    exe = d / ("relinker.exe" if IS_WINDOWS else "relinker")
    if not exe.is_file():
        return None
    try:
        info = json.loads((d / "VERSION.json").read_text())
    except Exception:
        info = {}
    return {
        "dir": str(d), "relinker": str(exe),
        "libs_windows": str(d / "libs-windows") if (d / "libs-windows").is_dir() else "",
        "libs_linux": str(d / "libs-linux") if (d / "libs-linux").is_dir() else "",
        "commit": info.get("commit", "?"), "date": info.get("date", ""), "subject": info.get("subject", ""),
        "built_at": info.get("built_at", ""), "libs": info.get("libs", 0),
        "patches": info.get("patches", []),
    }
