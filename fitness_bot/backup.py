from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


MAGIC = b"FITNESS-BACKUP-V1\0"
BACKUP_FILENAME = "fitness_bot.sqlite3"
MAX_DATABASE_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
REQUIRED_TABLES = {"telegram_accounts", "profiles", "food_database", "food_log"}
FULL_DATA_TABLES = {
    "telegram_accounts", "user_preferences", "profiles", "food_database", "food_log", "meal_templates",
    "weight_log", "activity_log", "sleep_log", "monobank_accounts", "finance_categories",
    "finance_budgets", "finance_accounts", "finance_jars", "finance_auto_rules", "finance_transactions",
    "goals", "habits", "habit_logs", "reflection_entries", "weekly_reflections", "training_exercises",
    "training_programs", "training_days", "training_program_exercises", "training_sessions", "training_exercise_logs",
}
COUNT_TABLES = {
    "telegram_accounts", "profiles", "food_database", "food_log", "activity_log", "weight_log", "sleep_log",
    "finance_transactions", "goals", "habits", "habit_logs", "reflection_entries", "weekly_reflections",
    "training_programs", "training_sessions", "training_exercise_logs",
}


class BackupError(ValueError):
    pass


def _fernet(secret: str, salt: bytes) -> Fernet:
    if len(secret) < 32:
        raise BackupError("Ключ резервних копій має містити щонайменше 32 символи.")
    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=600_000,
    ).derive(secret.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(key))


def make_backup_archive(database_bytes: bytes, secret: str, created_at: datetime) -> bytes:
    if not database_bytes or len(database_bytes) > MAX_DATABASE_BYTES:
        raise BackupError("Розмір бази не підтримується для резервування.")
    details = _validate_sqlite(database_bytes, require_full=True)
    salt = os.urandom(16)
    manifest = {
        "format": "fitness-bot-full-backup",
        "version": 1,
        "created_at": created_at.isoformat(timespec="seconds"),
        "database_sha256": hashlib.sha256(database_bytes).hexdigest(),
        "database_size": len(database_bytes),
        "tables": details["table_names"],
        "record_counts": details["record_counts"],
    }
    raw_zip = io.BytesIO()
    with zipfile.ZipFile(raw_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(BACKUP_FILENAME, database_bytes)
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    encrypted = MAGIC + salt + _fernet(secret, salt).encrypt(raw_zip.getvalue())
    if len(encrypted) > MAX_ARCHIVE_BYTES:
        raise BackupError("Архів перевищив 20 МБ — Telegram не зможе завантажити його для відновлення.")
    return encrypted


def _validate_sqlite(database_bytes: bytes, *, require_full: bool = False) -> dict[str, Any]:
    if not database_bytes or len(database_bytes) > MAX_DATABASE_BYTES:
        raise BackupError("Розмір бази в архіві недопустимий.")
    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as target_file:
        target_path = Path(target_file.name)
        target_file.write(database_bytes)
    try:
        target_path.chmod(0o600)
        connection = sqlite3.connect(str(target_path))
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise BackupError("Перевірка цілісності бази не пройшла.")
            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchone()
            if foreign_key_errors:
                raise BackupError("У базі знайдено порушення зв’язків між записами.")
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                if not row[0].startswith("sqlite_")
            }
            if not REQUIRED_TABLES.issubset(tables):
                raise BackupError("Файл не схожий на повну базу Fitness Bot.")
            if require_full and not FULL_DATA_TABLES.issubset(tables):
                missing = ", ".join(sorted(FULL_DATA_TABLES - tables))
                raise BackupError(f"У базі немає таблиць окремих розділів: {missing}.")
            users = int(connection.execute("SELECT COUNT(*) FROM telegram_accounts").fetchone()[0])
            profiles = int(connection.execute("SELECT COUNT(*) FROM profiles").fetchone()[0])
            record_counts = {
                table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in sorted(COUNT_TABLES & tables)
            }
            return {"tables": len(tables), "table_names": sorted(tables), "users": users, "profiles": profiles, "record_counts": record_counts}
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise BackupError("Не вдалося прочитати SQLite-базу з архіву.") from exc
    finally:
        target_path.unlink(missing_ok=True)


def read_backup_archive(archive_bytes: bytes, secret: str) -> tuple[bytes, dict[str, Any]]:
    if len(archive_bytes) > MAX_ARCHIVE_BYTES:
        raise BackupError("Архів перевищує обмеження 20 МБ.")
    legacy = False
    if archive_bytes.startswith(MAGIC):
        header_size = len(MAGIC) + 16
        if len(archive_bytes) <= header_size:
            raise BackupError("Архів резервної копії пошкоджений.")
        salt = archive_bytes[len(MAGIC):header_size]
        try:
            raw_zip = _fernet(secret, salt).decrypt(archive_bytes[header_size:])
        except InvalidToken as exc:
            raise BackupError("Не вдалося розшифрувати копію. Перевірте BACKUP_ENCRYPTION_KEY.") from exc
    elif zipfile.is_zipfile(io.BytesIO(archive_bytes)):
        # Backups made by older bot releases were ZIP files without encryption.
        raw_zip = archive_bytes
        legacy = True
    else:
        raise BackupError("Це не файл резервної копії Fitness Bot.")

    try:
        with zipfile.ZipFile(io.BytesIO(raw_zip), "r") as archive:
            names = set(archive.namelist())
            if BACKUP_FILENAME not in names:
                raise BackupError("В архіві немає файла бази даних.")
            database_info = archive.getinfo(BACKUP_FILENAME)
            if database_info.file_size > MAX_DATABASE_BYTES:
                raise BackupError("База в архіві завелика для безпечного відновлення.")
            database_bytes = archive.read(BACKUP_FILENAME)
            manifest: dict[str, Any] = {}
            if "manifest.json" in names:
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                if not isinstance(manifest, dict):
                    raise BackupError("Опис архіву має неправильний формат.")
                if manifest.get("format") != "fitness-bot-full-backup" or manifest.get("version") != 1:
                    raise BackupError("Версія формату резервної копії не підтримується.")
                expected_hash = manifest.get("database_sha256")
                if expected_hash != hashlib.sha256(database_bytes).hexdigest():
                    raise BackupError("Контрольна сума бази не збігається.")
                if manifest.get("database_size") != len(database_bytes):
                    raise BackupError("Розмір бази не збігається з описом архіву.")
                if manifest.get("tables") and sorted(manifest["tables"]) != _validate_sqlite(database_bytes)["table_names"]:
                    raise BackupError("Склад таблиць у базі не збігається з описом архіву.")
            elif not legacy:
                raise BackupError("В архіві відсутній опис резервної копії.")
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("Архів пошкоджений або має невідомий формат.") from exc

    details = _validate_sqlite(database_bytes, require_full=not legacy)
    details.update({
        "created_at": manifest.get("created_at"),
        "legacy_unencrypted": legacy,
    })
    return database_bytes, details
