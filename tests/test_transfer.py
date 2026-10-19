"""CSV import engine (core/csvimport.py) and query export (core/rowexport.write_query_result)."""
from __future__ import annotations

import json
import os
import sqlite3

import pytest
from PySide6.QtCore import QCoreApplication

from sqliteview.core import sqlb
from sqliteview.core.csvimport import (CsvImportError, CsvOptions, ImportCancelled, detect_fields, empty_values_for,
                                       import_csv_file, read_preview)
from sqliteview.core.db import DBBrowserDB
from sqliteview.core.rowexport import ExportCancelled, write_query_result

PEOPLE_CSV = "name,age,score,note\nalice,30,1.5,\nbob,41,2,hello\n\ncarl,x,3.25,\"multi\nline\"\n"


@pytest.fixture
def db(tmp_path):
    QCoreApplication.instance() or QCoreApplication([])
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE things(id INTEGER PRIMARY KEY, label TEXT NOT NULL, qty INTEGER DEFAULT (7), extra TEXT);
        INSERT INTO things VALUES (1, 'a,b', 2, NULL), (2, 'quote"d', 3, 'x');
    """)
    con.commit()
    con.close()
    d = DBBrowserDB()
    assert d.open(str(path)), d.last_error
    yield d
    d.close(force=True)


@pytest.fixture
def people_csv(tmp_path):
    p = tmp_path / "people.csv"
    p.write_bytes(PEOPLE_CSV.encode("utf-8"))
    return str(p)


def rows(db, sql):
    with db.exclusive("test") as conn:
        return db.fetch_all(conn, sql)[1]


def test_detect_fields_and_preview(people_csv):
    opts = CsvOptions()
    fields = detect_fields(people_csv, opts)
    assert [(f.name, f.type) for f in fields] == [("name", "TEXT"), ("age", "TEXT"), ("score", "REAL"),
                                                   ("note", "TEXT")]
    assert read_preview(people_csv, opts, 2) == [["alice", "30", "1.5", ""], ["bob", "41", "2", "hello"]]
    no_header = CsvOptions(header=False, detect_types=False)
    assert [f.name for f in detect_fields(people_csv, no_header)] == ["field1", "field2", "field3", "field4"]
    assert all(f.type == "" for f in detect_fields(people_csv, no_header))


def test_detect_fields_unique_and_sanitized_names(tmp_path):
    p = tmp_path / "dup.csv"
    p.write_text("a b,a b,\n1,2,3\n", encoding="utf-8")
    assert [f.name for f in detect_fields(str(p), CsvOptions(sanitize_names=True))] == ["a_b", "a_b_2", "field3"]


def test_import_new_table(db, people_csv):
    opts = CsvOptions()
    n = import_csv_file(db, people_csv, "people", opts, detect_fields(people_csv, opts))
    assert n == 3
    db.update_schema()
    assert db.get_table_by_name(sqlb.ObjectIdentifier("main", "people")) is not None
    assert rows(db, "SELECT name, age, score, note FROM people ORDER BY name") == [
        ("alice", "30", 1.5, None), ("bob", "41", 2.0, "hello"), ("carl", "x", 3.25, "multi\nline")]
    assert db.dirty  # staged, not committed


def test_import_existing_table_uses_defaults(db, tmp_path):
    p = tmp_path / "things.csv"
    p.write_text("id,label,qty,extra\n10,new,,\n", encoding="utf-8")
    opts = CsvOptions()
    tbl = db.get_table_by_name(sqlb.ObjectIdentifier("main", "things"))
    empty = empty_values_for(db, tbl)
    assert empty == [None, "", 7, None]
    import_csv_file(db, str(p), "things", opts, detect_fields(str(p), opts), to_existing=True, empty_values=empty)
    assert rows(db, "SELECT label, qty, extra FROM things WHERE id = 10") == [("new", 7, None)]
    assert empty_values_for(db, tbl, use_defaults=False)[2] is None


def test_import_error_rolls_back(db, tmp_path):
    p = tmp_path / "clash.csv"
    p.write_text("id,label,qty,extra\n50,ok,1,\n1,dup,1,\n", encoding="utf-8")
    opts = CsvOptions()
    with pytest.raises(CsvImportError, match="around row 1"):
        import_csv_file(db, str(p), "things", opts, detect_fields(str(p), opts), to_existing=True)
    assert rows(db, "SELECT COUNT(*) FROM things") == [(2,)]
    n = import_csv_file(db, str(p), "things", opts, detect_fields(str(p), opts), to_existing=True, conflict="ignore")
    assert n == 2 and rows(db, "SELECT COUNT(*) FROM things") == [(3,)]
    with pytest.raises(ValueError):
        import_csv_file(db, str(p), "things", opts, [], conflict="bogus")


def test_import_cancel_rolls_back(db, tmp_path, monkeypatch):
    import sqliteview.core.csvimport as csvimport
    monkeypatch.setattr(csvimport, "INSERT_BATCH_ROWS", 2)
    p = tmp_path / "many.csv"
    p.write_text("v\n" + "\n".join(str(i) for i in range(10)) + "\n", encoding="utf-8")
    opts = CsvOptions()
    with pytest.raises(ImportCancelled):
        import_csv_file(db, str(p), "many", opts, detect_fields(str(p), opts), progress=lambda _p: False)
    db.update_schema()
    assert db.get_table_by_name(sqlb.ObjectIdentifier("main", "many")) is None


@pytest.mark.parametrize("fmt", ["csv", "tsv", "json", "sql", "md"])
def test_write_query_result_formats(db, tmp_path, fmt):
    out = str(tmp_path / f"out.{fmt}")
    n = write_query_result(db, "SELECT * FROM things ORDER BY id", out, fmt, table="things", newline="\n")
    assert n == 2 and not os.path.exists(out + ".part")
    text = open(out, encoding="utf-8").read()
    if fmt == "csv":
        assert text.splitlines() == ['"id","label","qty","extra"', '1,"a,b",2,', '2,"quote""d",3,x']
    elif fmt == "tsv":
        assert text.splitlines()[1] == "1\ta,b\t2\t"
    elif fmt == "json":
        assert json.loads(text)[1] == {"id": 2, "label": 'quote"d', "qty": 3, "extra": "x"}
    elif fmt == "sql":
        assert text.startswith('INSERT INTO "things" ("id","label","qty","extra") VALUES (1,\'a,b\',2,NULL);')
    else:
        assert text.splitlines()[0] == "| id | label | qty | extra |"


def test_write_query_result_errors_leave_no_file(db, tmp_path):
    out = str(tmp_path / "bad.csv")
    with pytest.raises(sqlite3.Error):
        write_query_result(db, "SELECT * FROM nope", out)
    with pytest.raises(ValueError):
        write_query_result(db, "SELECT 1", out, "xml")
    big = "WITH RECURSIVE s(v) AS (SELECT 1 UNION ALL SELECT v + 1 FROM s WHERE v < 6000) SELECT v FROM s"
    with pytest.raises(ExportCancelled):
        write_query_result(db, big, out, progress=lambda _n: False)
    assert os.listdir(tmp_path) == ["t.db"]
