"""Almacenamiento de evidencias en Supabase Storage con fallback transparente.

Secrets opcionales:
SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY
SUPABASE_STORAGE_BUCKET (default: akt-documentos)
"""
from __future__ import annotations

import os
import re
import uuid

import requests


def _cfg():
    try:
        import streamlit as st
        url = st.secrets.get("SUPABASE_URL")
        key = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY")
        bucket = st.secrets.get("SUPABASE_STORAGE_BUCKET", "akt-documentos")
    except Exception:
        url = key = None
        bucket = "akt-documentos"
    return (url or os.environ.get("SUPABASE_URL"),
            key or os.environ.get("SUPABASE_SERVICE_ROLE_KEY"),
            os.environ.get("SUPABASE_STORAGE_BUCKET", bucket))


def _headers(key, mime=None):
    h = {"Authorization": f"Bearer {key}", "apikey": key}
    if mime:
        h["Content-Type"] = mime
    return h


def _ensure_bucket(url, key, bucket):
    r = requests.get(f"{url}/storage/v1/bucket/{bucket}", headers=_headers(key), timeout=15)
    if r.status_code == 200:
        return
    requests.post(f"{url}/storage/v1/bucket", headers={**_headers(key), "Content-Type": "application/json"},
                  json={"id": bucket, "name": bucket, "public": False}, timeout=15)


def upload_bytes(nombre: str, contenido: bytes, mime=None) -> str | None:
    url, key, bucket = _cfg()
    if not url or not key:
        return None
    _ensure_bucket(url.rstrip("/"), key, bucket)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", nombre or "archivo")
    path = f"{uuid.uuid4().hex}/{safe}"
    endpoint = f"{url.rstrip('/')}/storage/v1/object/{bucket}/{path}"
    r = requests.post(endpoint, headers={**_headers(key, mime), "x-upsert": "false"},
                      data=contenido, timeout=60)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Supabase Storage respondió {r.status_code}: {r.text[:200]}")
    return f"{bucket}/{path}"


def download_bytes(storage_path: str) -> bytes:
    url, key, _ = _cfg()
    if not url or not key:
        raise RuntimeError("Supabase Storage no está configurado.")
    bucket, path = storage_path.split("/", 1)
    endpoint = f"{url.rstrip('/')}/storage/v1/object/authenticated/{bucket}/{path}"
    r = requests.get(endpoint, headers=_headers(key), timeout=60)
    r.raise_for_status()
    return r.content
