# Patchwork for RIVALS

Patchwork swaps [RIVALS](https://www.roblox.com/games/17625359962) assets on your own PC. You can replace a texture, skin, sound or mesh with another asset ID, a web link, or a file on your computer. Only your own game client sees the change, and only while you're in RIVALS.

> Patchwork is a community project. It isn't made by or affiliated with Roblox Corporation.

## How it works

Roblox looks up every asset through `assetdelivery.roblox.com` before downloading it. Patchwork runs a small local proxy ([mitmproxy](https://mitmproxy.org) in local-redirect mode) that steps in at that lookup:

- **Swap with an asset ID:** Patchwork changes the ID Roblox asks for, so Roblox downloads your replacement.
- **Swap with a file:** Patchwork answers the lookup with a link to a file on your PC and serves that file to Roblox.

What it touches:

- **Only Roblox's traffic.** The redirector captures `RobloxPlayerBeta.exe` and nothing else. Your browser and other apps never go through it, and your Windows proxy settings don't change.
- **Only the asset server.** Of Roblox's traffic, only connections to `assetdelivery.roblox.com` are decrypted. Game servers, chat, login and everything else pass straight through.
- **Never the game's memory.** Patchwork doesn't inject code into Roblox or change it in memory.

### RIVALS only

Swaps apply only while you're in RIVALS. In any other game, Roblox loads its normal assets.

- Roblox writes a log of every session to `%LOCALAPPDATA%\Roblox\logs`, including which game you join. Patchwork follows that log, the same way launchers like Bloxstrap do.
- RIVALS is place `17625359962`. The first time you join it, Patchwork also notes the RIVALS experience ID from the log, so match servers and teleports inside RIVALS count too.
- If Roblox sends a `Roblox-Place-Id` header with an asset request, Patchwork checks that too.

### The certificate

To read Roblox's encrypted asset lookups, Patchwork needs Roblox to trust a certificate:

- On first start, Patchwork creates a certificate authority **unique to your PC** in `%APPDATA%\Patchwork\certs`. Its private key never leaves that folder, and no shared key ships with Patchwork.
- Roblox ignores the Windows certificate store and uses its own `ssl\cacert.pem` in each version folder. Patchwork adds its certificate there, between clearly marked lines.
- Nothing is added to the Windows certificate store, so other apps don't trust it.
- Patchwork also sets one Roblox client setting, `DFFlagHttpUseEmbeddedCaCerts = False`, in each version's `ClientSettings\ClientAppSettings.json`, so Roblox reads `cacert.pem` and not its built-in certificate list. Fleasion sets the same one. Your other settings in that file are kept.
- Launchers like Bloxstrap and Fishstrap copy their `Modifications` folder over Roblox on every launch. Patchwork makes the same two changes there, so the launcher doesn't undo them.
- **Remove certificate** (on the **Settings** tab) takes it and the setting out of every Roblox install and launcher folder, and deletes the key.

## Can I get banned?

Possibly. No asset swapper can promise you won't be. Roblox's Terms of Use forbid unauthorized third-party software that modifies the experience, and Roblox and the RIVALS developers decide who gets banned. **Use Patchwork at your own risk.**

> **You can be banned from RIVALS for using skin changers.** Swapping weapon or character skins with Patchwork makes it a skin changer, even though only you can see the change. Think carefully before using skin swaps on an account you care about.

Patchwork is built to keep the risk low:

- **The game itself is never touched.** Patchwork doesn't inject code into Roblox or change it in memory. That's what Roblox's anti-cheat mainly looks for.
- **Account and network traffic is left alone.** Logins, chat and game servers pass through untouched, and Patchwork never uses your account or sends anything to Roblox on its own.
- **Risky features are left out:** no custom FastFlags (beyond the one certificate setting above), no account tools and no username spoofing.
- **Link and file swaps don't change what Roblox asks its servers for.** Asset-ID swaps do, and Roblox could see that, so Patchwork reminds you when you add one. You can turn that reminder off on the **Settings** tab.
- **Stop puts Roblox back to normal.** When you press **Stop**, or close Patchwork while it's running, Patchwork removes its certificate and setting and clears the asset cache. Turn this off on the **Settings** tab if you'd rather keep them between sessions.

Two things are up to you:

- **Never use swaps that give you an advantage**, like see-through walls or easier-to-spot enemies. Even purely cosmetic swaps aren't risk-free: skin changers can get you banned too.
- **Try Patchwork on an alt account first.**

Only swap in files you made or are allowed to use. Patchwork doesn't come with any Roblox or third-party assets.

## Install

1. Download `Patchwork-v<version>-Windows.exe` (for example `Patchwork-v1.0.0-Windows.exe`) from the [Releases](../../releases) page. Only download it from there: copies from anywhere else could have been changed.
2. Run it. Windows SmartScreen may warn that the app is unrecognized, because the build isn't code-signed. Some antivirus tools also flag apps that redirect network traffic. The source is published here so you can check exactly what Patchwork does.
3. The first time you press **Start**, Windows asks for permission to run mitmproxy's redirector. It needs admin rights to capture Roblox's connections.

## Use

Patchwork's window has three tabs across the top: **Replacer**, **Activity** and **Settings**. The **Start** button at the top right starts and stops Patchwork. The badge next to it shows whether you're in RIVALS and swaps are on, paused, or waiting.

### Replacer

Swaps are grouped into profiles, for example "Skins" or "Hit sounds". The **Configuration** bar at the top manages them:

- **Editing** picks which profile's swaps are in the table.
- **Enabled** ticks which profiles are switched on. You can switch on several at once.
- **New**, **Duplicate**, **Rename** and **Delete** act on the profile you're editing.
- **Import** and **Export** load and save presets (see below).

To add a swap, use the **Add / Edit Profile** form under the table:

1. **Asset IDs:** the IDs to replace. Plain IDs, `rbxassetid://` and Roblox links all work.
2. **Replace With:** another asset ID, a direct web link to a file, or a file on your PC (**Browse…**). The line underneath shows what Patchwork detected.
3. Press **Add New**.

To change a swap, select it in the table and press **Load Selected** (or double-click it), edit the form, then press **Update Selected**.

- Each swap has its own on/off switch in the **Status** column.
- Click a column heading to sort by it, and use **Filter** to search.
- **On / Off** and **Remove** act on the selected swaps.
- The footer has **Clear Cache**, **Open Configs** and **Undo (Ctrl+Z)**.

Keyboard shortcuts:

| Keys | What they do |
| --- | --- |
| **Ctrl+N** | Jump to the form to add a swap |
| **Ctrl+F** | Filter swaps |
| **Ctrl+Z** | Undo the last change |
| **Ctrl+A** | Select every swap |
| **Space** | Turn the selected swaps on or off |
| **Delete** | Remove the selected swaps |
| **Enter** | Load the selected swap into the form |

The **Activity** tab shows every swap as Roblox loads it, which game you're in, and any warnings.

Roblox keeps assets it has already downloaded. Starting Patchwork clears that cache automatically when Roblox is closed. If a swap you add later doesn't show up, close Roblox, press **Clear Cache**, and join again. Do the same after turning a swap off.

### Web links

For a link, Patchwork downloads the file once and keeps a copy in `%APPDATA%\Patchwork\downloads`. Roblox gets the file from Patchwork, so it only ever talks to its own servers. **Settings → Clear downloaded files** fetches every link fresh.

### Presets and Fleasion files

- **Import** loads a preset, either a Patchwork preset or a Fleasion config with `replacement_rules`, as a new profile. Patchwork asks first if the preset downloads files from the web.
- From Fleasion configs, Patchwork reads the `id` and `cdn` modes. Rules using other modes are listed as skipped.
- **Export** saves the profile you're editing, as a Patchwork preset or a Fleasion preset. Fleasion presets can't point at files on your PC, so those swaps are left out.
- Each profile is a `.json` file in `%APPDATA%\Patchwork\configs` (**Open Configs**). You can drop preset files straight into that folder; they show up next time Patchwork starts.
- Replacement files kept in a subfolder there, such as `configs\Sky\up.png`, are saved as `/Sky/up.png`. A profile and its files can then be shared as one folder.

### Replacement files

Use the format Roblox expects for that asset type:

| Asset | File |
| --- | --- |
| Image / texture / skybox face | `.png`, `.jpg` or `.webp` |
| Sound | `.ogg` or `.mp3` |
| Mesh | Roblox `.mesh` |

You need the **image** ID of a texture or decal, not the decal's own ID.

### Settings

`%APPDATA%\Patchwork\settings.json` holds your Settings-tab choices, plus `intercept_hosts`, the hosts Patchwork decrypts. Leave `intercept_hosts` alone unless you know you need it.

## Supported installs

- **Supported:** Roblox from roblox.com, and launchers that keep their own copy under `%LOCALAPPDATA%\<Launcher>\Versions` (Bloxstrap, Fishstrap and similar).
- **Not supported:** the Microsoft Store version of Roblox, because its files can't be changed.

Roblox installs updates into a new folder. While Patchwork is running, it trusts its certificate in new versions every 20 seconds.

## Why the source is public

Patchwork's source code is published so anyone can check that it is safe, and isn't a RAT, a token logger or anything else hidden. It isn't here as a build kit: use the official download from the [Releases](../../releases) page.

Things worth checking, and where to look:

| Question | Where to look |
| --- | --- |
| Does it touch my Roblox account or cookies? | No code reads browser data, cookies or Roblox login files. Search the source for `ROBLOSECURITY` or `cookie`: nothing matches. |
| What does it connect to? | Only Roblox's asset server (`patchwork/proxy.py`), the web links in your own swaps (`patchwork/downloads.py`), and its own window on `127.0.0.1` (`patchwork/server.py`). It never sends anything about you anywhere. |
| What traffic can it read? | Only Roblox's own traffic, and of that only `assetdelivery.roblox.com` (`intercept_hosts` in `patchwork/config.py`, used by `patchwork/proxy.py`). |
| What does it change on my PC? | Roblox's `ssl\cacert.pem` and `ClientAppSettings.json`, the asset cache, and its own `%APPDATA%\Patchwork` folder (`patchwork/roblox.py`). Stop undoes the Roblox changes. |
| Does it inject into the game? | No. Nothing in the source touches Roblox's memory or process. |

How the code is organised:

| Path | What it does |
| --- | --- |
| `patchwork/rules.py` | Rewrites asset-delivery requests and responses |
| `patchwork/proxy.py` | The local proxy and its swap logic |
| `patchwork/roblox.py` | Finds Roblox installs, adds and removes the certificate and client setting, clears the cache |
| `patchwork/games.py` | Reads Roblox's log to tell when you're in RIVALS |
| `patchwork/config.py` | The swap model and "Replace with" detection |
| `patchwork/profiles.py` | Profiles in `configs/`, settings, undo |
| `patchwork/presets.py` | Patchwork and Fleasion preset import/export |
| `patchwork/downloads.py` | Download cache for web-link swaps |
| `patchwork/controller.py` | Everything the window does |
| `patchwork/server.py` | Serves the interface on 127.0.0.1, protected by a per-launch token |
| `patchwork/app.py` | Opens the window |
| `patchwork/webui/` | The interface |

## License

MIT. See [LICENSE](LICENSE).
