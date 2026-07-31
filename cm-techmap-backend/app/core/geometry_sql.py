"""
CM TECHMAP — Fragmentos SQL de saneamento de geometria

As colunas `polygon` (ai_detections, parcels) são GEOMETRY(POLYGON, 4326).
Entradas do mundo real raramente são tão bem-comportadas: WKT desenhado à mão
vem com auto-interseções, GeoJSON de extração pode virar MULTIPOLYGON após o
ST_MakeValid, e um INSERT direto quebra na checagem de tipo da coluna.

Estas expressões normalizam qualquer entrada para UM polígono válido:
  1. ST_MakeValid  — conserta auto-interseções e anéis malformados
  2. ST_CollectionExtract(..., 3) — mantém apenas as partes poligonais
  3. ST_Dump + ORDER BY ST_Area DESC LIMIT 1 — fica com o maior polígono

Se a entrada não contém nenhum polígono (ex.: WKT de um ponto), o SELECT
retorna NULL — e a coluna NOT NULL (onde houver) rejeita a linha com um erro
claro em vez de gravar lixo.
"""

# :geom — string GeoJSON da geometria
LARGEST_POLYGON_FROM_GEOJSON = """
    (SELECT g FROM (
        SELECT (ST_Dump(ST_CollectionExtract(ST_MakeValid(
            ST_SetSRID(ST_GeomFromGeoJSON(:geom), 4326)), 3))).geom AS g
    ) _parts ORDER BY ST_Area(g) DESC LIMIT 1)
"""

# :wkt — string WKT (SRID 4326)
LARGEST_POLYGON_FROM_WKT = """
    (SELECT g FROM (
        SELECT (ST_Dump(ST_CollectionExtract(ST_MakeValid(
            ST_SetSRID(ST_GeomFromText(:wkt), 4326)), 3))).geom AS g
    ) _parts ORDER BY ST_Area(g) DESC LIMIT 1)
"""
