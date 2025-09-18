# Instagram Unfollow Automation (Safety-First, Windows + Chrome + Playwright Python)

This tool automates unfollowing on Instagram from a real, visible Chrome session. It attaches via Chrome DevTools Protocol (CDP), locates usernames in the "Following" dialog, and performs human-like mouse movements and clicks using low-level CDP input events. It implements conservative rate limits, a whitelist, a file-based killswitch, and block-message detection.

Important: There is no 100% safe automation. Use this conservatively, start with DRY_RUN mode, and stop immediately if Instagram shows any limitation or block message.

## Features

- Conservative defaults: small daily/hourly caps and long randomized delays
- Whitelist of usernames to keep following
- DRY_RUN mode (log-only)
- Human-like cursor movement via CDP `Input.dispatchMouseEvent`
- Confirmation dialog handling
- File-based killswitch (`STOP_NOW`)
- Persistent state (`state.json`) to enforce daily caps across runs
- Works with your actual logged-in Chrome (no automated login/2FA)

## Requirements

- Windows
- Chrome installed
- Python 3.10+

## Clone and Install

```powershell
# 1) Clone repo
git clone <this-repo-url> auto_unfollow_instagram
cd auto_unfollow_instagram

# 2) Create venv
python -m venv .venv

# 3) Activate venv
.\.venv\Scripts\activate

# 4) Install dependencies
python -m pip install --upgrade pip
pip install -r requirements.txt

# 5) Install Playwright browsers
python -m playwright install
```

## Launch Chrome with Remote Debugging

Use the helper script to start a dedicated Chrome profile with remote debugging:

```powershell
# Close all Chrome windows first (recommended)
# taskkill /IM chrome.exe /F

# Start dedicated Chrome instance
./launch_chrome_remote_debug.bat
```

In the launched Chrome window:

1. Log in to Instagram (main account).
2. Navigate to your profile.
3. Click "Following" so the following list dialog opens and is visible.

Keep this window in front for the session.

## Configuration via .env

You can control both the launcher and the Python script using a `.env` file in the project root. The batch launcher reads:

- `CHROME_PATH` — Full path to `chrome.exe` (no quotes). Default: `C:\Program Files\Google\Chrome\Application\chrome.exe`
- `CHROME_USER_DATA_DIR` — Folder used as the Chrome profile for automation. Default: `%USERPROFILE%\ChromeAutomationProfile`
- `REMOTE_DEBUG_PORT` — Remote debugging port. Default: `9222`

The Python script reads:

- `CHROME_REMOTE_DEBUG_URL` — URL Playwright connects to. Default: `http://127.0.0.1:9222`
- `DRY_RUN` — `True` or `False`. Default: `True`
- `DEBUG_HIGHLIGHT` — `True` to draw temporary overlays on target buttons (yellow), confirm buttons (green), and the "Suggested for you" header (blue). Default: `False`
- `MAX_ACTIONS_PER_RUN` — Default: `50`
- `DAILY_CAP` — Default: `80`
- `PER_HOUR_CAP` — Default: `30`
- `MIN_DELAY_SEC` / `MAX_DELAY_SEC` — Defaults: `20` / `60`
- `MAX_NO_PROGRESS_ROUNDS` — End the run after this many cycles with no new usernames processed (helps avoid loops on whitelisted accounts). Default: `6`
- `WHITELIST_FILE` — Default: `whitelist.json`
- `STATE_FILE` — Default: `state.json`
- `LOG_FILE` — Default: `unfollow.log`
- `KILLSWITCH_FILE` — Default: `STOP_NOW`

An example `.env` is already provided. Adjust paths/caps as needed.

## About ChromeAutomationProfile (user-data-dir)

The folder specified by `CHROME_USER_DATA_DIR` is a dedicated Chrome profile directory. You do not need to put anything in it manually.

When you run `launch_chrome_remote_debug.bat`, Chrome will:

- Create the folder if it doesn't exist.
- Store cookies, local storage, and your Instagram login session inside this directory.

This keeps the automation session separate from your everyday Chrome profile, reducing risk and avoiding conflicts. If you prefer to reuse your existing default profile instead, you can set `CHROME_USER_DATA_DIR` in `.env` to point to your normal Chrome profile folder, but this is not recommended.

Notes:

- Only one Chrome instance can use a given user-data-dir at a time. Make sure other Chrome windows using the same profile are closed before launching.
- The launcher uses `--remote-debugging-port=9222`; the Python script connects to `CHROME_REMOTE_DEBUG_URL`.

## Configure Whitelist

Edit `whitelist.json` (created automatically on first run if missing):

```json
[
  "friend_one",
  "brand_abc",
  "@mybestfriend"
]
```

Handles are normalized to lowercase and without the leading `@`.

## Run (DRY_RUN first)

By default, the script runs in DRY_RUN mode. It will log who it would unfollow without clicking.

```powershell
python playwright_unfollow.py
```

If output looks correct, set `DRY_RUN=False` in `.env` and re-run. Start small (5–10 actions) and observe closely.

### What you’ll see

- A colored progress bar with live details: `unf:<count> skip:<count> rem:<remaining> act:<actions>`.
- Colored logs:
  - Green for successful clicks/confirmations and verified state changes.
  - Yellow for warnings/skips (e.g., whitelist) and informational notices.
  - Red for blocks or errors (script stops).
  - Blue/Cyan for flow transitions (e.g., reaching `Suggested for you`).

When `DEBUG_HIGHLIGHT=true`, temporary overlays show you exactly what the script targets.

## Safety Controls

- Daily cap: persisted in `state.json` (default 80/day). The script stops before exceeding it.
- Per-run cap: `MAX_ACTIONS_PER_RUN` (default 50).
- Random delays: 20–60 seconds between actions (configurable).
- Killswitch: create a file named `STOP_NOW` in the project folder to stop mid-run, or press Ctrl+C in the terminal.
- Block detection: the script scans page text for common block phrases (e.g., "we limit how often"). It stops immediately if detected.

If Instagram displays any warnings like "We limit how often you can do certain things", stop and wait 24–72 hours before trying again, and reduce your caps.

## Notes & Rationale

- Attaching to your own visible Chrome profile via CDP avoids many headless/webdriver fingerprints. See Playwright `connect_over_cdp` docs: https://playwright.dev/python/docs/api/class-browsertype#browsertype-connect-over-cdp
- Low-level CDP input (`Input.dispatchMouseEvent`) enables richer pointer telemetry than `element.click()`. CDP Input docs: https://chromedevtools.github.io/devtools-protocol/tot/Input/
- DOM-based selection ensures the correct button is targeted, while the click is performed via human-like pointer movements.
- Event `isTrusted` is `true` only for events generated by the user agent; scripted `dispatchEvent` are `false`: https://developer.mozilla.org/en-US/docs/Web/API/Event/isTrusted

### About the Chrome infobar

You may see an infobar about an "unsupported command-line flag" in the dedicated Chrome. It comes from `--disable-blink-features=AutomationControlled` used by the launcher to reduce automation fingerprints. It is harmless. If you prefer a clean UI, remove that flag from `launch_chrome_remote_debug.bat`.

## Troubleshooting

- If the script cannot find the Following dialog, make sure it is open and visible.
- If selectors fail due to UI changes, adjust the constants near the top of the script (container, list items, username selector, button selection logic).
- If you hit a block, stop immediately, wait at least 24–72 hours, and reduce caps.
- Reinstall requirements if you see import errors:
  ```powershell
  pip install -r requirements.txt --upgrade
  ```

## Disclaimer

Use at your own risk, for your own account only. This project is for educational and personal-use purposes.
