"""Conexión a base de datos con aislamiento automático de la app AKT.

En PostgreSQL/Supabase:
- Si public.proveedores no existe o ya corresponde a AKT, usa public.
- Si public.proveedores pertenece a otra aplicación, crea/usa el schema
  akt_proveedores y fija search_path=akt_proveedores,public.

Esto permite compartir un proyecto Supabase sin colisionar nombres de tablas.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from core.models import Base

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

AKT_SCHEMA = "akt_proveedores"
_AKT_PROVIDER_SIGNATURE = {"id", "codigo", "nombre", "tolerancia_averia_pct"}

_engine = None
_Session = None
_active_schema = None


def _resolve_url() -> str:
    url = None
    try:
        import streamlit as st  # noqa
        url = st.secrets.get("DATABASE_URL")  # type: ignore[attr-defined]
    except Exception:
        url = None
    url = url or os.environ.get("DATABASE_URL")
    if not url:
        url = f"sqlite:///{DATA_DIR / 'akt.db'}"
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def _postgres_kwargs():
    return {
        "pool_pre_ping": True,
        "future": True,
        "pool_size": 5,
        "max_overflow": 10,
        "pool_recycle": 1800,
    }


def _detectar_schema_postgres(url: str) -> str:
    """Detecta si public es compatible con AKT sin modificar tablas existentes."""
    probe = create_engine(url, **_postgres_kwargs())
    try:
        with probe.connect() as conn:
            columnas = set(conn.execute(text("""
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND table_name = 'proveedores'
            """)).scalars().all())

        # Base nueva o base ya propia de AKT: conserva public y datos existentes.
        if not columnas or _AKT_PROVIDER_SIGNATURE.issubset(columnas):
            return "public"

        # Colisión: public.proveedores pertenece a otra aplicación.
        # Crear un schema separado es una operación aditiva y no altera public.
        with probe.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{AKT_SCHEMA}"'))
        return AKT_SCHEMA
    finally:
        probe.dispose()


def get_active_schema() -> str | None:
    """Schema lógico de AKT. En SQLite devuelve None."""
    get_engine()
    return _active_schema


def get_engine():
    global _engine, _Session, _active_schema
    if _engine is not None:
        return _engine

    url = _resolve_url()
    if url.startswith("sqlite"):
        _active_schema = None
        _engine = create_engine(
            url, pool_pre_ping=True, future=True,
            connect_args={"check_same_thread": False})

        @event.listens_for(_engine, "connect")
        def _fk_on(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
    else:
        _active_schema = _detectar_schema_postgres(url)
        kwargs = _postgres_kwargs()
        if _active_schema != "public":
            kwargs["connect_args"] = {
                "options": f"-csearch_path={_active_schema},public"
            }
        _engine = create_engine(url, **kwargs)

    _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def is_postgres() -> bool:
    return get_engine().dialect.name.startswith("postgres")


def get_session():
    get_engine()
    return _Session()


@contextmanager
def session_scope():
    s = get_session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_db():
    """Crea/migra únicamente las tablas pertenecientes al schema activo de AKT."""
    engine = get_engine()
    Base.metadata.create_all(engine)
    from core.migrations import run_migrations
    run_migrations(engine, schema=get_active_schema())


def healthcheck() -> tuple[bool, str]:
    try:
        with get_engine().connect() as c:
            c.execute(text("SELECT 1"))
        schema = get_active_schema()
        detalle = get_engine().dialect.name
        if schema:
            detalle += f" · schema={schema}"
        return True, detalle
    except Exception as exc:  # pragma: no cover
        return False, str(exc)


def db_label() -> str:
    url = _resolve_url()
    if url.startswith("sqlite"):
        return "SQLite local (demo)"
    host = url.split("@")[-1].split("/")[0] if "@" in url else "postgres"
    try:
        schema = get_active_schema() or "public"
    except Exception:
        schema = "?"
    return f"Supabase / PostgreSQL · {host} · {schema}"
