import sqlite3
from decimal import Decimal

import pytest

from app import ROOT, calculate, create_app
from database_migrations import WINDOWS_UNITS


def windows_database(path, encoding="cp1251"):
    with sqlite3.connect(path) as db:
        db.executescript((ROOT / "schema.sql").read_bytes().decode(encoding))
        # Existing valid business data can predate the malformed CHECK clause.
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute(
            "INSERT INTO products(id,name,unit) VALUES (7,'Сохранённый картофель','кг')"
        )
        db.execute("PRAGMA ignore_check_constraints=OFF")
        db.execute(
            "INSERT INTO products(id,name,unit) VALUES (8,?,?)",
            ("Старая единица", "л".encode("utf-8").decode(encoding)),
        )
        db.execute("INSERT INTO stores(id,name) VALUES (5,'Сохранённый магазин')")
        db.execute("INSERT INTO baskets(id,name) VALUES (3,'Сохранённая корзина')")
        db.execute(
            "INSERT INTO items(id,basket_id,product_id,quantity) VALUES (9,3,7,'2')"
        )
        db.execute(
            "INSERT INTO prices(id,store_id,product_id,date,cents) VALUES (11,5,7,'2026-01-01',8000)"
        )
        db.execute("CREATE INDEX custom_product_name ON products(name)")


@pytest.mark.parametrize("encoding", ["cp1251", "cp1252"])
def test_windows_repair_preserves_data_links_and_backup(tmp_path, encoding):
    path = tmp_path / "old.sqlite3"
    windows_database(path, encoding)
    with sqlite3.connect(path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            db.execute("INSERT INTO products(name,unit) VALUES ('Картофель','л')")
    app = create_app(
        {"TESTING": True, "SECRET_KEY": "repair-test", "DATABASE": str(path)}
    )
    backup = app.config["DATABASE_REPAIR_BACKUP"]
    assert backup.is_file()
    with sqlite3.connect(backup) as db:
        assert (
            "кг".encode("utf-8").decode(encoding)
            in db.execute(
                "SELECT sql FROM sqlite_master WHERE name='products'"
            ).fetchone()[0]
        )
        assert db.execute("SELECT count(*) FROM products").fetchone()[0] == 2
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        assert db.execute("SELECT unit FROM products WHERE id=8").fetchone()[0] == "л"
        assert (
            db.execute("SELECT name FROM products WHERE id=7").fetchone()[0]
            == "Сохранённый картофель"
        )
        assert db.execute("SELECT product_id FROM items WHERE id=9").fetchone()[0] == 7
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name='custom_product_name'"
        ).fetchone()
        assert calculate(db, 3, "2026-01-01")[0]["total"] == Decimal("160.00")
    client = app.test_client()
    client.get("/prices")
    with client.session_transaction() as session:
        csrf = session["csrf"]
    for unit in ["кг", "л", "шт."]:
        response = client.post(
            "/prices",
            data={
                "csrf": csrf,
                "action": "product",
                "name": f"Новый продукт {unit}",
                "unit": unit,
            },
        )
        assert response.status_code == 302
    reopened = create_app(
        {"TESTING": True, "SECRET_KEY": "repair-test", "DATABASE": str(path)}
    )
    assert reopened.config["DATABASE_REPAIR_BACKUP"] is None
    assert len(list(tmp_path.glob("*.before-utf8-fix-*.sqlite3"))) == 1


def test_fresh_schema_reads_utf8_and_needs_no_repair(tmp_path):
    path = tmp_path / "new.sqlite3"
    app = create_app(
        {"TESTING": True, "SECRET_KEY": "repair-test", "DATABASE": str(path)}
    )
    assert app.config["DATABASE_REPAIR_BACKUP"] is None
    with sqlite3.connect(path) as db:
        for unit in ["кг", "л", "шт."]:
            db.execute("INSERT INTO products(name,unit) VALUES (?,?)", (unit, unit))
        assert db.execute("SELECT count(*) FROM products").fetchone()[0] == 3


def test_repair_rolls_back_if_existing_links_are_broken(tmp_path):
    path = tmp_path / "broken.sqlite3"
    windows_database(path)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO items(id,basket_id,product_id,quantity) VALUES (10,3,999,'1')"
        )
    with pytest.raises(sqlite3.IntegrityError, match="Связи базы"):
        create_app(
            {"TESTING": True, "SECRET_KEY": "repair-test", "DATABASE": str(path)}
        )
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM products").fetchone()[0] == 2
        assert (
            WINDOWS_UNITS[0]
            in db.execute(
                "SELECT sql FROM sqlite_master WHERE name='products'"
            ).fetchone()[0]
        )
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='products_utf8_repair'"
            ).fetchone()
            is None
        )


def test_old_demo_names_are_renamed_without_losing_records(tmp_path):
    path = tmp_path / "names.sqlite3"
    config = {"TESTING": True, "SECRET_KEY": "test", "DATABASE": str(path)}
    create_app(config)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO products(id,name,unit) VALUES (1,'Картофель','кг')")
        db.execute(
            "INSERT INTO products(id,name,unit) VALUES (2,'Учебный картофель','кг')"
        )
        db.execute(
            "INSERT INTO products(id,name,unit) VALUES (3,'Учебная морковь','кг')"
        )
        db.execute("INSERT INTO stores(id,name) VALUES (1,'Учебная Лавка')")
        db.execute(
            "INSERT INTO baskets(id,name) VALUES (1,'Учебная корзина для графиков')"
        )
        db.execute(
            "INSERT INTO items(id,basket_id,product_id,quantity) VALUES (1,1,2,'2')"
        )
        db.execute(
            "INSERT INTO prices(id,store_id,product_id,date,cents) VALUES (1,1,2,'2026-01-01',8000)"
        )
    app = create_app(config)
    backup = app.config["DEMO_NAMES_BACKUP"]
    assert backup.is_file()
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        names = [r[0] for r in db.execute("SELECT name FROM products ORDER BY id")]
        assert names == ["Картофель", "Картофель (2)", "Морковь"]
        assert db.execute("SELECT name FROM stores").fetchone()[0] == "Лавка"
        assert (
            db.execute("SELECT name FROM baskets").fetchone()[0]
            == "Корзина для графиков"
        )
        assert calculate(db, 1, "2026-01-01")[0]["total"] == Decimal("160.00")
        assert db.execute("SELECT product_id FROM items").fetchone()[0] == 2
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    with sqlite3.connect(backup) as db:
        assert (
            db.execute("SELECT name FROM products WHERE id=2").fetchone()[0]
            == "Учебный картофель"
        )
    assert create_app(config).config["DEMO_NAMES_BACKUP"] is None
