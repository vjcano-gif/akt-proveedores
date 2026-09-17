"""Almacenamiento de evidencias.

Si SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY están configurados, usa Supabase Storage.
Si no, la app conserva el fallback en base de datos para desarrollo/demo.
"""
from __future__ import annotations

import os
import urllib.error
import urllib.parse
import urllib.request


def _setting(name: str, default=None):
    try:
        import streamlit as st
        value = st.secrets.get(name)
        if value not in (None, ""):
            return value
    except Exception:
        pass
    return os.environ.get(name, default)


def configured() -> bool:
    return bool(_setting("SUPABASE_URL") and _setting("SUPABASE_SERVICE_ROLE_KEY"))


def bucket() -> str:
    return str(_setting("SUPABASE_STORAGE_BUCKET", "documentos-akt"))


def upload_bytes(path: str, data: bytes, mime: str | None = None) -> str:
    base = str(_setting("SUPABASE_URL") or "").rstrip("/")
    key = _setting("SUPABASE_SERVICE_ROLE_KEY")
    if not base or not key:
        raise RuntimeError("Supabase Storage no está configurado.")
    quoted = "/".join(urllib.parse.quote(p, safe="") for p in path.split("/"))
    url = f"{base}/storage/v1/object/{bucket()}/{quoted}"
    req = urllib.request.Request(url, data=data or b"", method="POST")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("apikey", str(key))
    req.add_header("Content-Type", mime or "application/octet-stream")
    req.add_header("x-upsert", "false")
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"Storage respondió {resp.status}")
    return path


def download_bytes(path: str) -> bytes:
    base = str(_setting("SUPABASE_URL") or "").rstrip("/")
    key = _setting("SUPABASE_SERVICE_ROLE_KEY")
    if not base or not key:
        raise RuntimeError("Supabase Storage no está configurado.")
    quoted = "/".join(urllib.parse.quote(p, safe="") for p in path.split("/"))
    url = f"{base}/storage/v1/object/{bucket()}/{quoted}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("apikey", str(key))
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()
