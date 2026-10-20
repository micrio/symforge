import sqlite3
import os

from .paths import db_path

DB_PATH = db_path()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Stores primary info about parent image sheets
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            filepath TEXT NOT NULL,
            analyzed INTEGER DEFAULT 0
        )
    ''')
    # Stores individual coordinates and labeling context
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS symbols (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id INTEGER,
            x INTEGER, y INTEGER, w INTEGER, h INTEGER,
            label TEXT DEFAULT 'unlabeled',
            is_checked INTEGER DEFAULT 1,
            is_exported INTEGER DEFAULT 0,
            FOREIGN KEY(image_id) REFERENCES images(id)
        )
    ''')
    # Key/value app settings (e.g. export directory)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    # Groups let several detected boxes be treated as one symbol
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id INTEGER,
            label TEXT DEFAULT 'unlabeled',
            is_checked INTEGER DEFAULT 1,
            is_exported INTEGER DEFAULT 0,
            FOREIGN KEY(image_id) REFERENCES images(id)
        )
    ''')
    # migrate older databases: symbols.group_id (NULL = standalone symbol)
    symbol_cols = [row[1] for row in cursor.execute("PRAGMA table_info(symbols)").fetchall()]
    if "group_id" not in symbol_cols:
        cursor.execute("ALTER TABLE symbols ADD COLUMN group_id INTEGER DEFAULT NULL")
    image_cols = [row[1] for row in cursor.execute("PRAGMA table_info(images)").fetchall()]
    if "analyzed" not in image_cols:
        cursor.execute("ALTER TABLE images ADD COLUMN analyzed INTEGER DEFAULT 0")
    # images that already have symbols were clearly analyzed before
    cursor.execute("UPDATE images SET analyzed = 1 WHERE id IN (SELECT DISTINCT image_id FROM symbols)")
    conn.commit()
    conn.close()


def get_setting(key, default=None):
    conn = get_db_connection()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    if row and row["value"]:
        return row["value"]
    return default


def set_setting(key, value):
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value)
    )
    conn.commit()
    conn.close()


def set_image_analyzed(image_id):
    conn = get_db_connection()
    conn.execute("UPDATE images SET analyzed = 1 WHERE id = ?", (image_id,))
    conn.commit()
    conn.close()


def list_symbols(image_id):
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM symbols WHERE image_id = ? ORDER BY y, x", (image_id,)
    ).fetchall()
    conn.close()
    return rows


def add_symbol(image_id, x, y, w, h):
    """Insert a manually drawn symbol box. Returns the new symbol id."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO symbols (image_id, x, y, w, h) VALUES (?, ?, ?, ?, ?)",
        (image_id, int(x), int(y), int(w), int(h)),
    )
    symbol_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return symbol_id


def list_groups(image_id):
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM groups WHERE image_id = ?", (image_id,)
    ).fetchall()
    conn.close()
    return rows


def _prune_small_groups(cursor, image_id):
    """Drop groups that no longer hold at least two symbols."""
    for row in cursor.execute("SELECT id FROM groups WHERE image_id = ?", (image_id,)).fetchall():
        count = cursor.execute(
            "SELECT COUNT(*) FROM symbols WHERE group_id = ?", (row["id"],)
        ).fetchone()[0]
        if count < 2:
            cursor.execute("UPDATE symbols SET group_id = NULL WHERE group_id = ?", (row["id"],))
            cursor.execute("DELETE FROM groups WHERE id = ?", (row["id"],))


def create_group(image_id, symbol_ids):
    """Group >=2 symbols into one unit. Returns the new group id (or None)."""
    ids = [int(i) for i in symbol_ids]
    if len(ids) < 2:
        return None
    conn = get_db_connection()
    cursor = conn.cursor()
    placeholders = ",".join("?" for _ in ids)
    members = cursor.execute(
        f"SELECT * FROM symbols WHERE image_id = ? AND id IN ({placeholders})",
        (image_id, *ids),
    ).fetchall()
    if len(members) < 2:
        conn.close()
        return None

    label = "unlabeled"
    for m in members:
        if m["label"] and m["label"] != "unlabeled":
            label = m["label"]
            break

    cursor.execute("INSERT INTO groups (image_id, label) VALUES (?, ?)", (image_id, label))
    group_id = cursor.lastrowid
    cursor.execute(
        f"UPDATE symbols SET group_id = ? WHERE image_id = ? AND id IN ({placeholders})",
        (group_id, image_id, *ids),
    )
    _prune_small_groups(cursor, image_id)
    conn.commit()
    conn.close()
    return group_id


def ungroup(group_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE symbols SET group_id = NULL WHERE group_id = ?", (group_id,))
    cursor.execute("DELETE FROM groups WHERE id = ?", (group_id,))
    conn.commit()
    conn.close()


def set_symbol_fields(symbol_id, label=None, is_checked=None, is_exported=None):
    _update_fields("symbols", symbol_id, label, is_checked, is_exported)


def set_group_fields(group_id, label=None, is_checked=None, is_exported=None):
    _update_fields("groups", group_id, label, is_checked, is_exported)


def _update_fields(table, row_id, label, is_checked, is_exported):
    sets, params = [], []
    if label is not None:
        sets.append("label = ?")
        params.append(label)
    if is_checked is not None:
        sets.append("is_checked = ?")
        params.append(is_checked)
    if is_exported is not None:
        sets.append("is_exported = ?")
        params.append(is_exported)
    if not sets:
        return
    params.append(row_id)
    conn = get_db_connection()
    conn.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()
    conn.close()


def get_units(image_id):
    """Logical units for an image: grouped symbols (merged bbox) + standalone symbols."""
    symbols = list_symbols(image_id)
    groups = {g["id"]: g for g in list_groups(image_id)}

    by_group = {}
    for s in symbols:
        if s["group_id"]:
            by_group.setdefault(s["group_id"], []).append(s)

    units, seen = [], set()
    for s in symbols:
        gid = s["group_id"]
        if not gid:
            units.append({
                "key": f"s{s['id']}", "kind": "symbol", "id": s["id"],
                "label": s["label"] or "", "is_checked": s["is_checked"],
                "is_exported": s["is_exported"],
                "x": s["x"], "y": s["y"], "w": s["w"], "h": s["h"],
                "members": [s["id"]],
            })
        elif gid not in seen:
            seen.add(gid)
            members = by_group.get(gid, [])
            if len(members) < 2:
                continue
            x = min(m["x"] for m in members)
            y = min(m["y"] for m in members)
            right = max(m["x"] + m["w"] for m in members)
            bottom = max(m["y"] + m["h"] for m in members)
            g = groups.get(gid)
            units.append({
                "key": f"g{gid}", "kind": "group", "id": gid,
                "label": (g["label"] if g else "") or "",
                "is_checked": g["is_checked"] if g else 1,
                "is_exported": g["is_exported"] if g else 0,
                "x": x, "y": y, "w": right - x, "h": bottom - y,
                "members": [m["id"] for m in members],
            })

    units.sort(key=lambda u: (u["y"], u["x"]))
    for i, u in enumerate(units, 1):
        u["index"] = i
    return units


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
