"""
Conexión a base de datos.

Funciona igual en local (SQLite) y en producción (Supabase / PostgreSQL).
Orden de resolución de la cadena de conexión:
  1. st.secrets["DATABASE_URL"]      (Streamlit Cloud)
  2. variable de entorno DATABASE_URL
  3. SQLite local en data/akt.db      (desarrollo / demo)
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

_engine = None
_Session = None


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
    # Supabase entrega postgres://, SQLAlchemy 2 exige postgresql+psycopg2://
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def get_engine():
    global _engine, _Session
    if _engine is None:
        url = _resolve_url()
        kwargs = {"pool_pre_ping": True, "future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            kwargs.update(pool_size=5, max_overflow=10, pool_recycle=1800)
        _engine = create_engine(url, **kwargs)

        if url.startswith("sqlite"):
            @event.listens_for(_engine, "connect")
            def _fk_on(dbapi_conn, _):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA journal_mode=WAL")
                cur.close()

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
    """Crea tablas nuevas y aplica migraciones incrementales a tablas existentes."""
    engine = get_engine()
    Base.metadata.create_all(engine)
    from core.migrations import run_migrations
    run_migrations(engine)


def healthcheck() -> tuple[bool, str]:
    try:
        with get_engine().connect() as c:
            c.execute(text("SELECT 1"))
        return True, get_engine().dialect.name
    except Exception as exc:  # pragma: no cover
        return False, str(exc)


def db_label() -> str:
    url = _resolve_url()
    if url.startswith("sqlite"):
        return "SQLite local (demo)"
    host = url.split("@")[-1].split("/")[0] if "@" in url else "postgres"
    return f"Supabase / PostgreSQL · {host}"
