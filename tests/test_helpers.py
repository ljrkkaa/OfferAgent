import os

import pytest

from khoj.processor.tools.online_search import (
    read_webpage_at_url,
    read_webpage_with_olostep,
)
from khoj.utils import helpers


def test_get_from_null_dict():
    # null handling
    assert helpers.get_from_dict(dict()) == dict()
    assert helpers.get_from_dict(dict(), None) is None

    # key present in nested dictionary
    # 1-level dictionary
    assert helpers.get_from_dict({"a": 1, "b": 2}, "a") == 1
    assert helpers.get_from_dict({"a": 1, "b": 2}, "c") is None

    # 2-level dictionary
    assert helpers.get_from_dict({"a": {"a_a": 1}, "b": 2}, "a") == {"a_a": 1}
    assert helpers.get_from_dict({"a": {"a_a": 1}, "b": 2}, "a", "a_a") == 1

    # key not present in nested dictionary
    # 2-level_dictionary
    assert helpers.get_from_dict({"a": {"a_a": 1}, "b": 2}, "b", "b_a") is None


def test_merge_dicts():
    # basic merge of dicts with non-overlapping keys
    assert helpers.merge_dicts(priority_dict={"a": 1}, default_dict={"b": 2}) == {"a": 1, "b": 2}

    # use default dict items when not present in priority dict
    assert helpers.merge_dicts(priority_dict={}, default_dict={"b": 2}) == {"b": 2}

    # do not override existing key in priority_dict with default dict
    assert helpers.merge_dicts(priority_dict={"a": 1}, default_dict={"a": 2}) == {"a": 1}


def test_get_file_type_allows_missing_mime_type():
    assert helpers.get_file_type(None, b"hello from upload") == ("plaintext", None)


def test_lru_cache():
    # Test initializing cache
    cache = helpers.LRU({"a": 1, "b": 2}, capacity=2)
    assert cache == {"a": 1, "b": 2}

    # Test capacity overflow
    cache["c"] = 3
    assert cache == {"b": 2, "c": 3}

    # Test delete least recently used item from LRU cache on capacity overflow
    cache["b"]  # accessing 'b' makes it the most recently used item
    cache["d"] = 4  # so 'c' is deleted from the cache instead of 'b'
    assert cache == {"b": 2, "d": 4}


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("KHOJ_RUN_LIVE_NETWORK_TESTS") != "1", reason="live network test disabled")
async def test_reading_webpage():
    # Arrange
    website = "https://en.wikipedia.org/wiki/Great_Chicago_Fire"

    # Act
    response = await read_webpage_at_url(website)

    # Assert
    assert (
        "An alarm sent from the area near the fire also failed to register at the courthouse where the fire watchmen were"
        in response
    )


@pytest.mark.skipif(os.getenv("OLOSTEP_API_KEY") is None, reason="OLOSTEP_API_KEY is not set")
@pytest.mark.asyncio
async def test_reading_webpage_with_olostep():
    # Arrange
    website = "https://en.wikipedia.org/wiki/Great_Chicago_Fire"

    # Act
    response = await read_webpage_with_olostep(website)

    # Assert
    assert (
        "An alarm sent from the area near the fire also failed to register at the courthouse where the fire watchmen were"
        in response
    )
