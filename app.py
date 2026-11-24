import hmac
import io
import os
import secrets
import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from time import time

import click
from itsdangerous import BadSignature, URLSafeTimedSerializer
from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    send_file,
    url_for,
)

from launcher import instance_id, source_version
from database_migrations import repair_windows_units, normalize_legacy_demo_names

from data_transfer import (
    MIME,
    TransferError,
    decode_file,
    demo_data,
    encode_file,
    export_data,
    merge_data,
    sample_data,
    validate_data,
)

ROOT = Path(__file__).resolve().parent
CENT = Decimal("0.01")


def money(value):
    if value is None:
        return "—"
    return f"{Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP):,.2f}".replace(
        ",", "\u202f"
    ).replace(".", ",")


def positive_decimal(raw, label, places=3):
    try:
        value = Decimal(raw.replace(",", ".").strip())
    except (InvalidOperation, AttributeError):
        raise ValueError(f"{label} должно быть числом.")
    if not value.is_finite() or value <= 0 or value > Decimal("1000000"):
        raise ValueError(f"{label} должно быть больше 0 и не больше 1 000 000.")
    if value.as_tuple().exponent < -places:
        raise ValueError(f"{label} — максимум {places} знака после запятой.")
    return value


def parse_date(raw):
    try:
        parsed = date.fromisoformat(raw)
        if parsed.isoformat() != raw:
            raise ValueError
        return raw
    except (ValueError, TypeError):
        raise ValueError("Укажите дату в формате ГГГГ-ММ-ДД.")


def calculate(db, basket_id, on_date):
    items = db.execute(
        "SELECT i.*, p.name, p.unit FROM items i JOIN products p ON p.id=i.product_id WHERE basket_id=? ORDER BY i.id",
        (basket_id,),
    ).fetchall()
    results = []
    for store in db.execute("SELECT * FROM stores ORDER BY id").fetchall():
        lines, total, missing = [], Decimal(0), []
        for item in items:
            price = db.execute(
                "SELECT * FROM prices WHERE store_id=? AND product_id=? AND date<=? ORDER BY date DESC LIMIT 1",
                (store["id"], item["product_id"], on_date),
            ).fetchone()
            cost = (
                Decimal(item["quantity"]) * Decimal(price["cents"]) / 100
                if price
                else None
            )
            if cost is None:
                missing.append(item["name"])
            else:
                total += cost
            lines.append(
                dict(
                    item,
                    price=Decimal(price["cents"]) / 100 if price else None,
                    cost=cost,
                    price_date=price["date"] if price else None,
                )
            )
        results.append(
            dict(
                store,
                total=total.quantize(CENT, rounding=ROUND_HALF_UP),
                complete=bool(items) and not missing,
                known_count=len(items) - len(missing),
                missing=missing,
                lines=lines,
            )
        )
    return results


def cheapest(results):
    complete = [row for row in results if row["complete"]]
    return min(complete, key=lambda row: row["total"]) if complete else None


def difference(old, new):
    if old is None or new is None or old == 0:
        return None
    return {"amount": new - old, "percent": (new - old) / old * 100}


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        DATABASE=os.environ.get(
            "BASKET_DATABASE", str(ROOT / "instance" / "basket.sqlite3")
        ),
        SECRET_KEY=os.environ.get("SECRET_KEY"),
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if test_config:
        app.config.update(test_config)
    app.config["SOURCE_VERSION"] = source_version()
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    if not app.config["SECRET_KEY"]:
        key_path = ROOT / "instance" / "session.key"
        key_path.parent.mkdir(exist_ok=True)
        try:
            descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "w") as key_file:
                key_file.write(secrets.token_hex(32))
        app.config["SECRET_KEY"] = key_path.read_text()
    reset_signer = URLSafeTimedSerializer(
        app.config["SECRET_KEY"], salt="clear-data-confirmation"
    )
    app.jinja_env.filters["money"] = money
    app.jinja_env.filters["cents"] = lambda value: money(Decimal(value) / 100)
    app.jinja_env.filters["qty"] = lambda value: format(
        Decimal(value).normalize(), "f"
    ).replace(".", ",")
    app.jinja_env.filters["day"] = lambda value: date.fromisoformat(value).strftime(
        "%d.%m.%Y"
    )
    app.jinja_env.filters["signed"] = lambda value: ("+" if value > 0 else "") + money(
        value
    )

    def db():
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DATABASE"])
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys=ON")
        return g.db

    @app.teardown_appcontext
    def close_db(exception):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    with app.app_context():
        db().executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
        db().commit()
        app.config["DATABASE_REPAIR_BACKUP"] = repair_windows_units(
            db(), app.config["DATABASE"]
        )
        app.config["DEMO_NAMES_BACKUP"] = normalize_legacy_demo_names(
            db(), app.config["DATABASE"]
        )

    @app.before_request
    def csrf_protection():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(32)
        if app.config.get("DATABASE_REPAIR_BACKUP") and not session.get(
            "utf8_repair_seen"
        ):
            flash(
                "Исправлена кодировка ограничений старой базы. Продукты можно добавлять. Резервная копия базы сохранена рядом с исходным файлом.",
                "success",
            )
            session["utf8_repair_seen"] = True
        if app.config.get("DEMO_NAMES_BACKUP") and not session.get("demo_names_seen"):
            flash(
                "Обновлены названия старых примеров. Данные и связи сохранены. Резервная копия базы создана рядом с исходным файлом.",
                "success",
            )
            session["demo_names_seen"] = True
        if request.method == "POST" and not hmac.compare_digest(
            session["csrf"], request.form.get("csrf", "")
        ):
            abort(
                400,
                description="Сессия формы истекла. Обновите страницу и повторите действие.",
            )

    @app.context_processor
    def common():
        return {"csrf": session.get("csrf", ""), "nav": request.endpoint}

    def basket_selection():
        baskets = db().execute("SELECT * FROM baskets ORDER BY id").fetchall()
        selected = request.args.get("basket", type=int)
        basket = next(
            (b for b in baskets if b["id"] == selected), baskets[0] if baskets else None
        )
        return baskets, basket

    def selection():
        baskets, basket = basket_selection()
        latest = (
            db().execute("SELECT MAX(date) FROM prices").fetchone()[0]
            or date.today().isoformat()
        )
        end = request.args.get("date", latest)
        start = request.args.get(
            "start",
            db().execute("SELECT MIN(date) FROM prices").fetchone()[0] or latest,
        )
        try:
            parse_date(end)
            parse_date(start)
            if start > end:
                raise ValueError("Начало периода должно быть не позже окончания.")
        except ValueError as error:
            flash(str(error), "error")
            start = end = latest
        return baskets, basket, start, end

    @app.get("/health")
    def health():
        return {
            "application": "consumer-basket",
            "instance": instance_id(app.config["DATABASE"]),
            "source_version": app.config["SOURCE_VERSION"],
        }

    @app.get("/")
    def comparison():
        baskets, basket, start, end = selection()
        rows = calculate(db(), basket["id"], end) if basket else []
        old = calculate(db(), basket["id"], start) if basket else []
        best, old_best = cheapest(rows), cheapest(old)
        delta = (
            difference(old_best["total"], best["total"]) if best and old_best else None
        )
        full = [r for r in rows if r["complete"]]
        saving = max(r["total"] for r in full) - best["total"] if best else None
        chart = {
            "labels": [r["name"] for r in rows],
            "values": [float(r["total"]) for r in rows],
            "complete": [r["complete"] for r in rows],
            "best": best["id"] if best else None,
            "ids": [r["id"] for r in rows],
        }
        return render_template(
            "comparison.html",
            baskets=baskets,
            basket=basket,
            start=start,
            end=end,
            rows=rows,
            best=best,
            delta=delta,
            saving=saving,
            full=full,
            chart=chart,
        )

    def receipt_context(basket):
        with db():
            db().execute("BEGIN")
            stores = db().execute("SELECT * FROM stores ORDER BY id").fetchall()
            store_ids = {store["id"] for store in stores}
            latest = (
                db().execute("SELECT max(date) FROM prices").fetchone()[0]
                or date.today().isoformat()
            )
            receipt_errors = []
            try:
                on_date = parse_date(request.args.get("date", latest))
            except ValueError as error:
                receipt_errors.append(str(error))
                on_date = latest
            selected_ids, selected = [], set()
            for index in range(1, 4):
                raw = request.args.get(f"shop{index}", "")
                store_id = request.args.get(f"shop{index}", type=int)
                if raw and store_id not in store_ids:
                    receipt_errors.append(
                        f"Магазин в столбце {index} недоступен. Выберите существующий магазин."
                    )
                    store_id = None
                if store_id is not None and store_id in selected:
                    receipt_errors.append("Выберите разные магазины в трёх столбцах.")
                    store_id = None
                selected_ids.append(store_id)
                selected.add(store_id)
            quotes = {row["id"]: row for row in calculate(db(), basket["id"], on_date)}
            items = (
                db()
                .execute(
                    "SELECT i.*,p.name,p.unit FROM items i JOIN products p ON p.id=i.product_id WHERE basket_id=? ORDER BY i.id",
                    (basket["id"],),
                )
                .fetchall()
            )
            slots = [
                {"number": index + 1, "quote": quotes.get(store_id)}
                for index, store_id in enumerate(selected_ids)
            ]
            rows, mixed_total, mixed_missing = [], Decimal(0), []
            for index, item in enumerate(items):
                cells = [
                    slot["quote"]["lines"][index] if slot["quote"] else None
                    for slot in slots
                ]
                prices = [
                    cell["price"]
                    for cell in cells
                    if cell is not None and cell["price"] is not None
                ]
                minimum = min(prices) if prices else None
                if minimum is None:
                    mixed_missing.append(item["name"])
                else:
                    mixed_total += Decimal(item["quantity"]) * minimum
                rows.append({"item": item, "cells": cells, "minimum": minimum})
            return {
                "basket": basket,
                "items": items,
                "stores": stores,
                "slots": slots,
                "receipt_rows": rows,
                "receipt_date": on_date,
                "receipt_errors": receipt_errors,
                "receipt_params": {
                    "date": on_date,
                    **{
                        f"shop{index + 1}": store_id or ""
                        for index, store_id in enumerate(selected_ids)
                    },
                },
                "has_selected_stores": any(selected_ids),
                "mixed": {
                    "total": mixed_total.quantize(CENT, rounding=ROUND_HALF_UP),
                    "missing": mixed_missing,
                    "known_count": len(items) - len(mixed_missing),
                    "complete": bool(items) and not mixed_missing,
                },
            }

    @app.get("/baskets/<int:basket_id>/receipt")
    def basket_receipt(basket_id):
        basket = (
            db().execute("SELECT * FROM baskets WHERE id=?", (basket_id,)).fetchone()
        )
        if basket is None:
            abort(404)
        response = app.make_response(
            render_template("basket_receipt.html", **receipt_context(basket))
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/baskets", methods=["GET", "POST"])
    def baskets():
        errors = {}
        view_params = {
            name: request.args[name]
            for name in ("date", "shop1", "shop2", "shop3")
            if name in request.args
        }
        if request.method == "POST":
            action = request.form.get("action")
            try:
                if action == "create":
                    name = checked_name("name", "Название корзины")
                    cursor = db().execute(
                        "INSERT INTO baskets(name) VALUES (?)", (name,)
                    )
                    db().commit()
                    flash("Корзина создана.", "success")
                    return redirect(
                        url_for("baskets", basket=cursor.lastrowid, **view_params)
                    )
                basket_id = require_id("basket_id", "baskets")
                if action == "delete":
                    db().execute("DELETE FROM baskets WHERE id=?", (basket_id,))
                elif action == "item":
                    product_id = require_id("product_id", "products")
                    quantity = positive_decimal(
                        request.form.get("quantity"), "Количество"
                    )
                    db().execute(
                        "INSERT INTO items(basket_id,product_id,quantity) VALUES (?,?,?) ON CONFLICT(basket_id,product_id) DO UPDATE SET quantity=excluded.quantity",
                        (basket_id, product_id, str(quantity)),
                    )
                elif action == "remove":
                    db().execute(
                        "DELETE FROM items WHERE id=? AND basket_id=?",
                        (request.form.get("item_id"), basket_id),
                    )
                else:
                    raise ValueError("Неизвестное действие.")
                db().commit()
                flash("Состав корзины сохранён.", "success")
                return redirect(url_for("baskets", basket=basket_id, **view_params))
            except (ValueError, sqlite3.IntegrityError) as error:
                db().rollback()
                errors[action] = (
                    "Корзина с таким названием уже существует."
                    if isinstance(error, sqlite3.IntegrityError)
                    else str(error)
                )
        all_baskets, basket = basket_selection()
        context = (
            receipt_context(basket)
            if basket
            else {"basket": None, "items": [], "receipt_params": {}}
        )
        return render_template(
            "baskets.html",
            baskets=all_baskets,
            **context,
            products=db().execute("SELECT * FROM products ORDER BY name").fetchall(),
            errors=errors,
        ), 422 if errors else 200

    def checked_name(field, label):
        value = request.form.get(field, "").strip()
        if not value or len(value) > 80:
            raise ValueError(f"{label} — от 1 до 80 символов.")
        return value

    def require_id(field, table):
        # Table names are fixed internal constants, values always use SQL parameters.
        value = request.form.get(field, type=int)
        if (
            table not in ("products", "stores", "baskets")
            or not db()
            .execute(f"SELECT id FROM {table} WHERE id=?", (value,))
            .fetchone()
        ):
            raise ValueError("Выберите существующую запись.")
        return value

    @app.route("/prices", methods=["GET", "POST"])
    def prices():
        errors = {}
        if request.method == "POST":
            action = request.form.get("action")
            try:
                if action == "store":
                    db().execute(
                        "INSERT INTO stores(name) VALUES (?)",
                        (checked_name("name", "Название магазина"),),
                    )
                elif action == "product":
                    unit = request.form.get("unit")
                    if unit not in ("кг", "л", "шт."):
                        raise ValueError("Выберите единицу измерения.")
                    name = checked_name("name", "Название продукта")
                    existing = (
                        db()
                        .execute("SELECT * FROM products WHERE name=?", (name,))
                        .fetchone()
                    )
                    if existing:
                        if existing["unit"] != unit:
                            raise ValueError(
                                f"Продукт «{existing['name']}» уже есть с единицей {existing['unit']}. "
                                "Выберите эту единицу или укажите другое название."
                            )
                        flash(
                            f"Продукт «{existing['name']}» уже доступен в списке. Можно внести его цену или добавить в корзину.",
                            "success",
                        )
                        return redirect(
                            url_for("prices", selected_product=existing["id"])
                        )
                    db().execute(
                        "INSERT INTO products(name,unit) VALUES (?,?)", (name, unit)
                    )
                elif action == "price":
                    store_id = require_id("store_id", "stores")
                    product_id = require_id("product_id", "products")
                    on_date = parse_date(request.form.get("date"))
                    cents = int(
                        positive_decimal(request.form.get("price"), "Цена", 2) * 100
                    )
                    db().execute(
                        "INSERT INTO prices(store_id,product_id,date,cents) VALUES (?,?,?,?) ON CONFLICT(store_id,product_id,date) DO UPDATE SET cents=excluded.cents",
                        (store_id, product_id, on_date, cents),
                    )
                elif action == "delete":
                    db().execute(
                        "DELETE FROM prices WHERE id=?",
                        (request.form.get("price_id", type=int),),
                    )
                else:
                    raise ValueError("Неизвестное действие.")
                db().commit()
                flash("Данные сохранены.", "success")
                return redirect(url_for("prices"))
            except (ValueError, sqlite3.IntegrityError) as error:
                db().rollback()
                if isinstance(error, sqlite3.IntegrityError):
                    if (
                        getattr(error, "sqlite_errorname", "")
                        == "SQLITE_CONSTRAINT_UNIQUE"
                    ):
                        errors[action] = (
                            "Название уже используется. Найдите запись в справочнике ниже."
                        )
                    else:
                        app.logger.warning(
                            "Record validation failed for %s: %s", action, error
                        )
                        errors[action] = (
                            "Запись не сохранена. Проверьте обязательные поля, единицу измерения и связанные записи."
                        )
                else:
                    errors[action] = str(error)
        stores = db().execute("SELECT * FROM stores ORDER BY id").fetchall()
        products = db().execute("SELECT * FROM products ORDER BY name").fetchall()
        store_id = request.args.get("store", type=int)
        product_id = request.args.get("product", type=int)
        records = (
            db()
            .execute(
                "SELECT r.*,s.name store,p.name product,p.unit FROM prices r JOIN stores s ON r.store_id=s.id JOIN products p ON r.product_id=p.id WHERE (? IS NULL OR s.id=?) AND (? IS NULL OR p.id=?) ORDER BY r.date DESC,s.id,p.name",
                (store_id, store_id, product_id, product_id),
            )
            .fetchall()
        )
        return render_template(
            "prices.html",
            stores=stores,
            products=products,
            records=records,
            store_id=store_id,
            product_id=product_id,
            today=date.today().isoformat(),
            selected_product=request.args.get("selected_product", type=int),
            errors=errors,
        ), 422 if errors else 200

    @app.errorhandler(413)
    def file_too_large(error):
        return render_template(
            "transfer.html",
            errors=["Файл слишком большой. Максимум 10 МБ."],
            counts=transfer_counts(),
        ), 413

    def transfer_counts():
        return {
            table: db().execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("products", "stores", "baskets", "items", "prices")
        }

    @app.route("/transfer", methods=["GET", "POST"])
    def transfer():
        errors = []
        if request.method == "POST":
            uploaded = request.files.get("file")
            try:
                if not uploaded or not uploaded.filename:
                    raise TransferError("Выберите файл CSV, XLSX или JSON.")
                file_format = Path(uploaded.filename).suffix.lower().lstrip(".")
                if file_format not in MIME:
                    raise TransferError(
                        "Поддерживаются .csv, .xlsx и .json. Старый формат .xls сохраните как .xlsx."
                    )
                data = validate_data(
                    decode_file(uploaded.read(), file_format),
                    positive_decimal,
                    parse_date,
                )
                summary = merge_data(db(), data, request.form.get("mode", "keep"))
                flash(
                    f"Импорт завершён. Добавлено — {summary['added']}, обновлено — {summary['updated']}, без изменений — {summary['skipped']}.",
                    "success",
                )
                return redirect(url_for("transfer"))
            except TransferError as error:
                errors = error.errors
            except sqlite3.Error:
                db().rollback()
                app.logger.exception("Data import failed")
                errors = [
                    "Импорт отменён. База данных занята или содержит конфликтующие записи. Повторите попытку после завершения других операций."
                ]
        return render_template(
            "transfer.html",
            counts=transfer_counts(),
            errors=errors,
            mode=request.form.get("mode", "keep"),
        ), 422 if errors else 200

    @app.post("/data/reset/prepare")
    def prepare_reset():
        with db():
            db().execute(
                "INSERT OR IGNORE INTO metadata(key,value) VALUES ('reset_generation',?)",
                (secrets.token_hex(32),),
            )
            generation = (
                db()
                .execute("SELECT value FROM metadata WHERE key='reset_generation'")
                .fetchone()[0]
            )
        confirmation = reset_signer.dumps(
            {
                "generation": generation,
                "csrf": session["csrf"],
                "issued_at": time(),
            }
        )
        response = app.json.response({"confirmation": confirmation, "wait_seconds": 3})
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/data/reset")
    def reset_data():
        try:
            confirmation = reset_signer.loads(
                request.form.get("confirmation", ""), max_age=300
            )
            age = time() - confirmation["issued_at"]
            if not hmac.compare_digest(confirmation["csrf"], session["csrf"]):
                raise ValueError("Откройте окно подтверждения заново.")
            if age < 3:
                raise ValueError("Подождите 3 секунды перед удалением данных.")
            if age > 300:
                raise ValueError("Подтверждение истекло. Откройте окно очистки заново.")
        except BadSignature:
            flash(
                "Подтверждение недействительно или истекло. Откройте окно очистки заново.",
                "error",
            )
            return redirect(url_for("transfer"), code=303)
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("transfer"), code=303)
        try:
            db().execute("BEGIN IMMEDIATE")
            generation = (
                db()
                .execute("SELECT value FROM metadata WHERE key='reset_generation'")
                .fetchone()
            )
            if not generation or not hmac.compare_digest(
                generation[0], confirmation["generation"]
            ):
                db().rollback()
                flash(
                    "Это подтверждение уже использовано. Откройте окно очистки заново.",
                    "error",
                )
                return redirect(url_for("transfer"), code=303)
            for table in (
                "items",
                "prices",
                "baskets",
                "products",
                "stores",
                "metadata",
            ):
                db().execute(f"DELETE FROM {table}")
            db().commit()
        except sqlite3.Error:
            db().rollback()
            app.logger.exception("Full data reset failed")
            flash(
                "Очистка отменена. Данные сохранены. Повторите попытку после завершения других операций.",
                "error",
            )
            return redirect(url_for("transfer"), code=303)
        session["csrf"] = secrets.token_hex(32)
        flash(
            "Все продукты, магазины, корзины и цены удалены из базы приложения.",
            "success",
        )
        return redirect(url_for("transfer"), code=303)

    @app.get("/transfer/export/<file_format>")
    def download_data(file_format):
        if file_format not in MIME:
            abort(404)
        is_demo = request.args.get("demo") == "1"
        is_template = request.args.get("template") == "1"
        if is_demo:
            data = demo_data()
        elif is_template:
            data = sample_data()
        else:
            # A read transaction keeps related tables in a single consistent snapshot.
            with db():
                db().execute("BEGIN")
                data = export_data(db())
        payload = encode_file(data, file_format)
        prefix = (
            "basket-data"
            if is_demo
            else "basket-template"
            if is_template
            else "basket-backup"
        )
        response = send_file(
            io.BytesIO(payload),
            mimetype=MIME[file_format],
            as_attachment=True,
            download_name=f"{prefix}-{date.today().isoformat()}.{file_format}",
            max_age=0,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/dynamics")
    def dynamics():
        baskets, basket, start, end = selection()
        rows, chart = [], {"labels": [], "datasets": []}
        if basket:
            dates = sorted(
                set(
                    [start, end]
                    + [
                        r[0]
                        for r in db()
                        .execute(
                            "SELECT DISTINCT date FROM prices WHERE date>=? AND date<=?",
                            (start, end),
                        )
                        .fetchall()
                    ]
                )
            )
            snapshots = [calculate(db(), basket["id"], day) for day in dates]
            chart["labels"] = dates
            for index, store in enumerate(snapshots[-1]):
                first = snapshots[0][index]
                delta = (
                    difference(first["total"], store["total"])
                    if first["complete"] and store["complete"]
                    else None
                )
                rows.append(
                    dict(
                        store,
                        old=first["total"] if first["known_count"] else None,
                        old_complete=first["complete"],
                        old_missing=first["missing"],
                        delta=delta,
                    )
                )
                chart["datasets"].append(
                    {
                        "store_name": store["name"],
                        "label": store["name"]
                        + (
                            " · известные позиции"
                            if any(
                                not snapshot[index]["complete"]
                                and snapshot[index]["known_count"]
                                for snapshot in snapshots
                            )
                            else ""
                        ),
                        "data": [
                            float(snapshot[index]["total"])
                            if snapshot[index]["known_count"]
                            else None
                            for snapshot in snapshots
                        ],
                        "complete": [
                            snapshot[index]["complete"] for snapshot in snapshots
                        ],
                        "missing": [
                            snapshot[index]["missing"] for snapshot in snapshots
                        ],
                    }
                )
        return render_template(
            "dynamics.html",
            baskets=baskets,
            basket=basket,
            start=start,
            end=end,
            rows=rows,
            chart=chart,
        )

    @app.cli.command("init-db")
    def init_db():
        """Create missing tables without deleting existing data."""
        click.echo("База данных готова.")

    @app.cli.command("seed-demo")
    def seed_demo():
        """Add fictional educational data once, without overwriting user records."""
        connection = db()
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM metadata WHERE key='demo_v1'").fetchone():
            click.echo("Демонстрационные данные уже загружены.")
            connection.rollback()
            return
        if any(
            connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            for table in ("baskets", "products", "stores", "prices")
        ):
            connection.rollback()
            raise click.ClickException(
                "База содержит рабочие данные. Для демонстрации задайте отдельный BASKET_DATABASE. Данные сохранены."
            )
        products = [
            ("Картофель", "кг", "3", 55),
            ("Молоко", "л", "2", 95),
            ("Хлеб пшеничный", "шт.", "2", 52),
            ("Яйца", "шт.", "10", 12),
            ("Куриное филе", "кг", "1", 390),
            ("Рис", "кг", "1", 115),
            ("Яблоки", "кг", "2", 135),
            ("Масло сливочное", "кг", "0.2", 1100),
            ("Сахар", "кг", "1", 78),
            ("Морковь", "кг", "1", 48),
        ]
        basket_id = connection.execute(
            "INSERT INTO baskets(name) VALUES ('Базовая корзина')"
        ).lastrowid
        for name, unit, quantity, _ in products:
            product_id = connection.execute(
                "INSERT INTO products(name,unit) VALUES (?,?)", (name, unit)
            ).lastrowid
            connection.execute(
                "INSERT INTO items(basket_id,product_id,quantity) VALUES (?,?,?)",
                (basket_id, product_id, quantity),
            )
        product_rows = connection.execute(
            "SELECT id FROM products ORDER BY id"
        ).fetchall()
        for shop_index, name in enumerate(["Лавка", "Городской", "У дома"]):
            shop_id = connection.execute(
                "INSERT INTO stores(name) VALUES (?)", (name,)
            ).lastrowid
            for month_index, day in enumerate(
                ["2026-09-01", "2026-10-01", "2026-11-01"]
            ):
                for product, product_row in zip(products, product_rows):
                    if shop_index == 2 and product[0] == "Молоко":
                        continue
                    cents = int(
                        (
                            Decimal(product[3])
                            * (
                                Decimal("1")
                                + Decimal(shop_index) * Decimal(".09")
                                + Decimal(month_index) * Decimal(".035")
                            )
                            * 100
                        ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                    )
                    connection.execute(
                        "INSERT INTO prices(store_id,product_id,date,cents) VALUES (?,?,?,?)",
                        (shop_id, product_row["id"], day, cents),
                    )
        connection.execute(
            "INSERT INTO metadata(key,value) VALUES ('demo_v1','fictional')"
        )
        connection.commit()
        click.echo(
            "Учебные данные загружены: 10 продуктов, 3 магазина, 3 даты. У «У дома» отсутствует цена молока."
        )

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=int(os.environ.get("BASKET_PORT", "5000")))
