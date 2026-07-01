"""Chrome process management for cdp_interceptor.

Public API
----------
- ``find_chrome(extra_paths=None)`` — locate the Chrome executable
- ``start_chrome(...)`` — spawn Chrome with the debug port + isolated profile
- ``clear_singleton_locks(profile_dir)`` — remove stale lock files
- ``ChromeNotFoundError`` — raised when no Chrome executable is found
"""

import logging
import os
import subprocess

logger = logging.getLogger("cdp_interceptor")


class ChromeNotFoundError(RuntimeError):
    """Raised when no Chrome executable can be located."""


_DEFAULT_CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]

_SINGLETON_LOCK_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def find_chrome(extra_paths: list[str] | None = None) -> str | None:
    """Return the first Chrome executable path that exists, or None.

    Searches the built-in default paths (Program Files, Program Files (x86),
    %LOCALAPPDATA%), plus any extras passed by the caller. Also honors the
    ``CHROME_PATHS_VAR`` environment variable if set.
    """
    # Build the search list in priority order. First hit wins.
    candidates = list(_DEFAULT_CHROME_PATHS)

    # Environment override lets users point at a non-standard install
    # (portable Chrome, Chromium build, Chrome Canary, etc.) without editing code.
    env_override = os.environ.get("CHROME_PATHS_VAR")
    if env_override:
        candidates.append(os.path.expandvars(env_override))

    # Caller-supplied extras go last so they act as fallbacks, not overrides.
    if extra_paths:
        candidates.extend(extra_paths)

    # Filter out any None/empty entries and return the first existing path.
    return next((p for p in candidates if p and os.path.exists(p)), None)


def start_chrome(
    chrome_path: str,
    *,
    headless: bool,
    debug_port: int,
    profile_dir: str,
    target_url: str,
) -> subprocess.Popen:
    """Launch Chrome with remote debugging enabled and load *target_url* in a
    new window. Uses an isolated ``--user-data-dir`` so the launched Chrome
    is a distinct process from any regular Chrome the user has open.
    """
    # Base flags — always applied regardless of headless/visible mode.
    args = [
        chrome_path,
        # Exposes Chrome's DevTools Protocol on localhost:<port>. The
        # `cdp_session` module connects to this port via WebSocket.
        f"--remote-debugging-port={debug_port}",
        # Newer Chrome versions block CDP connections unless the origin is
        # explicitly whitelisted. This whitelists our own localhost origin.
        f"--remote-allow-origins=http://localhost:{debug_port}",
        # The isolation boundary: a distinct user-data-dir means this Chrome
        # gets its own cookies, extensions, and — critically — its own
        # singleton lock, so it launches as a separate process from any
        # regular Chrome the user has open.
        f"--user-data-dir={profile_dir}",
        # Suppress prompts that would block programmatic launch:
        "--no-first-run",                    # "welcome" flow on fresh profile
        "--no-default-browser-check",        # "make Chrome default?" prompt
        "--no-restore-last-session",         # don't re-open old tabs
        "--disable-session-crashed-bubble",  # "Chrome didn't shut down cleanly" bar
    ]

    if headless:
        # `--headless=new` is Chrome's modern headless mode (as of Chrome 109+);
        # closer to real Chrome behavior than the legacy headless.
        args += [
            "--headless=new",
            "--disable-gpu",                 # required for stable headless on Windows
            "--window-size=1920,1080",       # ensures desktop-layout responsive breakpoints
            # Sites detect automation by checking `navigator.webdriver` and other
            # signals. This flag hides the most common ones; we also override
            # navigator.webdriver from cdp_session at page-load time for belt-and-suspenders.
            "--disable-blink-features=AutomationControlled",
            # A real-looking UA string. Headless Chrome's default UA contains
            # "HeadlessChrome" which triggers bot detection on many sites.
            (
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/134.0.0.0 Safari/537.36"
            ),
        ]
        logger.debug("launcher.start_chrome: launching headless")
    else:
        logger.debug("launcher.start_chrome: launching visible")

    # The URL argument is what tells Chrome which page to load. `--new-window`
    # asks for a fresh window rather than a tab in an existing session.
    args += ["--new-window", target_url]

    # stderr is redirected because Chrome logs a lot of harmless warnings
    # (GPU init, extension messages, etc.) that would otherwise flood the
    # caller's console.
    return subprocess.Popen(args, stderr=subprocess.DEVNULL)


def clear_singleton_locks(profile_dir: str) -> None:
    """Remove SingletonLock / SingletonCookie / SingletonSocket if present.

    Stale singleton locks left over from a prior Chrome crash can cause the
    next launch to hand off its URL to a non-existent process. Removing them
    is safe when no Chrome is currently using this profile.
    """
    # Chrome writes these three files into user-data-dir on startup and
    # normally deletes them on clean shutdown. A crash leaves them behind,
    # and the next launch may then think another Chrome is "already running"
    # for this profile and hand off the URL instead of starting fresh.
    for lf in _SINGLETON_LOCK_FILES:
        try:
            os.remove(os.path.join(profile_dir, lf))
        except FileNotFoundError:
            # No lock file present = no cleanup needed. Not an error.
            pass
