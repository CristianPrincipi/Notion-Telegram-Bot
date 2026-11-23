""""Le 3 notizie del giorno", without a model: the stories the most sources ran.

Titles are compared after normalisation (lower case, no punctuation, no Italian
or English stopwords) with rapidfuzz's token_set_ratio. Articles are grouped
single-link: one joins the first group holding a title at or above the
threshold. A group's score is the number of DISTINCT sources in it, plus one if
any of its titles matches the top-news feed. A tie goes to the group with more
distinct sources, then to the one whose newest article is newest.

WHY SOURCES BREAK A TIE BEFORE RECENCY: ANSA's top-news feed republishes ANSA's
own items, so every ANSA article on it scores 1 + 1 = 2 — the same as a story
two publishers ran. On recency alone the morning's market openings (published
at 08:00, on the top-news feed) beat a story ANSA and Il Post both covered at
06:44, every weekday.

WHAT IS GROUPED is every classified item of the last 24 hours (the `pool`), not
only the paper's: the per-source `max` keeps six ANSA Mondo items, and the
seventh is often the one BBC ran too. Only a group holding an article IN the
paper can be a top story, and that article is the one it leads with and points
to; the others are listed with their links.

token_set_ratio scores a title whose words are a subset of another's at 100, so
a title of one meaningful word would join anything that mentions it. Titles left
with fewer than MIN_TOKENS words after normalisation never cluster.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz

from services.newspaper_collect import Article, normalise_url

logger = logging.getLogger(__name__)

TOP_STORIES = 3
MIN_TOKENS = 2

STOPWORDS = frozenset("""
a ad al alla alle allo agli ai all anche che chi ci con contro così da dal dalla
dalle dallo dagli dai de degli dei del della delle dello di e è ed fa fra gli ha
hanno ho i il in la le lo l ma mi ne nei nel nella nelle nello negli non o per
più può se si sia sono su sul sulla sulle sullo sugli sui tra tre due un una uno
c dopo come cosa ecco ora oggi ieri sta stato stata stati essere era tutto tutti
the a an and or of to in on for with at by from as is are was were be been it its
this that these those after over into about than but not no new says say said
how why what who will would can could has have had his her their they we you
""".split())


def normalise_title(title: str) -> str:
    text = unicodedata.normalize("NFKC", title).lower()
    text = re.sub(r"[’'`´]", " ", text)        # l'Italia → l italia
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(w for w in text.split() if w not in STOPWORDS)


def _sources(articles) -> list[str]:
    seen = []
    for a in articles:
        if a.source not in seen:
            seen.append(a.source)
    return seen


@dataclass
class Story:
    articles: list            # newest first
    score: int
    on_top_feed: bool

    @property
    def sources(self) -> list[str]:
        return _sources(self.articles)

    @property
    def newest(self):
        return max(a.published for a in self.articles)

    @property
    def main(self) -> Article:
        """The most complete article IN THE PAPER (it has a body): full text
        before summary, then the longest."""
        return max((a for a in self.articles if a.body_html),
                   key=lambda a: (a.full_text, a.text_chars))

    @property
    def others(self) -> list[Article]:
        """Every other article, one per URL, the main one's URL excluded."""
        main = self.main
        seen = {normalise_url(main.url)}
        out = []
        for a in self.articles:
            if normalise_url(a.url) not in seen:
                seen.add(normalise_url(a.url))
                out.append(a)
        return out


def similar(a: str, b: str, threshold: int) -> bool:
    """Both titles already normalised."""
    if len(a.split()) < MIN_TOKENS or len(b.split()) < MIN_TOKENS:
        return False
    return fuzz.token_set_ratio(a, b) >= threshold


def cluster(articles: list[Article], threshold: int) -> list[list[Article]]:
    """Single-link groups, newest article first in each."""
    groups: list[list[tuple[str, Article]]] = []
    for article in sorted(articles, key=lambda a: a.published, reverse=True):
        norm = normalise_title(article.title)
        home = next((g for g in groups if any(similar(norm, n, threshold) for n, _ in g)), None)
        if home is None:
            groups.append([(norm, article)])
        else:
            home.append((norm, article))
    return [[a for _, a in g] for g in groups]


def top_stories(paper: list[Article], pool: list[Article], top_titles: list[str],
                threshold: int, count: int = TOP_STORIES) -> list[Story]:
    """The `count` best groups that hold at least one article of the paper.

    `pool` may hold the paper's own articles (the same objects); each URL is
    grouped once."""
    # One URL is one report, whatever feed carried it: the same ANSA story under
    # ANSA Politica and ANSA Economia must score 1, not 2 (review of #80). The
    # paper's copy comes first, so it is the one kept.
    everything, seen = [], set()
    for article in [*paper, *pool]:
        key = normalise_url(article.url)
        if key not in seen:
            seen.add(key)
            everything.append(article)
    in_paper = {id(a) for a in paper}

    top_norms = [normalise_title(t) for t in top_titles]
    stories = []
    for group in cluster(everything, threshold):
        if not any(id(a) in in_paper for a in group):
            continue
        on_top = any(similar(normalise_title(a.title), t, threshold)
                     for a in group for t in top_norms)
        stories.append(Story(group, len(_sources(group)) + (1 if on_top else 0), on_top))
    stories.sort(key=lambda s: (s.score, len(s.sources), s.newest), reverse=True)
    chosen = stories[:count]
    for story in chosen:
        logger.info("Newspaper: top story (score %d, %d sources%s): %s", story.score,
                    len(story.sources), ", on the top-news feed" if story.on_top_feed else "",
                    story.main.title)
    return chosen
