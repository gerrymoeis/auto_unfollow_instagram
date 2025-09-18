def _parse_count_label(text: str) -> int | None:
    """Parse counts such as '17', '1,234', '1.2k', '1,2 rb', '1.2 jt', '1.2m'.
    Returns integer value or None if cannot parse.
    """
    try:
        s = (text or "").strip().lower()
        # Remove non-breaking spaces
        s = s.replace("\xa0", " ")
        # Find first number with optional decimal
        import re
        m = re.search(r"(\d+[\.,]?\d*)\s*([a-z]+)?", s)
        if not m:
            # fall back: all digits concatenated
            digits = "".join(ch for ch in s if ch.isdigit())
            return int(digits) if digits else None
        num_str, suffix = m.group(1), (m.group(2) or "").strip()
        num_str = num_str.replace(",", ".")
        try:
            val = float(num_str)
        except Exception:
            digits = "".join(ch for ch in num_str if ch.isdigit())
            if not digits:
                return None
            val = float(digits)

        mult = 1
        if suffix in ("k", "rb"):  # thousand
            mult = 1000
        elif suffix in ("m", "jt"):  # million / juta
            mult = 1_000_000
        elif suffix in ("b", "md"):  # billion (rare)
            mult = 1_000_000_000
        return int(round(val * mult))
    except Exception:
        return None

"""
Playwright Unfollow Tool (Hybrid CDP with human-like input)

Steps to use:
  1) Run Chrome via launch_chrome_remote_debug.bat (with your own profile).
  2) In that Chrome, log in to Instagram, open your profile, and open the "Following" dialog.
  3) Run: python playwright_unfollow.py

Safe defaults: DRY_RUN=True, conservative caps and delays. Edit config below.
"""

import asyncio
import json
import math
import os
import random
import time
from pathlib import Path
from typing import List, Tuple, Optional

from playwright.async_api import async_playwright, Page
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn, SpinnerColumn

# -------------------- CONFIG (via .env with safe defaults) --------------------
load_dotenv()
console = Console()

def env_str(key: str, default: str) -> str:
    return os.getenv(key, default)

def env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except Exception:
        return default

def env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


DRY_RUN: bool = env_bool("DRY_RUN", True)  # True = log only; False = actually click
WHITELIST_FILE: str = env_str("WHITELIST_FILE", "whitelist.json")  # list of usernames to keep (lowercase)
STATE_FILE: str = env_str("STATE_FILE", "state.json")             # persistent counts
LOG_FILE: str = env_str("LOG_FILE", "unfollow.log")
KILLSWITCH_FILE: str = env_str("KILLSWITCH_FILE", "STOP_NOW")     # create this file to stop promptly
DEBUG_HIGHLIGHT: bool = env_bool("DEBUG_HIGHLIGHT", False)

CHROME_REMOTE_DEBUG_URL: str = env_str("CHROME_REMOTE_DEBUG_URL", "http://127.0.0.1:9222")

MAX_ACTIONS_PER_RUN: int = env_int("MAX_ACTIONS_PER_RUN", 50)
DAILY_CAP: int = env_int("DAILY_CAP", 80)
PER_HOUR_CAP: int = env_int("PER_HOUR_CAP", 30)
MIN_DELAY_SEC: int = env_int("MIN_DELAY_SEC", 20)
MAX_DELAY_SEC: int = env_int("MAX_DELAY_SEC", 60)
MAX_NO_PROGRESS_ROUNDS: int = env_int("MAX_NO_PROGRESS_ROUNDS", 6)

# Selectors tuned to provided HTML: dialog + any buttons inside; filter by text/aria containing 'Following'
FOLLOWING_DIALOG_SELECTOR: str = 'div[role="dialog"]'
FOLLOWING_BUTTONS_SELECTOR: str = f"{FOLLOWING_DIALOG_SELECTOR} button"
# Find first anchor link within the same row/container to extract username from href
USERNAME_LINK_SUB_SELECTOR: str = "xpath=.//a[@role='link' and starts-with(@href, '/')][1]"

# Detection strings (lowercase)
BLOCK_PATTERNS = [
    "we limit how often",
    "action blocked",
    "try again later",
    "we restrict certain",
    "we've detected",
    "please verify",
    "challenge_required",
]

# Localization tokens (English + Indonesian)
# Buttons in list dialog
FOLLOWING_TOKENS = ("following", "mengikuti")
FOLLOW_TOKENS = ("follow", "ikuti")
# Confirm dialog
CONFIRM_UNFOLLOW_TOKENS = ("unfollow", "berhenti mengikuti", "berhenti mengikut", "berhenti")
CANCEL_TOKENS = ("cancel", "batal")
# Header that marks end of real following list
SUGGESTED_HEADER_TOKENS = (
    "suggested for you",
    "disarankan",
    "disarankan untuk anda",
    "disarankan untuk kamu",
    "disarankan untukmu",
)

# -------------------- UTILITIES --------------------

def log(msg: str, style: str | None = None) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    if style:
        console.print(line, style=style)
    else:
        console.print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_whitelist() -> set[str]:
    p = Path(WHITELIST_FILE)
    if not p.exists():
        p.write_text(json.dumps(["friend_one", "brand_abc"], indent=2))
        log(f"Created template {WHITELIST_FILE}. Edit it and re-run.")
    try:
        arr = json.loads(p.read_text())
        return {str(s).strip().lower().lstrip('@') for s in arr}
    except Exception as e:
        log(f"Failed to read whitelist: {e}")
        return set()


def load_state() -> dict:
    p = Path(STATE_FILE)
    if not p.exists():
        return {"daily_unfollows": {}, "total": 0}
    try:
        return json.loads(p.read_text())
    except Exception as e:
        log(f"Failed to read state: {e}")
        return {"daily_unfollows": {}, "total": 0}


def save_state(state: dict) -> None:
    Path(STATE_FILE).write_text(json.dumps(state, indent=2))


def today_key() -> str:
    return time.strftime("%Y-%m-%d")


def killswitch_triggered() -> bool:
    return Path(KILLSWITCH_FILE).exists()


async def _highlight_box(page: Page, box: dict, color: str = "#ff3b30", duration_ms: int = 600) -> None:
    if not DEBUG_HIGHLIGHT or not box:
        return
    x, y, w, h = box.get("x"), box.get("y"), box.get("width"), box.get("height")
    if x is None or y is None or w is None or h is None:
        return
    script = (
        "(x,y,w,h,color,dur)=>{"
        "const id='cascade-hl-'+Math.random().toString(36).slice(2);"
        "const d=document.createElement('div');"
        "d.id=id; d.style.cssText=\"position:fixed;pointer-events:none;z-index:2147483647;\"+"
        "`left:${x}px;top:${y}px;width:${w}px;height:${h}px;`+"
        "`border:2px solid ${color};border-radius:8px;box-shadow:0 0 8px ${color};`;"
        "document.body.appendChild(d);"
        "setTimeout(()=>{const el=document.getElementById(id);if(el)el.remove();}, dur);"
        "}"
    )
    try:
        await page.evaluate(script, x, y, w, h, color, duration_ms)
    except Exception:
        pass


async def _get_following_count(page: Page) -> Optional[int]:
    """Try to read the profile 'following' count from the page header.
    Returns int or None if not found. Works with different locales by stripping non-digits.
    """
    try:
        # Prefer anchors outside dialog that link to /following/
        el = await page.query_selector("xpath=(//a[contains(@href,'/following/')][not(ancestor::div[@role='dialog'])])[1]")
        if not el:
            return None
        txt = ((await el.inner_text()) or "").strip()
        parsed = _parse_count_label(txt)
        if parsed is not None:
            return parsed
        # try attribute title/aria-label fallbacks
        for attr in ("title", "aria-label"):
            try:
                a = await el.get_attribute(attr)
            except Exception:
                a = None
            if a:
                parsed = _parse_count_label(a)
                if parsed is not None:
                    return parsed
        return None
    except Exception:
        return None

async def _close_dialog_if_open(page: Page) -> None:
    try:
        dlg = await page.query_selector(FOLLOWING_DIALOG_SELECTOR)
        if dlg:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
    except Exception:
        pass

# -------------------- HUMAN-LIKE CURSOR --------------------

def _bezier(points: List[Tuple[float, float]], t: float) -> Tuple[float, float]:
    pts = points[:]
    while len(pts) > 1:
        nxt: list[Tuple[float, float]] = []
        for i in range(len(pts) - 1):
            x = pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t
            y = pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t
            nxt.append((x, y))
        pts = nxt
    return pts[0]


def generate_curved_path(x0: float, y0: float, x1: float, y1: float, steps: int = 28) -> List[Tuple[float, float]]:
    mx = (x0 + x1) / 2 + (random.random() - 0.5) * 80
    my = (y0 + y1) / 2 + (random.random() - 0.5) * 40
    p0 = (x0, y0)
    p1 = (mx, my)
    p2 = (x1, y1)
    path: List[Tuple[float, float]] = []
    for i in range(steps + 1):
        t = i / steps
        tt = 0.5 - 0.5 * math.cos(math.pi * t)
        x, y = _bezier([p0, p1, p2], tt)
        jitter_scale = (1 - abs(2 * t - 1))
        x += (random.random() - 0.5) * 3 * jitter_scale
        y += (random.random() - 0.5) * 3 * jitter_scale
        path.append((x, y))
    return path

# -------------------- CORE --------------------

async def connect_instagram_page() -> tuple[Optional[Page], Optional[object]]:
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(CHROME_REMOTE_DEBUG_URL)
    except Exception as e:
        log(f"CDP connect failed: {e}")
        await pw.stop()
        return None, None

    page: Optional[Page] = None
    try:
        for ctx in browser.contexts:
            for p in ctx.pages:
                if "instagram.com" in p.url:
                    page = p
                    break
            if page:
                break
        if not page and browser.contexts and browser.contexts[0].pages:
            page = browser.contexts[0].pages[0]
    except Exception:
        pass

    if not page:
        log("No Instagram page is open. Open Instagram and the Following dialog, then re-run.")
        # Do not close the external browser; just stop Playwright client
        await pw.stop()
        return None, None

    return page, pw


async def run_once() -> None:
    whitelist = load_whitelist()
    state = load_state()
    today = today_key()
    state.setdefault("daily_unfollows", {})
    state["daily_unfollows"].setdefault(today, 0)

    if state["daily_unfollows"][today] >= DAILY_CAP:
        log("Daily cap already reached. Exiting.")
        return

    page, pw = await connect_instagram_page()
    if page is None or pw is None:
        return

    client = await page.context.new_cdp_session(page)

    # start mouse pos at viewport center
    vp = page.viewport_size or {"width": 1200, "height": 800}
    mouse_x = vp["width"] / 2
    mouse_y = vp["height"] / 2
    # dedicated scroll anchor kept inside dialog to avoid page-level scrolling
    scroll_x = mouse_x
    scroll_y = mouse_y

    # ensure following dialog exists (fallback to page-level if not found)
    has_dialog = True
    dialog_bbox = None
    try:
        await page.wait_for_selector(FOLLOWING_DIALOG_SELECTOR, timeout=5000)
    except Exception:
        has_dialog = False
        log("Following dialog not detected. Proceeding with page-level button search.")

    # position mouse in the dialog to ensure wheel scroll affects it
    if has_dialog:
        try:
            dlg = await page.query_selector(FOLLOWING_DIALOG_SELECTOR)
            if dlg:
                db = await dlg.bounding_box()
                if db:
                    mouse_x = db["x"] + db["width"] / 2
                    mouse_y = db["y"] + min(100, db["height"] / 2)
                    scroll_x, scroll_y = mouse_x, mouse_y
                    dialog_bbox = db
        except Exception:
            pass

    # summary counters
    actions = 0
    stable_no_new = 0
    last_visible = 0
    start_time = time.time()
    recent_actions: list[float] = []  # timestamps of real unfollows
    seen_usernames: set[str] = set()
    unfollowed_usernames: list[str] = []
    before_count = await _get_following_count(page)

    # Setup progress bar (indeterminate if before_count is unknown)
    bar = BarColumn(bar_width=None, pulse=True)
    progress = Progress(
        SpinnerColumn(style="cyan"),
        TextColumn("[bold green]Unfollow[/bold green]"),
        bar,
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        TextColumn("•"),
        TextColumn("{task.fields[detail]}"),
        console=console,
    )
    progress.start()
    task_total = before_count if before_count is not None else None
    task = progress.add_task("run", total=task_total, fields={"detail": "initializing..."})
    processed_usernames: set[str] = set()  # usernames either unfollowed or skipped (whitelist)
    skipped_whitelist_usernames: list[str] = []
    no_action_rounds = 0

    while True:
        if killswitch_triggered():
            log("Killswitch detected. Stopping run.", style="bold red")
            break
        if actions >= MAX_ACTIONS_PER_RUN:
            log("Reached MAX_ACTIONS_PER_RUN. Stopping run.", style="yellow")
            break
        if state["daily_unfollows"][today] >= DAILY_CAP:
            log("Reached DAILY_CAP. Stopping run.", style="yellow")
            break
        # per-hour cap check (actual actions within last 3600s)
        now = time.time()
        recent_actions = [t for t in recent_actions if now - t < 3600]
        if len(recent_actions) >= PER_HOUR_CAP:
            log("Reached PER_HOUR_CAP (last 60 minutes). Stopping run.", style="yellow")
            break

        # enumerate all buttons in dialog (or whole page if no dialog); filter to 'Following'
        processed_before = len(processed_usernames)
        actions_before_loop = actions
        buttons_selector = FOLLOWING_BUTTONS_SELECTOR if has_dialog else "button"
        raw_buttons = await page.query_selector_all(buttons_selector)
        if not raw_buttons:
            # try scrolling a bit to load
            await client.send("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": scroll_x, "y": scroll_y, "deltaX": 0, "deltaY": random.randint(320, 480)})
            await asyncio.sleep(1.2)
            stable_no_new += 1
            # check if we've reached the 'Suggested for you' section inside dialog
            if has_dialog:
                try:
                    dlg = await page.query_selector(FOLLOWING_DIALOG_SELECTOR)
                    if dlg:
                        headers = await dlg.query_selector_all("h4, h3, span")
                        for h in headers:
                            try:
                                ht = ((await h.inner_text()) or "").strip().lower()
                            except Exception:
                                ht = ""
                            if any(tok in ht for tok in SUGGESTED_HEADER_TOKENS):
                                log("Reached 'Suggested for you' section. Ending.", style="cyan")
                                stable_no_new = 999  # force break
                                break
                except Exception:
                    pass
            if stable_no_new >= MAX_NO_PROGRESS_ROUNDS:
                log("No buttons found after multiple scrolls. Ending.", style="yellow")
                break
            progress.update(task, fields={"detail": f"scrolling ({stable_no_new})"})
            continue
        else:
            stable_no_new = 0

        # Build filtered list of 'Following/Mengikuti' buttons only
        following_btns = []
        for b in raw_buttons:
            try:
                t0 = ((await b.inner_text()) or "").strip().lower()
            except Exception:
                t0 = ""
            is_following = any(tok in t0 for tok in FOLLOWING_TOKENS)
            if not is_following:
                try:
                    aria0 = ((await b.get_attribute("aria-label")) or "").strip().lower()
                except Exception:
                    aria0 = ""
                if any(tok in aria0 for tok in FOLLOWING_TOKENS):
                    is_following = True
            if is_following:
                following_btns.append(b)

        if not following_btns:
            # If no following buttons are left in view, check for Suggested header to finish
            if has_dialog:
                try:
                    dlg = await page.query_selector(FOLLOWING_DIALOG_SELECTOR)
                    if dlg:
                        headers = await dlg.query_selector_all("h4, h3, span")
                        for h in headers:
                            try:
                                ht = ((await h.inner_text()) or "").strip().lower()
                            except Exception:
                                ht = ""
                            if any(tok in ht for tok in SUGGESTED_HEADER_TOKENS):
                                # confirm one follow/ikuti button exists after header
                                suggested_reached = False
                                try:
                                    btns_after = await h.query_selector_all("xpath=following::button")
                                    for bb in btns_after[:20]:  # bounded scan
                                        try:
                                            tbb = ((await bb.inner_text()) or "").strip().lower()
                                        except Exception:
                                            tbb = ""
                                        if any(tt in tbb for tt in FOLLOW_TOKENS):
                                            suggested_reached = True
                                            break
                                except Exception:
                                    pass
                                if suggested_reached:
                                    hbox = await h.bounding_box()
                                    await _highlight_box(page, hbox, "#0a84ff", 800)
                                    log("Detected 'Suggested for you' with Follow buttons after it — finishing.")
                                    stable_no_new = 999
                                    break
                except Exception:
                    pass
            if stable_no_new > 6:
                log("No 'Following' buttons visible after multiple scrolls. Ending.")
                break

            # try small scroll within dialog to load more rows
            try:
                await client.send("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": scroll_x, "y": scroll_y, "deltaX": 0, "deltaY": random.randint(280, 420)})
                await asyncio.sleep(1.0)
            except Exception:
                pass
            continue

        for btn in following_btns:
            if killswitch_triggered():
                log("Killswitch detected during item loop.")
                break
            if actions >= MAX_ACTIONS_PER_RUN:
                log("Reached MAX_ACTIONS_PER_RUN inside loop.")
                break

            try:
                # for logging only
                btn_text = ((await btn.inner_text()) or "").strip().lower()

                # find username by nearest ancestor container and first link within it
                container = await btn.query_selector("xpath=ancestor-or-self::div[.//a[@role='link' and starts-with(@href, '/')]][1]")
                link = None
                if container:
                    link = await container.query_selector(USERNAME_LINK_SUB_SELECTOR)
                if not link:
                    continue
                href = (await link.get_attribute("href")) or ""
                username = href.strip("/").split("/")[0].lower()
                if not username:
                    continue
                if username in whitelist:
                    if username not in processed_usernames:
                        log(f"Skip (whitelist): {username}", style="yellow")
                        skipped_whitelist_usernames.append(username)
                        processed_usernames.add(username)
                        seen_usernames.add(username)
                    continue
                if username in seen_usernames:
                    # already processed/considered this one this run
                    continue

                # ensure the button is visible within dialog
                try:
                    await btn.scroll_into_view_if_needed()
                    await btn.evaluate("el => el.scrollIntoView({block: 'center'})")
                except Exception:
                    pass

                # compute button center
                box = await btn.bounding_box()
                if not box:
                    continue
                bx = box["x"] + box["width"] / 2
                by = box["y"] + box["height"] / 2

                await _highlight_box(page, box, "#ffd60a", 700)

                log(f"[Target] {username} | btn='{btn_text}'", style="cyan")
                seen_usernames.add(username)

                # move cursor along curved path
                for (px, py) in generate_curved_path(mouse_x, mouse_y, bx, by, steps=random.randint(20, 32)):
                    await client.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": px, "y": py})
                    await asyncio.sleep(random.uniform(0.008, 0.03))
                    mouse_x, mouse_y = px, py

                await asyncio.sleep(random.uniform(0.08, 0.45))

                if DRY_RUN:
                    log(f"[DRY_RUN] Would click 'Following' for {username} (then confirm Unfollow)")
                else:
                    # click press + release
                    await client.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": bx, "y": by, "button": "left", "clickCount": 1})
                    await asyncio.sleep(random.uniform(0.06, 0.18))
                    await client.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": bx, "y": by, "button": "left", "clickCount": 1})
                    log(f"Clicked initial button for {username}", style="green")

                    # wait & detect confirm dialog
                    await asyncio.sleep(random.uniform(0.6, 1.2))
                    body_text = (await page.inner_text("body")).lower()
                    if any(p in body_text for p in BLOCK_PATTERNS):
                        log("Block-like text detected. Stopping.", style="bold red")
                        try:
                            progress.stop()
                        except Exception:
                            pass
                        await client.detach()
                        return

                    # find confirm button by label text
                    confirm_btn = None
                    for c in await page.query_selector_all("button"):
                        try:
                            t = ((await c.inner_text()) or "").strip().lower()
                        except Exception:
                            t = ""
                        if any(k in t for k in ("unfollow", "berhenti mengikuti", "berhenti")):
                            confirm_btn = c
                            break

                    if confirm_btn:
                        cbox = await confirm_btn.bounding_box()
                        if cbox:
                            cbx = cbox["x"] + cbox["width"] / 2
                            cby = cbox["y"] + cbox["height"] / 2
                            await _highlight_box(page, cbox, "#34c759", 700)
                            for (px, py) in generate_curved_path(mouse_x, mouse_y, cbx, cby, steps=random.randint(12, 20)):
                                await client.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": px, "y": py})
                                await asyncio.sleep(random.uniform(0.008, 0.02))
                                mouse_x, mouse_y = px, py
                            await asyncio.sleep(random.uniform(0.06, 0.2))
                            await client.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": cbx, "y": cby, "button": "left", "clickCount": 1})
                            await asyncio.sleep(random.uniform(0.06, 0.2))
                            await client.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": cbx, "y": cby, "button": "left", "clickCount": 1})
                            log("Clicked confirm unfollow", style="green")
                        else:
                            log("Confirm button bbox missing; skipped confirm.", style="yellow")
                    else:
                        log("No confirm button found (maybe immediate unfollow).", style="yellow")

                    # verify the row button changed to 'Follow/Ikuti'
                    verified = False
                    for _ in range(5):
                        try:
                            row_link = await page.query_selector(f"a[role='link'][href='/{username}/']")
                            if row_link:
                                new_container = await row_link.query_selector("xpath=ancestor-or-self::div[.//button][1]")
                                if new_container:
                                    row_buttons = await new_container.query_selector_all("button")
                                    for rb in row_buttons:
                                        try:
                                            t2 = ((await rb.inner_text()) or "").strip().lower()
                                        except Exception:
                                            t2 = ""
                                        if any(k in t2 for k in FOLLOW_TOKENS):
                                            verified = True
                                            break
                            if verified:
                                break
                        except Exception:
                            pass
                        await asyncio.sleep(0.5)

                    if verified:
                        log("Verified state changed to 'Follow/Ikuti'.", style="green")
                    else:
                        log("WARN: Could not verify state change to 'Follow/Ikuti' — counting with caution.", style="yellow")

                    # update state on actual actions
                    state["daily_unfollows"][today] = state["daily_unfollows"].get(today, 0) + 1
                    state["total"] = state.get("total", 0) + 1
                    recent_actions.append(time.time())
                    save_state(state)
                    log(f"Persisted: daily[{today}]={state['daily_unfollows'][today]} total={state['total']}", style="dim")
                    unfollowed_usernames.append(username)
                    processed_usernames.add(username)

                actions += 1

                # delay between actions
                delay = random.uniform(MIN_DELAY_SEC, MAX_DELAY_SEC)
                log(f"Sleeping ~{int(delay)}s before next action (actions={actions}).", style="dim")
                slept = 0.0
                while slept < delay:
                    if killswitch_triggered():
                        log("Killswitch detected during sleep.")
                        break
                    await asyncio.sleep(1.0)
                    slept += 1.0

                # Removed occasional natural scroll to avoid closing dialog or page scroll

                # post-action block check
                body_text = (await page.inner_text("body")).lower()
                if any(p in body_text for p in BLOCK_PATTERNS):
                    log("Block-like text detected after action. Stopping.", style="bold red")
                    try:
                        progress.stop()
                    except Exception:
                        pass
                    await client.detach()
                    return

                if state["daily_unfollows"][today] >= DAILY_CAP:
                    log("Reached DAILY_CAP after action. Stopping.")
                    break

            except Exception as e:
                log(f"Error processing item: {e}", style="bold red")
                await asyncio.sleep(1.0)
                continue

        # if we have processed as many unique usernames as reported in the initial header, finish
        if before_count is not None and len(processed_usernames) >= before_count:
            log(f"Processed {len(processed_usernames)} usernames (header reported {before_count}). Finishing to avoid loops.", style="cyan")
            break

        # detect no-progress cycles (e.g., whitelists repeating)
        if len(processed_usernames) == processed_before and actions == actions_before_loop:
            no_action_rounds += 1
        else:
            no_action_rounds = 0
        if no_action_rounds >= MAX_NO_PROGRESS_ROUNDS:
            log("No new usernames processed after multiple cycles — ending to avoid whitelist loops.", style="yellow")
            break

        # load more: prefer scrolling last 'Following' button into view to keep position stable
        try:
            last_following = following_btns[-1] if 'following_btns' in locals() and following_btns else None
            if last_following:
                await last_following.scroll_into_view_if_needed()
                await asyncio.sleep(random.uniform(0.8, 1.4))
            else:
                await client.send("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": scroll_x, "y": scroll_y, "deltaX": 0, "deltaY": random.randint(320, 520)})
                await asyncio.sleep(random.uniform(0.9, 1.4))
        except Exception:
            pass

        # Update progress details for the bar
        try:
            processed = len(processed_usernames)
            rem = (before_count - processed) if before_count is not None else "?"
            detail = f"proc:{processed} unf:{len(unfollowed_usernames)} skip:{len(skipped_whitelist_usernames)} rem:{rem} act:{actions}"
            # If total unknown initially and now known, set it
            if before_count is not None and progress.tasks[0].total is None:
                progress.update(task, total=before_count)
            completed_val = processed if before_count is not None else 0
            progress.update(task, completed=completed_val, fields={"detail": detail})
        except Exception:
            pass

        # end-of-list detection: Suggested header present inside dialog (confirm Follow buttons after it)
        if has_dialog and not following_btns:
            try:
                dlg = await page.query_selector(FOLLOWING_DIALOG_SELECTOR)
                if dlg:
                    headers = await dlg.query_selector_all("h4, h3, span")
                    for h in headers:
                        try:
                            ht = ((await h.inner_text()) or "").strip().lower()
                        except Exception:
                            ht = ""
                        if any(tok in ht for tok in SUGGESTED_HEADER_TOKENS):
                            suggested_reached = False
                            try:
                                btns_after = await h.query_selector_all("xpath=following::button")
                                for bb in btns_after[:20]:
                                    try:
                                        tbb = ((await bb.inner_text()) or "").strip().lower()
                                    except Exception:
                                        tbb = ""
                                    if any(tt in tbb for tt in FOLLOW_TOKENS):
                                        suggested_reached = True
                                        break
                            except Exception:
                                pass
                            if suggested_reached:
                                hbox = await h.bounding_box()
                                await _highlight_box(page, hbox, "#0a84ff", 800)
                                log("Detected 'Suggested for you' with Follow buttons after it — finishing.", style="cyan")
                                stable_no_new = 999
                                break
            except Exception:
                pass

        # check if more buttons are appearing as we scroll; if not, end gracefully
        new_buttons = await page.query_selector_all(buttons_selector)
        if len(new_buttons) == last_visible:
            stable_no_new += 1
        else:
            stable_no_new = 0
        last_visible = len(new_buttons)
        if stable_no_new >= MAX_NO_PROGRESS_ROUNDS:
            log("No new buttons loaded after multiple scrolls. Ending.", style="yellow")
            break

    # summary
    try:
        progress.stop()
    except Exception:
        pass
    # attempt to close dialog, reload page, and measure following count reliably
    await _close_dialog_if_open(page)
    try:
        await page.reload()
        await page.wait_for_load_state("networkidle")
    except Exception:
        pass
    after_count = None
    for _ in range(3):
        after_count = await _get_following_count(page)
        if after_count is not None:
            break
        await asyncio.sleep(0.6)
    proc_total = len(processed_usernames)
    log(f"Summary: Processed {proc_total} accounts (unf:{len(unfollowed_usernames)} skip:{len(skipped_whitelist_usernames)})", style="cyan")
    if unfollowed_usernames:
        names = ", ".join(unfollowed_usernames)
        log(f"Summary: Unfollowed {len(unfollowed_usernames)} accounts: {names}")
    else:
        log("Summary: No accounts unfollowed this run.")
    if skipped_whitelist_usernames:
        sample = ", ".join(skipped_whitelist_usernames[:30])
        more = "" if len(skipped_whitelist_usernames) <= 30 else f" (+{len(skipped_whitelist_usernames)-30} more)"
        log(f"Summary: Skipped (whitelist) {len(skipped_whitelist_usernames)} accounts: {sample}{more}")
    if before_count is not None or after_count is not None:
        log(f"Following count before -> after: {before_count} -> {after_count}")
        if before_count is not None:
            expected_after = before_count - len(unfollowed_usernames)
            if after_count is not None and after_count != expected_after:
                log(f"Note: Expected after-count ~{expected_after} based on actions; measured {after_count}. This can lag due to UI caching. Manual refresh usually reconciles.")

    try:
        await client.detach()
    except Exception:
        pass
    # Stop Playwright connection (do not close the external Chrome)
    try:
        await pw.stop()
    except Exception:
        pass


async def main() -> None:
    log("=== Playwright Unfollow Tool START ===", style="bold cyan")
    log(f"DRY_RUN={DRY_RUN}; using whitelist file '{WHITELIST_FILE}'", style="cyan")
    try:
        await run_once()
    except asyncio.CancelledError:
        log("Cancelled — exiting.")
    except KeyboardInterrupt:
        log("KeyboardInterrupt — exiting.")
    except Exception as e:
        log(f"Fatal error: {e}")
    log("=== FINISHED ===")


if __name__ == "__main__":
    asyncio.run(main())
