import io
import sqlite3

import pytest

import app as app_module
from app import ROOT, create_app

TABLES = ("products", "stores", "baskets", "items", "prices", "metadata")


@pytest.fixture
def application(tmp_path):
    application = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "reset-test",
            "DATABASE": str(tmp_path / "reset.sqlite3"),
        }
    )
    assert application.test_cli_runner().invoke(args=["seed-demo"]).exit_code == 0
    return application


def database(application):
    return sqlite3.connect(application.config["DATABASE"])


def counts(application):
    with database(application) as db:
        return {
            table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in TABLES
        }


def csrf(client):
    client.get("/transfer")
    with client.session_transaction() as session:
        return session["csrf"]


def prepare(client):
    token = csrf(client)
    response = client.post("/data/reset/prepare", data={"csrf": token})
    assert response.status_code == 200
    assert response.json["wait_seconds"] == 3
    return {"csrf": token, "confirmation": response.json["confirmation"]}


def test_reset_wait_is_checked_on_server(application, monkeypatch):
    client = application.test_client()
    clock = [1000.0]
    monkeypatch.setattr(app_module, "time", lambda: clock[0])
    confirmation = prepare(client)
    original = counts(application)
    for elapsed in [0, 1, 2.99]:
        clock[0] = 1000 + elapsed
        response = client.post("/data/reset", data=confirmation, follow_redirects=True)
        assert "Подождите 3 секунды" in response.get_data(as_text=True)
        assert counts(application) == original
    clock[0] = 1003
    response = client.post("/data/reset", data=confirmation, follow_redirects=True)
    assert "удалены из базы" in response.get_data(as_text=True)
    assert counts(application) == dict.fromkeys(TABLES, 0)
    reopened = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "reset-test",
            "DATABASE": application.config["DATABASE"],
        }
    )
    assert counts(reopened) == dict.fromkeys(TABLES, 0)
    with client.session_transaction() as session:
        assert session["csrf"] != confirmation["csrf"]
    with database(application) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_invalid_expired_and_foreign_confirmations_keep_data(application, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(app_module, "time", lambda: clock[0])
    client = application.test_client()
    confirmation = prepare(client)
    original = counts(application)
    assert client.post("/data/reset", data={}).status_code == 400
    assert client.get("/data/reset").status_code == 405
    assert client.get("/data/reset/prepare").status_code == 405
    for value in ["", "fake-confirmation", confirmation["confirmation"] + "tampered"]:
        response = client.post(
            "/data/reset",
            data={**confirmation, "confirmation": value},
            follow_redirects=True,
        )
        assert "недействительно" in response.get_data(as_text=True)
        assert counts(application) == original
    other = application.test_client()
    other_csrf = csrf(other)
    clock[0] = 1004
    response = other.post(
        "/data/reset", data={**confirmation, "csrf": other_csrf}, follow_redirects=True
    )
    assert "Откройте окно" in response.get_data(as_text=True)
    assert counts(application) == original
    clock[0] = 1301
    response = client.post("/data/reset", data=confirmation, follow_redirects=True)
    assert "истекло" in response.get_data(as_text=True)
    assert counts(application) == original


def test_old_confirmation_cannot_delete_new_data_after_reset(application, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(app_module, "time", lambda: clock[0])
    client = application.test_client()
    confirmation = prepare(client)
    old_cookie = client.get_cookie("session").value
    clock[0] = 1003
    assert client.post("/data/reset", data=confirmation).status_code == 303
    with database(application) as db:
        db.execute("INSERT INTO products(name,unit) VALUES ('Новая запись','кг')")
    client.set_cookie("session", old_cookie)
    response = client.post("/data/reset", data=confirmation, follow_redirects=True)
    assert "уже использовано" in response.get_data(as_text=True)
    with database(application) as db:
        assert db.execute("SELECT name FROM products").fetchone()[0] == "Новая запись"


def test_reset_failure_rolls_back_all_tables(application, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(app_module, "time", lambda: clock[0])
    client = application.test_client()
    confirmation = prepare(client)
    original = counts(application)
    with database(application) as db:
        db.execute(
            "CREATE TRIGGER reject_reset BEFORE DELETE ON products BEGIN SELECT RAISE(ABORT,'test rollback'); END"
        )
    clock[0] = 1003
    response = client.post("/data/reset", data=confirmation, follow_redirects=True)
    assert "Очистка отменена" in response.get_data(as_text=True)
    assert counts(application) == original


@pytest.mark.parametrize("extension", ["json", "csv", "xlsx"])
def test_reset_then_import_has_plain_unique_names_and_persists(
    application, monkeypatch, extension
):
    clock = [1000.0]
    monkeypatch.setattr(app_module, "time", lambda: clock[0])
    client = application.test_client()
    confirmation = prepare(client)
    clock[0] = 1003
    assert client.post("/data/reset", data=confirmation).status_code == 303
    assert counts(application) == dict.fromkeys(TABLES, 0)
    empty = client.get("/transfer/export/json").json
    assert all(empty[table] == [] for table in TABLES if table != "metadata")
    for path in ["/", "/baskets", "/prices", "/dynamics"]:
        assert client.get(path).status_code == 200
    for attempt in range(2):
        response = client.post(
            "/transfer",
            data={
                "csrf": csrf(client),
                "mode": "keep",
                "file": (
                    io.BytesIO(
                        (ROOT / "examples" / f"basket-data.{extension}").read_bytes()
                    ),
                    f"basket-data.{extension}",
                ),
            },
            content_type="multipart/form-data",
        )
        assert response.status_code == 302
    reopened = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "reset-test",
            "DATABASE": application.config["DATABASE"],
        }
    )
    with database(reopened) as db:
        products = [row[0] for row in db.execute("SELECT name FROM products")]
        stores = [row[0] for row in db.execute("SELECT name FROM stores")]
        assert len(products) == 10 and len(stores) == 3
        assert all(
            "учебн" not in name.lower() and "(2)" not in name
            for name in products + stores
        )
        assert db.execute("SELECT count(*) FROM prices").fetchone()[0] == 145


def test_only_preparing_confirmation_does_not_delete_business_data(application):
    original = counts(application)
    prepare(application.test_client())
    current = counts(application)
    assert {key: value for key, value in original.items() if key != "metadata"} == {
        key: value for key, value in current.items() if key != "metadata"
    }
