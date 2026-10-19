"""Launch SQliteView:  python main.py [database] [-R] [-s file.sql] [-t table]"""
import sys

from sqliteview.app import main

if __name__ == "__main__":
    sys.exit(main())
