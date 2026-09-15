import asyncio

from erragent.context import context, current_context


def test_context_merges_fields():
    assert current_context() == {}
    with context(a=1, b=2):
        assert current_context() == {"a": 1, "b": 2}
    assert current_context() == {}


def test_nested_context_merges_and_unwinds():
    with context(a=1):
        with context(b=2):
            assert current_context() == {"a": 1, "b": 2}
        assert current_context() == {"a": 1}
    assert current_context() == {}


def test_inner_context_overrides_same_key():
    with context(node="outer"):
        with context(node="inner"):
            assert current_context()["node"] == "inner"
        assert current_context()["node"] == "outer"


def test_sync_decorator_applies_context():
    @context(node="fetch")
    def do_work():
        return current_context()

    assert current_context() == {}
    assert do_work() == {"node": "fetch"}
    assert current_context() == {}


async def test_async_decorator_applies_context():
    @context(node="fetch")
    async def do_work():
        return current_context()

    assert await do_work() == {"node": "fetch"}


async def test_context_survives_create_task_boundary():
    async def read_context():
        return current_context()

    with context(request_id="req-1"):
        task = asyncio.create_task(read_context())
        result = await task

    assert result == {"request_id": "req-1"}
