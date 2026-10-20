from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import cv2
import os
import shutil
import subprocess
import sys
import uuid
from urllib.parse import quote
from .database import (
    init_db, get_db_connection, get_setting, set_setting,
    get_units, create_group, ungroup, list_symbols, add_symbol,
    set_symbol_fields, set_group_fields, set_image_analyzed, _prune_small_groups,
)
from .paths import resource_dir, uploads_dir, exports_dir

app = FastAPI(title="SymForge")

APP_DIR = resource_dir()
UPLOAD_DIR = uploads_dir()
EXPORT_DIR = exports_dir()
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)

static_dir = os.path.join(APP_DIR, "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
templates = Jinja2Templates(directory=os.path.join(APP_DIR, "templates"))


def get_export_dir():
    """Current export base directory (from DB settings, fallback to default)."""
    return get_setting("export_dir", EXPORT_DIR)


def choose_folder_native():
    """Open the OS native folder picker and return the chosen absolute path (or None)."""
    if sys.platform == "darwin":
        script = 'POSIX path of (choose folder with prompt "Select export folder")'
        try:
            p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        except OSError:
            return None
        return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None
    if sys.platform.startswith("linux"):
        for cmd in (["zenity", "--file-selection", "--directory"],
                    ["kdialog", "--getexistingdirectory"]):
            try:
                p = subprocess.run(cmd, capture_output=True, text=True)
            except OSError:
                continue
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip()
        return None
    if sys.platform.startswith("win"):
        ps = ("Add-Type -AssemblyName System.Windows.Forms; "
              "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
              "if ($d.ShowDialog() -eq 'OK') { Write-Output $d.SelectedPath }")
        try:
            p = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               capture_output=True, text=True)
        except OSError:
            return None
        return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None
    return None


@app.on_event("startup")
def on_startup():
    init_db()


# ---- undo / redo history (in-memory, per image) ----
UNDO_LIMIT = 100
_HISTORY = {}  # image_id -> {"undo": [state, ...], "redo": [state, ...]}


def _snapshot(image_id):
    conn = get_db_connection()
    symbols = [dict(r) for r in conn.execute(
        "SELECT * FROM symbols WHERE image_id = ?", (image_id,)).fetchall()]
    groups = [dict(r) for r in conn.execute(
        "SELECT * FROM groups WHERE image_id = ?", (image_id,)).fetchall()]
    conn.close()
    return {"symbols": symbols, "groups": groups}


def _push_history(image_id):
    state = _snapshot(image_id)
    h = _HISTORY.setdefault(image_id, {"undo": [], "redo": []})
    h["undo"].append(state)
    if len(h["undo"]) > UNDO_LIMIT:
        h["undo"].pop(0)
    h["redo"].clear()


def _restore_state(image_id, state):
    conn = get_db_connection()
    conn.execute("DELETE FROM symbols WHERE image_id = ?", (image_id,))
    conn.execute("DELETE FROM groups WHERE image_id = ?", (image_id,))
    for g in state["groups"]:
        conn.execute(
            "INSERT INTO groups (id, image_id, label, is_checked, is_exported) "
            "VALUES (?, ?, ?, ?, ?)",
            (g["id"], image_id, g["label"], g["is_checked"], g["is_exported"])
        )
    for s in state["symbols"]:
        conn.execute(
            "INSERT INTO symbols (id, image_id, x, y, w, h, label, is_checked, is_exported, group_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (s["id"], image_id, s["x"], s["y"], s["w"], s["h"],
             s["label"], s["is_checked"], s["is_exported"], s.get("group_id"))
        )
    conn.commit()
    conn.close()


def detect_symbols(image_id, file_path, min_area, padding):
    """Run contour detection and persist symbol boxes for an image."""
    set_image_analyzed(image_id)
    conn = get_db_connection()
    cursor = conn.cursor()

    # clear any prior detection for a re-analyze
    cursor.execute("DELETE FROM groups WHERE image_id = ?", (image_id,))
    cursor.execute("DELETE FROM symbols WHERE image_id = ?", (image_id,))

    img = cv2.imread(file_path)
    if img is None:
        conn.commit()
        conn.close()
        return
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    h_max, w_max, _ = img.shape
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w * h > min_area:
            x_pad = max(0, x - padding)
            y_pad = max(0, y - padding)
            w_pad = min(w_max - x_pad, w + (padding * 2))
            h_pad = min(h_max - y_pad, h + (padding * 2))
            cursor.execute(
                "INSERT INTO symbols (image_id, x, y, w, h) VALUES (?, ?, ?, ?, ?)",
                (image_id, x_pad, y_pad, w_pad, h_pad)
            )
    conn.commit()
    conn.close()


def refine_symbol_box(file_path, x, y, w, h):
    """Snap a drawn rectangle to the ink it covers.

    Returns (x, y, w, h) fitted to the detected strokes, or None if nothing
    was found (caller then keeps the drawn rectangle).
    """
    img = cv2.imread(file_path)
    if img is None:
        return None
    height, width = img.shape[:2]
    search = 8  # also look slightly outside the drawn rectangle
    rx0 = max(0, x - search)
    ry0 = max(0, y - search)
    rx1 = min(width, x + w + search)
    ry1 = min(height, y + h + search)
    if rx1 <= rx0 or ry1 <= ry0:
        return None

    roi = img[ry0:ry1, rx0:rx1]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    boxes = []
    for c in contours:
        bx, by, bw, bh = cv2.boundingRect(c)
        if bw * bh < 4:
            continue
        gx, gy = bx + rx0, by + ry0
        # keep contours whose centre lies inside the drawn rectangle
        cx, cy = gx + bw / 2, gy + bh / 2
        if x <= cx <= x + w and y <= cy <= y + h:
            boxes.append((gx, gy, bw, bh))
    if not boxes:
        return None

    nx0 = min(b[0] for b in boxes)
    ny0 = min(b[1] for b in boxes)
    nx1 = max(b[0] + b[2] for b in boxes)
    ny1 = max(b[1] + b[3] for b in boxes)

    pad = 2
    nx0 = max(0, nx0 - pad)
    ny0 = max(0, ny0 - pad)
    nx1 = min(width, nx1 + pad)
    ny1 = min(height, ny1 + pad)
    return (int(nx0), int(ny0), int(nx1 - nx0), int(ny1 - ny0))


@app.get("/", response_class=HTMLResponse)
async def home_dashboard(request: Request):
    conn = get_db_connection()
    images = conn.execute("SELECT * FROM images ORDER BY id DESC").fetchall()
    conn.close()
    return templates.TemplateResponse("index.html", {
        "request": request, "images": images, "active_image": None, "symbols": None,
        "boxes": [], "units": [], "member_outlines": [],
        "single_units": [], "group_units": [], "export_dir": get_export_dir()
    })


@app.post("/upload")
async def upload_images(files: list[UploadFile] = File(...)):
    conn = get_db_connection()
    cursor = conn.cursor()
    first_id = None
    for file in files:
        # unique on-disk name so re-uploading a same-named file never overwrites
        ext = os.path.splitext(file.filename or "")[1].lower()
        stored_name = f"{uuid.uuid4().hex}{ext}"
        file_path = os.path.join(UPLOAD_DIR, stored_name)
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        cursor.execute(
            "INSERT INTO images (filename, filepath) VALUES (?, ?)",
            (file.filename, file_path)
        )
        if first_id is None:
            first_id = cursor.lastrowid
    conn.commit()
    conn.close()
    if first_id is None:
        return RedirectResponse(url="/", status_code=303)
    return RedirectResponse(url=f"/review/{first_id}", status_code=303)


@app.post("/analyze/{image_id}")
async def analyze_image(image_id: int, min_area: int = Form(150), padding: int = Form(8)):
    _HISTORY.pop(image_id, None)  # re-analyzing wipes undo/redo history
    conn = get_db_connection()
    image_record = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    conn.close()
    if image_record:
        detect_symbols(image_id, image_record["filepath"], min_area, padding)
    return RedirectResponse(url=f"/review/{image_id}", status_code=303)


@app.get("/review/{image_id}", response_class=HTMLResponse)
async def review_symbols(image_id: int, request: Request):
    conn = get_db_connection()
    image_record = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    all_images = conn.execute("SELECT * FROM images ORDER BY id DESC").fetchall()
    conn.close()

    symbols = list_symbols(image_id) if image_record else []
    units = get_units(image_id) if image_record else []

    # number every symbol box in reading order (group members each get one)
    included_sids = set()
    for u in units:
        if u["is_checked"]:
            included_sids.update(u["members"])

    box_number = {}
    n = 0
    for s in symbols:
        if s["id"] in included_sids:
            n += 1
            box_number[s["id"]] = n
    for u in units:
        u["label_indices"] = [box_number[sid] for sid in u["members"] if sid in box_number]

    boxes = []
    for u in units:
        label = ", ".join(str(n) for n in u["label_indices"])
        boxes.append({
            "id": u["id"], "kind": u["kind"], "unit_key": u["key"],
            "x": u["x"], "y": u["y"], "w": u["w"], "h": u["h"],
            "index": "" if u["kind"] == "group" else label,
            "is_checked": u["is_checked"],
            "members": ",".join(str(m) for m in u["members"]),
        })

    # show member symbol boxes (with their own labels) for label groups
    sym_by_id = {s["id"]: s for s in symbols}
    member_outlines = []
    for u in units:
        if u["kind"] == "group" and u["is_checked"]:
            for sid in u["members"]:
                m = sym_by_id.get(sid)
                if m:
                    member_outlines.append({
                        "x": m["x"], "y": m["y"], "w": m["w"], "h": m["h"],
                        "sid": sid, "index": box_number.get(sid, ""),
                    })

    img_w = img_h = 0
    img_filename = ""
    if image_record:
        img_filename = os.path.basename(image_record["filepath"])
        img = cv2.imread(image_record["filepath"])
        if img is not None:
            img_h, img_w = img.shape[:2]

    return templates.TemplateResponse("index.html", {
        "request": request, "images": all_images, "active_image": image_record,
        "symbols": symbols, "units": units, "boxes": boxes,
        "member_outlines": member_outlines,
        "single_units": [u for u in units if u["kind"] == "symbol"],
        "group_units": [u for u in units if u["kind"] == "group"],
        "img_w": img_w, "img_h": img_h, "img_filename": img_filename,
        "symbol_order": [s["id"] for s in symbols],
        "analyzed": bool(image_record["analyzed"]) if image_record else False,
        "export_dir": get_export_dir()
    })


@app.post("/delete/{image_id}")
async def delete_image(image_id: int):
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    if row:
        try:
            os.remove(row["filepath"])
        except OSError:
            pass
        conn.execute("DELETE FROM groups WHERE image_id = ?", (image_id,))
        conn.execute("DELETE FROM symbols WHERE image_id = ?", (image_id,))
        conn.execute("DELETE FROM images WHERE id = ?", (image_id,))
        conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)


@app.post("/image/rename/{image_id}")
async def rename_image(image_id: int, name: str = Form(...), next_url: str = Form("/")):
    name = (name or "").strip()
    conn = get_db_connection()
    row = conn.execute("SELECT filename FROM images WHERE id = ?", (image_id,)).fetchone()
    if row and name:
        ext = os.path.splitext(row["filename"] or "")[1]
        # keep the original extension; ignore any typed-in one
        if ext and name.lower().endswith(ext.lower()):
            name = name[:-len(ext)]
        conn.execute("UPDATE images SET filename = ? WHERE id = ?", (name + ext, image_id))
        conn.commit()
    conn.close()
    if next_url.startswith("/"):
        return RedirectResponse(url=next_url, status_code=303)
    return RedirectResponse(url=f"/review/{image_id}", status_code=303)


@app.post("/symbol/{image_id}")
async def create_symbol(image_id: int, x: int = Form(...), y: int = Form(...),
                        w: int = Form(...), h: int = Form(...)):
    if w > 0 and h > 0:
        conn = get_db_connection()
        row = conn.execute("SELECT filepath FROM images WHERE id = ?", (image_id,)).fetchone()
        conn.close()
        if row:
            refined = refine_symbol_box(row["filepath"], x, y, w, h)
            if refined:
                x, y, w, h = refined
        _push_history(image_id)
        add_symbol(image_id, x, y, w, h)
    return RedirectResponse(url=f"/review/{image_id}", status_code=303)


@app.post("/group/{image_id}")
async def group_symbols(image_id: int, sids: str = Form(...)):
    ids = [s.strip() for s in sids.split(",") if s.strip().isdigit()]
    if len(ids) >= 2:
        _push_history(image_id)
        create_group(image_id, ids)
    return RedirectResponse(url=f"/review/{image_id}", status_code=303)


@app.post("/exclude/{image_id}")
async def exclude_units(image_id: int, units: str = Form(...)):
    """Uncheck several units at once (drag-select exclude)."""
    keys = [k.strip() for k in units.split(",") if k.strip()]
    conn = get_db_connection()
    targets = []
    for key in keys:
        table = "symbols" if key.startswith("s") else "groups"
        try:
            row_id = int(key[1:])
        except ValueError:
            continue
        row = conn.execute(
            f"SELECT is_checked FROM {table} WHERE id = ? AND image_id = ?", (row_id, image_id)
        ).fetchone()
        if row and row["is_checked"]:
            targets.append((table, row_id))
    if targets:
        _push_history(image_id)
        for table, row_id in targets:
            conn.execute(f"UPDATE {table} SET is_checked = 0 WHERE id = ?", (row_id,))
        conn.commit()
    conn.close()
    return RedirectResponse(url=f"/review/{image_id}", status_code=303)


@app.post("/combine/{image_id}")
async def combine_symbols(image_id: int, sids: str = Form(...)):
    """Merge several detected boxes into ONE normal symbol (e.g. the dot + stem of 'i')."""
    ids = [s.strip() for s in sids.split(",") if s.strip().isdigit()]
    if len(ids) < 2:
        return RedirectResponse(url=f"/review/{image_id}", status_code=303)
    conn = get_db_connection()
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM symbols WHERE image_id = ? AND id IN ({placeholders})",
        (image_id, *ids),
    ).fetchall()
    if len(rows) < 2:
        conn.close()
        return RedirectResponse(url=f"/review/{image_id}", status_code=303)
    _push_history(image_id)
    x = min(r["x"] for r in rows)
    y = min(r["y"] for r in rows)
    right = max(r["x"] + r["w"] for r in rows)
    bottom = max(r["y"] + r["h"] for r in rows)
    label = "unlabeled"
    for r in rows:
        if r["label"] and r["label"] != "unlabeled":
            label = r["label"]
            break
    conn.execute(f"DELETE FROM symbols WHERE image_id = ? AND id IN ({placeholders})", (image_id, *ids))
    _prune_small_groups(conn, image_id)
    conn.execute(
        "INSERT INTO symbols (image_id, x, y, w, h, label) VALUES (?, ?, ?, ?, ?, ?)",
        (image_id, x, y, right - x, bottom - y, label),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url=f"/review/{image_id}", status_code=303)


@app.post("/group/add/{image_id}")
async def add_symbol_to_group(image_id: int, symbol_id: int = Form(...), group_id: int = Form(...)):
    conn = get_db_connection()
    sym = conn.execute(
        "SELECT id FROM symbols WHERE id = ? AND image_id = ?", (symbol_id, image_id)
    ).fetchone()
    grp = conn.execute(
        "SELECT id FROM groups WHERE id = ? AND image_id = ?", (group_id, image_id)
    ).fetchone()
    if sym and grp:
        _push_history(image_id)
        conn.execute("UPDATE symbols SET group_id = ? WHERE id = ?", (group_id, symbol_id))
        conn.commit()
    conn.close()
    return RedirectResponse(url=f"/review/{image_id}", status_code=303)


@app.post("/ungroup/{image_id}")
async def ungroup_symbols(image_id: int, group_id: int = Form(...)):
    _push_history(image_id)
    ungroup(group_id)
    return RedirectResponse(url=f"/review/{image_id}", status_code=303)


@app.post("/toggle/{image_id}")
async def toggle_unit(image_id: int, unit_key: str = Form(...)):
    table = "symbols" if unit_key.startswith("s") else "groups"
    try:
        row_id = int(unit_key[1:])
    except ValueError:
        return {"is_checked": None}
    conn = get_db_connection()
    row = conn.execute(
        f"SELECT is_checked FROM {table} WHERE id = ? AND image_id = ?", (row_id, image_id)
    ).fetchone()
    if not row:
        conn.close()
        return {"is_checked": None}
    new_val = 0 if row["is_checked"] else 1
    _push_history(image_id)
    conn.execute(f"UPDATE {table} SET is_checked = ? WHERE id = ?", (new_val, row_id))
    conn.commit()
    conn.close()
    return {"is_checked": new_val}


@app.post("/rename/{image_id}")
async def rename_unit(image_id: int, unit_key: str = Form(...), label: str = Form("")):
    table = "symbols" if unit_key.startswith("s") else "groups"
    try:
        row_id = int(unit_key[1:])
    except ValueError:
        return {"ok": False}
    conn = get_db_connection()
    row = conn.execute(
        f"SELECT label FROM {table} WHERE id = ? AND image_id = ?", (row_id, image_id)
    ).fetchone()
    if not row:
        conn.close()
        return {"ok": False}
    if (row["label"] or "") != label:
        _push_history(image_id)
        conn.execute(f"UPDATE {table} SET label = ? WHERE id = ?", (label, row_id))
        conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/undo/{image_id}")
async def undo_image(image_id: int):
    h = _HISTORY.get(image_id)
    if h and h["undo"]:
        h["redo"].append(_snapshot(image_id))
        _restore_state(image_id, h["undo"].pop())
    return RedirectResponse(url=f"/review/{image_id}", status_code=303)


@app.post("/redo/{image_id}")
async def redo_image(image_id: int):
    h = _HISTORY.get(image_id)
    if h and h["redo"]:
        h["undo"].append(_snapshot(image_id))
        _restore_state(image_id, h["redo"].pop())
    return RedirectResponse(url=f"/review/{image_id}", status_code=303)


@app.post("/settings/pick-export-dir")
def pick_export_dir(image_id: int = Form(None)):
    chosen = choose_folder_native()
    if chosen:
        set_setting("export_dir", os.path.abspath(chosen))
    if image_id:
        return RedirectResponse(url=f"/review/{image_id}", status_code=303)
    return RedirectResponse(url="/", status_code=303)


@app.post("/export/{image_id}")
async def process_export(image_id: int, request: Request):
    form_data = await request.form()
    fmt = form_data.get("format", "png").lower()
    ext = "jpg" if fmt in ("jpg", "jpeg") else "png"

    conn = get_db_connection()
    image_record = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    conn.close()

    img = cv2.imread(image_record["filepath"])
    base_export_dir = get_export_dir()

    naming = (form_data.get("naming", "label") or "label").lower()
    counters = {}
    exported_count = 0

    symbols = {s["id"]: s for s in list_symbols(image_id)}
    units = get_units(image_id)
    for unit in units:
        key = unit["key"]
        is_checked = 1 if f"keep_{key}" in form_data else 0
        target_label = (form_data.get(f"label_{key}", "unlabeled") or "").strip() or "unlabeled"

        if unit["kind"] == "group":
            set_group_fields(unit["id"], label=target_label, is_checked=is_checked)
        else:
            set_symbol_fields(unit["id"], label=target_label, is_checked=is_checked)

        if not is_checked:
            continue

        safe_label = target_label.replace("/", "_").replace("\\", "_").replace(os.sep, "_")
        label_dir = os.path.join(base_export_dir, safe_label)
        os.makedirs(label_dir, exist_ok=True)

        # groups are only for labeling: export every member symbol as its own crop
        for sid in unit["members"]:
            sym = symbols.get(sid)
            if not sym:
                continue
            x, y, w, h = sym["x"], sym["y"], sym["w"], sym["h"]
            cropped = img[y:y+h, x:x+w]

            if naming == "original":
                stem = f"crop_img{image_id}_s{sid}"
            else:
                n = counters.get(safe_label, 0) + 1
                counters[safe_label] = n
                stem = safe_label if n == 1 else f"{safe_label}-{n}"

            export_path = os.path.join(label_dir, f"{stem}.{ext}")
            if ext == "jpg":
                cv2.imwrite(export_path, cropped, [cv2.IMWRITE_JPEG_QUALITY, 95])
            else:
                cv2.imwrite(export_path, cropped)
            exported_count += 1

            set_symbol_fields(sid, is_exported=1)

        if unit["kind"] == "group":
            set_group_fields(unit["id"], is_exported=1)

    if exported_count:
        msg = f"Exported {exported_count} crop" + ("s" if exported_count != 1 else "")
    else:
        msg = "Nothing selected to export"
    return RedirectResponse(url=f"/review/{image_id}?toast={quote(msg)}", status_code=303)
