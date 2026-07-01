import logging
from typing import List, Optional, Tuple, Type

from asgiref.sync import sync_to_async

from khoj.database.adapters import EntryAdapters
from khoj.database.models import Agent, KhojUser
from khoj.database.models import Entry as DbEntry
from khoj.processor.content.text_to_entries import TextToEntries
from khoj.utils import state
from khoj.utils.helpers import timer
from khoj.utils.jsonl import load_jsonl
from khoj.utils.lexical import query_terms
from khoj.utils.rawconfig import Entry, SearchResponse
from khoj.utils.state import SearchType

logger = logging.getLogger(__name__)

search_type_to_entry_type = {
    SearchType.Org.value: DbEntry.EntryType.ORG,
    SearchType.Markdown.value: DbEntry.EntryType.MARKDOWN,
    SearchType.Plaintext.value: DbEntry.EntryType.PLAINTEXT,
    SearchType.Pdf.value: DbEntry.EntryType.PDF,
    SearchType.All.value: None,
}

LEXICAL_TOP_K = 10


def _lexical_distance(entry: DbEntry, raw_query: str, terms: list[str]) -> float | None:
    heading = (entry.heading or "").lower()
    compiled = (entry.compiled or "").lower()
    raw = (entry.raw or "").lower()
    file_path = (entry.file_path or "").lower()
    haystack = "\n".join([heading, compiled, raw, file_path])

    if not terms:
        return None

    term_hits = sum(1 for term in terms if term in haystack)
    if term_hits == 0:
        return None

    score = term_hits / len(terms)
    score += 0.25 * sum(1 for term in terms if term in heading)
    score += 0.1 * sum(1 for term in terms if term in file_path)

    normalized_query = raw_query.lower().strip()
    if normalized_query and normalized_query in haystack:
        score += 1.0

    return 1 / (1 + score)


def extract_entries(jsonl_file) -> List[Entry]:
    "Load entries from compressed jsonl"
    return list(map(Entry.from_dict, load_jsonl(jsonl_file)))


async def query(
    raw_query: str,
    user: KhojUser,
    type: SearchType = SearchType.All,
    max_distance: float = None,
    agent: Optional[Agent] = None,
) -> Tuple[List[dict], List[Entry]]:
    "Search for entries that answer the query"

    file_type = search_type_to_entry_type[type.value]
    terms = query_terms(raw_query, ignore_prefixes=("file:", "dt:"))

    def lexical_lookup():
        filtered_entries = EntryAdapters.apply_filters(user, raw_query, file_type_filter=file_type, agent=agent)
        scored_hits = []
        for entry in filtered_entries:
            distance = _lexical_distance(entry, raw_query, terms)
            if distance is None:
                continue
            entry.distance = distance
            scored_hits.append(entry)
        scored_hits.sort(key=lambda hit: (hit.distance, hit.file_path or "", hit.id))
        return scored_hits[:LEXICAL_TOP_K]

    with timer("Lexical Search Time", logger, state.device):
        return await sync_to_async(lexical_lookup)()


def collate_results(hits, dedupe=True):
    hit_ids = set()
    hit_hashes = set()
    for hit in hits:
        if dedupe and (hit.hashed_value in hit_hashes or hit.corpus_id in hit_ids):
            continue

        else:
            hit_hashes.add(hit.hashed_value)
            hit_ids.add(hit.corpus_id)
            yield SearchResponse.model_validate(
                {
                    "entry": hit.raw,
                    "score": hit.distance,
                    "corpus_id": str(hit.corpus_id),
                    "additional": {
                        "source": hit.file_source,
                        "file": hit.file_path,
                        "uri": hit.url,
                        "compiled": hit.compiled,
                        "heading": hit.heading,
                    },
                }
            )


def deduplicated_search_responses(hits: List[SearchResponse]):
    hit_ids = set()
    for hit in hits:
        if hit.additional["compiled"] in hit_ids:
            continue

        else:
            hit_ids.add(hit.additional["compiled"])
            yield SearchResponse.model_validate(
                {
                    "entry": hit.entry,
                    "score": hit.score,
                    "corpus_id": hit.corpus_id,
                    "additional": {
                        "source": hit.additional["source"],
                        "file": hit.additional["file"],
                        "uri": hit.additional["uri"],
                        "query": hit.additional["query"],
                        "compiled": hit.additional["compiled"],
                        "heading": hit.additional["heading"],
                    },
                }
            )


def setup(
    text_to_entries: Type[TextToEntries],
    files: dict[str, str],
    regenerate: bool,
    user: KhojUser,
    config=None,
) -> Tuple[int, int]:
    if config:
        num_new_entries, num_deleted_entries = text_to_entries(config).process(
            files=files, user=user, regenerate=regenerate
        )
    else:
        num_new_entries, num_deleted_entries = text_to_entries().process(files=files, user=user, regenerate=regenerate)

    if files:
        file_names = [file_name for file_name in files]

        logger.info(
            f"Deleted {num_deleted_entries} entries. Created {num_new_entries} new entries for user {user} from files {file_names[:10]} ..."
        )

    return num_new_entries, num_deleted_entries
