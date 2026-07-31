"""Regression tests for the detection-persistence contract.

Production incident: the DSM pipeline extracted building footprints and
uploaded footprints.geojson to object storage, but never wrote rows into
ai_detections — the table the IPTU malha fina joins against. The fiscal
analysis therefore ALWAYS reported zero discrepancies, silently. These tests
pin the mapping between the extractor's GeoJSON and the table columns.
"""

from app.tasks.post_processing import _features_to_detection_rows


def _feature(**overrides):
    base = {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[-50.37, -15.44], [-50.369, -15.44],
                             [-50.369, -15.439], [-50.37, -15.439],
                             [-50.37, -15.44]]],
        },
        "properties": {
            "height": 4.2,
            "max_height": 6.1,
            "area_m2": 87.5,
            "building_type": "residencial",
        },
    }
    base.update(overrides)
    return base


class TestFeaturesToDetectionRows:
    def test_maps_extractor_keys_to_analysis_contract(self):
        # O extrator emite height/area_m2; a malha fina lê a COLUNA area_sqm
        # e properties->>'height_m'. Este mapeamento é o elo entre os dois.
        rows, skipped = _features_to_detection_rows([_feature()])
        assert skipped == 0
        assert len(rows) == 1
        row = rows[0]
        assert row["area"] == 87.5
        assert row["height"] == 4.2
        import json
        props = json.loads(row["props"])
        assert props["height_m"] == 4.2
        assert props["area_sqm"] == 87.5
        geom = json.loads(row["geom"])
        assert geom["type"] == "Polygon"

    def test_canonical_keys_take_precedence_when_present(self):
        f = _feature()
        f["properties"]["height_m"] = 9.9
        f["properties"]["area_sqm"] = 120.0
        rows, _ = _features_to_detection_rows([f])
        assert rows[0]["height"] == 9.9
        assert rows[0]["area"] == 120.0

    def test_skips_non_polygon_geometries_without_dropping_the_rest(self):
        point = _feature(geometry={"type": "Point", "coordinates": [0, 0]})
        rows, skipped = _features_to_detection_rows([point, _feature()])
        assert skipped == 1
        assert len(rows) == 1

    def test_multipolygon_is_accepted(self):
        mp = _feature(geometry={
            "type": "MultiPolygon",
            "coordinates": [[[[-50.37, -15.44], [-50.369, -15.44],
                              [-50.369, -15.439], [-50.37, -15.44]]]],
        })
        rows, skipped = _features_to_detection_rows([mp])
        assert skipped == 0
        assert len(rows) == 1

    def test_missing_properties_produce_safe_defaults(self):
        bare = _feature(properties={})
        rows, _ = _features_to_detection_rows([bare])
        row = rows[0]
        assert row["area"] == 0.0
        assert row["height"] is None
        assert 0.0 <= row["conf"] <= 1.0

    def test_empty_input(self):
        rows, skipped = _features_to_detection_rows([])
        assert rows == []
        assert skipped == 0
