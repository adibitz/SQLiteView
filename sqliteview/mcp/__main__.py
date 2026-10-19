"""``python -m sqliteview.mcp``: same as the ``sqliteview-mcp`` command."""
import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
