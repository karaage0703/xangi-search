import json
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from xangi_search.index import SearchIndex, SentenceTransformerEmbedder


@pytest.mark.parametrize("modern", [True, False])
def test_e5_prefixes_reach_encoder(monkeypatch, modern):
    calls = []

    class Model:
        prompts = {}
        default_prompt_name = None
        max_seq_length = 512

        def __init__(self, name):
            pass

        def encode(self, texts, **kwargs):
            calls.append((texts, kwargs))
            return np.ones((len(texts), 2))

    if modern:
        Model.encode_query = Model.encode
        Model.encode_document = Model.encode
    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=Model)
    )
    embedder = SentenceTransformerEmbedder()
    embedder(["猫"], query=True)
    embedder(["猫の資料"])
    assert [c[1].get("prompt") for c in calls] == ["query: ", "passage: "]
    other = SentenceTransformerEmbedder("other/model")
    other(["猫"], query=True)
    assert "prompt" not in calls[-1][1]
    assert json.loads(embedder.embedding_signature)["model"] == embedder.model_name


class Embedder:
    def __init__(self, signature, dimensions=3, fail=False):
        self.embedding_signature = signature
        self.dimensions = dimensions
        self.fail = fail
        self.calls = []

    def __call__(self, texts, *, query=False):
        self.calls.extend(texts)
        if self.fail and "fact text" in texts:
            raise RuntimeError("embedding failed")
        return np.ones((len(texts), self.dimensions), dtype=np.float32)


def seed(tmp_path, embedder):
    (tmp_path / "doc.md").write_text("document text")
    index = SearchIndex(tmp_path, embedder=embedder)
    index.reindex()
    index.add_facts([{"text": "fact text"}])
    index.close()


@pytest.mark.parametrize("legacy", [True, False])
def test_changed_configuration_rebuilds_documents_and_facts(tmp_path, legacy):
    seed(tmp_path, Embedder("old", 2))
    index = SearchIndex(tmp_path, embedder=None)
    before = dict(index.connection.execute("SELECT * FROM facts").fetchone())
    if legacy:
        index.connection.execute("DELETE FROM embedding_metadata")
        index.connection.commit()
    index.close()
    new = Embedder("new")
    index = SearchIndex(tmp_path, embedder=new)
    assert set(new.calls) == {"document text", "fact text"}
    assert index._embedding_matrix.shape == (1, 3)
    assert index._fact_matrix.shape == (1, 3)
    after = dict(index.connection.execute("SELECT * FROM facts").fetchone())
    assert {k: v for k, v in before.items() if k != "embedding"} == {
        k: v for k, v in after.items() if k != "embedding"
    }
    assert index.reindex()["changed_files"] == 0
    index.close()
    same = Embedder("new")
    index = SearchIndex(tmp_path, embedder=same)
    assert same.calls == []
    index.close()


def test_failed_migration_rolls_back_and_retries(tmp_path):
    seed(tmp_path, Embedder("old", 2))
    with pytest.raises(RuntimeError, match="embedding failed"):
        SearchIndex(tmp_path, embedder=Embedder("new", fail=True))
    index = SearchIndex(tmp_path)
    assert index._embedding_matrix.shape == (1, 2)
    assert index._fact_matrix.shape == (1, 2)
    assert (
        index.connection.execute("SELECT signature FROM embedding_metadata").fetchone()[
            0
        ]
        == "old"
    )
    index.close()
    index = SearchIndex(tmp_path, embedder=Embedder("new"))
    assert index._fact_matrix.shape == (1, 3)
    index.close()


def test_keyword_only_changes_are_embedded_when_vectors_return(tmp_path):
    seed(tmp_path, Embedder("same"))
    index = SearchIndex(tmp_path)
    (tmp_path / "doc.md").write_text("updated document")
    index.reindex()
    index.add_facts([{"text": "new fact"}])
    index.close()
    embedder = Embedder("same")
    index = SearchIndex(tmp_path, embedder=embedder)
    assert set(embedder.calls) == {"updated document", "new fact"}
    assert index.stats()["vectors"] == 1
    index.close()
