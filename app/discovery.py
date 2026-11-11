"""Deterministic query suggestions only; no search or task execution."""

from collections.abc import Sequence


_FOOD_CREATOR_QUERIES = {
    '食品': ('食品测评', '美食分享', '美食制作'),
    '美食': ('美食分享', '美食制作', '美食测评'),
    '零食': ('零食测评', '零食分享', '零食制作'),
}
_FASHION_CREATOR_QUERIES = {
    '女装': ('女生穿搭分享', '女生日常穿搭', '女生一衣多穿'),
    '女装穿搭': ('女生穿搭分享', '女生日常穿搭', '女生一衣多穿'),
}
_CREATOR_QUERIES = {**_FOOD_CREATOR_QUERIES, **_FASHION_CREATOR_QUERIES}


def _clean_terms(terms: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(term.strip() for term in terms if term.strip()))


def propose_creator_queries(
    keyword: str,
    queries: Sequence[str] | None = None,
    content_preferences: Sequence[str] | None = None,
) -> list[str]:
    """Return up to three ordered queries without mutating the supplied brief.

    Only known exact broad categories receive creator-content defaults. Specific
    queries win; a specific keyword also wins over bare-category queries.
    For an otherwise broad brief, preferences are used literally, retaining
    category scope when absent. No constraints or exclusions are inferred.
    Callers remain responsible for validating query lengths and task fields.
    """
    keyword = keyword.strip()
    selected = _clean_terms(queries or [])
    if any(query not in _CREATOR_QUERIES for query in selected):
        return selected[:3]
    if keyword not in _CREATOR_QUERIES:
        return [keyword] if keyword else selected[:3]

    preferences = [term for term in _clean_terms(content_preferences or [])
                   if term not in _CREATOR_QUERIES]
    if preferences:
        if keyword in _FASHION_CREATOR_QUERIES:
            scope_terms = ('女装', '女生', '女性', '女士', '女孩')
            scope = '女装'
        else:
            scope_terms = _FOOD_CREATOR_QUERIES
            scope = keyword
        return [term if any(category in term for category in scope_terms)
                else f'{scope} {term}' for term in preferences[:3]]
    return list(_CREATOR_QUERIES[keyword])
