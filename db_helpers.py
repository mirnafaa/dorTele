import sqlite3

DB_NAME = "users.db"

def get_db_connection():
    """Membuka koneksi ke database dengan Row Factory."""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def register_user(user_id: int, user_name: str):
    """Mendaftarkan user baru ke DB. Diabaikan jika sudah ada."""
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, user_name) VALUES (?, ?)",
            (user_id, user_name)
        )
        conn.commit()
    except sqlite3.Error as e:
        print(f"Error saat register_user: {e}")
    finally:
        conn.close()

def get_user(user_id: int) -> sqlite3.Row | None:
    """Mengambil semua data untuk satu user."""
    conn = get_db_connection()
    try:
        cursor = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user_data = cursor.fetchone()
        return user_data
    except sqlite3.Error as e:
        print(f"Error saat get_user: {e}")
        return None
    finally:
        conn.close()

def update_credits(user_id: int, amount_to_change: int):
    """Menambah (angka positif) atau mengurangi (angka negatif) kredit user."""
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE users SET credits = credits + ? WHERE user_id = ?",
            (amount_to_change, user_id)
        )
        conn.commit()
    except sqlite3.Error as e:
        print(f"Error saat update_credits: {e}")
    finally:
        conn.close()

def set_permission(user_id: int, permission_name: str, value: int):
    """Mengatur status izin (0 atau 1) untuk user."""
    allowed_permissions = ['can_cek_akun', 'can_cek_fore', 'is_admin', 'can_cek_order']
    if permission_name not in allowed_permissions:
        raise ValueError(f"Nama izin tidak valid: {permission_name}")
        
    conn = get_db_connection()
    try:
        sql = f"UPDATE users SET {permission_name} = ? WHERE user_id = ?"
        conn.execute(sql, (value, user_id))
        conn.commit()
    except sqlite3.Error as e:
        print(f"Error saat set_permission: {e}")
    finally:
        conn.close()

def get_users_paginated(page: int = 1, per_page: int = 5, exclude_admin_id: int = 0):
    """Mengambil daftar user dari database dengan paginasi."""
    conn = get_db_connection()
    try:
        offset = (page - 1) * per_page
        query = """
            SELECT user_id, user_name FROM users
            WHERE user_id != ?
            ORDER BY user_name
            LIMIT ? OFFSET ?
        """
        cursor = conn.execute(query, (exclude_admin_id, per_page, offset))
        users = cursor.fetchall()
        
        count_cursor = conn.execute("SELECT COUNT(user_id) FROM users WHERE user_id != ?", (exclude_admin_id,))
        total_count = count_cursor.fetchone()[0]
        total_pages = max(1, (total_count + per_page - 1) // per_page)
        
        return users, total_pages
        
    except sqlite3.Error as e:
        print(f"Error saat get_users_paginated: {e}")
        return [], 1
    finally:
        conn.close()