# SQliteView

SQliteView is a Python / Qt for Python (PySide6) port of [DB Browser for SQLite](https://github.com/sqlitebrowser/sqlitebrowser)
with a redesigned interface. The database engine behaviour follows the original C++ code; the UI and UX are new.

## Download

Ready-to-run builds for **Windows**, **macOS** and **Linux** are attached to each
[GitHub release](https://github.com/adibitz/SQLiteView/releases). No Python needed.

| Platform | File | How to run |
|---|---|---|
| Windows 10/11 (x64) | `SQliteView-<version>-windows-x64-setup.exe` | Run the installer (optionally associates .db/.sqlite files). |
| | `SQliteView-<version>-windows-x64.zip` | Portable: unzip anywhere and run `SQliteView.exe`. |
| macOS 11+ (Apple silicon / Intel) | `SQliteView-<version>-macos-arm64.dmg` / `-macos-x64.dmg` | Open the DMG and drag SQliteView to Applications. |
| Linux x86_64 (glibc 2.35+) | `SQliteView-<version>-linux-x86_64.AppImage` | `chmod +x SQliteView-*.AppImage` then run it. |
| | `SQliteView-<version>-linux-x86_64.tar.gz` | Extract and run `SQliteView/SQliteView`. |

The builds aren't code-signed yet. On Windows, SmartScreen may say "Windows protected your PC": choose
**More info › Run anyway**. On macOS, the first time, right-click the app and choose **Open** (or run
`xattr -dr com.apple.quarantine /Applications/SQliteView.app`). Some Linux distributions need `libxcb-cursor0`
(`sudo apt install libxcb-cursor0`) and, for the AppImage, `libfuse2`.

### Command line

```
sqliteview                      # start page          (python main.py … when running from source)
sqliteview my.db                # open a database
sqliteview db.sqlite -R         # read-only
sqliteview db.sqlite -t users   # open and browse a table
sqliteview db.sqlite -s q.sql   # open (and run) a SQL file;  add -q to quit afterwards
sqliteview work.sqbpro          # open a project (database, tabs, filters, layout)
```

## Use with Claude and other AI assistants (MCP)

SQliteView includes an [MCP](https://modelcontextprotocol.io) server, `sqliteview-mcp`. With it, Claude Desktop,
Claude Code, Cursor, VS Code and other MCP clients can read your schema, run and explain queries, filter rows,
edit data, and import or export files. You never start it yourself: the AI client launches it in the background
and talks to it over stdin/stdout.

**Set it up once:** open **Help › Connect AI Assistant…** in SQliteView, pick your client, and copy the snippet.
It already contains the right path for your installation and, if you like, the database you have open. Or write
it by hand:

| Client | Configuration |
|---|---|
| Claude Code | `claude mcp add sqliteview -- sqliteview-mcp --db D:/data/shop.db` |
| Claude Desktop / Cursor | `{"mcpServers": {"sqliteview": {"command": "sqliteview-mcp", "args": ["--db", "D:/data/shop.db"]}}}` in `claude_desktop_config.json` / `~/.cursor/mcp.json` |
| VS Code | `{"servers": {"sqliteview": {"type": "stdio", "command": "sqliteview-mcp", "args": ["--db", "D:/data/shop.db"]}}}` in `.vscode/mcp.json` |

In the packaged builds the command is the `sqliteview-mcp` program next to SQliteView: `C:\Program Files\SQliteView\sqliteview-mcp.exe`,
`/Applications/SQliteView.app/Contents/MacOS/sqliteview-mcp`, or `SQliteView-….AppImage mcp` on Linux.

```
sqliteview-mcp --db shop.db                  # read and edit shop.db
sqliteview-mcp --db shop.db --read-only      # the assistant can only read (no edit/import tools; exports only create new files)
sqliteview-mcp --root D:/data                # no database yet; the assistant opens or creates one in D:/data
sqliteview-mcp --db shop.db --root D:/csv    # also allow importing/exporting files in D:/csv
```

**Tools**

| Area | Tools |
|---|---|
| Schema | `get_schema` (all CREATE statements), `list_tables`, `describe_table` (columns, keys, foreign keys, indices, triggers, sample rows), `database_info`, `list_database_files`, `open_database` |
| Query | `run_query` (one read-only statement, with parameters and paging), `explain_query` (checks SQL without running it, shows the query plan) |
| Filter | `filter_rows`: conditions (`equals`, `gt`, `between`, `starts`, `regex`, `null` …), search in all columns, sorting, column choice and paging, without writing SQL; returns the matching count and the equivalent SQL |
| Edit | `insert_rows`, `update_rows`, `delete_rows` (by filters or `_rowid_`), `create_table`, `execute_sql` (scripts), `pending_changes`, `commit_changes`, `revert_changes` |
| Import / export | `import_csv`, `import_json`, `import_sql`; `export_data` (table, filtered table or query to CSV, TSV, JSON, SQL or Markdown), `export_sql_dump` |

There are also resources (`sqlite://schema`, `sqlite://tables/{name}`) and two prompts (`explore_database`,
`answer_question`).

**Safety**

* Changes are staged, like in the app: nothing reaches the file until `commit_changes` (Write Changes), and
  `revert_changes` undoes everything since the last commit. Uncommitted changes are discarded when the client
  disconnects. `dry_run` previews updates, deletes and imports.
* A few failures make SQLite drop the whole transaction, not just one change: a write stopped by the time
  limit, `ON CONFLICT ROLLBACK`, and `RAISE(ROLLBACK)` in a trigger. The tool error then says that every
  uncommitted change was lost.
* Updating or deleting without a filter needs an explicit `all_rows=true`.
* SQL from the assistant runs under an SQLite authorizer. Read tools can only read. Write tools can't ATTACH
  files, change PRAGMA settings, end the transaction or load extensions. Statements stop after 30 seconds
  (`--query-timeout`). Values are always bound as parameters, and table and column names are quoted.
* The server never loads the extensions listed in Preferences › Extensions, and doesn't run the "SQL to
  execute after opening" either. Extensions such as fileio would give the assistant file access.
* Files can only be read and written inside the `--root` folders (by default, the folder of `--db`). Existing
  files are only overwritten with `overwrite=true` (never in read-only mode), and exports never replace a
  database file.
* Regular expressions (the `regex` filter, `REGEXP`) run in Python and can't be stopped by the time limit, so
  a pathological pattern can stall the server until it finishes.
* Tools carry MCP hints (`readOnlyHint`, `destructiveHint`) so clients can ask you before destructive steps.
* If SQliteView has the same database open with unwritten changes, the assistant gets a "database is locked"
  message until you write or revert them.

## What's new in the UI

| Upstream | This edition |
|---|---|
| Four fixed tabs (Structure / Browse / Pragmas / Execute SQL) | Document tabs: one per table, per SQL editor, plus a Database Info page. Open as many as you like. |
| "Database Structure" tab + "DB Schema" dock | **Explorer** sidebar: searchable tree of tables, views, indices, triggers and columns, with context menus and drag-to-editor. |
| "Edit Database Cell" dock | **Inspector**: shows type, size and row of the selected cell; Text / JSON (validated, pretty-printed) / Hex / Image modes, chosen automatically. |
| Write / Revert buried in the toolbar | Transaction state is always visible: the Write Changes button turns amber and the status bar shows "Uncommitted changes" when anything is pending. |
| Menus only | **Command palette** (Ctrl+K): run any command, open any table/column, reopen recent files. |
| Separate filter row widget | Filter row built into the grid header; active filters are highlighted; a global search box searches all columns; footer shows rows, active filters and sort order. |
| Find/Replace dialog | Inline find bar in editors (Ctrl+F), comment toggle (Ctrl+/), schema-aware autocompletion, error underline + gutter marker for the failing statement. |
| Modal dialogs for everything | Inline validation and live SQL previews in Create/Modify Table, Create Index and Insert Record dialogs; live preview while importing CSV. |
| Qt default look | DM Sans interface font (bundled, SIL Open Font License — see `sqliteview/assets/fonts/OFL.txt`); light and dark themes with design tokens (follows the OS by default, Ctrl+Shift+L to toggle), comfortable/compact density, crisp vector icons. |
| Plot dock (QCustomPlot) | **Plot** panel (Qt Charts) that follows the current table or SQL result: X column or row number, up to eight numeric Y columns as lines, scatter or bars, themed series colours, hover values, zoom, save as image. |
| Display format / conditional format dialogs | Right-click a column header: **Display Format…** (presets for dates, hex, case, JSON … or your own SQL expression, validated inline) and **Conditional Formats…** (ordered rules in the filter syntax, with live colour preview; text stays legible when only a background is set). |
| Endless scrolling only | **Pagination** (50 – 5,000 rows per page, or “All” for one scrolling list), page navigation, “Showing 101–200 of 20,000”, go to any row. |
| Filter row | Filter row plus a **filter builder** (column › condition › value, e.g. between, starts with, is NULL), and removable chips for every active filter, search and sort. |
| Click header to sort | Header right-click menu: sort ascending/descending, add a secondary sort, clear sorting, filter, hide/show columns, fit width. |
| Cell selection | **Multi-select** cells or whole rows (toolbar toggle, Ctrl/Shift-click, Ctrl+A); a selection bar shows the count with copy (CSV, TSV, JSON, SQL, Markdown), export, set NULL and delete. Double-click a row number for **row details**. |
| Export whole tables | Export the whole table, the filtered & sorted rows, the current page, or just the selected rows (CSV, JSON, SQL INSERT, Markdown, TSV). |
| — | Full-screen grid: the expand button on a table or on query results (or F11) hides everything else; Esc restores the layout. |
| — | Start page with recent files, drag & drop (.db opens, .csv imports, .sql opens in an editor), toasts for non-blocking feedback. |

## Architecture

```
app/
  main.py                    entry point
  mcp_main.py                entry point of the bundled sqliteview-mcp program
  sqliteview/
    app.py                   QApplication bootstrap, CLI (port of main.cpp / Application.cpp)
    core/                    engine — no widgets
      db.py                  DBBrowserDB            <- src/sqlitedb.cpp
      sqlb.py                schema object model    <- src/sql/sqlitetypes.cpp, ObjectIdentifier.cpp
      parser.py              CREATE TABLE/INDEX parser <- src/sql/parser (bison grammar)
      query.py               SELECT builder          <- src/sql/Query.cpp
      filters.py             filter syntax -> SQL     <- CondFormat::filterToSqlCondition, GlobalFilter
      table_model.py         lazy table model         <- src/sqlitetablemodel.cpp
      rowloader.py           background chunk loader  <- src/RowLoader.cpp, RowCache.h
      runsql.py              SQL execution worker     <- src/RunSql.cpp
      sqlsplit.py            statement splitting (sqlite3.complete_statement)
      data.py                value helpers            <- src/Data.cpp
      settings.py            settings + defaults      <- src/Settings.cpp
      project.py             .sqbpro project files    <- MainWindow::saveProject / loadProject
      condformat.py          conditional formats      <- src/CondFormat.cpp, SqliteTableModel::getMatchingCondFormat
      display_format.py      display format presets   <- src/ColumnDisplayFormatDialog.cpp
      plotdata.py            rows -> plot series      <- src/PlotDock.cpp (guessDataType, data conversion)
      csvimport.py           CSV parsing, type detection, import <- src/csvparser.cpp, ImportCsvDialog
      rowexport.py           rows -> CSV/TSV/JSON/SQL/Markdown, streaming export <- ExportDataDialog
    mcp/                     MCP server for AI assistants (no widgets)
      server.py              tools, resources, prompts, `sqliteview-mcp` command line
      session.py             open database, folder sandbox, SQL authorizers, staging/commit
      schema.py, reading.py, editing.py, transfer.py   the tools, grouped by area
      models.py              structured tool inputs (filters, sort keys, column specs)
      clientconfig.py        launch command + client config snippets (no MCP SDK import; used by the UI)
    ui/                      PySide6 widgets (new design)
      main_window.py, explorer.py, table_tab.py, sql_tab.py, data_grid.py, inspector.py,
      sql_editor.py, db_info_tab.py, log_panel.py, plot_dock.py, welcome.py, command_palette.py,
      theme.py (tokens + QSS), icons.py (inline SVG), widgets.py (design-system components)
      dialogs/               edit_table, edit_index, import_csv, export_data, export_sql,
                             add_record, preferences, display_format, cond_format, connect_ai, misc
    assets/                  bundled DM Sans font (+ OFL licence) and app icons
  tests/                     engine + UI tests (run offscreen)
  scripts/
    build.py                 build + package for the current OS (PyInstaller)
    make_icons.py            regenerate assets/icons from the vector glyph (needs Pillow)
    make_demo_db.py          sample database used by the tests
  packaging/
    sqliteview.spec          PyInstaller spec (one-folder build with SQliteView + sqliteview-mcp, macOS .app bundle)
    windows/sqliteview.iss   Inno Setup installer
    linux/sqliteview.desktop desktop entry (tarball / AppImage)
  .github/workflows/release.yml   CI: tests + Windows/macOS/Linux builds, releases on tags
  pyproject.toml             package metadata, dependencies, `sqliteview` and `sqliteview-mcp` commands
```

### Engine behaviour kept from the original

* Every change is wrapped in a `SAVEPOINT`; nothing reaches the file until **Write Changes** (`RELEASE`), and
  **Revert Changes** rolls everything back. Undo points work the same way.
* Records are addressed by `_rowid_`, or by the primary key for `WITHOUT ROWID` tables (composite keys through the
  same `sqlb_make_single_value()` SQL function). Views are read-only until a pseudo primary key is chosen with the
  key button in the view's toolbar (edits then need `INSTEAD OF` triggers on the view, as upstream).
* Project files (File › Open Project / Save Project As) use upstream's `.sqbpro` XML: the database path (relative
  when next to the project), read-only flag, connection pragmas, attached databases, table tabs with sort, filters,
  global search, column widths and view primary key, and SQL editors. Tab order and window layout go in extra
  elements upstream ignores, so projects open in either application.
* ALTER TABLE is emulated exactly like upstream: rename/add/rename-column via SQLite's own ALTER TABLE, otherwise
  rebuild the table with `defer_foreign_keys`, copy data, restore indices and triggers. When `foreign_keys` is on,
  pending changes are committed first and a `foreign_key_check` decides whether to keep the change.
* Browsing loads rows lazily in chunks (`db/prefetchsize`, default 50 000) on a worker thread; the row count is a
  separate `COUNT(*)` query that only runs when needed.
* The filter syntax (`=`, `<>`, `>`, `<=`, `a~b`, `/regex/`, `=NULL`, wildcards …), multi-column sorting, the
  `REGEXP` function, `UTF16`/`UTF16CI` collations, CSV type detection, the SQL dump format and the "PRAGMA/VACUUM
  needs a commit" rule all follow the C++ implementation.

### Differences forced by Python (and how they're handled)

* No bison parser: `parser.py` is a recursive-descent parser for the same object model. As a safety net its result
  is compared with `PRAGMA table_xinfo`; if they disagree the table falls back to the PRAGMA columns, like upstream
  does for statements it can't parse.
* `sqlite3_prepare` tails aren't exposed to Python, so scripts are split with `sqlite3.complete_statement()` — the
  same C tokenizer SQLite uses, so triggers (`BEGIN … END`) and strings split correctly.
* `sqlite3_collation_needed` isn't exposed: a missing collation is detected from the error, the user is asked, and
  a fallback is registered before retrying.
* Values keep their SQLite type (`None/int/float/str/bytes`) instead of being `QByteArray`; invalid UTF-8 in TEXT
  columns is returned as bytes and shown as BLOB rather than failing.
* A dropped table that a view references can't be renamed back on SQLite ≥ 3.26; the rebuild path temporarily sets
  `legacy_alter_table` inside the transaction (upstream fails here).

### Performance — where the native code is

All heavy lifting runs in native code: SQLite itself (C, via the standard `sqlite3` module, which releases the GIL
while stepping so background loading doesn't block the UI), Qt (C++, via PySide6) for rendering and models, and the
C-implemented `csv` and `json` modules for import/export. Python only orchestrates. Bulk paths use `executemany` and
`fetchmany`, and the grid only touches the rows on screen. A custom C++ extension (pybind11) can be added later for
hot spots if profiling shows a need; no compiler was available when this port was made.

## Not ported yet

* SQLCipher encryption (needs a SQLCipher-enabled `sqlite3` build), DBHub.io remote features (deprecated upstream)
* Translations
* Plot: logarithmic axes, stacked bars, per-series line/point styles and printing; formats: upstream's per-cell
  ("row ID") formats, and bold/italic/font/alignment in conditional formats (this edition sets colours only)
* Project files don't store the Explorer's expanded items or per-table encodings, hidden columns, display/conditional
  formats and plot settings (upstream files that contain them still open; those parts are ignored)

## Installation for developers

You need **Python 3.10+** (3.12 recommended) and **git**. PySide6 ships its own Qt, so nothing else is needed on
Windows and macOS. On Linux, install Qt's system libraries first:

```bash
# Debian / Ubuntu
sudo apt install libegl1 libgl1 libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 libxcb-keysyms1 libfontconfig1
# Fedora
sudo dnf install mesa-libEGL libxkbcommon-x11 xcb-util-cursor xcb-util-wm xcb-util-keysyms fontconfig
```

1. Clone the repository

   ```bash
   git clone https://github.com/adibitz/SQLiteView.git
   cd SQLiteView
   ```

2. Create and activate a virtual environment

   ```bash
   python -m venv .venv
   source .venv/bin/activate        # macOS / Linux
   .venv\Scripts\activate           # Windows (PowerShell: .venv\Scripts\Activate.ps1)
   ```

3. Install the app in editable mode with the developer tools (pytest, PyInstaller, Pillow) and the MCP server

   ```bash
   python -m pip install --upgrade pip
   python -m pip install -e ".[dev]"
   ```

   Only want to run it? `python -m pip install -r requirements.txt` installs just PySide6;
   `python -m pip install -e ".[mcp]"` adds the MCP server (`sqliteview-mcp`, or `python -m sqliteview.mcp`).

4. Run it

   ```bash
   sqliteview            # the command installed in step 3
   python main.py        # same thing, straight from the source tree
   ```

   To try it with sample data: `python scripts/make_demo_db.py demo.db` then `sqliteview demo.db`
   (`*.db` files are git-ignored).

5. Run the tests

   ```bash
   python -m pytest -q
   ```

   Tests run with `QT_QPA_PLATFORM=offscreen` and a temporary settings file, so they open no windows and never
   touch your real settings.

### Project layout and conventions

* `sqliteview/core` is the engine and must not import widgets; `sqliteview/ui` is everything Qt Widgets.
  `sqliteview/mcp` builds on `core` only. The UI may import `mcp/clientconfig.py`, which doesn't need the MCP SDK.
* The MCP server owns stdout (it carries the protocol): log to stderr through `logging`, never `print`.
* Colours, spacing and fonts come from the design tokens in `ui/theme.py`; icons from `ui/icons.py`; reusable
  controls from `ui/widgets.py`. Use those rather than hard-coding styles.
* Settings and their defaults live in `core/settings.py` (stored with `QSettings` under the name `sqliteview`).
* The version is `__version__` in `sqliteview/__init__.py`; `pyproject.toml`, the PyInstaller spec, the build
  script and the installer all read it from there.

## Building distributable packages

Packages are built with [PyInstaller](https://pyinstaller.org). It can't cross-compile, so each platform's package
is built on that platform — locally, or by CI for all three at once.

### Locally (current OS)

```bash
python -m pip install -e ".[dev]"
python scripts/build.py
```

Everything lands in `dist/`:

| OS | Output | Extra tool (optional) |
|---|---|---|
| Windows | `SQliteView-<ver>-windows-x64.zip` (portable) and `…-setup.exe` | [Inno Setup 6](https://jrsoftware.org/isinfo.php) for the installer (`iscc` on PATH or in Program Files) |
| macOS | `SQliteView-<ver>-macos-<arch>.dmg` and `.zip` with `SQliteView.app` | none (`hdiutil` and `ditto` ship with macOS) |
| Linux | `SQliteView-<ver>-linux-<arch>.tar.gz` and `.AppImage` | [appimagetool](https://github.com/AppImage/appimagetool) on PATH or in `$APPIMAGETOOL` |

The unpacked app is in `dist/SQliteView/` (`dist/SQliteView.app` on macOS) if you want to try it before
packaging. `python scripts/build.py --skip-bundle` re-packages an existing build without running PyInstaller again.

The bundle holds two programs that share one `_internal` folder: `SQliteView` (windowed) and `sqliteview-mcp`
(console). The MCP server needs real stdin/stdout, which windowed programs don't have on Windows. After
bundling, `build.py` starts `sqliteview-mcp` like an AI client would (handshake, tool list, two tool calls) and
stops the build if it doesn't answer. This catches modules PyInstaller missed and anything that writes to stdout.
Build Linux packages on the oldest distribution you want to support (the app needs that glibc or newer).

### With GitHub Actions (all platforms)

`.github/workflows/release.yml` runs on every push and pull request to `main`: it runs the tests, then builds
Windows x64, macOS arm64, macOS x64 and Linux x86_64 packages. Download them from the workflow run's
**Artifacts** section.

### Making a release

1. Bump `__version__` in `sqliteview/__init__.py` and commit.
2. Tag and push:

   ```bash
   git tag v0.2.0
   git push origin main --tags
   ```

3. The workflow builds every package and publishes a GitHub Release for the tag with all files attached and
   generated release notes.

### Signing (not set up yet)

The builds are unsigned. For signed releases add a Windows code-signing certificate (`signtool` after the
PyInstaller step and on the installer) and, on macOS, a Developer ID certificate with `codesign --deep` plus
`xcrun notarytool` before creating the DMG. Store the certificates as GitHub Actions secrets.

### App icons

The icons in `sqliteview/assets/icons/` (PNG, `.ico`, `.icns`) are generated from the vector glyph in
`ui/icons.py` by `python scripts/make_icons.py` (needs Pillow, included in `.[dev]`). Commit the regenerated files.

## License

Like the original, this port is available under the GPL-3.0 / MPL-2.0 dual license of DB Browser for SQLite.
The bundled DM Sans font is licensed under the SIL Open Font License 1.1 (`sqliteview/assets/fonts/OFL.txt`).
Distributed packages include Qt for Python (PySide6, LGPLv3).
