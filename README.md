# AnyPS5 Relinker

A simple desktop app for converting PS5 executables to run with [AnyPS5](https://github.com/boykopovar/AnyPS5) on your PC.

**Pick a file → click Convert → get a folder you can run.**

> Not affiliated with AnyPS5 or Sony. This app doesn't include, download or decrypt any games. You need your own decrypted game files.

## Get started (Windows)

1. Install [Python](https://www.python.org/downloads/) 3.9 or newer. Tick **Add to PATH**.
2. Download this repository.
3. Double-click **`run.bat`**.

AnyPS5 is already included, so there's nothing else to install.
Linux/macOS: run `./run.sh` instead.

## How to use it

1. **Drop in the game's `eboot.bin`.** It must be the **decrypted** version. The `sce_module` folder next to it is found automatically.
2. Choose **Windows** (or Linux).
3. Pick an **output folder**.
4. Click **Convert**.

You get:

```
app.exe
libs/
app0/
```

## The game's files in `app0`

After converting, the app copies the game's **data** into `app0` for you, including `sce_sys/` and, for Unity games, `Media/`. It finds the dump by looking for `sce_sys/param.json` in the folder with the `eboot.bin`, then up to two folders above it. That covers a decrypted `eboot.bin` kept in a subfolder of the dump, such as `D:\decrypted\eboot.bin` with the game at `D:\`. Files already in `app0` are never overwritten, so converting again only copies what's missing.

If it can't find the dump, the console says so. Then copy the data from your dump into `app0` yourself, without overwriting the `.guest.prx` files already there.

Then run the game from PowerShell so you can see any errors:

```powershell
cd path\to\your\output
.\app.exe
```

## Fixing common problems

| Problem | Fix |
|---|---|
| **"Not ELF"** shown on the file | The file is encrypted. You need the decrypted version. This app can't decrypt files. |
| **`param.json not found`** when launching | The game dump wasn't found next to `eboot.bin`. Copy `sce_sys/` from the dump into `app0`. |
| Other "file not found" errors when launching | Copy the rest of the game's data into `app0`. |
| **"Duplicate guest export"** when converting | Two modules clash. Remove one of the two files named in the message. |
| Game crashes after starting | Usually something AnyPS5 doesn't support yet. See [Compatibility](#compatibility). |

## Compatibility

Converting only changes the code. **Whether the game runs depends on AnyPS5**, which is still early. If you hit a crash, report it at [AnyPS5 issues](https://github.com/boykopovar/AnyPS5/issues) with the exact error text and the game's title ID.

## Unity games

Unity plugins in `Media/Plugins` are converted for you and linked into the game automatically. You don't need to add them by hand.

## Which AnyPS5 does it use? (Settings)

| Choice | What it is |
|---|---|
| **Official release** (default) | The built-in AnyPS5 v0.1.1. A button appears when a newer release is out. |
| **Latest build** | Builds AnyPS5's newest code on your PC. More games work, but the first build downloads about 300 MB, needs about 5 GB of disk and takes 20 to 40 minutes. |
| **My own build** | Use a `relinker.exe` you built yourself. |

"Latest build" also applies the small fixes in the `patches/` folder. A fix is skipped automatically if AnyPS5 already has it.

## Make a standalone app

Run **`build.bat`**. It creates `dist\AnyPS5Relinker\` with an `.exe` that doesn't need Python. Zip the whole folder to share it.

## Options

| Option | What it does |
|---|---|
| Convert for Intel CPUs | Turn on if your PC has an Intel processor (`--to-intel`) |
| Hide console window | Uses the Windows GUI mode (`--windows-gui`) |
| Startup diagnostics | Reports missing dependencies at launch (`--windows-diagnostics`) |
| Write registry file | Saves a `.registry.json` next to the exe (`--registry`) |
| Unused import filter | Removes imports the game never uses (`unused-filter=0/1/2`) |

The exact command is always shown at the bottom of the app.

## Good to know

- Your original files are never changed.
- Settings are stored in `%APPDATA%\AnyPS5Relinker\settings.json`.
- "Latest build" files live in `%LOCALAPPDATA%\A5build\`. Delete that folder to free the space.
- The app checks GitHub for AnyPS5 updates on launch. You can turn this off in Settings.

## License and credits

The built-in `engine/` folder contains unmodified AnyPS5 release files, © the AnyPS5 contributors, licensed under **GPL v2**. Source: <https://github.com/boykopovar/AnyPS5>. See [`engine/NOTICE.txt`](engine/NOTICE.txt).

PS5 and PlayStation are trademarks of Sony Interactive Entertainment.
