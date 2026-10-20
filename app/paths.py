"""Filesystem locations that work both from source and inside a frozen .app bundle.

Read-only resources (templates, static assets) are bundled inside the app.
Writable data (database, uploads, exports) goes to a user-writable location.
"""
import os
import sys


def is_frozen():
    return getattr(sys, "frozen", False)


def resource_dir():
    """Folder holding read-only bundled resources (templates/, static/)."""
    if is_frozen():
        return os.path.join(sys._MEIPASS, "app")
    return os.path.dirname(os.path.abspath(__file__))


def data_dir():
    """Writable folder for database, uploads and exports."""
    if is_frozen():
        base = os.path.expanduser("~/Library/Application Support/FastSymbolCropper")
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(base, exist_ok=True)
    return base


def db_path():
    if is_frozen():
        return os.path.join(data_dir(), "metadata.db")
    return os.path.join(data_dir(), "metadata.db")


def uploads_dir():
    if is_frozen():
        return os.path.join(data_dir(), "uploads")
    return os.path.join(resource_dir(), "static", "uploads")


def exports_dir():
    if is_frozen():
        return os.path.join(data_dir(), "exports")
    return os.path.join(resource_dir(), "static", "exports")
