"""片段關鍵字搜尋的有界分詞。"""

import re


def keyword_terms(query: str) -> list[str]:
    if "\x00" in query:
        raise ValueError("The search query contains an invalid character.")
    terms = list(dict.fromkeys(re.findall(r"\w+", query.lower())))
    if not terms or len(terms) > 16:
        raise ValueError("The search query must contain between 1 and 16 distinct terms.")
    return terms
