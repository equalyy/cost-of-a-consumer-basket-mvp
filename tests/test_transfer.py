import copy
import io
import json
import sqlite3
from datetime import datetime
from decimal import Decimal

import pytest
from openpyxl import load_workbook

from app import calculate, create_app, parse_date, positive_decimal
from data_transfer import (
    decode_file,
    encode_file,
    sample_data,
    validate_data,
)


@pytest.fixture
def application(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-only",
            "DATABASE": str(tmp_path / "source.sqlite3"),
        }
    )


def upload(client, content, extension, mode="keep"):
    client.get("/transfer")
    with client.session_transaction() as session:
        token = session["csrf"]
    return client.post(
        "/transfer",
        data={
            "csrf": token,
            "mode": mode,
            "file": (io.BytesIO(content), "backup." + extension),
        },
        content_type="multipart/form-data",
    )


def database(application):
    db = sqlite3.connect(application.config["DATABASE"])
    db.row_factory = sqlite3.Row
    return db


@pytest.mark.parametrize("extension", ["csv", "xlsx", "json"])
def test_export_import_between_computers(application, tmp_path, extension):
    source = application.test_client()
    assert (
        upload(source, encode_file(sample_data(), extension), extension).status_code
        == 302
    )
    exported = source.get("/transfer/export/" + extension)
    assert exported.status_code == 200
    assert "attachment;" in exported.headers["Content-Disposition"]
    assert exported.headers["Cache-Control"] == "no-store"
    target_app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "target-test",
            "DATABASE": str(tmp_path / "target.sqlite3"),
        }
    )
    target = target_app.test_client()
    assert upload(target, exported.data, extension).status_code == 302
    assert upload(target, exported.data, extension).status_code == 302
    with database(target_app) as db:
        assert db.execute("SELECT count(*) FROM products").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM prices").fetchone()[0] == 2
        assert calculate(db, 1, "2026-01-01")[0]["total"] == Decimal("260.00")
    reopened = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "target-test",
            "DATABASE": target_app.config["DATABASE"],
        }
    )
    assert reopened.test_client().get("/transfer").status_code == 200
    with database(reopened) as db:
        assert calculate(db, 1, "2026-01-01")[0]["total"] == Decimal("260.00")


@pytest.mark.parametrize("extension", ["csv", "xlsx", "json"])
def test_templates_and_empty_exports(application, extension):
    client = application.test_client()
    empty = client.get("/transfer/export/" + extension)
    data = validate_data(
        decode_file(empty.data, extension), positive_decimal, parse_date
    )
    assert all(
        not data[table]
        for table in ["products", "stores", "baskets", "items", "prices"]
    )
    template = client.get("/transfer/export/" + extension + "?template=1")
    assert upload(client, template.data, extension).status_code == 302


def test_id_remapping_conflicts_and_modes(application):
    client = application.test_client()
    data = sample_data()
    assert upload(client, encode_file(data, "json"), "json").status_code == 302
    changed = copy.deepcopy(data)
    for row in changed["products"]:
        row["id"] += 100
    for table in ("items", "prices"):
        for row in changed[table]:
            row["product_id"] += 100
    changed["items"][0]["quantity"] = "3"
    changed["prices"][0]["price"] = "90.00"
    payload = encode_file(changed, "json")
    assert upload(client, payload, "json", "keep").status_code == 302
    with database(application) as db:
        assert calculate(db, 1, "2026-01-01")[0]["total"] == Decimal("260.00")
    assert upload(client, payload, "json", "update").status_code == 302
    with database(application) as db:
        assert calculate(db, 1, "2026-01-01")[0]["total"] == Decimal("370.00")
        assert db.execute("SELECT count(*) FROM products").fetchone()[0] == 2
    changed["products"][1]["unit"] = "кг"
    changed["stores"].append({"id": 2, "name": "Не должен сохраниться"})
    response = upload(client, encode_file(changed, "json"), "json", "update")
    assert response.status_code == 422
    with database(application) as db:
        assert db.execute("SELECT count(*) FROM stores").fetchone()[0] == 1
        assert calculate(db, 1, "2026-01-01")[0]["total"] == Decimal("370.00")


@pytest.mark.parametrize(
    "change",
    [
        "price",
        "quantity",
        "date",
        "link",
        "duplicate_id",
        "duplicate_name",
        "unit",
        "missing_field",
    ],
)
def test_invalid_records_rollback_everything(application, change):
    data = sample_data()
    if change == "price":
        data["prices"][1]["price"] = "-1"
    if change == "quantity":
        data["items"][1]["quantity"] = "NaN"
    if change == "date":
        data["prices"][1]["date"] = "2026-02-30"
    if change == "link":
        data["items"][1]["product_id"] = 999
    if change == "duplicate_id":
        data["products"][1]["id"] = 1
    if change == "duplicate_name":
        data["products"][1]["name"] = data["products"][0]["name"]
    if change == "unit":
        data["products"][1]["unit"] = "упаковка"
    if change == "missing_field":
        del data["products"][1]["name"]
    response = upload(
        application.test_client(), json.dumps(data, ensure_ascii=False).encode(), "json"
    )
    assert response.status_code == 422
    assert "Импорт отменён" in response.get_data(as_text=True)
    with database(application) as db:
        assert db.execute("SELECT count(*) FROM products").fetchone()[0] == 0


@pytest.mark.parametrize("extension", ["csv", "xlsx"])
def test_errors_include_sheet_or_csv_row(application, extension):
    data = sample_data()
    data["prices"][1]["price"] = "-1"
    response = upload(
        application.test_client(), encode_file(data, extension), extension
    )
    text = response.get_data(as_text=True)
    assert response.status_code == 422
    assert ("строка 9" if extension == "csv" else "Лист prices, строка 3") in text


@pytest.mark.parametrize("extension", ["csv", "xlsx", "json"])
def test_untrusted_spreadsheet_text_roundtrip(application, extension):
    data = sample_data()
    data["products"][0]["name"] = "=1+1"
    data["products"][1]["name"] = "'=literal"
    encoded = encode_file(data, extension)
    if extension == "csv":
        assert "'=1+1" in encoded.decode("utf-8-sig")
    if extension == "xlsx":
        workbook = load_workbook(io.BytesIO(encoded))
        assert workbook["products"]["B2"].data_type == "s"
        assert workbook["products"]["B2"].value == "=1+1"
        workbook.close()
    assert upload(application.test_client(), encoded, extension).status_code == 302
    with database(application) as db:
        assert [r[0] for r in db.execute("SELECT name FROM products ORDER BY id")] == [
            "=1+1",
            "'=literal",
        ]


def test_excel_formulas_rejected_and_excel_dates_supported(application):
    payload = encode_file(sample_data(), "xlsx")
    workbook = load_workbook(io.BytesIO(payload))
    workbook["prices"]["D2"] = datetime(2026, 1, 1)
    stream = io.BytesIO()
    workbook.save(stream)
    assert (
        upload(application.test_client(), stream.getvalue(), "xlsx").status_code == 302
    )
    workbook["products"]["B2"] = "=1+1"
    stream = io.BytesIO()
    workbook.save(stream)
    workbook.close()
    assert (
        upload(application.test_client(), stream.getvalue(), "xlsx").status_code == 422
    )


@pytest.mark.parametrize(
    "extension,payload",
    [
        ("xlsx", b"broken"),
        ("csv", b"wrong,header\n1,2"),
        ("json", b"{bad json"),
        ("json", b"null"),
        ("xls", b"old excel"),
    ],
)
def test_malformed_files(application, extension, payload):
    assert upload(application.test_client(), payload, extension).status_code == 422


def test_upload_limit_csrf_and_empty_file(application):
    client = application.test_client()
    assert client.post("/transfer", data={}).status_code == 400
    assert upload(client, b"", "json").status_code == 422
    assert upload(client, b"x" * (10 * 1024 * 1024), "json").status_code == 413
    assert client.get("/transfer/export/exe").status_code == 404
    assert (
        upload(
            client, encode_file(sample_data(), "json"), "json", "invalid"
        ).status_code
        == 422
    )


def test_product_creation_default_unit_and_repeat(application):
    client = application.test_client()
    client.get("/prices")
    with client.session_transaction() as session:
        token = session["csrf"]
    data = {"csrf": token, "action": "product", "name": "Картофель", "unit": "кг"}
    assert client.post("/prices", data=data).status_code == 302
    response = client.post("/prices", data=data, follow_redirects=True)
    assert response.status_code == 200
    assert "уже доступен в списке" in response.get_data(as_text=True)
    with database(application) as db:
        assert db.execute("SELECT count(*) FROM products").fetchone()[0] == 1
    assert client.post("/prices", data={**data, "unit": "л"}).status_code == 422
    assert client.post("/prices", data={**data, "name": " Морковь "}).status_code == 302
    with database(application) as db:
        assert db.execute("SELECT count(*) FROM products").fetchone()[0] == 2


def test_other_constraint_is_not_reported_as_duplicate(application):
    with database(application) as db:
        db.execute(
            "CREATE TRIGGER reject_product BEFORE INSERT ON products BEGIN SELECT RAISE(ABORT, 'test constraint'); END"
        )
    client = application.test_client()
    client.get("/prices")
    with client.session_transaction() as session:
        token = session["csrf"]
    response = client.post(
        "/prices",
        data={"csrf": token, "action": "product", "name": "Картофель", "unit": "кг"},
    )
    assert response.status_code == 422
    text = response.get_data(as_text=True)
    assert "уже существует" not in text
    assert "Запись не сохранена" in text
    with database(application) as db:
        assert db.execute("SELECT count(*) FROM products").fetchone()[0] == 0


def test_existing_case_distinct_names_remain_portable(application):
    data = sample_data()
    data["products"][0]["name"] = "Картофель"
    data["products"][1]["name"] = "картофель"
    client = application.test_client()
    assert upload(client, encode_file(data, "json"), "json").status_code == 302
    response = client.get("/transfer/export/xlsx")
    decoded = validate_data(
        decode_file(response.data, "xlsx"), positive_decimal, parse_date
    )
    assert [row["name"] for row in decoded["products"]] == ["Картофель", "картофель"]


def test_existing_database_stays_usable_after_upgrade(application):
    client = application.test_client()
    client.get("/prices")
    with client.session_transaction() as session:
        token = session["csrf"]
    assert (
        client.post(
            "/prices",
            data={
                "csrf": token,
                "action": "product",
                "name": "Старый продукт",
                "unit": "кг",
            },
        ).status_code
        == 302
    )
    upgraded = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "upgrade",
            "DATABASE": application.config["DATABASE"],
        }
    )
    response = upgraded.test_client().get("/prices")
    assert "Старый продукт" in response.get_data(as_text=True)
    assert (
        upgraded.test_client().get("/transfer/export/json").json["products"][0]["name"]
        == "Старый продукт"
    )


@pytest.mark.parametrize("extension", ["json", "csv", "xlsx"])
def test_graph_demo_files_ready_to_import(application, extension):
    from app import ROOT, cheapest

    data = (ROOT / "examples" / ("basket-data." + extension)).read_bytes()
    decoded_file = decode_file(data, extension)
    for table in ("products", "stores", "baskets"):
        assert all("учебн" not in row["name"].lower() for row in decoded_file[table])
    response = upload(application.test_client(), data, extension)
    assert response.status_code == 302
    with database(application) as db:
        assert db.execute("SELECT count(*) FROM products").fetchone()[0] == 10
        assert db.execute("SELECT count(*) FROM prices").fetchone()[0] == 145
        assert db.execute("SELECT count(DISTINCT date) FROM prices").fetchone()[0] == 5
        july = calculate(db, 1, "2026-07-01")
        october = calculate(db, 1, "2026-10-01")
        november = calculate(db, 1, "2026-11-01")
        assert cheapest(july)["name"] == "Лавка"
        assert cheapest(october)["name"] == "Маркет"
        assert cheapest(november)["total"] == Decimal("1819.00")
        assert not november[2]["complete"]
        assert november[2]["total"] < cheapest(november)["total"]
    generated = application.test_client().get(
        "/transfer/export/" + extension + "?demo=1"
    )
    assert generated.status_code == 200
    decoded = validate_data(
        decode_file(generated.data, extension), positive_decimal, parse_date
    )
    assert len(decoded["prices"]) == 145
