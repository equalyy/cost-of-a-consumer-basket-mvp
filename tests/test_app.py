import sqlite3
from decimal import Decimal

import pytest

from app import calculate, cheapest, create_app, difference, positive_decimal


@pytest.fixture
def application(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-only-key",
            "DATABASE": str(tmp_path / "test.sqlite3"),
        }
    )


def post(client, path, data):
    client.get(path)
    with client.session_transaction() as session:
        token = session["csrf"]
    return client.post(path, data={**data, "csrf": token})


def connection(application):
    db = sqlite3.connect(application.config["DATABASE"])
    db.row_factory = sqlite3.Row
    return db


def prepare(client):
    assert (
        post(client, "/baskets", {"action": "create", "name": "Тестовая"}).status_code
        == 302
    )
    for name, unit in [("Картофель", "кг"), ("Молоко", "л")]:
        assert (
            post(
                client, "/prices", {"action": "product", "name": name, "unit": unit}
            ).status_code
            == 302
        )
    for name in ["Полный", "Неполный"]:
        post(client, "/prices", {"action": "store", "name": name})
    for product, quantity in [(1, "2"), (2, "1")]:
        post(
            client,
            "/baskets?basket=1",
            {
                "action": "item",
                "basket_id": "1",
                "product_id": str(product),
                "quantity": quantity,
            },
        )
    for shop, product, day, price in [
        (1, 1, "2026-01-01", "80"),
        (1, 2, "2026-01-01", "100"),
        (1, 1, "2026-02-01", "88"),
        (1, 2, "2026-02-01", "110"),
        (2, 1, "2026-01-01", "1"),
    ]:
        post(
            client,
            "/prices",
            {
                "action": "price",
                "store_id": str(shop),
                "product_id": str(product),
                "date": day,
                "price": price,
            },
        )


def test_acceptance_calculation_missing_and_dates(application):
    prepare(application.test_client())
    with connection(application) as db:
        first = calculate(db, 1, "2026-01-20")
        second = calculate(db, 1, "2026-02-01")
        assert first[0]["total"] == Decimal("260.00")
        assert second[0]["total"] == Decimal("286.00")
        assert first[1]["total"] == Decimal("2.00")
        assert first[1]["missing"] == ["Молоко"]
        assert not first[1]["complete"]
        assert cheapest(first)["name"] == "Полный"
        delta = difference(first[0]["total"], second[0]["total"])
        assert delta == {"amount": Decimal("26.00"), "percent": Decimal("10.0")}
        assert cheapest(calculate(db, 1, "2025-12-31")) is None
        assert difference(Decimal(0), Decimal(2)) is None


def test_persistence_and_all_pages(application):
    client = application.test_client()
    prepare(client)
    reopened = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-only-key",
            "DATABASE": application.config["DATABASE"],
        }
    )
    for route in [
        "/",
        "/baskets",
        "/prices",
        "/dynamics",
        "/?start=2026-01-01&date=2026-02-01",
    ]:
        response = reopened.test_client().get(route)
        assert response.status_code == 200
    with connection(reopened) as db:
        assert cheapest(calculate(db, 1, "2026-02-01"))["total"] == Decimal("286.00")


@pytest.mark.parametrize(
    "value", ["0", "-1", "NaN", "Infinity", "abc", "1000001", "0.0001"]
)
def test_invalid_quantity_keeps_records(application, value):
    client = application.test_client()
    prepare(client)
    response = post(
        client,
        "/baskets?basket=1",
        {"action": "item", "basket_id": "1", "product_id": "1", "quantity": value},
    )
    assert response.status_code == 422
    with connection(application) as db:
        assert (
            db.execute("SELECT quantity FROM items WHERE product_id=1").fetchone()[0]
            == "2"
        )


def test_price_upsert_validation_and_csrf(application):
    client = application.test_client()
    prepare(client)
    price = {
        "action": "price",
        "store_id": "1",
        "product_id": "1",
        "date": "2026-01-01",
        "price": "81.25",
    }
    assert client.post("/prices", data=price).status_code == 400
    assert post(client, "/prices", price).status_code == 302
    for invalid in [
        {"price": "-1"},
        {"price": "1.001"},
        {"date": "2026-02-30"},
        {"store_id": "999"},
        {"date": "20260101"},
    ]:
        assert post(client, "/prices", {**price, **invalid}).status_code == 422
    with connection(application) as db:
        assert (
            db.execute(
                "SELECT cents FROM prices WHERE store_id=1 AND product_id=1 AND date=?",
                ("2026-01-01",),
            ).fetchone()[0]
            == 8125
        )
        assert db.execute("SELECT count(*) FROM prices").fetchone()[0] == 5


def test_demo_is_idempotent_and_non_destructive(application):
    runner = application.test_cli_runner()
    assert runner.invoke(args=["seed-demo"]).exit_code == 0
    assert runner.invoke(args=["seed-demo"]).exit_code == 0
    with connection(application) as db:
        assert db.execute("SELECT count(*) FROM products").fetchone()[0] == 10
        assert db.execute("SELECT count(*) FROM stores").fetchone()[0] == 3
        assert db.execute("SELECT count(DISTINCT date) FROM prices").fetchone()[0] == 3
        rows = calculate(db, 1, "2026-11-01")
        assert [row["complete"] for row in rows] == [True, True, False]


def test_demo_refuses_existing_work(application):
    post(application.test_client(), "/baskets", {"action": "create", "name": "Рабочая"})
    assert application.test_cli_runner().invoke(args=["seed-demo"]).exit_code == 1
    with connection(application) as db:
        assert db.execute("SELECT name FROM baskets").fetchone()[0] == "Рабочая"
        assert db.execute("SELECT count(*) FROM products").fetchone()[0] == 0


def test_empty_states_escape_and_remove(application):
    client = application.test_client()
    for route in ["/", "/baskets", "/prices", "/dynamics"]:
        assert client.get(route).status_code == 200
    response = post(
        client, "/baskets", {"action": "create", "name": "<script>alert(1)</script>"}
    )
    assert response.status_code == 302
    assert b"&lt;script&gt;" in client.get("/baskets").data
    assert (
        post(client, "/baskets", {"action": "delete", "basket_id": "1"}).status_code
        == 302
    )
    with connection(application) as db:
        assert db.execute("SELECT count(*) FROM baskets").fetchone()[0] == 0


def test_fractional_money_summed_before_rounding(application):
    client = application.test_client()
    prepare(client)
    post(
        client,
        "/baskets",
        {"action": "item", "basket_id": "1", "product_id": "1", "quantity": "0.001"},
    )
    post(
        client,
        "/prices",
        {
            "action": "price",
            "store_id": "1",
            "product_id": "1",
            "date": "2026-01-01",
            "price": "5.55",
        },
    )
    with connection(application) as db:
        assert calculate(db, 1, "2026-01-01")[0]["total"] == Decimal("100.01")
    assert positive_decimal("1,25", "Количество") == Decimal("1.25")
