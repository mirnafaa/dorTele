import sqlite3
import os

DB_NAME = "users.db"

# Hapus database lama jika ada, untuk membuat yang baru
if os.path.exists(DB_NAME):
    os.remove(DB_NAME)
    print(f"Database '{DB_NAME}' lama telah dihapus.")

try:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Buat tabel 'users' dengan kolom izin baru
    cursor.execute("""
    CREATE TABLE users (
        user_id INTEGER PRIMARY KEY,
        user_name TEXT DEFAULT '',
        credits INTEGER DEFAULT 0,
        can_cek_akun INTEGER DEFAULT 0,
        can_cek_fore INTEGER DEFAULT 0,
        can_cek_order INTEGER DEFAULT 0,
        is_admin INTEGER DEFAULT 0
    );
    """)

    conn.commit()
    conn.close()
    print(f"✅ Berhasil! Database '{DB_NAME}' baru telah dibuat.")

except sqlite3.Error as e:
    print(f"Terjadi error saat membuat database: {e}")