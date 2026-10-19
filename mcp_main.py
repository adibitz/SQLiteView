"""Launch the SQliteView MCP server:  python mcp_main.py --db file.db  (entry point of the packaged sqliteview-mcp)"""
import sys

from sqliteview.mcp.server import main

if __name__ == "__main__":
    sys.exit(main())
