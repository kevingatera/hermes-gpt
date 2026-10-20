"""Launch the browser bridge from Hermes Agent's Python interpreter."""

import sys
from pathlib import Path


def main() -> None:
    # The Agent interpreter may not have Hermes GPT installed. An absolute
    # launcher path and this fixed import root keep workspace packages from
    # replacing the bridge, including on Python 3.10 without safe-path mode.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from hermes_gpt.browser.bridge import main as run_bridge

    run_bridge()


if __name__ == "__main__":
    main()
