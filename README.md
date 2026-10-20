# SymForge

**Turn one symbol sheet into hundreds of labeled AI-training crops — fast.**

SymForge is a local web app that auto-detects individual symbols inside a single scanned sheet, lets you fix and label them in the browser, then exports one PNG/JPG per symbol into label-named folders. No more manually cropping images one by one — build training datasets in minutes instead of hours.

Built with **FastAPI + Jinja2 + SQLite + OpenCV**, styled with Tailwind CSS.

---

## Screenshot
<img width="1509" height="857" alt="Screenshot 2026-10-21 at 3 33 27 AM" src="https://github.com/user-attachments/assets/25632844-ac49-4e1b-b70c-3b78711b73b1" />

---

## Why SymForge?

- **One sheet in, many training images out** — detect every symbol on a page automatically instead of cropping each one by hand.
- **Faster dataset creation** — label once, export hundreds of clean, consistently sized crops in a single run.
- **Built for messy scans** — adjust detection sensitivity, snap manual boxes to the ink, and merge split shapes like the dot and stem of `i`.
- **Organized exports** — crops land in per-label folders, ready to drop into an AI training pipeline.

---

## Features

- **Upload** multiple images at once (UUID filenames, originals never overwritten)
- **Auto-detect** symbol boxes via OpenCV contour detection (adjustable minimum area + padding)
- **Bulk dataset creation** — slice one sheet into many individual training images in one pass
- **Re-analyze** an image at any time (wipes previous boxes for that image)
- **Manual boxes** — draw a rectangle; it snaps to the ink underneath
- **Combine** boxes into one symbol (e.g. dot + stem of `i`)
- **Groups** — treat several boxes as one logical symbol without merging pixels
- **Label / exclude** units, bulk exclude via drag-select
- **Rename** images and symbols/groups inline
- **Pan / zoom** canvas, light + dark themes
- **Undo / redo** per image (in-memory, 100 steps)
- **Export** selected crops as PNG or JPG, organized per label, with `label` or `original` naming
- **Native folder picker** for export destination (macOS / Linux / Windows)
- **Portable paths** — source mode and frozen (PyInstaller `.app`) mode

---

## Tech Stack

| Layer     | Tech                                      |
|-----------|-------------------------------------------|
| Backend   | FastAPI 0.115, Uvicorn 0.31               |
| Templates | Jinja2                                    |
| Imaging   | OpenCV (headless), NumPy, Pillow          |
| Storage   | SQLite (`metadata.db`)                    |
| Frontend  | Vanilla JS, Tailwind CSS 4                |

---

## Project Structure

```
SymForge/
├── app/
│   ├── main.py            # FastAPI app, routes, detection, export logic
│   ├── database.py        # SQLite schema, migrations, CRUD, unit grouping
│   ├── paths.py           # source vs frozen .app path resolution
│   ├── templates/
│   │   └── index.html     # dashboard + review UI (single-page)
│   └── static/
│       ├── src/tailwind.css   # Tailwind source
│       ├── tailwind.css       # built output (committed)
│       ├── uploads/           # uploaded source images (dev)
│       └── exports/           # exported crops (dev)
├── run.py                 # uvicorn dev launcher
├── requirements.txt
├── package.json           # Tailwind build scripts
└── metadata.db            # local SQLite database
```

---

## Setup

Requires **Python 3.10+** and **Node.js** (only for CSS rebuilds).

```bash
# 1. Python env
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Frontend toolchain (optional — only if editing styles)
npm install

# 3. Run
python run.py
```

Open <http://127.0.0.1:8000>.

### CSS build

```bash
npm run build:css    # one-off minified build
npm run watch:css    # watch mode
```

---

## Usage

1. **Upload** one or more symbol sheets from the sidebar.
2. Set **Minimum Area** and **Padding**, click **Analyze Image** to auto-detect boxes.
3. Review on the canvas:
   - **Pan** mode: drag to move the view.
   - **Select** mode: drag a marquee to pick boxes → group, combine, or exclude them.
   - Click a box to toggle include/exclude; edit its label in the **Single** / **Grouped** tabs.
   - Drag on empty canvas to add a manual box (snaps to nearby ink).
4. Pick an **export folder** (native picker), **format** (PNG/JPG), and **naming**:
   - `label` → `a.png`, `a-2.png`, `a-3.png` …
   - `original` → `crop_img{image_id}_s{symbol_id}.png`
5. Click **Export Selected**. Crops go into one subfolder per label.

Keyboard shortcuts: `Ctrl/Cmd+Z` undo, `Ctrl/Cmd+Shift+Z` redo.

---

## Data & Storage

Source (dev) mode:

| Data      | Location                     |
|-----------|------------------------------|
| Database  | `./metadata.db`              |
| Uploads   | `app/static/uploads/`        |
| Exports   | `app/static/exports/` (default; changeable in UI) |

Frozen (PyInstaller) mode:

| Data      | Location                                                   |
|-----------|------------------------------------------------------------|
| Database  | `~/Library/Application Support/FastSymbolCropper/metadata.db` |
| Uploads   | `~/Library/Application Support/FastSymbolCropper/uploads/` |
| Exports   | `~/Library/Application Support/FastSymbolCropper/exports/` |

### Schema

- `images(id, filename, filepath, analyzed)`
- `symbols(id, image_id, x, y, w, h, label, is_checked, is_exported, group_id)`
- `groups(id, image_id, label, is_checked, is_exported)`
- `settings(key, value)` — e.g. `export_dir`

Migrations run automatically on startup (`init_db`).

---

## Routes

| Method | Path                        | Purpose                                   |
|--------|-----------------------------|-------------------------------------------|
| GET    | `/`                         | Dashboard / image list                    |
| POST   | `/upload`                   | Upload images                             |
| GET    | `/review/{image_id}`        | Review canvas                             |
| POST   | `/analyze/{image_id}`       | Run contour detection                     |
| POST   | `/delete/{image_id}`        | Delete image + its symbols/groups         |
| POST   | `/image/rename/{image_id}`  | Rename image                              |
| POST   | `/symbol/{image_id}`        | Add manual (snapped) symbol box           |
| POST   | `/combine/{image_id}`       | Merge boxes into one symbol               |
| POST   | `/group/{image_id}`         | Group ≥2 symbols                          |
| POST   | `/group/add/{image_id}`     | Add symbol to existing group              |
| POST   | `/ungroup/{image_id}`       | Dissolve group                            |
| POST   | `/toggle/{image_id}`        | Toggle include/exclude                    |
| POST   | `/exclude/{image_id}`       | Bulk exclude                              |
| POST   | `/rename/{image_id}`        | Rename symbol/group label                 |
| POST   | `/undo/{image_id}`          | Undo                                      |
| POST   | `/redo/{image_id}`          | Redo                                      |
| POST   | `/settings/pick-export-dir` | Native export folder picker               |
| POST   | `/export/{image_id}`        | Export selected crops                     |

---

## Notes & Limitations

- Undo/redo history is **in-memory and per image** — lost on server restart or re-analyze.
- Exports are generated per detected symbol box; group membership only affects labeling, not pixels.
- `metadata.db` is gitignored — local only.
- No test suite yet (`npm test` is a placeholder).
- Export filenames are deduplicated within a run by a `-N` suffix; existing files in the target folder are overwritten.
