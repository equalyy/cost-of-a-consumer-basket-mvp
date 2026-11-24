import io
import json
import re
import sqlite3
from decimal import Decimal

import pytest

from app import ROOT, calculate, cheapest, create_app


@pytest.fixture
def application(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "receipt-test",
            "DATABASE": str(tmp_path / "receipts.sqlite3"),
        }
    )


def csrf(client):
    client.get("/baskets")
    with client.session_transaction() as session:
        return session["csrf"]


def prepare(application):
    client = application.test_client()
    token = csrf(client)
    response = client.post(
        "/transfer",
        data={
            "csrf": token,
            "file": (
                io.BytesIO((ROOT / "examples/basket-data.json").read_bytes()),
                "data.json",
            ),
            "mode": "keep",
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    return client


def chart_data(response):
    match = re.search(
        r'<script id="chart-data" type="application/json">(.*?)</script>',
        response.get_data(as_text=True),
        re.S,
    )
    return json.loads(match.group(1))


def receipt_data(application, url):
    captured = []
    from flask import template_rendered

    def record(sender, template, context, **extra):
        captured.append(context)

    with template_rendered.connected_to(record, application):
        response = application.test_client().get(url)
    assert response.status_code == 200
    return response, captured[-1]


def test_dynamics_shows_partial_discounter_and_keeps_rating_complete_only(application):
    client = prepare(application)
    response = client.get("/dynamics?basket=1&start=2026-07-01&date=2026-11-01")
    chart = chart_data(response)
    assert len(chart["datasets"]) == 3
    discounter = chart["datasets"][2]
    assert discounter["data"] == [1359.0, 1434.5, 1464.7, 1510.0, 1449.6]
    assert discounter["complete"] == [False] * 5
    assert discounter["missing"] == [["Молоко"]] * 5
    assert "известные позиции" in discounter["label"]
    assert "1\u202f449,60" in response.get_data(as_text=True)
    with sqlite3.connect(application.config["DATABASE"]) as db:
        db.row_factory = sqlite3.Row
        assert cheapest(calculate(db, 1, "2026-11-01"))["name"] == "Лавка"


def test_dynamics_missing_all_prices_is_gap_and_later_partial_point_is_shown(
    application,
):
    client = prepare(application)
    response = client.get("/dynamics?basket=1&start=2026-06-01&date=2026-07-01")
    chart = chart_data(response)
    assert all(dataset["data"][0] is None for dataset in chart["datasets"])
    assert chart["datasets"][2]["data"][1] == 1359.0
    _, context = receipt_data(
        application, "/dynamics?basket=1&start=2026-07-01&date=2026-11-01"
    )
    assert context["rows"][2]["delta"] is None
    assert context["rows"][0]["delta"]["percent"] == Decimal("7")


def test_three_columns_and_mixed_minimum_use_decimal_prices(application):
    prepare(application)
    response, context = receipt_data(
        application, "/baskets?basket=1&date=2026-11-01&shop1=1&shop2=2&shop3=3"
    )
    assert [slot["quote"]["total"] for slot in context["slots"]] == [
        Decimal("1819.00"),
        Decimal("1938.00"),
        Decimal("1449.60"),
    ]
    assert context["mixed"]["total"] == Decimal("1652.90")
    assert context["mixed"]["complete"]
    row = context["receipt_rows"][0]
    assert row["item"]["quantity"] == "3"
    assert [cell["price"] for cell in row["cells"]] == [
        Decimal("58.85"),
        Decimal("62.70"),
        Decimal("52.80"),
    ]
    assert row["minimum"] == Decimal("52.80")
    assert "3 × 58,85 ₽" in response.get_data(as_text=True)
    assert "1\u202f652,90" in response.get_data(as_text=True)
    assert not context["slots"][2]["quote"]["complete"]


def test_columns_optional_and_distinct_and_dates_validated(application):
    prepare(application)
    _, empty = receipt_data(application, "/baskets?basket=1")
    assert all(slot["quote"] is None for slot in empty["slots"])
    assert not empty["receipt_errors"]
    assert not empty["has_selected_stores"]
    _, one = receipt_data(application, "/baskets?basket=1&shop2=3&date=2026-11-01")
    assert [slot["quote"] is not None for slot in one["slots"]] == [False, True, False]
    assert one["mixed"]["total"] == Decimal("1449.60")
    assert one["mixed"]["missing"] == ["Молоко"]
    assert not one["mixed"]["complete"]
    _, duplicate = receipt_data(
        application, "/baskets?basket=1&shop1=1&shop2=1&shop3=999&date=bad"
    )
    assert duplicate["slots"][0]["quote"]["id"] == 1
    assert duplicate["slots"][1]["quote"] is None
    assert duplicate["slots"][2]["quote"] is None
    assert len(duplicate["receipt_errors"]) == 3
    assert duplicate["receipt_date"] == "2026-11-01"


def test_dates_before_prices_and_new_history_are_used(application):
    client = prepare(application)
    _, before = receipt_data(application, "/baskets?basket=1&shop1=1&date=2026-06-01")
    assert before["receipt_date"] == "2026-06-01"
    assert before["slots"][0]["quote"]["known_count"] == 0
    assert before["mixed"]["known_count"] == 0
    assert before["mixed"]["missing"]
    token = csrf(client)
    assert (
        client.post(
            "/prices",
            data={
                "csrf": token,
                "action": "price",
                "product_id": "1",
                "store_id": "1",
                "date": "2026-11-15",
                "price": "70",
            },
        ).status_code
        == 302
    )
    _, old = receipt_data(application, "/baskets/1/receipt?shop1=1&date=2026-11-14")
    _, new = receipt_data(application, "/baskets/1/receipt?shop1=1&date=2026-11-15")
    assert old["receipt_rows"][0]["cells"][0]["price"] == Decimal("58.85")
    assert new["receipt_rows"][0]["cells"][0]["price"] == Decimal("70")
    assert new["slots"][0]["quote"]["total"] == Decimal("1852.45")


def test_edit_quantity_and_remove_keep_selected_stores_and_date(application):
    client = prepare(application)
    url = "/baskets?basket=1&shop1=1&shop2=2&shop3=3&date=2026-11-01"
    token = csrf(client)
    response = client.post(
        url,
        data={
            "csrf": token,
            "action": "item",
            "basket_id": "1",
            "product_id": "1",
            "quantity": "4",
        },
    )
    assert response.status_code == 302
    assert all(
        part in response.location
        for part in ["shop1=1", "shop2=2", "shop3=3", "date=2026-11-01"]
    )
    _, changed = receipt_data(application, response.location)
    assert changed["slots"][0]["quote"]["total"] == Decimal("1877.85")
    response = client.post(
        url, data={"csrf": token, "action": "remove", "basket_id": "1", "item_id": "1"}
    )
    assert response.status_code == 302
    _, removed = receipt_data(application, response.location)
    assert len(removed["receipt_rows"]) == 9
    assert removed["slots"][0]["quote"]["total"] == Decimal("1642.45")


def test_empty_baskets_stores_and_partial_endpoint(application):
    client = application.test_client()
    assert client.get("/baskets").status_code == 200
    token = csrf(client)
    client.post("/baskets", data={"csrf": token, "action": "create", "name": "Пустая"})
    _, empty = receipt_data(application, "/baskets?basket=1")
    assert not empty["items"]
    assert not empty["stores"]
    assert client.get("/baskets/999/receipt").status_code == 404
    response = client.get("/baskets/1/receipt")
    assert (
        response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    )
    assert "<html" not in response.get_data(as_text=True)


def test_mixed_minimum_combines_two_incomplete_stores(application):
    with sqlite3.connect(application.config["DATABASE"]) as db:
        db.execute(
            "INSERT INTO products(id,name,unit) VALUES (1,'Картофель','кг'),(2,'Молоко','л')"
        )
        db.execute(
            "INSERT INTO stores(id,name) VALUES (1,'Овощи'),(2,'Молочные продукты')"
        )
        db.execute("INSERT INTO baskets(id,name) VALUES (1,'Смешанная')")
        db.execute(
            "INSERT INTO items(basket_id,product_id,quantity) VALUES (1,1,'2'),(1,2,'1')"
        )
        db.execute(
            "INSERT INTO prices(store_id,product_id,date,cents) VALUES (1,1,'2026-01-01',8000),(2,2,'2026-01-01',10000)"
        )
    _, context = receipt_data(
        application, "/baskets?basket=1&shop1=1&shop2=2&date=2026-01-01"
    )
    assert not context["slots"][0]["quote"]["complete"]
    assert not context["slots"][1]["quote"]["complete"]
    assert context["mixed"]["complete"]
    assert context["mixed"]["total"] == Decimal("260.00")


def test_equal_prices_are_counted_once_and_fractional_costs_are_not_rounded_early(
    application,
):
    with sqlite3.connect(application.config["DATABASE"]) as db:
        db.execute(
            "INSERT INTO products(id,name,unit) VALUES (1,'Продукт 1','кг'),(2,'Продукт 2','кг')"
        )
        db.execute("INSERT INTO stores(id,name) VALUES (1,'Первый'),(2,'Второй')")
        db.execute("INSERT INTO baskets(id,name) VALUES (1,'Малая')")
        db.execute(
            "INSERT INTO items(basket_id,product_id,quantity) VALUES (1,1,'0.001'),(1,2,'0.001')"
        )
        db.execute(
            "INSERT INTO prices(store_id,product_id,date,cents) VALUES (1,1,'2026-01-01',555),(1,2,'2026-01-01',555),(2,1,'2026-01-01',555),(2,2,'2026-01-01',555)"
        )
    _, context = receipt_data(
        application, "/baskets?basket=1&shop1=1&shop2=2&date=2026-01-01"
    )
    assert context["mixed"]["total"] == Decimal("0.01")
    assert context["mixed"]["complete"]
    assert all(
        slot["quote"]["total"] == Decimal("0.01")
        for slot in context["slots"]
        if slot["quote"]
    )
