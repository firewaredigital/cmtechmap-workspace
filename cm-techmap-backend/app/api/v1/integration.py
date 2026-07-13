"""
CM TECHMAP — Integration API
Import cadastral data from municipal systems and export decisions.
"""

import csv
import io
import json
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, require_gestor, require_tenant_admin

logger = logging.getLogger("cm_techmap.api.integration")

router = APIRouter(prefix="/integration", tags=["Integration"])


# ══════════════════════════════════════════════════════════════════════════════
# IMPORT
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/import/csv")
async def import_cadastro_csv(
    file: UploadFile = File(..., description="CSV file with cadastral data"),
    col_cadastral_code: str = Query("inscricao", description="Column name for cadastral code"),
    col_address: str = Query("endereco", description="Column name for address"),
    col_neighborhood: str = Query("bairro", description="Column name for neighborhood"),
    col_owner: str = Query("proprietario", description="Column name for owner"),
    col_area_terreno: str = Query("area_terreno", description="Column for lot area (m²)"),
    col_area_construida: str = Query("area_construida", description="Column for built area (m²)"),
    col_uso_solo: str = Query("uso_solo", description="Column for land use"),
    col_zona_fiscal: str = Query("zona_fiscal", description="Column for IPTU zone"),
    col_iptu_valor: str = Query("valor_iptu", description="Column for current IPTU value"),
    col_cpf_cnpj: str = Query("cpf_cnpj", description="Column for owner CPF/CNPJ"),
    encoding: str = Query("utf-8", description="File encoding (utf-8, latin-1, etc.)"),
    delimiter: str = Query(",", max_length=1),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_tenant_admin),
):
    """
    Import cadastral data from a CSV file.
    Columns are mapped via query parameters for maximum flexibility.
    Uses UPSERT — existing parcels (by cadastral_code) are updated.
    """
    if not file.filename or not file.filename.lower().endswith((".csv", ".txt")):
        raise HTTPException(400, "Arquivo deve ser CSV (.csv ou .txt)")

    content = await file.read()
    try:
        text_content = content.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        raise HTTPException(400, f"Falha ao decodificar arquivo com encoding '{encoding}'")

    reader = csv.DictReader(io.StringIO(text_content), delimiter=delimiter)
    if not reader.fieldnames:
        raise HTTPException(400, "CSV sem cabeçalho")

    # Preview: return field names if no data
    available_columns = list(reader.fieldnames)

    imported = 0
    errors = 0
    error_details = []

    for i, row in enumerate(reader, start=2):
        cadastral_code = (row.get(col_cadastral_code) or "").strip()
        if not cadastral_code:
            errors += 1
            error_details.append({"line": i, "error": "Código cadastral vazio"})
            continue

        try:
            area_terreno = float(row.get(col_area_terreno, "0") or "0")
        except ValueError:
            area_terreno = None
        try:
            area_construida = float(row.get(col_area_construida, "0") or "0")
        except ValueError:
            area_construida = None
        try:
            iptu_valor = float(row.get(col_iptu_valor, "0") or "0")
        except ValueError:
            iptu_valor = None

        try:
            await db.execute(text("""
                INSERT INTO parcels
                    (cadastral_code, address, neighborhood, owner_name, owner_cpf_cnpj,
                     registered_area_sqm, registered_built_area_sqm,
                     land_use, iptu_zone, iptu_value_current_brl)
                VALUES (:code, :addr, :neigh, :owner, :cpf,
                        :area, :built, :uso, :zona, :iptu)
                ON CONFLICT (cadastral_code) DO UPDATE SET
                    address = COALESCE(EXCLUDED.address, parcels.address),
                    neighborhood = COALESCE(EXCLUDED.neighborhood, parcels.neighborhood),
                    owner_name = COALESCE(EXCLUDED.owner_name, parcels.owner_name),
                    owner_cpf_cnpj = COALESCE(EXCLUDED.owner_cpf_cnpj, parcels.owner_cpf_cnpj),
                    registered_area_sqm = COALESCE(EXCLUDED.registered_area_sqm, parcels.registered_area_sqm),
                    registered_built_area_sqm = COALESCE(EXCLUDED.registered_built_area_sqm, parcels.registered_built_area_sqm),
                    land_use = COALESCE(EXCLUDED.land_use, parcels.land_use),
                    iptu_zone = COALESCE(EXCLUDED.iptu_zone, parcels.iptu_zone),
                    iptu_value_current_brl = COALESCE(EXCLUDED.iptu_value_current_brl, parcels.iptu_value_current_brl),
                    updated_at = NOW()
            """), {
                "code": cadastral_code,
                "addr": (row.get(col_address) or "").strip() or None,
                "neigh": (row.get(col_neighborhood) or "").strip() or None,
                "owner": (row.get(col_owner) or "").strip() or None,
                "cpf": (row.get(col_cpf_cnpj) or "").strip() or None,
                "area": area_terreno,
                "built": area_construida,
                "uso": (row.get(col_uso_solo) or "").strip() or None,
                "zona": (row.get(col_zona_fiscal) or "").strip() or None,
                "iptu": iptu_valor,
            })
            imported += 1
        except Exception as e:
            errors += 1
            error_details.append({"line": i, "code": cadastral_code, "error": str(e)[:200]})

    await db.commit()

    return {
        "imported": imported,
        "errors": errors,
        "total_lines": imported + errors,
        "available_columns": available_columns,
        "error_details": error_details[:20],  # Limit error details
    }


@router.post("/import/csv/preview")
async def preview_csv(
    file: UploadFile = File(...),
    encoding: str = Query("utf-8"),
    delimiter: str = Query(",", max_length=1),
    preview_rows: int = Query(5, ge=1, le=20),
):
    """Preview CSV contents: show columns and first N rows without importing."""
    content = await file.read()
    try:
        text_content = content.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        raise HTTPException(400, f"Falha ao decodificar com encoding '{encoding}'")

    reader = csv.DictReader(io.StringIO(text_content), delimiter=delimiter)
    columns = list(reader.fieldnames or [])

    rows = []
    for i, row in enumerate(reader):
        if i >= preview_rows:
            break
        rows.append(dict(row))

    return {
        "columns": columns,
        "preview_rows": rows,
        "total_columns": len(columns),
    }


# ══════════════════════════════════════════════════════════════════════════════
# EXPORT
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/export/decisions")
async def export_decisions(
    project_id: UUID | None = Query(None),
    status: str = Query("approved"),
    format: str = Query("csv", description="csv|json"),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_gestor),
):
    """
    Export approved/rejected decisions for the municipal system.
    CSV format matches common municipal software import requirements.
    """
    conditions = ["d.status = :status"]
    params: dict[str, Any] = {"status": status}

    if project_id:
        conditions.append("d.project_id = :pid")
        params["pid"] = str(project_id)

    where = " AND ".join(conditions)

    result = await db.execute(text(f"""
        SELECT d.cadastral_code,
               d.address,
               d.neighborhood,
               d.owner_name,
               d.registered_area_sqm,
               d.detected_area_sqm,
               d.difference_sqm,
               d.iptu_current_brl,
               d.iptu_proposed_brl,
               d.estimated_iptu_gap_brl,
               d.discrepancy_type,
               d.status,
               d.reviewed_at,
               d.rejection_reason
        FROM discrepancies d
        WHERE {where}
        ORDER BY d.cadastral_code
    """), params)

    rows = []
    for r in result.mappings().all():
        row = dict(r)
        if row.get("reviewed_at"):
            row["reviewed_at"] = row["reviewed_at"].isoformat()
        rows.append(row)

    if format == "csv":
        if not rows:
            return {"csv": "", "count": 0}
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
        return {"csv": output.getvalue(), "count": len(rows), "format": "csv"}

    return {"data": rows, "count": len(rows), "format": "json"}


# ══════════════════════════════════════════════════════════════════════════════
# COLUMN MAPPINGS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/mappings/suggested")
async def get_suggested_mappings():
    """Return suggested column mappings for common municipal CSV formats."""
    return {
        "mappings": [
            {
                "name": "Padrão CM TechMap",
                "columns": {
                    "col_cadastral_code": "inscricao",
                    "col_address": "endereco",
                    "col_neighborhood": "bairro",
                    "col_owner": "proprietario",
                    "col_area_terreno": "area_terreno",
                    "col_area_construida": "area_construida",
                    "col_uso_solo": "uso_solo",
                    "col_zona_fiscal": "zona_fiscal",
                    "col_iptu_valor": "valor_iptu",
                    "col_cpf_cnpj": "cpf_cnpj",
                },
            },
            {
                "name": "Betha Sistemas",
                "columns": {
                    "col_cadastral_code": "INSCRICAO_IMOBILIARIA",
                    "col_address": "ENDERECO_COMPLETO",
                    "col_neighborhood": "BAIRRO",
                    "col_owner": "NOME_CONTRIBUINTE",
                    "col_area_terreno": "AREA_TERRENO_M2",
                    "col_area_construida": "AREA_CONSTRUIDA_M2",
                    "col_uso_solo": "TIPO_USO",
                    "col_zona_fiscal": "ZONA_FISCAL",
                    "col_iptu_valor": "VALOR_LANCAMENTO",
                    "col_cpf_cnpj": "CPF_CNPJ",
                },
            },
            {
                "name": "IPM Sistemas",
                "columns": {
                    "col_cadastral_code": "cd_inscricao",
                    "col_address": "nm_logradouro",
                    "col_neighborhood": "nm_bairro",
                    "col_owner": "nm_proprietario",
                    "col_area_terreno": "nr_area_terreno",
                    "col_area_construida": "nr_area_construida",
                    "col_uso_solo": "tp_uso_solo",
                    "col_zona_fiscal": "cd_zona",
                    "col_iptu_valor": "vl_iptu",
                    "col_cpf_cnpj": "nr_cpf_cnpj",
                },
            },
        ],
    }
