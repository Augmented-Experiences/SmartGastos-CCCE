"""
Gestión de base de datos SQLite para pyme-ledger-ai.
Inicialización, sesiones y utilidades de persistencia.

Mejoras de concurrencia (Sprint 2):
- Reemplazado StaticPool por NullPool para evitar bloqueos en FastAPI async.
- Configurado WAL mode y busy_timeout para mejor concurrencia en SQLite.
- Agregado context manager para sesiones seguras.
"""
import os
import json
import logging
from pathlib import Path
from contextlib import contextmanager
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import NullPool
from models import Base

logger = logging.getLogger(__name__)

# Configuración — ruta absoluta desde __file__ como fallback
# database.py está en server/, el plugin está en server/../
_BASE_DIR = Path(__file__).parent.parent.resolve()
DATA_DIR = Path(os.environ.get("DATA_DIR", str(_BASE_DIR / "data")))
DB_PATH = DATA_DIR / "pyme_ledger.db"

# Crear directorio si no existe
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Crear engine SQLite con NullPool (cada request obtiene su propia conexión)
# Esto evita el problema "database is locked" en entornos async con múltiples hilos.
engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={
        "check_same_thread": False,
        "timeout": 30,  # busy_timeout en segundos para esperar si hay lock
    },
    poolclass=NullPool,  # Sin pool: cada sesión abre/cierra su propia conexión
    echo=False
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, connection_record):
    """Configura pragmas de SQLite para mejor concurrencia y rendimiento."""
    cursor = dbapi_conn.cursor()
    # WAL mode permite lecturas concurrentes con escrituras
    cursor.execute("PRAGMA journal_mode=WAL")
    # Timeout de 30 segundos si la DB está bloqueada
    cursor.execute("PRAGMA busy_timeout=30000")
    # Sync normal es suficiente con WAL
    cursor.execute("PRAGMA synchronous=NORMAL")
    # Cache de 64MB para mejor rendimiento
    cursor.execute("PRAGMA cache_size=-65536")
    cursor.close()


# Crear session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@contextmanager
def get_db_session():
    """Context manager para obtener una sesión de base de datos con cierre garantizado."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    """Inicializa la base de datos creando todas las tablas."""
    Base.metadata.create_all(bind=engine)
    _ensure_documento_columns()
    logger.info("Base de datos inicializada en: %s", DB_PATH)
    print(f"OK: Base de datos inicializada en: {DB_PATH}", flush=True)


def _ensure_documento_columns():
    """Agrega columnas nuevas en bases ya creadas (create_all no altera tablas)."""
    from sqlalchemy import inspect, text

    try:
        inspector = inspect(engine)
        if "documentos" not in inspector.get_table_names():
            return
        names = {col["name"] for col in inspector.get_columns("documentos")}
        if "tipo_cambio" not in names:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE documentos ADD COLUMN tipo_cambio FLOAT DEFAULT 1.0"
                ))
            logger.info("Columna documentos.tipo_cambio agregada")
    except Exception as exc:
        logger.warning("No se pudo migrar documentos.tipo_cambio: %s", exc)


def get_db() -> Session:
    """Obtiene una sesión de base de datos (generador para FastAPI Depends)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def save_json(path: Path, data: dict):
    """Guarda un diccionario como JSON con UTF-8."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_json(path: Path, default=None) -> dict:
    """Carga un JSON desde archivo."""
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"Error cargando JSON {path}: {e}")
            return default or {}
    return default or {}


def save_list_json(path: Path, data: list):
    """Guarda una lista como JSON con UTF-8."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_list_json(path: Path, default=None) -> list:
    """Carga una lista JSON desde archivo."""
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"Error cargando JSON {path}: {e}")
            return default or []
    return default or []
