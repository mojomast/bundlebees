"""JsonQueue reader: the live dispatch-queue envelope and its flat-map contract.

The live factory-hook/driver pass reads an envelope shaped

    {"schema": "bundlebees.queue/1", "generated_at": ..., "features": [], "note": ...}

whose empty ``features`` list used to reach ``body.get`` and crash every pass
with ``AttributeError: 'list' object has no attribute 'get'``. These tests pin
the reader to that shape without relaxing the flat ``{feature_id: card}`` map
that every existing queue test uses, and they only use scratch directories.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bundlebees_plugin import queue as Q


def card(feature_id, *, job_id="job_1", repo="mojomast/smoke-real", status="queued", deps=()):
    return {"repo": repo, "job_id": job_id, "title": feature_id, "status": status, "depends_on": list(deps)}


LIVE = {
    "schema": "bundlebees.queue/1",
    "generated_at": "2026-11-18T00:00:00Z",
    "features": [],
    "note": "published by the dispatch lane",
}


def write(path: Path, document) -> Q.JsonQueue:
    path.write_text(json.dumps(document), encoding="utf-8")
    return Q.JsonQueue(path)


def test_the_live_empty_envelope_reads_as_an_empty_queue(tmp_path):
    backend = write(tmp_path / "queue.json", LIVE)

    assert backend.list_items() == []
    assert backend.get("f1") is None


def test_a_schema_less_features_list_is_still_an_envelope(tmp_path):
    backend = write(tmp_path / "queue.json", {"features": []})

    assert backend.list_items() == []


def test_a_flat_map_with_a_feature_named_features_stays_flat(tmp_path):
    backend = write(tmp_path / "queue.json", {"features": card("features")})

    items = backend.list_items()
    assert [item.feature_id for item in items] == ["features"]
    assert items[0].job_id == "job_1"


def test_an_envelope_features_mapping_matches_the_equivalent_flat_map(tmp_path):
    features = {"f1": card("f1"), "f2": card("f2", job_id="job_2", deps=["f1"])}
    envelope = {**LIVE, "features": features}
    flat = write(tmp_path / "flat.json", features)
    wrapped = write(tmp_path / "queue.json", envelope)

    assert wrapped.list_items() == flat.list_items()
    assert [(i.feature_id, i.job_id, i.depends_on, i.status) for i in wrapped.list_items()] == [
        ("f1", "job_1", (), "queued"),
        ("f2", "job_2", ("f1",), "queued"),
    ]
    assert wrapped.get("f2").job_id == "job_2"


def test_an_envelope_features_list_maps_cards_by_feature_id_or_id(tmp_path):
    envelope = {
        **LIVE,
        "features": [
            {**card("f1"), "feature_id": "f1"},
            {**card("f2", job_id="job_2"), "id": "f2"},
        ],
    }
    backend = write(tmp_path / "queue.json", envelope)

    items = backend.list_items()
    assert [item.feature_id for item in items] == ["f1", "f2"]
    assert [item.job_id for item in items] == ["job_1", "job_2"]


def test_set_status_on_an_envelope_preserves_schema_generated_at_and_note(tmp_path):
    path = tmp_path / "queue.json"
    backend = write(path, {**LIVE, "features": {"f1": card("f1")}})

    backend.set_status("f1", "running", {"job_id": "job_1"})

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["schema"] == "bundlebees.queue/1"
    assert saved["generated_at"] == "2026-11-18T00:00:00Z"
    assert saved["note"] == "published by the dispatch lane"
    assert set(saved) == {"schema", "generated_at", "features", "note"}
    assert saved["features"]["f1"]["status"] == "running"
    assert saved["features"]["f1"]["evidence"] == {"job_id": "job_1"}
    assert saved["features"]["f1"]["updated_at"]
    assert backend.get("f1").status == "running"


def test_set_status_on_an_envelope_feature_list_updates_the_card_in_place(tmp_path):
    path = tmp_path / "queue.json"
    backend = write(path, {**LIVE, "features": [{**card("f1"), "feature_id": "f1"}]})

    backend.set_status("f1", "blocked")

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["features"][0]["feature_id"] == "f1"
    assert saved["features"][0]["status"] == "blocked"
    assert backend.get("f1").status == "blocked"


def test_set_status_unknown_envelope_feature_is_queue_unknown(tmp_path):
    backend = write(tmp_path / "queue.json", {**LIVE, "features": {"f1": card("f1")}})

    with pytest.raises(Q.QueueError) as caught:
        backend.set_status("missing", "running")

    assert caught.value.code == "queue_unknown"
    assert caught.value.fix


def test_a_flat_map_is_unchanged_including_set_status_shape(tmp_path):
    path = tmp_path / "queue.json"
    backend = write(path, {"f1": card("f1")})

    assert [item.feature_id for item in backend.list_items()] == ["f1"]
    backend.set_status("f1", "running", {"job_id": "job_1"})

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert set(saved) == {"f1"}, "a flat map must not gain envelope keys"
    assert saved["f1"]["status"] == "running"
    assert saved["f1"]["evidence"] == {"job_id": "job_1"}
    assert saved["f1"]["updated_at"]


@pytest.mark.parametrize(
    "document",
    [
        pytest.param([], id="top-level-list"),
        pytest.param([card("f1")], id="top-level-list-of-cards"),
        pytest.param({"f1": []}, id="flat-card-is-a-list"),
        pytest.param({"f1": "card"}, id="flat-card-is-a-string"),
        pytest.param({"f1": None}, id="flat-card-is-null"),
        pytest.param({"schema": "bundlebees.queue/1", "features": ["f1"]}, id="features-list-of-strings"),
        pytest.param({"schema": "bundlebees.queue/1", "features": [{"job_id": "job_1"}]},
                     id="features-list-card-without-id"),
        pytest.param({"schema": "bundlebees.queue/1", "features": [{"feature_id": "", "id": None}]},
                     id="features-list-card-with-empty-id"),
        pytest.param({"schema": "bundlebees.queue/1", "features": "f1"}, id="features-is-a-string"),
        pytest.param({"schema": "bundlebees.queue/1", "features": {"f1": ["not", "a", "card"]}},
                     id="features-mapping-to-non-dict"),
    ],
)
def test_unknown_shapes_raise_a_typed_queue_invalid_with_a_repair_fix(tmp_path, document):
    backend = write(tmp_path / "queue.json", document)

    with pytest.raises(Q.QueueError) as caught:
        backend.list_items()

    assert caught.value.code == "queue_invalid"
    assert caught.value.fix
    assert "queue.json" in caught.value.message or "card" in caught.value.message


def test_unknown_shapes_never_leak_attribute_error_or_type_error(tmp_path):
    for document in ("a string", 3, None, True):
        backend = write(tmp_path / "queue.json", document)
        with pytest.raises(Q.QueueError) as caught:
            backend.list_items()
        assert caught.value.code == "queue_invalid"
