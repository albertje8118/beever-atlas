from __future__ import annotations

import pytest

from beever_atlas.models.domain import AtomicFact
from plugins.stores.embedded._sqlite_vector import SQLiteVectorStore


@pytest.mark.asyncio
async def test_sqlite_vector_treats_none_sentinel_as_unclustered(tmp_path):
    db_path = tmp_path / "embedded.db"
    store = SQLiteVectorStore(db_path=str(db_path))
    await store.startup()

    fact = AtomicFact(
        channel_id="channel-1",
        memory_text="A fact that should still be clusterable.",
        cluster_id="__none__",
        text_vector=[1.0, 0.0, 0.0],
    )
    await store.batch_upsert_facts([fact])

    assert await store.count_unclustered_facts("channel-1") == 1

    unclustered = await store.get_unclustered_facts("channel-1")
    assert [item.id for item in unclustered] == [fact.id]
    assert unclustered[0].cluster_id is None

    all_ids = []
    async for item in store.iter_all_fact_ids("channel-1"):
        all_ids.append(item)
    assert all_ids == [(fact.id, "__none__")]