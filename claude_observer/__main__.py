"""
Claude Usage Observer — package entry point.
Run with:  python -m claude_observer
"""

# config must be imported first so .env is loaded before any other module
# reads environment variables.
import claude_observer.config  # noqa: F401

from claude_observer.logging_setup import log
from claude_observer.core.widget import ClaudeUsageWidget


def main():
    log.info("Claude Usage Widget starting up")
    ClaudeUsageWidget().run()


if __name__ == "__main__":
    main()
