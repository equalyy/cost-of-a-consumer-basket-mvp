"""Portable full-dataset files. Foreign IDs are remapped during transactional merge."""

import csv
import io
import json
import sqlite3
from decimal import Decimal
from datetime import date, datetime
from zipfile import ZipFile

from openpyxl import Workbook, load_workbook

COLUMNS = {
    "products": ["id", "name", "unit"],
    "stores": ["id", "name"],
    "baskets": ["id", "name"],
    "items": ["id", "basket_id", "product_id", "quantity"],
    "prices": ["id", "store_id", "product_id", "date", "price"],
}
CSV_COLUMNS = [
    "entity",
    "id",
    "name",
    "unit",
    "basket_id",
    "product_id",
    "store_id",
    "quantity",
    "date",
    "price",
]
MIME = {
    "csv": "text/csv; charset=utf-8",
    "json": "application/json",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
MAX_ROWS = 50000


class TransferError(ValueError):
    def __init__(self, errors):
        self.errors = errors if isinstance(errors, list) else [errors]
        super().__init__(" ".join(self.errors))


def empty_data():
    return {"version": 1, **{table: [] for table in COLUMNS}}


def export_data(db):
    data = empty_data()
    for table in COLUMNS:
        for row in db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall():
            record = dict(row)
            if table == "prices":
                record["price"] = format(Decimal(record.pop("cents")) / 100, ".2f")
            data[table].append(record)
    return data


def spreadsheet_text(value):
    if isinstance(value, str) and (
        value.startswith("'")
        or value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r"))
        or value.startswith(("\t", "\r", "\n"))
    ):
        return "'" + value
    return value


def restore_text(value):
    if value.startswith("'") and spreadsheet_text(value[1:]) == value:
        return value[1:]
    return value


def encode_file(data, file_format):
    if file_format == "json":
        return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    if file_format == "csv":
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS, delimiter=";")
        writer.writeheader()
        for table, columns in COLUMNS.items():
            for row in data[table]:
                writer.writerow(
                    {
                        "entity": table,
                        **{key: spreadsheet_text(row[key]) for key in columns},
                    }
                )
        return stream.getvalue().encode("utf-8-sig")
    if file_format == "xlsx":
        workbook = Workbook()
        workbook.remove(workbook.active)
        for table, columns in COLUMNS.items():
            sheet = workbook.create_sheet(table)
            sheet.append(columns)
            sheet.freeze_panes = "A2"
            for row in data[table]:
                sheet.append([row[column] for column in columns])
                for cell in sheet[sheet.max_row]:
                    if isinstance(cell.value, str):
                        cell.data_type = "s"
            sheet.auto_filter.ref = sheet.dimensions
            for column in sheet.columns:
                sheet.column_dimensions[column[0].column_letter].width = 24
        stream = io.BytesIO()
        workbook.save(stream)
        workbook.close()
        return stream.getvalue()
    raise TransferError("Выберите CSV, XLSX или JSON.")


def decode_file(content, file_format):
    data = empty_data()
    if file_format == "json":
        try:
            data = json.loads(content.decode("utf-8-sig"), parse_float=Decimal)
        except (UnicodeError, ValueError, RecursionError):
            raise TransferError(
                "JSON повреждён. Используйте UTF-8 и структуру из шаблона."
            )
        return data
    if file_format == "csv":
        try:
            text = content.decode("utf-8-sig")
            try:
                dialect = csv.Sniffer().sniff(text[:8192], delimiters=";,")
            except csv.Error:
                dialect = None
            reader = csv.DictReader(
                io.StringIO(text, newline=""),
                delimiter=dialect.delimiter if dialect else ";",
            )
            if reader.fieldnames != CSV_COLUMNS:
                raise TransferError(
                    "CSV — неверные заголовки. Скачайте шаблон и сохраните файл в UTF-8."
                )
            for index, row in enumerate(reader, 2):
                if index > MAX_ROWS + 1:
                    raise TransferError(f"Файл содержит больше {MAX_ROWS} записей.")
                table = row.get("entity")
                if (
                    table not in COLUMNS
                    or None in row
                    or any(value is None for value in row.values())
                ):
                    raise TransferError(
                        f"CSV, строка {reader.line_num} — неверный тип записи или число столбцов."
                    )
                data[table].append(
                    {
                        **{key: restore_text(row[key]) for key in COLUMNS[table]},
                        "_location": f"CSV, строка {reader.line_num}",
                    }
                )
        except (UnicodeError, csv.Error):
            raise TransferError("CSV повреждён. Используйте кодировку UTF-8 и шаблон.")
        return data
    if file_format == "xlsx":
        try:
            with ZipFile(io.BytesIO(content)) as archive:
                if (
                    len(archive.infolist()) > 1000
                    or sum(entry.file_size for entry in archive.infolist())
                    > 50 * 1024 * 1024
                ):
                    raise TransferError(
                        "XLSX слишком большой после распаковки. Максимум 50 МБ."
                    )
            workbook = load_workbook(
                io.BytesIO(content), read_only=True, data_only=False
            )
            try:
                if set(workbook.sheetnames) != set(COLUMNS):
                    raise TransferError(
                        "XLSX должен содержать пять листов из шаблона — products, stores, baskets, items, prices."
                    )
                count = 0
                for table, columns in COLUMNS.items():
                    if (
                        workbook[table].max_row
                        and workbook[table].max_row > MAX_ROWS + 1
                    ):
                        raise TransferError(
                            f"Лист {table} содержит больше {MAX_ROWS} строк."
                        )
                    rows = workbook[table].iter_rows()
                    header = next(rows, ())
                    if [cell.value for cell in header] != columns:
                        raise TransferError(
                            f"Лист {table}, строка 1 — неверные заголовки. Используйте шаблон."
                        )
                    for index, cells in enumerate(rows, 2):
                        if all(cell.value is None for cell in cells):
                            continue
                        count += 1
                        if count > MAX_ROWS:
                            raise TransferError(
                                f"Файл содержит больше {MAX_ROWS} записей."
                            )
                        if any(cell.data_type in ("f", "e") for cell in cells):
                            raise TransferError(
                                f"Лист {table}, строка {index} — замените формулы и ошибки Excel обычными значениями."
                            )
                        if len(cells) != len(columns):
                            raise TransferError(
                                f"Лист {table}, строка {index} — неверное число столбцов."
                            )
                        data[table].append(
                            {
                                **dict(zip(columns, [cell.value for cell in cells])),
                                "_location": f"Лист {table}, строка {index}",
                            }
                        )
            finally:
                workbook.close()
        except TransferError:
            raise
        except Exception as error:
            # Parser errors may come from XML backends with different exception types.
            raise TransferError(
                "XLSX повреждён или имеет неподдерживаемую структуру. Используйте шаблон."
            ) from error
        return data
    raise TransferError("Поддерживаются только файлы .csv, .xlsx и .json.")


def validate_data(data, positive_decimal, parse_date):
    if (
        not isinstance(data, dict)
        or type(data.get("version")) is not int
        or data["version"] != 1
    ):
        raise TransferError("Ожидается формат версии 1. Скачайте файл-шаблон.")
    if any(not isinstance(data.get(table), list) for table in COLUMNS):
        raise TransferError("Файл должен содержать все пять разделов из шаблона.")
    if sum(len(data[table]) for table in COLUMNS) > MAX_ROWS:
        raise TransferError(f"Файл содержит больше {MAX_ROWS} записей.")
    errors, ids, natural_keys = [], {}, {}
    result = empty_data()
    for table, columns in COLUMNS.items():
        ids[table], natural_keys[table] = set(), set()
        for index, source in enumerate(data[table], 1):
            location = (
                source.get("_location", f"{table}, запись {index}")
                if isinstance(source, dict)
                else f"{table}, запись {index}"
            )
            try:
                if not isinstance(source, dict) or any(
                    key not in source for key in columns
                ):
                    raise ValueError("Заполните все обязательные поля из шаблона.")
                row = {key: source[key] for key in columns}
                row["_location"] = location
                for key in columns:
                    if key == "id" or key.endswith("_id"):
                        raw = row[key]
                        if (
                            isinstance(raw, bool)
                            or not str(raw).isdigit()
                            or not 0 < int(raw) <= 9223372036854775807
                        ):
                            raise ValueError(
                                f"{key} должен быть положительным целым ID."
                            )
                        row[key] = int(raw)
                if row["id"] in ids[table]:
                    raise ValueError("ID повторяется внутри раздела.")
                if "name" in row:
                    if (
                        not isinstance(row["name"], str)
                        or not row["name"].strip()
                        or len(row["name"].strip()) > 80
                    ):
                        raise ValueError(
                            "Название должно содержать от 1 до 80 символов."
                        )
                    row["name"] = row["name"].strip()
                    natural_key = row["name"]
                elif table == "items":
                    natural_key = (row["basket_id"], row["product_id"])
                    row["quantity"] = str(
                        positive_decimal(str(row["quantity"]), "Количество")
                    )
                else:
                    date_value = row["date"]
                    if isinstance(date_value, datetime):
                        date_value = date_value.date()
                    row["date"] = parse_date(
                        date_value.isoformat()
                        if isinstance(date_value, date)
                        else str(date_value)
                    )
                    natural_key = (row["store_id"], row["product_id"], row["date"])
                    row["cents"] = int(
                        positive_decimal(str(row.pop("price")), "Цена", 2) * 100
                    )
                if table == "products" and row["unit"] not in ("кг", "л", "шт."):
                    raise ValueError("Единица продукта — кг, л или шт.")
                if natural_key in natural_keys[table]:
                    raise ValueError(
                        "Запись с таким названием или сочетанием связей повторяется."
                    )
                ids[table].add(row["id"])
                natural_keys[table].add(natural_key)
                result[table].append(row)
            except (ValueError, TypeError) as error:
                errors.append(f"{location} — {error}")
                if len(errors) >= 50:
                    raise TransferError(errors)
    for table, links in [
        ("items", {"basket_id": "baskets", "product_id": "products"}),
        ("prices", {"store_id": "stores", "product_id": "products"}),
    ]:
        for row in result[table]:
            for column, target in links.items():
                if row[column] not in ids[target]:
                    errors.append(
                        f"{row['_location']} — {column}={row[column]} отсутствует в разделе {target} файла."
                    )
    if errors:
        raise TransferError(errors[:50])
    return result


def merge_data(db, data, mode):
    if mode not in ("keep", "update"):
        raise TransferError("Выберите способ обработки совпадающих записей.")
    counts = {"added": 0, "updated": 0, "skipped": 0}
    mapping = {}
    try:
        db.execute("BEGIN IMMEDIATE")
        for table in ("products", "stores", "baskets"):
            mapping[table] = {}
            existing = {
                row["name"].strip(): dict(row)
                for row in db.execute(f"SELECT * FROM {table}")
            }
            for row in data[table]:
                match = existing.get(row["name"])
                if match:
                    if table == "products" and match["unit"] != row["unit"]:
                        raise TransferError(
                            f"{row['_location']} — у продукта «{row['name']}» уже установлена единица {match['unit']}. Исправьте единицу в файле или используйте другое название."
                        )
                    mapping[table][row["id"]] = match["id"]
                    counts["skipped"] += 1
                else:
                    fields = COLUMNS[table][1:]
                    placeholders = ",".join("?" for field in fields)
                    cursor = db.execute(
                        f"INSERT INTO {table}({','.join(fields)}) VALUES ({placeholders})",
                        [row[key] for key in fields],
                    )
                    mapping[table][row["id"]] = cursor.lastrowid
                    existing[row["name"]] = dict(row, id=cursor.lastrowid)
                    counts["added"] += 1
        for table in ("items", "prices"):
            for row in data[table]:
                if table == "items":
                    links = {
                        "basket_id": mapping["baskets"][row["basket_id"]],
                        "product_id": mapping["products"][row["product_id"]],
                    }
                    values = {"quantity": row["quantity"]}
                else:
                    links = {
                        "store_id": mapping["stores"][row["store_id"]],
                        "product_id": mapping["products"][row["product_id"]],
                        "date": row["date"],
                    }
                    values = {"cents": row["cents"]}
                where = " AND ".join(f"{column}=?" for column in links)
                match = db.execute(
                    f"SELECT * FROM {table} WHERE {where}", list(links.values())
                ).fetchone()
                if match is None:
                    fields = {**links, **values}
                    placeholders = ",".join("?" for field in fields)
                    db.execute(
                        f"INSERT INTO {table}({','.join(fields)}) VALUES ({placeholders})",
                        list(fields.values()),
                    )
                    counts["added"] += 1
                elif mode == "update" and any(
                    str(match[key]) != str(value) for key, value in values.items()
                ):
                    assignments = ",".join(f"{column}=?" for column in values)
                    db.execute(
                        f"UPDATE {table} SET {assignments} WHERE id=?",
                        [*values.values(), match["id"]],
                    )
                    counts["updated"] += 1
                else:
                    counts["skipped"] += 1
        db.commit()
    except (TransferError, sqlite3.Error):
        db.rollback()
        raise
    return counts


def sample_data():
    return {
        "version": 1,
        "products": [
            {"id": 1, "name": "Картофель", "unit": "кг"},
            {"id": 2, "name": "Молоко", "unit": "л"},
        ],
        "stores": [{"id": 1, "name": "Магазин"}],
        "baskets": [{"id": 1, "name": "Корзина"}],
        "items": [
            {"id": 1, "basket_id": 1, "product_id": 1, "quantity": "2"},
            {"id": 2, "basket_id": 1, "product_id": 2, "quantity": "1"},
        ],
        "prices": [
            {
                "id": 1,
                "store_id": 1,
                "product_id": 1,
                "date": "2026-01-01",
                "price": "80.00",
            },
            {
                "id": 2,
                "store_id": 1,
                "product_id": 2,
                "date": "2026-01-01",
                "price": "100.00",
            },
        ],
    }


def demo_data():
    """Fictional learning dataset with changing winners and an incomplete store."""
    products = [
        ("Картофель", "кг", "3", "55"),
        ("Молоко", "л", "2", "95"),
        ("Хлеб", "шт.", "2", "52"),
        ("Яйца", "шт.", "10", "12"),
        ("Куриное филе", "кг", "1", "390"),
        ("Рис", "кг", "1", "115"),
        ("Яблоки", "кг", "2", "135"),
        ("Сливочное масло", "кг", "0.2", "1100"),
        ("Сахар", "кг", "1", "78"),
        ("Морковь", "кг", "1", "48"),
    ]
    dates = ["2026-07-01", "2026-08-01", "2026-09-01", "2026-10-01", "2026-11-01"]
    shops = [
        ("Лавка", ["1", "1.06", "1.02", "1.11", "1.07"]),
        ("Маркет", ["1.12", "1.05", "1.09", "1.03", "1.14"]),
        ("Дискаунтер", ["0.9", "0.95", "0.97", "1", "0.96"]),
    ]
    data = empty_data()
    data["baskets"] = [{"id": 1, "name": "Корзина для графиков"}]
    for product_id, (name, unit, quantity, base_price) in enumerate(products, 1):
        data["products"].append({"id": product_id, "name": name, "unit": unit})
        data["items"].append(
            {
                "id": product_id,
                "basket_id": 1,
                "product_id": product_id,
                "quantity": quantity,
            }
        )
    for store_id, (name, factors) in enumerate(shops, 1):
        data["stores"].append({"id": store_id, "name": name})
        for day, factor in zip(dates, factors):
            for product_id, product in enumerate(products, 1):
                if store_id == 3 and product_id == 2:
                    continue
                price = (Decimal(product[3]) * Decimal(factor)).quantize(
                    Decimal("0.01")
                )
                data["prices"].append(
                    {
                        "id": len(data["prices"]) + 1,
                        "store_id": store_id,
                        "product_id": product_id,
                        "date": day,
                        "price": str(price),
                    }
                )
    return data
