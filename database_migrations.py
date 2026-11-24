"""Repair the known pre-UTF-8 Windows CHECK constraint without changing IDs."""

import re
import sqlite3
from pathlib import Path
from uuid import uuid4

UNITS = ("кг", "л", "шт.")
WINDOWS_UNITS = tuple(unit.encode("utf-8").decode("cp1251") for unit in UNITS)
WINDOWS_VARIANTS = [
    tuple(unit.encode("utf-8").decode(encoding) for unit in UNITS)
    for encoding in ("cp1251", "cp1252")
]


def needs_unit_repair(db):
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='products'"
    ).fetchone()
    if not row:
        return False
    match = re.search(r"CHECK\s*\(\s*unit\s+IN\s*\(([^)]*)\)", row[0], re.IGNORECASE)
    if not match:
        return False
    allowed = tuple(re.findall(r"'([^']*)'", match.group(1)))
    return any(set(allowed) == set(variant) for variant in WINDOWS_VARIANTS)


def repair_windows_units(db, database_path):
    if not needs_unit_repair(db):
        return None
    backup_path = Path(
        str(database_path) + f".before-utf8-fix-{uuid4().hex[:10]}.sqlite3"
    )
    with sqlite3.connect(backup_path) as backup:
        db.backup(backup)
    # Foreign-key enforcement can only change outside a transaction. References
    # keep pointing to products while its replacement is built and swapped.
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        db.execute("BEGIN IMMEDIATE")
        if not needs_unit_repair(db):
            db.rollback()
            return None
        objects = db.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name='products' AND type IN ('index','trigger') AND sql IS NOT NULL"
        ).fetchall()
        db.execute(
            "CREATE TABLE products_utf8_repair (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, unit TEXT NOT NULL CHECK(unit IN ('кг','л','шт.')))"
        )
        replacements = [
            value
            for variant in WINDOWS_VARIANTS
            for pair in zip(variant, UNITS)
            for value in pair
        ]
        cases = " ".join(
            "WHEN ? THEN ?" for variant in WINDOWS_VARIANTS for unit in UNITS
        )
        db.execute(
            f"INSERT INTO products_utf8_repair(id,name,unit) SELECT id,name,CASE unit {cases} ELSE unit END FROM products",
            replacements,
        )
        db.execute("DROP TABLE products")
        db.execute("ALTER TABLE products_utf8_repair RENAME TO products")
        for object_sql in objects:
            db.execute(object_sql[0])
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise sqlite3.IntegrityError(
                "Связи базы требуют проверки. Автоматическое восстановление отменено, исходные данные сохранены."
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute("PRAGMA foreign_keys=ON")
    return backup_path


LEGACY_DEMO_NAMES = {
    "products": {
        "Учебный картофель": "Картофель",
        "Учебное молоко": "Молоко",
        "Учебный хлеб": "Хлеб",
        "Учебные яйца": "Яйца",
        "Учебное куриное филе": "Куриное филе",
        "Учебный рис": "Рис",
        "Учебные яблоки": "Яблоки",
        "Учебное сливочное масло": "Сливочное масло",
        "Учебный сахар": "Сахар",
        "Учебная морковь": "Морковь",
    },
    "stores": {
        "Учебная Лавка": "Лавка",
        "Учебный Маркет": "Маркет",
        "Учебный Дискаунтер": "Дискаунтер",
        "Учебный магазин": "Магазин",
    },
    "baskets": {
        "Учебная корзина для графиков": "Корзина для графиков",
        "Учебная корзина": "Корзина",
    },
}


def normalize_legacy_demo_names(db, database_path):
    pending = []
    for table, replacements in LEGACY_DEMO_NAMES.items():
        for row in db.execute(f"SELECT id,name FROM {table} ORDER BY id"):
            if row["name"] in replacements:
                pending.append(
                    (table, row["id"], row["name"], replacements[row["name"]])
                )
    if not pending:
        return None
    backup_path = Path(
        str(database_path) + f".before-demo-names-{uuid4().hex[:10]}.sqlite3"
    )
    with sqlite3.connect(backup_path) as backup:
        db.backup(backup)
    try:
        db.execute("BEGIN IMMEDIATE")
        for table, record_id, old_name, base_name in pending:
            current = db.execute(
                f"SELECT name FROM {table} WHERE id=?", (record_id,)
            ).fetchone()
            if not current or current["name"] != old_name:
                continue
            used = {row["name"] for row in db.execute(f"SELECT name FROM {table}")}
            new_name = base_name
            suffix = 2
            while new_name in used:
                new_name = f"{base_name} ({suffix})"
                suffix += 1
            db.execute(f"UPDATE {table} SET name=? WHERE id=?", (new_name, record_id))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return backup_path
