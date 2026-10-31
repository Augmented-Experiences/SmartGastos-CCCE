"""
Orquestador de pipeline de procesamiento de documentos.
Coordina OCR → Extracción → Clasificación → Auditoría.
"""
import os
import json
import uuid
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime
from sqlalchemy.orm import Session

from agents import get_ocr_agent, get_extractor_agent, get_classifier_agent, get_auditor_agent
from models import Documento, Empresa, CategoriaContable, CentroCosto, EstadoRevision, TipoDocumento


class DocumentProcessingPipeline:
    """Orquestador del pipeline de procesamiento de documentos."""
    
    def __init__(self, db: Session, empresa_id: str):
        """
        Inicializa el pipeline.
        
        Args:
            db: Sesión de base de datos
            empresa_id: ID de la empresa
        """
        self.db = db
        self.empresa_id = empresa_id
        self.ocr_agent = get_ocr_agent()
        self.extractor_agent = get_extractor_agent()
        self.classifier_agent = get_classifier_agent()
        self.auditor_agent = get_auditor_agent()
    
    def process_document(self, file_path: str, document_type: str = None) -> Dict:
        """
        Procesa un documento completo a través del pipeline.
        
        Args:
            file_path: Ruta al archivo del documento
            document_type: Tipo de documento (Factura, Boleta, etc.)
            
        Returns:
            Dict con resultado del procesamiento
        """
        result = {
            "id": str(uuid.uuid4()),
            "file": file_path,
            "status": "processing",
            "steps": {},
            "errors": []
        }
        
        try:
            # Paso 1: OCR
            result["steps"]["ocr"] = self._step_ocr(file_path)
            if result["steps"]["ocr"].get("error"):
                result["errors"].append(result["steps"]["ocr"]["error"])
                result["status"] = "error"
                return result
            
            text_extracted = result["steps"]["ocr"].get("text", "")
            
            # Paso 2: Extracción de campos
            result["steps"]["extraction"] = self._step_extraction(text_extracted)
            fields = result["steps"]["extraction"].get("fields", {})
            
            # Paso 3: Clasificación
            categories = self._get_categories()
            cost_centers = self._get_cost_centers()
            result["steps"]["classification"] = self._step_classification(
                fields, categories, cost_centers
            )
            
            # Paso 4: Auditoría
            historical_docs = self._get_historical_documents()
            result["steps"]["audit"] = self._step_audit(
                fields, historical_docs
            )
            
            # Paso 5: Guardar en BD
            doc_record = self._save_to_database(
                file_path,
                text_extracted,
                fields,
                result["steps"]["classification"],
                result["steps"]["audit"],
                document_type
            )
            
            result["id"] = doc_record.id
            result["status"] = "completed"
            result["database_id"] = doc_record.id
            result["requires_review"] = (
                result["steps"]["classification"].get("confianza", 0) < 0.6 or
                result["steps"]["audit"].get("requiere_revision", False)
            )
            
        except Exception as e:
            result["status"] = "error"
            result["errors"].append(str(e))
        
        return result
    
    def _step_ocr(self, file_path: str) -> Dict:
        """Ejecuta paso de OCR."""
        try:
            result = self.ocr_agent.extract_from_file(file_path)
            return {
                "text": result.get("text", ""),
                "method": result.get("method", "unknown"),
                "hash": result.get("hash"),
                "error": result.get("error")
            }
        except Exception as e:
            return {"error": f"Error en OCR: {str(e)}", "text": ""}
    
    def _step_extraction(self, text: str) -> Dict:
        """Ejecuta paso de extracción."""
        try:
            fields = self.extractor_agent.extract_fields(text)
            return {
                "fields": fields,
                "success": not fields.get("_extraction_error")
            }
        except Exception as e:
            return {
                "error": f"Error en extracción: {str(e)}",
                "fields": {}
            }
    
    def _step_classification(self, fields: Dict, categories: list, cost_centers: list) -> Dict:
        """Ejecuta paso de clasificación."""
        try:
            result = self.classifier_agent.classify(fields, categories, cost_centers)
            return result
        except Exception as e:
            return {
                "error": f"Error en clasificación: {str(e)}",
                "categoria_sugerida": None,
                "confianza": 0.0
            }
    
    def _step_audit(self, fields: Dict, historical_docs: list) -> Dict:
        """Ejecuta paso de auditoría."""
        try:
            result = self.auditor_agent.audit(fields, historical_docs)
            return result
        except Exception as e:
            return {
                "error": f"Error en auditoría: {str(e)}",
                "excepciones": [],
                "requiere_revision": True
            }
    
    def _get_categories(self) -> list:
        """Obtiene categorías contables de la empresa."""
        try:
            categories = self.db.query(CategoriaContable).filter(
                CategoriaContable.empresa_id == self.empresa_id,
                CategoriaContable.activa == True
            ).all()
            
            return [
                {
                    "id": c.id,
                    "nombre": c.nombre,
                    "tipo_gasto": c.tipo_gasto,
                    "keywords": c.keywords,
                    "deducibilidad": c.deducibilidad
                }
                for c in categories
            ]
        except Exception as e:
            print(f"Error obteniendo categorías: {e}")
            return []
    
    def _get_cost_centers(self) -> list:
        """Obtiene centros de costo de la empresa."""
        try:
            centers = self.db.query(CentroCosto).filter(
                CentroCosto.empresa_id == self.empresa_id,
                CentroCosto.activo == True
            ).all()
            
            return [
                {
                    "id": c.id,
                    "codigo": c.codigo,
                    "nombre": c.nombre,
                    "descripcion": c.descripcion
                }
                for c in centers
            ]
        except Exception as e:
            print(f"Error obteniendo centros de costo: {e}")
            return []
    
    def _get_historical_documents(self, limit: int = 50) -> list:
        """Obtiene documentos históricos para comparación."""
        try:
            docs = self.db.query(Documento).filter(
                Documento.empresa_id == self.empresa_id
            ).order_by(Documento.fecha_creacion.desc()).limit(limit).all()
            
            return [
                {
                    "id": d.id,
                    "proveedor": d.proveedor,
                    "folio": d.folio,
                    "monto_total": d.monto_total,
                    "fecha_emision": d.fecha_emision.isoformat() if d.fecha_emision else None,
                    "categoria_id": d.categoria_id
                }
                for d in docs
            ]
        except Exception as e:
            print(f"Error obteniendo documentos históricos: {e}")
            return []
    
    def _save_to_database(self, file_path: str, text: str, fields: Dict,
                         classification: Dict, audit: Dict, doc_type: str = None) -> Documento:
        """Guarda el documento procesado en la base de datos."""
        
        # Determinar tipo de documento
        if not doc_type:
            doc_type_str = fields.get("tipo_documento", "Otro")
        else:
            doc_type_str = doc_type
        
        # Mapear a enum
        try:
            tipo_enum = TipoDocumento[doc_type_str.upper().replace(" ", "_")]
        except KeyError:
            tipo_enum = TipoDocumento.OTRO
        
        # Buscar categoría por nombre
        categoria_id = None
        if classification.get("categoria_sugerida"):
            categoria = self.db.query(CategoriaContable).filter(
                CategoriaContable.empresa_id == self.empresa_id,
                CategoriaContable.nombre == classification["categoria_sugerida"]
            ).first()
            if categoria:
                categoria_id = categoria.id
        
        # Buscar centro de costo por código
        centro_costo_id = None
        if classification.get("centro_costo_sugerido"):
            cc = self.db.query(CentroCosto).filter(
                CentroCosto.empresa_id == self.empresa_id,
                CentroCosto.codigo == classification["centro_costo_sugerido"]
            ).first()
            if cc:
                centro_costo_id = cc.id
        
        # Crear documento
        documento = Documento(
            id=str(uuid.uuid4()),
            empresa_id=self.empresa_id,
            tipo_documento=tipo_enum,
            proveedor=fields.get("proveedor"),
            rut_proveedor=fields.get("rut_proveedor"),
            fecha_emision=fields.get("fecha_emision"),
            folio=fields.get("folio"),
            monto_neto=fields.get("monto_neto"),
            iva=fields.get("iva"),
            monto_total=fields.get("monto_total"),
            moneda=fields.get("moneda", "CLP"),
            categoria_id=categoria_id,
            categoria_sugerida=classification.get("categoria_sugerida"),
            confianza_clasificacion=classification.get("confianza", 0.0),
            centro_costo_id=centro_costo_id,
            centro_costo_sugerido=classification.get("centro_costo_sugerido"),
            estado_revision=EstadoRevision.PENDIENTE if audit.get("requiere_revision") else EstadoRevision.REVISADO,
            ruta_archivo_original=file_path,
            hash_documento=audit.get("hash_documento"),
            texto_extraido=text[:5000],  # Limitar a 5000 caracteres
            campos_extraidos=json.dumps(fields, ensure_ascii=False),
            excepciones=json.dumps(audit.get("excepciones", []), ensure_ascii=False)
        )
        
        self.db.add(documento)
        self.db.commit()
        
        return documento
    
    def batch_process(self, folder_path: str, file_extensions: list = None) -> Dict:
        """
        Procesa múltiples documentos de una carpeta.
        
        Args:
            folder_path: Ruta a la carpeta
            file_extensions: Extensiones a procesar
            
        Returns:
            Resumen del procesamiento
        """
        if file_extensions is None:
            file_extensions = [".pdf", ".jpg", ".jpeg", ".png"]
        
        folder = Path(folder_path)
        results = []
        
        for file_path in sorted(folder.rglob("*")):
            if file_path.suffix.lower() in file_extensions:
                result = self.process_document(str(file_path))
                results.append(result)
        
        # Resumen
        summary = {
            "total": len(results),
            "completados": len([r for r in results if r["status"] == "completed"]),
            "errores": len([r for r in results if r["status"] == "error"]),
            "requieren_revision": len([r for r in results if r.get("requires_review")]),
            "resultados": results
        }
        
        return summary
