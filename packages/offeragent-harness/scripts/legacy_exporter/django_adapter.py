"""Optional Django bridge, isolated from both Harness runtime and default CLI.

The caller in the old environment owns ORM/model selection and passes lazy,
read-only iterables.  Importing this module never imports or configures Django.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DjangoLegacyRowSource:
    querysets: tuple[Iterable[Any], ...]
    mapper: Callable[[Any], Mapping[str, Any]]

    def iter_rows(self) -> Iterator[Mapping[str, Any]]:
        for queryset in self.querysets:
            iterator = getattr(queryset, "iterator", None)
            rows = iterator(chunk_size=1_000) if callable(iterator) else iter(queryset)
            for value in rows:
                yield self.mapper(value)


__all__ = ["DjangoLegacyRowSource"]
