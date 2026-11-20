# Patchwork for RIVALS

Patchwork swaps [RIVALS](https://www.roblox.com/games/17625359962) images, sounds and skins on your own PC. You can replace an asset with another asset ID, a web link, or a file on your computer. Only your own game client sees the change.

> Patchwork is a community project. It isn't made by or affiliated with Roblox Corporation.

## How it works

Roblox saves every asset it downloads in a cache on your PC: `%LOCALAPPDATA%\Roblox\rbx-storage.db` plus the `rbx-storage` folder next to it. Patchwork swaps assets by rewriting their entries in that cache:

1. **Finding the asset.** For each asset ID you swap, Patchwork downloads the original once from Roblox (as a signed-out visitor) and remembers its fingerprint. A cache entry with the same fingerprint is that asset.
2. **Swapping it.** When Roblox has downloaded that asset, Patchwork backs up the cached original and puts your replacement in its place. It keeps Roblox's own record of the download and updates the length and checksum so Roblox accepts it.
3. **Showing up.** Roblox uses the swapped copy the next time it loads that asset, usually your next match. If Roblox downloads the asset again, Patchwork swaps it again.
4. **Putting it back.** Turning a swap off, or quitting Patchwork, puts the original back.

What it touches, and what it doesn't:

- **Roblox's cache only.** No network interception, no certificates, no Roblox settings changes. It works the same whichever launcher you use (Roblox, Bloxstrap, Fishstrap, Froststrap), because they all share that cache.
- **Never the game's memory or your account.** Patchwork doesn't inject code into Roblox, and never reads or uses your login.

What can be swapped:

- **Images** (textures, skyboxes, UI, item backgrounds): your PNG, JPG or WEBP is converted to the format of the cached original.
- **Sounds:** but Roblox only lets signed-in players download most sounds, and Patchwork never uses your account. So most sounds can't be found automatically yet, and Activity tells you which.
- **Skins:** a mesh or model swapped for another skin's mesh or model, by asset ID.
- **Not allowed:** removing or blanking out 3D objects (that would make them invisible, an advantage rather than a skin), turning images into 3D or the other way round, and Roblox's collision data. `.obj` files can't be used.
- **Not yet:** textures Roblox stores as GPU texture packs (KTX2). Activity tells you when an asset is one of these.

The cache format and checksum are ported from [rbxcache](https://github.com/PirxcyFinal/rbxcache) (MIT). See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Can I get banned?

Possibly. No asset swapper can promise you won't be. Roblox's Terms of Use forbid unauthorized third-party software that modifies the experience, and Roblox and the RIVALS developers decide who gets banned. **Use Patchwork at your own risk.**

> **You can be banned from RIVALS for using skin changers.** Swapping weapon or character skins with Patchwork makes it a skin changer, even though only you can see the change. Think carefully before using skin swaps on an account you care about.

Patchwork is built to keep the risk low:

- **The game itself is never touched.** Patchwork doesn't inject code into Roblox or change it in memory. That's what Roblox's anti-cheat mainly looks for.
- **No connection to your account.** Patchwork never reads or uses your Roblox login. Its only requests to Roblox are signed-out downloads of the assets you swap.
- **3D objects can't be made invisible.** Skins can be swapped for other skins, but nothing 3D can be removed.
- **Quitting Patchwork puts the originals back.** Turn this off on the **Settings** tab if you'd rather keep swaps between sessions.

Two things are up to you:

- **Never use swaps that give you an advantage**, like see-through walls or easier-to-spot enemies. Even purely cosmetic swaps aren't risk-free: skin changers can get you banned too.
- **Try Patchwork on an alt account first.**

Only swap in files you made or are allowed to use. Patchwork doesn't come with any Roblox or third-party assets.

## Install

1. Download `Patchwork-v<version>-Windows.exe` (for example `Patchwork-v1.0.0-Windows.exe`) from the [Releases](../../releases) page. Only download it from there: copies from anywhere else could have been changed.
2. Run it. Windows SmartScreen may warn that the app is unrecognized, because the build isn't code-signed. The source is published here so you can check exactly what Patchwork does.
3. Patchwork runs as administrator, so Windows asks for permission when you open it. When it starts with Windows (on by default, see **Settings**), it runs from Task Scheduler and there's no prompt.

## Use

Patchwork's window has three tabs across the top: **Replacer**, **Activity** and **Settings**. There's no Start button: Patchwork starts swapping as soon as it opens, and keeps going until you quit it from its tray icon. The badge at the top right shows how many assets are swapped in Roblox's cache.

- **System tray.** Closing the window keeps Patchwork running in the system tray (next to the clock), so swaps keep working. Right-click the tray icon to open Patchwork, clear Roblox's cache, or quit. Quitting puts the original assets back.
- **Start with Windows.** Patchwork starts in the tray when you sign in to Windows, so swaps work whenever you open Roblox. Turn this off on the **Settings** tab. Opening Patchwork again brings the running window back instead of starting a second copy.
- **Swaps not showing?** A swap applies once Roblox has downloaded that asset, and shows the next time Roblox loads it, so play a match and then another. Activity says which assets are swapped, which are still waiting for Roblox to download them, and which can't be swapped and why.

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

The **Activity** tab shows each asset as it's swapped in Roblox's cache, a regular summary (swapped, waiting for Roblox to download, can't be swapped), and any warnings.

**Cache tab, like rbxcache.** Browse everything Roblox has downloaded, most recently used first, with previews (including decoded KTX2 textures) and playable sounds. Click any file to replace it with your own, put the original back, or save a copy. No asset IDs needed, so it works for RIVALS' own textures, sounds and skins too.

**Skins tab: skin capture.** RIVALS' own skins can't be looked up by asset ID without a Roblox login, which Patchwork never uses. Instead you show the skin you have and the skin you want in game, Patchwork notes which cache files Roblox loads for each, and you match them up (mesh to mesh, texture to texture). The captured swap is reapplied whenever Roblox downloads those files again.

**Clear Cache** deletes Roblox's cache but keeps the assets Patchwork has swapped, so your swaps still load instantly afterwards. It works with Roblox open.

### Web links

For a link, Patchwork downloads the file once and keeps a copy in `%APPDATA%\Patchwork\downloads`. **Settings → Clear downloaded files** fetches every link fresh.

### Presets and Fleasion files

- **Import** loads a preset, either a Patchwork preset or a Fleasion config with `replacement_rules`, as a new profile. Patchwork asks first if the preset downloads files from the web.
- From Fleasion configs, Patchwork reads `id` rules (including removals), `cdn` links, `local` files and groups. `.obj` models and anything else are listed as skipped.
- **Export** saves the profile you're editing, as a Patchwork preset or a Fleasion preset.
- Each profile is a `.json` file in `%APPDATA%\Patchwork\configs` (**Open Configs**). You can drop preset files straight into that folder; they show up next time Patchwork starts.
- Replacement files kept in a subfolder there, such as `configs\Sky\up.png`, are saved as `/Sky/up.png`. A profile and its files can then be shared as one folder.

### Replacement files

Use the format Roblox expects for that asset type:

| Asset | File |
| --- | --- |
| Image / texture / skybox face | `.png`, `.jpg` or `.webp` |
| Sound | `.ogg` or `.mp3` |
| Skin (mesh or model) | Another skin's asset ID, not a file |

You need the **image** ID of a texture or decal, not the decal's own ID.

### Settings

`%APPDATA%\Patchwork\settings.json` holds your Settings-tab choices. Patchwork also keeps:
- `idmap.json`, the fingerprints of asset IDs it has looked up;
- `originals\`, backups of the cache entries it changed.

## Supported installs

- **Supported:** Roblox from roblox.com and every launcher that uses Roblox's normal cache folder (Bloxstrap, Fishstrap, Froststrap and similar).
- **Not supported:** the Microsoft Store version of Roblox, which keeps its cache elsewhere.

## Why the source is public

Patchwork's source code is published so anyone can check that it is safe, and isn't a RAT, a token logger or anything else hidden. It isn't here as a build kit: use the official download from the [Releases](../../releases) page.

Things worth checking, and where to look:

| Question | Where to look |
| --- | --- |
| Does it touch my Roblox account or cookies? | No code reads browser data, cookies or Roblox login files. Search the source for `ROBLOSECURITY` or `cookie`: nothing matches. |
| What does it connect to? | Roblox's asset server, signed out, to download the assets you swap (`fetch_asset` in `patchwork/swapper.py`); the web links in your own swaps (`patchwork/downloads.py`); and its own window on `127.0.0.1` (`patchwork/server.py`). It never sends anything about you anywhere. |
| Does it read my network traffic? | No. There's no proxy or network interception of any kind. |
| What does it change on my PC? | Entries in Roblox's asset cache for the assets you swap, with backups (`patchwork/cache.py`, `patchwork/swapper.py`), and its own `%APPDATA%\Patchwork` folder. Quitting puts the originals back. |
| Does it inject into the game? | No. Nothing in the source touches Roblox's memory or process. |

How the code is organised:

| Path | What it does |
| --- | --- |
| `patchwork/cache.py` | Reads and rewrites entries in Roblox's cache (format ported from rbxcache) |
| `patchwork/swapper.py` | Finds each asset's cache entry and applies, re-applies and undoes swaps |
| `patchwork/roblox.py` | Finds and closes Roblox, clears the cache, and removes the certificate setup older versions added |
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
