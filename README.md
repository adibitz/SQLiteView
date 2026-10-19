# SQliteView

SQliteView is a Python / Qt for Python (PySide6) port of [DB Browser for SQLite](https://github.com/sqlitebrowser/sqlitebrowser)
with a redesigned interface. The database engine behaviour follows the original C++ code; the UI and UX are new.

```
python -m pip install -r requirements.txt
python main.py                      # start page
python main.py my.db                # open a database
python main.py db.sqlite -R         # read-only
python main.py db.sqlite -t users   # open and browse a table
python main.py db.sqlite -s q.sql   # open (and run) a SQL file
python main.py work.sqbpro          # open a project (database, tabs, filters, layout)
```

`scripts/make_demo_db.py PATH` builds the sample database the tests use.

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
    ui/                      PySide6 widgets (new design)
      main_window.py, explorer.py, table_tab.py, sql_tab.py, data_grid.py, inspector.py,
      sql_editor.py, db_info_tab.py, log_panel.py, plot_dock.py, welcome.py, command_palette.py,
      theme.py (tokens + QSS), icons.py (inline SVG), widgets.py (design-system components)
      dialogs/               edit_table, edit_index, import_csv, export_data, export_sql,
                             add_record, preferences, display_format, cond_format, misc
  tests/                     engine + UI tests (run offscreen)
  scripts/make_demo_db.py
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

## Tests

```
python -m pip install pytest
python -m pytest tests -q
```

Tests run with `QT_QPA_PLATFORM=offscreen` and a temporary settings file, so they never touch your real settings.

## License

Like the original, this port is available under the GPL-3.0 / MPL-2.0 dual license of DB Browser for SQLite.
