"""Module entry point so ``python -m hermes_gpt`` matches the console script."""

from __future__ import annotations

from hermes_gpt.server.app import main

if __name__ == "__main__":
    main()
