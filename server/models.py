"""
Modelos de datos para pyme-ledger-ai.
Define las entidades principales: Empresa, Centro de Costo, Categoría Contable, Documento.
"""
from sqlalchemy import Column, String, Integer, Float, DateTime, Boolean, ForeignKey, Text, Enum
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime
import enum

Base = declarative_base()


class Empresa(Base):
    """Entidad que representa una empresa/PyME."""
    __tablename__ = "empresas"
    
    id = Column(String(36), primary_key=True)
    razon_social = Column(String(255), nullable=False)
    nombre_fantasia = Column(String(255))
    rut = Column(String(20), unique=True, nullable=False)
    pais = Column(String(50), default="Colombia")
    moneda_base = Column(String(3), default="COP")
    regimen_tributario = Column(String(50))  # Ej: "Régimen Simplificado", "Régimen General"
    carpeta_raiz = Column(String(500))  # Ruta a carpeta de documentos
    reglas_contables = Column(Text)  # JSON con reglas personalizadas
    activa = Column(Boolean, default=True)
    fecha_creacion = Column(DateTime, default=datetime.utcnow)
    fecha_actualizacion = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relaciones
    centros_costo = relationship("CentroCosto", back_populates="empresa", cascade="all, delete-orphan")
    categorias = relationship("CategoriaContable", back_populates="empresa", cascade="all, delete-orphan")
    documentos = relationship("Documento", back_populates="empresa", cascade="all, delete-orphan")


class CentroCosto(Base):
    """Entidad que representa un centro de costo dentro de una empresa."""
    __tablename__ = "centros_costo"
    
    id = Column(String(36), primary_key=True)
    empresa_id = Column(String(36), ForeignKey("empresas.id"), nullable=False)
    codigo = Column(String(50), nullable=False)
    nombre = Column(String(255), nullable=False)
    descripcion = Column(Text)
    padre_id = Column(String(36), ForeignKey("centros_costo.id"))  # Para jerarquía
    activo = Column(Boolean, default=True)
    etiquetas = Column(Text)  # JSON array de etiquetas
    fecha_creacion = Column(DateTime, default=datetime.utcnow)
    
    # Relaciones
    empresa = relationship("Empresa", back_populates="centros_costo")
    documentos = relationship("Documento", back_populates="centro_costo")


class CategoriaContable(Base):
    """Entidad que representa una categoría contable."""
    __tablename__ = "categorias_contables"
    
    id = Column(String(36), primary_key=True)
    empresa_id = Column(String(36), ForeignKey("empresas.id"), nullable=False)
    codigo = Column(String(50), nullable=False)
    nombre = Column(String(255), nullable=False)
    tipo_gasto = Column(String(50))  # Ej: "Operación", "Marketing", "Impuestos", "Inversión"
    deducibilidad = Column(String(50), default="Total")  # "Total", "Parcial", "No deducible"
    regla_iva = Column(String(50))  # "Recuperable", "No recuperable"
    keywords = Column(Text)  # JSON array de palabras clave para clasificación automática
    proveedores_frecuentes = Column(Text)  # JSON array de proveedores asociados
    activa = Column(Boolean, default=True)
    fecha_creacion = Column(DateTime, default=datetime.utcnow)
    
    # Relaciones
    empresa = relationship("Empresa", back_populates="categorias")
    documentos = relationship("Documento", back_populates="categoria")


class TipoDocumento(str, enum.Enum):
    """Tipos de documentos soportados."""
    FACTURA = "Factura"
    BOLETA = "Boleta"
    CARTOLA = "Cartola"
    COMPROBANTE = "Comprobante"
    OTRO = "Otro"


class EstadoRevision(str, enum.Enum):
    """Estados de revisión de documentos."""
    PENDIENTE = "Pendiente"
    REVISADO = "Revisado"
    RECHAZADO = "Rechazado"
    DUPLICADO = "Duplicado"


class Documento(Base):
    """Entidad que representa un documento contable (factura, boleta, etc.)."""
    __tablename__ = "documentos"
    
    id = Column(String(36), primary_key=True)
    empresa_id = Column(String(36), ForeignKey("empresas.id"), nullable=False)
    tipo_documento = Column(Enum(TipoDocumento), default=TipoDocumento.OTRO)
    
    # Información del proveedor
    proveedor = Column(String(255))
    rut_proveedor = Column(String(20))
    
    # Información financiera
    fecha_emision = Column(DateTime)
    folio = Column(String(50))
    monto_neto = Column(Float, default=0.0)
    iva = Column(Float, default=0.0)
    monto_total = Column(Float, default=0.0)
    moneda = Column(String(3), default="COP")
    tipo_cambio = Column(Float, default=1.0)  # 1 unidad de moneda = N de moneda_base
    
    # Clasificación
    categoria_id = Column(String(36), ForeignKey("categorias_contables.id"))
    categoria_sugerida = Column(String(255))
    confianza_clasificacion = Column(Float, default=0.0)  # 0.0 a 1.0
    
    centro_costo_id = Column(String(36), ForeignKey("centros_costo.id"))
    centro_costo_sugerido = Column(String(255))
    
    # Revisión
    estado_revision = Column(Enum(EstadoRevision), default=EstadoRevision.PENDIENTE)
    notas_revision = Column(Text)
    revisado_por = Column(String(255))
    fecha_revision = Column(DateTime)
    
    # Archivo
    ruta_archivo_original = Column(String(500))
    ruta_archivo_final = Column(String(500))
    hash_documento = Column(String(64), unique=True)  # SHA256 para detectar duplicados
    
    # Procesamiento
    motor_ocr = Column(String(50))  # "Tesseract", "EasyOCR", "Directo"
    modelo_extraccion = Column(String(50))  # Modelo LLM usado
    texto_extraido = Column(Text)  # Texto OCR/extraído
    campos_extraidos = Column(Text)  # JSON con campos clave extraídos
    
    # Auditoría
    excepciones = Column(Text)  # JSON array de excepciones detectadas
    fecha_creacion = Column(DateTime, default=datetime.utcnow)
    fecha_actualizacion = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relaciones
    empresa = relationship("Empresa", back_populates="documentos")
    categoria = relationship("CategoriaContable", back_populates="documentos")
    centro_costo = relationship("CentroCosto", back_populates="documentos")


class AgentConfig(Base):
    """Configuración de agentes LLM."""
    __tablename__ = "agent_configs"
    
    id = Column(String(36), primary_key=True)
    nombre = Column(String(255), nullable=False)
    tipo = Column(String(50))  # "OCR", "Extractor", "Clasificador", "Auditor", "Recomendador", "Analítico"
    modelo = Column(String(100), nullable=False)
    system_prompt = Column(Text)
    temperatura = Column(Float, default=0.7)
    max_tokens = Column(Integer, default=2048)
    timeout = Column(Integer, default=300)
    activo = Column(Boolean, default=True)
    fecha_creacion = Column(DateTime, default=datetime.utcnow)
    fecha_actualizacion = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SesionProcesamiento(Base):
    """Registro de sesiones de procesamiento de documentos."""
    __tablename__ = "sesiones_procesamiento"
    
    id = Column(String(36), primary_key=True)
    empresa_id = Column(String(36), ForeignKey("empresas.id"), nullable=False)
    fecha_inicio = Column(DateTime, default=datetime.utcnow)
    fecha_fin = Column(DateTime)
    cantidad_documentos = Column(Integer, default=0)
    documentos_procesados = Column(Integer, default=0)
    documentos_con_error = Column(Integer, default=0)
    estado = Column(String(50), default="En progreso")  # "En progreso", "Completado", "Error"
    log_procesamiento = Column(Text)  # JSON con detalles del procesamiento
    ruta_carpeta_origen = Column(String(500))
    ruta_carpeta_salida = Column(String(500))
