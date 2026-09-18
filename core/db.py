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
_schema_preflight_done = False


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


def _quote_table(engine, schema: str | None, table: str) -> str:
    prep = engine.dialect.identifier_preparer
    qt = prep.quote(table)
    if schema:
        return f"{prep.quote_schema(schema)}.{qt}"
    return qt


def _preflight_required_columns():
    """Garantiza columnas aditivas críticas antes de cualquier consulta ORM.

    SQLAlchemy create_all() no altera tablas existentes. Por eso una instalación
    previa puede tener proveedores sin columnas agregadas por versiones nuevas.
    Este guard es deliberadamente pequeño y seguro: solo agrega columnas
    opcionales conocidas y nunca elimina/renombra datos.
    """
    global _schema_preflight_done
    if _schema_preflight_done:
        return

    engine = get_engine()
    if engine.dialect.name != "postgresql":
        _schema_preflight_done = True
        return

    schema = _active_schema or "public"
    qprov = _quote_table(engine, schema, "proveedores")
    with engine.begin() as conn:
        conn.execute(text(
            f"ALTER TABLE IF EXISTS {qprov} "
            "ADD COLUMN IF NOT EXISTS ubicacion_origen VARCHAR(80)"
        ))
        conn.execute(text(
            f"ALTER TABLE IF EXISTS {qprov} "
            "ADD COLUMN IF NOT EXISTS ubicacion_destino VARCHAR(80)"
        ))
    _schema_preflight_done = True


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
        base_engine = create_engine(url, **_postgres_kwargs())
        if _active_schema != "public":
            # Fuerza todas las tablas y FKs del metadata de AKT al schema
            # dedicado. A diferencia de search_path, esto también hace que
            # create_all/checkfirst no confunda public.proveedores con la
            # tabla de AKT.
            _engine = base_engine.execution_options(
                schema_translate_map={None: _active_schema}
            )
        else:
            _engine = base_engine

    _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def is_postgres() -> bool:
    return get_engine().dialect.name.startswith("postgres")


def get_session():
    get_engine()
    _preflight_required_columns()
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

    # Si proveedores ya existe de una versión anterior, repara primero sus
    # columnas críticas. Si no existe, ALTER TABLE IF EXISTS no hace nada.
    _preflight_required_columns()

    Base.metadata.create_all(engine)

    # En una base nueva create_all ya crea las columnas. En una existente,
    # el preflight anterior las agregó antes de cualquier consulta ORM.
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
