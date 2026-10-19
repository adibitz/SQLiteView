"""Engine tests (parser, query builder, filters, DBBrowserDB, RunSql, table model)."""
from __future__ import annotations

import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PySide6.QtCore import QCoreApplication  # noqa: E402

from sqliteview.core import parser, sqlb  # noqa: E402
from sqliteview.core.data import is_binary, is_text_only, parse_clipboard, remove_comments_from_query  # noqa: E402
from sqliteview.core.db import DBBrowserDB  # noqa: E402
from sqliteview.core.filters import filter_to_sql_condition, tokenize_global_filter  # noqa: E402
from sqliteview.core.query import OrderBy, Query, SortDirection  # noqa: E402
from sqliteview.core.sqlsplit import split_statements, statement_at  # noqa: E402


@pytest.fixture(scope="session")
def app():
    return QCoreApplication.instance() or QCoreApplication([])


def spin(app, cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def db(tmp_path, app):
    path = tmp_path / "test.db"
    con = sqlite3.connect(path)
    con.executescript('''
        CREATE TABLE parent(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL DEFAULT 'x' COLLATE NOCASE);
        CREATE TABLE child(
            id integer NOT NULL,
            parent_id INTEGER REFERENCES parent(id) ON DELETE CASCADE,
            "weird ""name""" varchar(20) UNIQUE CHECK(length("weird ""name""") < 10),
            total REAL GENERATED ALWAYS AS (id * 2) STORED,
            PRIMARY KEY(id)
        );
        CREATE INDEX idx_child_parent ON child(parent_id DESC);
        CREATE VIEW v_parent AS SELECT * FROM parent;
        CREATE TRIGGER trg AFTER INSERT ON parent BEGIN SELECT 1; END;
        CREATE TABLE wr(a TEXT, b INT, c BLOB, PRIMARY KEY(a, b)) WITHOUT ROWID;
        INSERT INTO parent(name) VALUES ('alice'), ('bob'), ('carl');
    ''')
    con.commit()
    con.close()
    d = DBBrowserDB()
    assert d.open(str(path)), d.last_error
    yield d
    d.close(force=True)


# ---------------------------------------------------------------------------- parser

def test_parse_table_constraints():
    t = parser.parse_table(
        'CREATE TABLE "t" (id INTEGER PRIMARY KEY AUTOINCREMENT, a TEXT NOT NULL DEFAULT \'x\', '
        "b REAL CHECK(b > 0) UNIQUE, c INT REFERENCES other(x) ON DELETE CASCADE, "
        "d INT GENERATED ALWAYS AS (id+1) VIRTUAL, CONSTRAINT u UNIQUE(a, b), FOREIGN KEY(b) REFERENCES z(y)) STRICT")
    assert t.fully_parsed
    assert t.name == "t"
    assert t.field_names() == ["id", "a", "b", "c", "d"]
    assert t.primary_key_names() == ["id"]
    assert t.primary_key().auto_increment
    assert t.fields[1].notnull and t.fields[1].default_value == "'x'"
    assert t.fields[2].check == "b > 0" and t.fields[2].unique
    assert t.foreign_key(["c"]).table == "other"
    assert t.foreign_key(["c"]).constraint == "ON DELETE CASCADE"
    assert t.fields[4].generated.expression == "id+1"
    assert t.strict
    # Regenerated SQL must be valid SQLite and parse to the same structure
    sql = t.sql()
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE other(x PRIMARY KEY)")
    con.execute("CREATE TABLE z(y PRIMARY KEY)")
    con.execute(sql)
    t2 = parser.parse_table(sql)
    assert t2.field_names() == t.field_names()
    assert t2.primary_key_names() == ["id"]


def test_parse_types_and_keywords_as_names():
    t = parser.parse_table('CREATE TABLE x ("key" unsigned big int, [order] varchar(10, 2), `check` text, value)')
    assert t.fully_parsed
    assert [(f.name, f.type) for f in t.fields] == [
        ("key", "unsigned big int"), ("order", "varchar(10, 2)"), ("check", "text"), ("value", "")]


def test_parse_without_rowid_and_virtual():
    t = parser.parse_table("CREATE TABLE w(a, b, PRIMARY KEY(a,b)) WITHOUT ROWID")
    assert t.without_rowid and t.rowid_columns() == ["a", "b"]
    v = parser.parse_table("CREATE VIRTUAL TABLE f USING fts5(a, b)")
    assert v.is_virtual() and v.virtual_using == "fts5(a, b)"


def test_parse_failure_falls_back():
    t = parser.parse_table("CREATE TABLE t AS SELECT 1")
    assert not t.fully_parsed


def test_parse_index():
    i = parser.parse_index('CREATE UNIQUE INDEX "i" ON t (a COLLATE NOCASE, b DESC, lower(c)) WHERE a > 1')
    assert i.fully_parsed and i.unique and i.table == "t"
    assert [c.order for c in i.fields] == ["", "DESC", ""]
    assert i.fields[2].is_expression
    assert i.where_expr == "a > 1"


# ---------------------------------------------------------------------------- helpers

def test_escape_identifier_quoting():
    assert sqlb.escape_identifier('a"b') == '"a""b"'
    sqlb.set_identifier_quoting(sqlb.EscapeQuoting.GraveAccents)
    try:
        assert sqlb.escape_identifier("a`b") == "`a``b`"
    finally:
        sqlb.set_identifier_quoting(sqlb.EscapeQuoting.DoubleQuotes)


@pytest.mark.parametrize("value,expected", [
    ("abc", "LIKE '%abc%' ESCAPE '\\'"),
    ("a%", "LIKE 'a%' ESCAPE '\\'"),
    ("=5", "= '5'"),
    (">5", "> 5"),
    (">=x", ">= 'x'"),
    ("=NULL", "IS NULL"),
    ("<>NULL", "IS NOT NULL"),
    ("1~10", "BETWEEN 1 AND 10"),
    ("/^a.*/", "REGEXP '^a.*'"),
    ("", ""),
])
def test_filter_to_sql(value, expected):
    assert filter_to_sql_condition(value) == expected


def test_global_filter_tokenize():
    assert tokenize_global_filter("a  b /c d/") == ["a", "b", "/c d/"]


def test_query_builder():
    q = Query(sqlb.ObjectIdentifier("main", "t"))
    q.column_names = ["_rowid_", "a", "b"]
    q.where["a"] = "LIKE '%x%'"
    q.order_by = [OrderBy("b", SortDirection.Descending)]
    assert q.build_query(True) == 'SELECT "_rowid_",* FROM "main"."t" WHERE "a" LIKE \'%x%\' ORDER BY "b" DESC'
    assert q.build_count_query().startswith('SELECT COUNT(*) FROM "main"."t" WHERE')


def test_split_statements_with_trigger():
    sql = "CREATE TRIGGER t AFTER INSERT ON x BEGIN SELECT 1; SELECT 2; END; SELECT ';'; -- c\n SELECT 3"
    parts = split_statements(sql)
    assert len(parts) == 3
    assert parts[0].endswith("END;")
    assert parts[2].strip() == "SELECT 3"
    st = statement_at(sql, sql.index("';'"))
    assert st.text.startswith("SELECT ';'")


def test_data_helpers():
    assert is_text_only(b"hello")
    assert not is_text_only(b"\x00\x01\x02")
    assert is_binary(b"\x89PNG\x00\x00")
    assert not is_binary("text")
    assert remove_comments_from_query("SELECT 1 -- x\n/* y */") == "SELECT 1"
    assert parse_clipboard("a\tb\n1\t2\n") == [["a", "b"], ["1", "2"]]


# ---------------------------------------------------------------------------- DBBrowserDB

def test_open_and_schema(db):
    main = db.schemata["main"]
    assert {"parent", "child", "v_parent", "wr"} <= set(main.tables)
    assert "idx_child_parent" in main.indices and "trg" in main.triggers
    child = main.tables["child"]
    assert child.fully_parsed
    assert child.field_names() == ["id", "parent_id", 'weird "name"', "total"]
    assert child.foreign_key(["parent_id"]).table == "parent"
    assert main.tables["v_parent"].is_view()
    assert main.tables["v_parent"].field_names() == ["id", "name"]
    assert main.tables["wr"].rowid_columns() == ["a", "b"]


def test_savepoints_and_revert(db):
    t = sqlb.ObjectIdentifier("main", "parent")
    rowid = db.add_record(t)
    assert rowid is not None and db.dirty
    assert db.update_record(t, "name", rowid, "carol")
    assert db.get_row(t, rowid)[1] == "carol"
    assert db.revert_all()
    assert not db.dirty
    assert db.get_row(t, rowid) is None


def test_commit(db, tmp_path):
    t = sqlb.ObjectIdentifier("main", "parent")
    assert db.delete_records(t, [1, 2])
    assert db.release_all_savepoints()
    assert not db.dirty
    con = sqlite3.connect(db.cur_file)
    assert con.execute("SELECT COUNT(*) FROM parent").fetchone()[0] == 1


def test_without_rowid_editing(db):
    t = sqlb.ObjectIdentifier("main", "wr")
    assert db.execute_sql("INSERT INTO wr VALUES ('k', 1, X'00FF')")
    key = db.query_single_value("SELECT sqlb_make_single_value(a, b) FROM wr")
    assert key == "1:k1:1"
    assert db.update_record(t, "c", key, b"\x01", ["a", "b"])
    assert db.query_single_value("SELECT c FROM wr") == b"\x01"
    assert db.delete_records(t, [key])
    assert db.query_single_value("SELECT COUNT(*) FROM wr") == 0


def test_alter_table_rename_drop_add(db):
    old = db.get_table_by_name(sqlb.ObjectIdentifier("main", "parent"))
    new = old.clone()
    new.name = "people"
    new.fields[1].name = "full_name"
    new.fields.append(sqlb.Field.make("age", "INTEGER", default="0"))
    track = [("id", "id"), ("name", "full_name"), (None, "age")]
    assert db.alter_table(sqlb.ObjectIdentifier("main", "parent"), new, track), db.last_error
    t = db.get_table_by_name(sqlb.ObjectIdentifier("main", "people"))
    assert t is not None and t.field_names() == ["id", "full_name", "age"]
    assert db.query_single_value("SELECT full_name FROM people WHERE id=1") == "alice"
    # Drop a column -> table rebuild path; trigger on the table must survive
    new2 = t.clone()
    new2.remove_field("age")
    new2.fields[1].set_notnull(False)
    assert db.alter_table(sqlb.ObjectIdentifier("main", "people"), new2, [("age", None)]), db.last_error
    t2 = db.get_table_by_name(sqlb.ObjectIdentifier("main", "people"))
    assert t2.field_names() == ["id", "full_name"]
    assert "trg" in db.schemata["main"].triggers
    assert db.query_single_value("SELECT COUNT(*) FROM people") == 3


def test_execute_multi_sql_rollback(db):
    ok = db.execute_multi_sql("INSERT INTO parent(name) VALUES('z'); INSERT INTO nope VALUES(1);")
    assert not ok and "statement #2" in db.last_error
    assert db.query_single_value("SELECT COUNT(*) FROM parent WHERE name='z'") == 0


def test_dump_roundtrip(db, tmp_path):
    out = tmp_path / "dump.sql"
    tables = [n for n in db.schemata["main"].tables if not n.startswith("sqlite_")]
    assert db.dump(str(out), tables, True, False, False, True, True, False)
    con = sqlite3.connect(":memory:")
    con.executescript(out.read_text(encoding="utf-8"))
    assert con.execute("SELECT COUNT(*) FROM parent").fetchone()[0] == 3
    assert con.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchone()[0] == "trg"


def test_regexp_and_collation(db):
    assert db.query_single_value("SELECT 'abc' REGEXP '^a'") == 1
    assert db.query_single_value("SELECT 'B' < 'a' COLLATE UTF16CI") == 0


def test_missing_collation_is_registered(db, tmp_path):
    db.collation_handler = lambda name: True
    con = sqlite3.connect(db.cur_file)
    con.create_collation("MYCOLL", lambda a, b: (a > b) - (a < b))
    con.execute("CREATE TABLE c(x TEXT COLLATE MYCOLL)")
    con.commit()
    con.close()
    db.update_schema()
    assert db.query_single_value("SELECT COUNT(*) FROM c ORDER BY x") == 0


def test_invalid_utf8_is_returned_as_bytes(db):
    db.execute_sql("CREATE TABLE bad(x TEXT)")
    db.execute_sql("INSERT INTO bad VALUES (CAST(X'FF00FE' AS TEXT))")
    assert db.query_single_value("SELECT x FROM bad") == b"\xff\x00\xfe"


# ---------------------------------------------------------------------------- RunSql / model

def test_runsql(db, app):
    from sqliteview.core.runsql import RunSql
    sql = "CREATE TABLE r(a);\nINSERT INTO r VALUES (1),(2);\nSELECT * FROM r;\nbroken sql;"
    runner = RunSql(db, sql, 0, len(sql))
    events = []
    runner.statementExecuted.connect(lambda m, a, b: events.append(("exec", m)))
    runner.statementReturnsRows.connect(lambda q, a, b, ms: events.append(("rows", q)))
    runner.statementErrored.connect(lambda m, a, b: events.append(("err", m)))
    runner.structureUpdated.connect(db.update_schema)
    done = []
    runner.finished.connect(lambda: done.append(1))
    runner.start()
    assert spin(app, lambda: done)
    runner.wait()
    kinds = [e[0] for e in events]
    assert kinds == ["exec", "exec", "rows", "err"]
    assert "2 rows affected" in events[1][1]
    assert db.dirty
    assert "r" in db.schemata["main"].tables or spin(app, lambda: "r" in db.schemata["main"].tables)


def test_table_model_lazy_loading_filter_sort_edit(db, app):
    from sqliteview.core.settings import Settings
    from sqliteview.core.table_model import SqliteTableModel
    db.execute_sql("CREATE TABLE big(n INTEGER, s TEXT)")
    db.execute_multi_sql("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x<1234) "
                         "INSERT INTO big SELECT x, 'v' || x FROM c;")
    Settings.set("db", "prefetchsize", 200)
    try:
        m = SqliteTableModel(db)
        m.set_table_query(Query(sqlb.ObjectIdentifier("main", "big")))
        assert spin(app, lambda: m.real_row_count() == 1234)
        assert m.rowCount() == 1234
        assert spin(app, lambda: m.row_available(0))
        assert m.data(m.index(0, 2)) == "v1"
        # row far away gets loaded lazily
        m.data(m.index(1000, 1))
        assert spin(app, lambda: m.row_available(1000))
        assert m.raw_value(1000, 1) == 1001
        m.update_filter(1, ">1200")
        assert spin(app, lambda: m.real_row_count() == 34 and m.row_available(0))
        m.sort(1, 1)  # descending
        assert spin(app, lambda: m.row_available(0) and m.raw_value(0, 1) == 1234)
        assert m.setData(m.index(0, 2), "changed")
        assert db.query_single_value("SELECT s FROM big WHERE n=1234") == "changed"
        assert m.setData(m.index(0, 1), "77")
        assert db.query_single_value("SELECT typeof(n) FROM big WHERE s='changed'") == "integer"
        m.shutdown()
    finally:
        Settings.set("db", "prefetchsize", 50000)
