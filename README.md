# Patchwork

Patchwork swaps Roblox assets on your own PC. You can replace a texture, skybox, sound or mesh with another asset ID or with a file on your computer. Only your own game client sees the change.

> Patchwork is a community project. It isn't made by or affiliated with Roblox Corporation.

## How it works

Roblox looks up every asset through `assetdelivery.roblox.com` before downloading it. Patchwork runs a small local proxy ([mitmproxy](https://mitmproxy.org) in local-redirect mode) that steps in at that lookup:

- **Swap with an asset ID:** Patchwork changes the ID Roblox asks for, so Roblox downloads your replacement.
- **Swap with a file:** Patchwork answers the lookup with a link to a file on your PC and serves that file to Roblox.

What it touches:

- **Only Roblox's traffic.** The redirector captures `RobloxPlayerBeta.exe` and nothing else. Your browser and other apps never go through it, and your Windows proxy settings don't change.
- **Only the asset server.** Of Roblox's traffic, only connections to `assetdelivery.roblox.com` are decrypted. Game servers, chat, login and everything else pass straight through.
- **Never the game's memory.** Patchwork doesn't inject code into Roblox or change it in memory.

### The certificate

To read Roblox's encrypted asset lookups, Patchwork needs Roblox to trust a certificate:

- On first start, Patchwork creates a certificate authority **unique to your PC** in `%APPDATA%\Patchwork\certs`. Its private key never leaves that folder, and no shared key ships with Patchwork.
- Roblox ignores the Windows certificate store and uses its own `ssl\cacert.pem` in each version folder. Patchwork adds its certificate there, between clearly marked lines.
- Nothing is added to the Windows certificate store, so other apps don't trust it.
- **Remove certificate** in the app takes it out of every Roblox install and deletes the key.

## Is it allowed?

- **Roblox's rules:** Roblox's Terms of Use forbid unauthorized third-party software that modifies the experience, so any asset swapper is a gray area. Patchwork avoids what anti-cheat looks for (memory tampering and code injection), but **use it at your own risk**.
- **Keep it cosmetic:** don't use swaps that give you an advantage, like see-through walls or highlighted enemies. Individual games can and do ban players for that.
- **Use your own files:** only swap in files you made or are allowed to use. Patchwork doesn't come with any Roblox or third-party assets.

## Install

1. Download `Patchwork.exe` from the [Releases](../../releases) page, or from the latest successful **Build** run under the Actions tab.
2. Run it. Windows SmartScreen may warn that the app is unrecognized, because the build isn't code-signed. Some antivirus tools also flag apps that redirect network traffic. The source is all here if you want to check it or build it yourself.
3. The first time you press **Start**, Windows asks for permission to run mitmproxy's redirector. It needs admin rights to capture Roblox's connections.

## Use

1. Press **Add swap**:
   - Paste the asset ID(s) to replace. Plain IDs, `rbxassetid://` and Roblox links all work.
   - Choose what to replace them with: another asset ID, or a file.
2. Press **Start**.
3. Open Roblox, or close it and open it again if it was already running.

Swaps you add or change while Patchwork is running apply to assets Roblox downloads afterwards.

Roblox keeps assets it has already downloaded. If a swap doesn't show up, close Roblox, press **Clear Roblox cache**, and join again. Do the same after turning a swap off.

### Replacement files

Use the format Roblox expects for that asset type:

| Asset | File |
| --- | --- |
| Image / texture / skybox face | `.png` or `.jpg` |
| Sound | `.ogg` or `.mp3` |
| Mesh | Roblox `.mesh` |

You need the **image** ID of a texture or decal, not the decal's own ID.

### Settings file

Swaps are stored in `%APPDATA%\Patchwork\config.json`. Two advanced settings live there too:

- `intercept_hosts`: which hosts are decrypted.
- `processes`: which programs are captured. Add `"RobloxStudioBeta.exe"` to use Patchwork with Studio.

## Supported installs

- **Supported:** Roblox from roblox.com, and launchers that keep their own copy under `%LOCALAPPDATA%\<Launcher>\Versions` (Bloxstrap, Fishstrap and similar).
- **Not supported:** the Microsoft Store version of Roblox, because its files can't be changed.

Roblox installs updates into a new folder. While Patchwork is running, it trusts its certificate in new versions every 20 seconds.

## Build from source

You need Python 3.12 or newer (from python.org, with Tcl/Tk).

Install Patchwork with its development tools:

```bash
pip install -e ".[dev]"
```

Run the tests:

```bash
pytest
```

Start the app from source:

```bash
python -m patchwork
```

Build `dist\Patchwork.exe`:

```bash
pyinstaller packaging/patchwork.spec --noconfirm
```

Check that a build has everything it needs (this writes a report file):

```bash
dist\Patchwork.exe --self-check selfcheck.txt
```

Every push runs `.github/workflows/build.yml`, which tests Patchwork on Linux and Windows, builds the `.exe` and runs its self-check. Pushing a `v*` tag publishes a release.

## Project layout

| Path | What it does |
| --- | --- |
| `patchwork/rules.py` | Rewrites asset-delivery requests and responses (no mitmproxy dependency) |
| `patchwork/proxy.py` | The mitmproxy addon and the background proxy runner |
| `patchwork/roblox.py` | Finds Roblox installs, adds and removes the certificate, clears the cache |
| `patchwork/config.py` | The swap list and settings |
| `patchwork/gui.py` | The desktop window |

## License

MIT. See [LICENSE](LICENSE).
