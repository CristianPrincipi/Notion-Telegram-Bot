"""news_sources.yaml: read it, check it, hand back one NewsConfig.

Every problem in the file is listed in ONE error, like config.validate(): a
newspaper that names a misspelt section in three places should say so once, not
over three mornings.
"""

import logging
import re
from dataclasses import dataclass, field

import yaml

import config

logger = logging.getLogger(__name__)

# The order of the EPUB, and the only names a source, the category map or the
# keywords may use.
SECTIONS = ("Finanza", "Politica Italiana", "Tecnologia", "Geopolitica", "Scienze", "Ambiente")
AUTO = "auto"
DEFAULT_TITLE = "Il Giornale"


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    section: str          # one of SECTIONS, or AUTO
    max: int


@dataclass(frozen=True)
class NewsConfig:
    sources: tuple
    top_news_feed: str | None
    category_map: dict = field(default_factory=dict)
    keywords: dict = field(default_factory=dict)
    cluster_threshold: int = 75
    hour: int = config.NEWSPAPER_DEFAULT_HOUR
    minute: int = config.NEWSPAPER_DEFAULT_MINUTE
    images: bool = False
    title: str = DEFAULT_TITLE


_SCHEDULE = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")


def _parse_schedule(value, problems):
    match = _SCHEDULE.fullmatch(str(value).strip())
    if not match:
        problems.append(f"schedule must be HH:MM, got {value!r}")
        return config.NEWSPAPER_DEFAULT_HOUR, config.NEWSPAPER_DEFAULT_MINUTE
    return int(match.group(1)), int(match.group(2))


def _parse_sources(raw, problems):
    if not isinstance(raw, list) or not raw:
        problems.append("sources must be a non-empty list")
        return ()
    sources = []
    for number, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            problems.append(f"source {number} is not a mapping")
            continue
        name = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        section = str(item.get("section") or "").strip()
        limit = item.get("max")
        label = name or f"source {number}"
        if not name:
            problems.append(f"source {number} has no name")
        if not url.startswith(("http://", "https://")):
            problems.append(f"{label}: url must start with http:// or https://")
        if section != AUTO and section not in SECTIONS:
            problems.append(f"{label}: section {section!r} is not one of {', '.join(SECTIONS)} or auto")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            problems.append(f"{label}: max must be a whole number above 0, got {limit!r}")
            limit = 1
        sources.append(Source(name, url, section, limit))
    return tuple(sources)


def _parse_category_map(raw, problems):
    raw = raw or {}
    if not isinstance(raw, dict):
        problems.append("category_map must be a mapping of feed category → section")
        return {}
    for category, section in raw.items():
        if section not in SECTIONS:
            problems.append(f"category_map: {category!r} → {section!r} is not a section")
    return {str(k).strip(): v for k, v in raw.items()}


def _parse_keywords(raw, problems):
    raw = raw or {}
    if not isinstance(raw, dict):
        problems.append("keywords must be a mapping of section → list of words")
        return {}
    keywords = {}
    for section, words in raw.items():
        if section not in SECTIONS:
            problems.append(f"keywords: {section!r} is not a section")
            continue
        if not isinstance(words, list):
            problems.append(f"keywords: {section} must be a list")
            continue
        keywords[section] = tuple(str(w).strip().lower() for w in words if str(w).strip())
    return keywords


def parse_config(data) -> tuple[NewsConfig | None, str | None]:
    """A loaded YAML document → (NewsConfig, None), or (None, every problem)."""
    if not isinstance(data, dict):
        return None, "news_sources.yaml: the file is empty or not a mapping"
    problems = []
    sources = _parse_sources(data.get("sources"), problems)
    category_map = _parse_category_map(data.get("category_map"), problems)
    keywords = _parse_keywords(data.get("keywords"), problems)

    threshold = data.get("cluster_threshold", 75)
    if not isinstance(threshold, int) or isinstance(threshold, bool) or not 0 < threshold <= 100:
        problems.append(f"cluster_threshold must be a whole number from 1 to 100, got {threshold!r}")
        threshold = 75

    hour, minute = _parse_schedule(data.get("schedule", "06:30"), problems)

    images = data.get("images", False)
    if images is not False:
        problems.append("images: true is not implemented yet — set it to false")

    top = data.get("top_news_feed")
    if top is not None and not str(top).startswith(("http://", "https://")):
        problems.append("top_news_feed must be a URL")
        top = None

    title = str(data.get("title") or DEFAULT_TITLE).strip()

    if problems:
        return None, "news_sources.yaml:\n- " + "\n- ".join(problems)
    return NewsConfig(sources=sources, top_news_feed=top, category_map=category_map,
                      keywords=keywords, cluster_threshold=threshold, hour=hour,
                      minute=minute, images=False, title=title), None


def load_config(path: str = config.NEWSPAPER_SOURCES_FILE) -> tuple[NewsConfig | None, str | None]:
    """Read and check the file. Read on every build, so an edit needs no restart
    except for `schedule`, which the job queue takes once at startup."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        return None, f"news_sources.yaml not found at {path}"
    except (OSError, yaml.YAMLError) as e:
        return None, f"news_sources.yaml could not be read: {e}"
    return parse_config(data)


def schedule_time(path: str = config.NEWSPAPER_SOURCES_FILE) -> tuple[int, int]:
    """(hour, minute) for the job. A broken file falls back to the config default
    and says so in the log; the job itself then reports the file in the chat."""
    cfg, err = load_config(path)
    if err:
        logger.warning("Newspaper schedule falls back to %02d:%02d: %s",
                       config.NEWSPAPER_DEFAULT_HOUR, config.NEWSPAPER_DEFAULT_MINUTE, err)
        return config.NEWSPAPER_DEFAULT_HOUR, config.NEWSPAPER_DEFAULT_MINUTE
    return cfg.hour, cfg.minute
