# -*- coding: utf-8 -*-
"""
Migration script để thêm các columns mới cho real bot support
Chạy 1 lần trước khi start bot

Usage: python migrate_db.py
"""

import sqlite3
import sys


def migrate(db_path='trading_bot.db'):
    """Thêm các columns mới vào bot_configs table"""

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Lấy danh sách columns hiện có
    cursor.execute("PRAGMA table_info(bot_configs)")
    existing_columns = [col[1] for col in cursor.fetchall()]
    print(f"📋 Existing columns: {existing_columns}")

    # Các columns cần thêm
    new_columns = [
        ("is_real_bot", "BOOLEAN DEFAULT 0"),
        ("account_name", "VARCHAR(100)"),
        ("api_key", "VARCHAR(200)"),
        ("api_secret", "VARCHAR(200)"),
        ("source_bot_id", "INTEGER"),
        ("chat_id", "VARCHAR(100)"),
    ]

    added = 0
    for col_name, col_type in new_columns:
        if col_name not in existing_columns:
            try:
                sql = f"ALTER TABLE bot_configs ADD COLUMN {col_name} {col_type}"
                cursor.execute(sql)
                print(f"✅ Added column: {col_name}")
                added += 1
            except Exception as e:
                print(f"❌ Error adding {col_name}: {e}")
        else:
            print(f"⏭️ Column already exists: {col_name}")

    conn.commit()
    conn.close()

    print(f"\n🎉 Migration completed! Added {added} new columns.")
    return added


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else 'trading_bot.db'
    print(f"🔧 Migrating database: {db_path}\n")
    migrate(db_path)