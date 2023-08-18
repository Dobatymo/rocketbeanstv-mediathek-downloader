import errno
import json
import logging
import re
import sqlite3
import time
import warnings
from argparse import ArgumentDefaultsHelpFormatter, ArgumentParser, ArgumentTypeError, Namespace
from collections import defaultdict
from datetime import datetime
from functools import lru_cache
from itertools import islice
from operator import itemgetter
from os import fspath, strerror
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    TypeVar,
    Union,
)

from dateutil.parser import isoparse
from genutility.args import is_file
from genutility.iter import progress
from genutility.unqlite import query_by_field_intersect
from platformdirs import user_data_dir
from rbtv import RBTVAPI, HTTPError, batch_iter, bohne_name_to_id, name_of_season, show_name_to_id

try:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError, UnavailableVideoError, sanitize_filename
except ImportError:
    from youtube_dl import YoutubeDL
    from youtube_dl.utils import DownloadError, UnavailableVideoError, sanitize_filename

    warnings.warn("Using `youtube-dl`. For better performance please install `yt-dlp`.")

if TYPE_CHECKING:
    import unqlite

try:
    from unqlite import UnQLite
except ImportError:
    DEFAULT_BACKEND = "live"
    ALL_BACKENDS = ["live"]
    warnings.warn("Local backend not available. Install unqlite.")
else:
    DEFAULT_BACKEND = "local"
    ALL_BACKENDS = ["local", "live"]

JsonDict = Dict[str, Any]
T = TypeVar("T")

__version__ = "0.8"

APP_NAME = "rocketbeanstv-mediathek-downloader"
APP_AUTHOR = "Dobatymo"

APPDATA = Path(user_data_dir(APP_NAME, APP_AUTHOR))
DEFAULT_BASEPATH = Path(".")
DEFAULT_DB_PATH = APPDATA / "rbtv.udb"
DEFAULT_RECORD_PATH = APPDATA / "rbtv.sqlite"
DEFAULT_OUTDIRTPL = "{show_name}/{season_name}"
DEFAULT_OUTTMPL = "%(title)s-%(id)s.%(format_id)s.%(ext)s"
DEFAULT_MISSING_VALUE = "-"
DEFAULT_RETRIES = 10
TOO_MANY_REQUESTS_DELAY = 60
DEFAULT_TOKEN_REGEX = r"^.*-([0-9A-Za-z_-]{10}[048AEIMQUYcgkosw])\.[0-9+]+\.[0-9a-zA-Z]{3,4}$"

# similar to "bestvideo+bestaudio/best", but with improved fallback to "best"
# if separate streams are not possible
DEFAULT_FORMAT = None

OUTDIRTPL_KEYS = (
    "show_id",
    "show_name",
    "season_id",
    "season_name",
    "season_number",
    "episode_id",
    "episode_name",
    "episode_number",
    "year",
    "month",
    "day",
    "hour",
    "minute",
    "second",
    "duration",
)
SINGLE_BLOG_TPL = "blog-{blog_id}.json"
ALL_BLOG_TPL = "blog-posts.jl"


class InvalidCollection(ValueError):
    pass


def youtube_token_to_url(token: str) -> str:
    return f"https://www.youtube.com/watch?v={token}"


def one(seq: Sequence[T]) -> T:
    if len(seq) != 1:
        raise ValueError("Input must be of length 1")
    return seq[0]


def unqlite_all(col: "unqlite.Collection") -> Iterator[JsonDict]:
    ret = col.all()

    if ret is None:
        raise InvalidCollection("Collection doesn't exist")

    return ret


def opt_int(s: Optional[str]) -> Optional[int]:
    if s is None:
        return None
    else:
        return int(s)


def posint(s: str) -> int:
    number = int(s)

    if number <= 0:
        msg = f"{s} is not strictly greater than 0"
        raise ArgumentTypeError(msg)

    return number


def episode_iter(eps_combined: JsonDict) -> Iterator[JsonDict]:
    return batch_iter(eps_combined, "episodes")


def is_in_season(episode: JsonDict) -> bool:
    return bool(episode.get("seasonId"))


def parse_datetime(datestr: Optional[str]) -> Optional[datetime]:
    if not datestr:
        return None

    return isoparse(datestr)


class Records:
    def insert_episode(self, episode_id: int) -> None:
        raise NotImplementedError

    def is_episode_complete(self, episode_id: int) -> bool:
        raise NotImplementedError

    def remove_episode(self, episode_id: int) -> bool:
        raise NotImplementedError

    def insert_part(
        self,
        episode_id: int,
        episode_part: int,
        youtube_token: Optional[str] = None,
        local_path: Optional[str] = None,
        info: Optional[Dict[str, Any]] = None,
    ) -> None:
        raise NotImplementedError

    def is_part_complete(self, episode_id: int, episode_part: int) -> bool:
        raise NotImplementedError

    def remove_part(self, episode_id: int, episode_part: int) -> bool:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class MemoryRecords(Records):
    all = "all"

    def __init__(self) -> None:
        self.downloaded_episodes: Set[Union[int, Tuple[int, int]]] = set()

    def insert_episode(self, episode_id: int) -> None:
        self.downloaded_episodes.add(episode_id)

    def is_episode_complete(self, episode_id: int) -> bool:
        return episode_id in self.downloaded_episodes

    def insert_part(
        self,
        episode_id: int,
        episode_part: int,
        youtube_token: Optional[str] = None,
        local_path: Optional[str] = None,
        info: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.downloaded_episodes.add((episode_id, episode_part))

    def is_part_complete(self, episode_id: int, episode_part: int) -> bool:
        return (episode_id, episode_part) in self.downloaded_episodes


class PlaintextRecords(Records):
    all = "all"

    def __init__(self, path: str) -> None:
        self.downloaded_episodes = set(self._parse_record_file(path))
        self.record_file = open(path, "a", encoding="utf-8")

    @classmethod
    def _parse_record_file(cls, path: str) -> Iterator[Union[int, Tuple[int, int]]]:
        try:
            with open(path, encoding="utf-8") as fr:
                for line in fr:
                    episode_id, episode_part = line.rstrip("\n").split(" ")
                    if episode_part == cls.all:
                        yield int(episode_id)
                    else:
                        yield int(episode_id), int(episode_part)
        except FileNotFoundError:
            return

    def insert_episode(self, episode_id: int) -> None:
        self.downloaded_episodes.add(episode_id)

        self.record_file.write(f"{episode_id} {self.all}\n")
        self.record_file.flush()

    def is_episode_complete(self, episode_id: int) -> bool:
        return episode_id in self.downloaded_episodes

    def insert_part(
        self,
        episode_id: int,
        episode_part: int,
        youtube_token: Optional[str] = None,
        local_path: Optional[str] = None,
        info: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.downloaded_episodes.add((episode_id, episode_part))

        self.record_file.write(f"{episode_id} {episode_part}\n")
        self.record_file.flush()

    def is_part_complete(self, episode_id: int, episode_part: int) -> bool:
        return (episode_id, episode_part) in self.downloaded_episodes

    def close(self) -> None:
        self.record_file.close()


class SqliteRecords(Records):
    def __init__(self, path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(path)
        self.create_tables()

    def create_tables(self) -> None:
        with self.con:
            cur = self.con.cursor()
            cur.executescript(
                """
                BEGIN TRANSACTION;
                CREATE TABLE IF NOT EXISTS parts (
                episode_id INTEGER NOT NULL,
                episode_part INTEGER NOT NULL,
                youtube_token TEXT NOT NULL,
                local_path TEXT NOT NULL,
                info JSON NULL,
                PRIMARY KEY (episode_id, episode_part)
                );
                CREATE TABLE IF NOT EXISTS episodes (
                episode_id INTEGER PRIMARY KEY
                );
                CREATE INDEX IF NOT EXISTS idx_parts_local_path ON parts (local_path);
                COMMIT;"""
            )

    def insert_episode(self, episode_id: int) -> None:
        with self.con:
            cur = self.con.cursor()
            cur.execute("INSERT INTO episodes (episode_id) VALUES (?);", (episode_id,))

    def is_episode_complete(self, episode_id: int) -> bool:
        with self.con:
            cur = self.con.cursor()
            return bool(list(cur.execute("SELECT episode_id FROM episodes WHERE episode_id = ?;", (episode_id,))))

    def remove_episode(self, episode_id: int) -> bool:
        with self.con:
            cur = self.con.cursor()
            cur.execute("DELETE FROM episodes WHERE episode_id = ?;", (episode_id,))
            return bool(cur.rowcount)

    def insert_part(
        self,
        episode_id: int,
        episode_part: int,
        youtube_token: Optional[str] = None,
        local_path: Optional[str] = None,
        info: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self.con:
            cur = self.con.cursor()
            cur.execute(
                """INSERT INTO parts (episode_id, episode_part, youtube_token, local_path, info)
                VALUES (?, ?, ?, ?, ?);
            """,
                (episode_id, episode_part, youtube_token, local_path, json.dumps(info, ensure_ascii=False)),
            )  # needs json(?)

    def is_part_complete(self, episode_id: int, episode_part: int) -> bool:
        with self.con:
            cur = self.con.cursor()
            return bool(
                list(
                    cur.execute(
                        "SELECT episode_id, episode_part FROM parts WHERE episode_id = ? AND episode_part = ?;",
                        (episode_id, episode_part),
                    )
                )
            )

    def remove_part(self, episode_id: int, episode_part: int) -> bool:
        with self.con:
            cur = self.con.cursor()
            cur.execute("DELETE FROM parts WHERE episode_id = ? AND episode_part = ?;", (episode_id, episode_part))
            return bool(cur.rowcount)

    def _iter(self):
        with self.con:
            cur = self.con.cursor()
            for episode_id, episode_part, youtube_token, local_path, info in cur.execute(
                "SELECT episode_id, episode_part, youtube_token, local_path, info FROM parts;"
            ):
                yield episode_id, episode_part, youtube_token, local_path, json.loads(info)

    def __iter__(self):
        return self._iter()

    def close(self) -> None:
        self.con.close()

    def execute(self, query, args=()):
        with self.con:
            cur = self.con.cursor()
            yield from cur.execute(query, args)


class Backend:
    def __enter__(self) -> "Backend":
        return self

    def __exit__(self, *args):
        pass

    def get_season_info(self, episode: JsonDict) -> JsonDict:
        in_season = is_in_season(episode)

        if in_season:
            try:
                season = self.get_season(episode["showId"], episode["seasonId"])
            except KeyError:
                logging.warning(
                    "Season not found for show=%s season=%s episode=%s",
                    episode["showId"],
                    episode["seasonId"],
                    episode["id"],
                )
                season_id: Optional[str] = episode["seasonId"]
                season_name: Optional[str] = None
                season_number: Optional[int] = None
            else:
                season_id = episode["seasonId"]
                season_name = sanitize_filename(name_of_season(season))
                season_number = opt_int(season["numeric"])
        else:
            season_id = None
            season_name = None
            season_number = None

        return {
            k: v
            for k, v in {
                "id": season_id,
                "name": season_name,
                "number": season_number,
            }.items()
            if v is not None
        }

    def get_episodes(self, episode_ids: Iterable[int]) -> Iterator[JsonDict]:
        raise NotImplementedError

    def get_season(self, show_id: int, season_id: int) -> JsonDict:
        raise NotImplementedError

    def get_episodes_by_season(
        self, season_ids: Iterable[int], sort_by: Optional[str] = None, limit: Optional[int] = None
    ) -> Iterator[JsonDict]:
        raise NotImplementedError

    def get_episodes_by_show(
        self,
        show_ids: Iterable[int],
        unsorted_only: bool = False,
        sort_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Iterator[JsonDict]:
        raise NotImplementedError

    def get_episodes_by_show_name(
        self,
        show_names: Iterable[str],
        unsorted_only: bool = False,
        sort_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Iterator[JsonDict]:
        raise NotImplementedError

    def get_all_episodes(
        self, unsorted_only: bool = False, sort_by: Optional[str] = None, limit: Optional[int] = None
    ) -> Iterator[JsonDict]:
        raise NotImplementedError

    def get_episodes_by_bohne(
        self,
        bohne_ids: Iterable[int],
        num: int,
        exclusive: bool,
        sort_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Iterable[JsonDict]:
        raise NotImplementedError

    def get_episodes_by_bohne_name(
        self,
        bohne_names: Iterable[str],
        num: int,
        exclusive: bool,
        sort_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Iterable[JsonDict]:
        raise NotImplementedError

    def get_shows(self, show_ids: Iterable[int]) -> Iterator[JsonDict]:
        raise NotImplementedError

    def get_all_shows(self, sort_by: Optional[str] = None, limit: Optional[int] = None) -> Iterator[JsonDict]:
        raise NotImplementedError

    def get_shows_by_name(self, show_names: Iterable[str]) -> Iterator[JsonDict]:
        raise NotImplementedError

    def get_posts(self, blog_ids: Iterable[int]) -> Iterator[JsonDict]:
        raise NotImplementedError

    def get_all_posts(self, sort_by: Optional[str] = None, limit: Optional[int] = None) -> Iterator[JsonDict]:
        raise NotImplementedError

    def get_all_bohnen(self, sort_by: Optional[str] = None, limit: Optional[int] = None) -> Iterator[JsonDict]:
        raise NotImplementedError

    def get_bohnen_by_name(self, bohne_names: Iterable[str]) -> Iterator[JsonDict]:
        raise NotImplementedError

    def get_bohnen(self, bohne_ids: Iterable[int]) -> Iterator[JsonDict]:
        raise NotImplementedError

    def search(self, text: str) -> Tuple[List[JsonDict], List[JsonDict], List[JsonDict]]:
        raise NotImplementedError

    def get_episodes_by_youtube_token(
        self, youtube_tokens: Iterable[str], sort_by: Optional[str] = None, limit: Optional[int] = None
    ) -> Iterator[JsonDict]:
        raise NotImplementedError


class LiveBackend(Backend):
    def __init__(self) -> None:
        self.api = RBTVAPI()

    def get_episodes(self, episode_ids: Iterable[int]) -> Iterator[JsonDict]:
        for episode_id in episode_ids:
            yield one(self.api.get_episode(episode_id)["episodes"])

    def get_season(self, show_id: int, season_id: int) -> JsonDict:
        return self.api.get_season(show_id, season_id)

    def get_episodes_by_season(
        self, season_ids: Iterable[int], sort_by: Optional[str] = None, limit: Optional[int] = None
    ) -> Iterator[JsonDict]:
        def episodes() -> Iterator[JsonDict]:
            for season_id in season_ids:
                yield from episode_iter(self.api.get_episodes_by_season(season_id))

        return sort_by_item(episodes(), sort_by, limit)

    def get_shows(self, show_ids: Iterable[int]) -> Iterator[JsonDict]:
        for show_id in show_ids:
            yield self.api.get_show(show_id)

    def get_shows_by_name(self, show_names: Iterable[str]) -> Iterator[JsonDict]:
        show_ids = [self.api.show_name_to_id(show_name) for show_name in show_names]
        return self.get_shows(show_ids)

    def get_episodes_by_show(
        self,
        show_ids: Iterable[int],
        unsorted_only: bool = False,
        sort_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Iterator[JsonDict]:
        if unsorted_only:
            iterfunc = self.api.get_unsorted_episodes_by_show
        else:
            iterfunc = self.api.get_episodes_by_show

        def episodes():
            for show_id in show_ids:
                yield from episode_iter(iterfunc(show_id))

        return sort_by_item(episodes(), sort_by, limit)

    def get_episodes_by_show_name(
        self,
        show_names: Iterable[str],
        unsorted_only: bool = False,
        sort_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Iterator[JsonDict]:
        show_ids = [self.api.show_name_to_id(show_name) for show_name in show_names]
        return self.get_episodes_by_show(show_ids, unsorted_only, sort_by, limit)

    def get_all_episodes(
        self, unsorted_only: bool = False, sort_by: Optional[str] = None, limit: Optional[int] = None
    ) -> Iterator[JsonDict]:
        show_ids = [show["id"] for show in self.api.get_shows_mini()]
        return self.get_episodes_by_show(show_ids, unsorted_only, sort_by, limit)

    def get_all_shows(self, sort_by: Optional[str] = None, limit: Optional[int] = None) -> Iterator[JsonDict]:
        return sort_by_item(self.api.get_shows(), sort_by, limit)

    def get_all_bohnen(self, sort_by: Optional[str] = None, limit: Optional[int] = None) -> Iterator[JsonDict]:
        return sort_by_item(self.api.get_bohnen_portraits(), sort_by, limit)

    def get_posts(self, blog_ids: Iterable[int]) -> Iterator[JsonDict]:
        for blog_id in blog_ids:
            yield self.api.get_blog_post_preview(blog_id)

    def get_all_posts(self, sort_by: Optional[str] = None, limit: Optional[int] = None) -> Iterator[JsonDict]:
        return sort_by_item(self.api.get_blog_posts_preview(), sort_by, limit)

    def get_bohnen(self, bohne_ids: Iterable[int]) -> Iterator[JsonDict]:
        for bohne_id in bohne_ids:
            yield self.api.get_bohne_portrait(bohne_id)

    def get_bohnen_by_name(self, bohne_names: Iterable[str]) -> Iterator[JsonDict]:
        bohne_ids = [self.api.bohne_name_to_id(bohne_name) for bohne_name in bohne_names]
        return self.get_bohnen(bohne_ids)

    @staticmethod
    def filter_sets(bohnen: Dict[int, Set[int]], bohne_ids: Set[int], num: int, exclusive: bool) -> Iterator[int]:
        for episode_id, ids in bohnen.items():
            if len(bohne_ids & ids) >= num:  # at last n people are in this episode
                if not exclusive or not (ids - bohne_ids):  # nobody else in this episode if exclusive
                    yield episode_id

    def get_episodes_by_bohne(
        self,
        bohne_ids: Iterable[int],
        num: int,
        exclusive: bool,
        sort_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Iterable[JsonDict]:
        if num == 1 and not exclusive:  # low memory fast path for common options

            def episodes() -> Iterator[JsonDict]:
                for bohne_id in bohne_ids:
                    yield from episode_iter(self.api.get_episodes_by_bohne(bohne_id))

        else:

            def episodes() -> List[JsonDict]:
                episodes: Dict[int, JsonDict] = {}

                for bohne_id in bohne_ids:
                    for episode in episode_iter(self.api.get_episodes_by_bohne(bohne_id)):
                        episode_id = int(episode["id"])
                        episodes[episode_id] = episode

                bohnen = {ep_id: set(ep["hosts"]) for ep_id, ep in episodes.items()}
                return [episodes[episode_id] for episode_id in self.filter_sets(bohnen, set(bohne_ids), num, exclusive)]

        return sort_by_item(episodes(), sort_by, limit)

    def get_episodes_by_bohne_name(
        self,
        bohne_names: Iterable[str],
        num: int,
        exclusive: bool,
        sort_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Iterable[JsonDict]:
        bohne_ids = [self.api.bohne_name_to_id(bohne_name) for bohne_name in bohne_names]
        return self.get_episodes_by_bohne(bohne_ids, num, exclusive, sort_by, limit)

    def search(self, text: str) -> Tuple[List[JsonDict], List[JsonDict], List[JsonDict]]:
        result = self.api.search(text)
        shows = result["shows"]
        episodes = result["episodes"]
        posts = result["blog"]
        return shows, episodes, posts

    def get_episodes_by_youtube_token(
        self, youtube_tokens: Iterable[str], sort_by: Optional[str] = None, limit: Optional[int] = None
    ) -> Iterator[JsonDict]:
        raise RuntimeError("Operation not yet supported by live backend")


class LocalBackend(Backend):
    UNQLITE_OPEN_READONLY = 0x00000001  # how to import from unqlite?

    def __init__(self, path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError(errno.ENOENT, strerror(errno.ENOENT), fspath(path))

        self.db = UnQLite(fspath(path), flags=self.UNQLITE_OPEN_READONLY)

    @classmethod
    def create(cls, path: Path, verbose: bool = False) -> None:
        api = RBTVAPI()

        with UnQLite(fspath(path)) as db:
            shows = db.collection("shows")
            shows.drop()

            episodes = db.collection("episodes")
            episodes.drop()

            bohnen = db.collection("bohnen")
            bohnen.drop()

            blog = db.collection("blog")
            blog.drop()

            shows.create()
            all_shows = list(api.get_shows())
            shows.store(all_shows)

            episodes.create()
            for show in progress(
                unqlite_all(shows), extra_info_callback=lambda i, l: "processing shows", disable=not verbose
            ):
                show_id = show["id"]
                try:
                    all_episodes = list(episode_iter(api.get_episodes_by_show(show_id)))
                except HTTPError as e:
                    if e.response.status_code == 400:
                        logging.warning(
                            "Failed to get episodes from show id=%s title=%s podcast=%s",
                            show_id,
                            show["title"],
                            show["isTruePodcast"],
                        )
                    else:
                        raise
                else:
                    episodes.store(all_episodes)

            bohnen.create()
            all_bohnen = list(api.get_bohnen_portraits())
            bohnen.store(all_bohnen)

            blog.create()
            all_blog = list(api.get_blog_posts())
            blog.store(all_blog)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "LocalBackend":
        return self

    def __exit__(self, *args):
        self.close()

    def get_episodes(self, episode_ids: Iterable[int]) -> Iterator[JsonDict]:
        episode_ids = set(episode_ids)
        episodes = self.db.collection("episodes")
        return episodes.filter(lambda doc: doc["id"] in episode_ids)

    @lru_cache(maxsize=128)
    def get_season(self, show_id: int, season_id: int) -> JsonDict:
        shows = self.db.collection("shows")
        show = one(shows.filter(lambda doc: doc["id"] == show_id))

        for season in show["seasons"]:
            if season["id"] == season_id:
                return season

        raise KeyError(f"Season id not found: show={show_id} season={season_id}")

    def get_episodes_by_season(
        self, seasons_ids: Iterable[int], sort_by: Optional[str] = None, limit: Optional[int] = None
    ) -> Iterator[JsonDict]:
        season_ids = set(seasons_ids)
        episodes = self.db.collection("episodes")
        return sort_by_item(episodes.filter(lambda doc: doc["seasonId"] in season_ids), sort_by, limit)

    def get_shows(self, show_ids: Iterable[int]) -> Iterator[JsonDict]:
        show_ids = set(show_ids)
        shows = self.db.collection("shows")
        return shows.filter(lambda doc: doc["id"] in show_ids)

    def get_shows_by_name(self, show_names: Iterable[str]) -> Iterator[JsonDict]:
        shows = self.db.collection("shows")
        show_ids = [show_name_to_id(unqlite_all(shows), show_name) for show_name in show_names]
        return self.get_shows(show_ids)

    def get_episodes_by_show(
        self,
        show_ids: Iterable[int],
        unsorted_only: bool = False,
        sort_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Iterator[JsonDict]:
        show_ids = set(show_ids)
        episodes = self.db.collection("episodes")
        if unsorted_only:
            return sort_by_item(
                episodes.filter(lambda doc: doc["showId"] in show_ids and not is_in_season(doc)), sort_by, limit
            )
        else:
            return sort_by_item(episodes.filter(lambda doc: doc["showId"] in show_ids), sort_by, limit)

    def get_episodes_by_show_name(
        self,
        show_names: Iterable[str],
        unsorted_only: bool = False,
        sort_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Iterator[JsonDict]:
        shows = self.db.collection("shows")
        show_ids = [show_name_to_id(unqlite_all(shows), show_name) for show_name in show_names]
        return self.get_episodes_by_show(show_ids, unsorted_only, sort_by, limit)

    def get_all_episodes(
        self, unsorted_only: bool = False, sort_by: Optional[str] = None, limit: Optional[int] = None
    ) -> Iterator[JsonDict]:
        episodes = self.db.collection("episodes")
        if unsorted_only:
            return sort_by_item(episodes.filter(lambda doc: not is_in_season(doc)), sort_by, limit)
        else:
            return sort_by_item(unqlite_all(episodes), sort_by, limit)

    def get_all_shows(self, sort_by: Optional[str] = None, limit: Optional[int] = None) -> Iterator[JsonDict]:
        shows = self.db.collection("shows")
        return sort_by_item(unqlite_all(shows), sort_by, limit)

    def get_all_bohnen(self, sort_by: Optional[str] = None, limit: Optional[int] = None) -> Iterator[JsonDict]:
        bohnen = self.db.collection("bohnen")
        return sort_by_item(unqlite_all(bohnen), sort_by, limit)

    def get_posts(self, blog_ids: Iterable[int]) -> Iterator[JsonDict]:
        blog_ids = set(blog_ids)
        blog = self.db.collection("blog")
        return blog.filter(lambda doc: doc["id"] in blog_ids)

    def get_all_posts(self, sort_by: Optional[str] = None, limit: Optional[int] = None) -> Iterator[JsonDict]:
        blog = self.db.collection("blog")
        return sort_by_item(unqlite_all(blog), sort_by, limit)

    def get_bohnen(self, bohne_ids: Iterable[int]) -> Iterator[JsonDict]:
        bohne_ids = set(bohne_ids)
        bohnen = self.db.collection("bohnen")
        return bohnen.filter(lambda doc: doc["mgmtid"] in bohne_ids)

    def get_bohnen_by_name(self, bohne_names: Iterable[str]) -> Iterator[JsonDict]:
        bohnen = self.db.collection("bohnen")
        bohne_ids = [bohne_name_to_id(unqlite_all(bohnen), bohne_name) for bohne_name in bohne_names]
        return self.get_bohnen(bohne_ids)

    def get_episodes_by_bohne(
        self,
        bohne_ids: Iterable[int],
        num: int,
        exclusive: bool,
        sort_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Iterator[JsonDict]:
        bohne_ids = set(bohne_ids)
        episodes = self.db.collection("episodes")

        def filter_sets(doc):
            ids = set(doc["hosts"])
            if len(bohne_ids & ids) >= num:  # at last n people are in this episode
                if not exclusive or not (ids - bohne_ids):  # nobody else in this episode if exclusive
                    return True
            return False

        return sort_by_item(episodes.filter(filter_sets), sort_by, limit)

    def get_episodes_by_bohne_name(
        self,
        bohne_names: Iterable[str],
        num: int,
        exclusive: bool,
        sort_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Iterator[JsonDict]:
        bohnen = self.db.collection("bohnen")
        bohne_ids = [bohne_name_to_id(unqlite_all(bohnen), bohne_name) for bohne_name in bohne_names]
        return self.get_episodes_by_bohne(bohne_ids, num, exclusive, sort_by, limit)

    def search(self, text: str) -> Tuple[List[JsonDict], List[JsonDict], List[JsonDict]]:
        shows = self.db.collection("shows")
        episodes = self.db.collection("episodes")
        blog = self.db.collection("blog")

        shows = shows.filter(find_in_columns(text, ("title", "description")))
        episodes = episodes.filter(find_in_columns(text, ("title", "description")))
        posts = blog.filter(find_in_columns(text, ("title", "subtitle", "contentMK", "contentHTML")))

        return shows, episodes, posts

    def get_episodes_by_youtube_token(
        self, youtube_tokens: Iterable[str], sort_by: Optional[str] = None, limit: Optional[int] = None
    ) -> Iterator[JsonDict]:
        youtube_tokens = list(youtube_tokens)
        return sort_by_item(
            query_by_field_intersect(self.db, "episodes", "youtubeTokens", youtube_tokens), sort_by, limit
        )


class RBTVDownloader:
    def __init__(
        self,
        backend: Backend,
        records: Records,
        basepath: Path = DEFAULT_BASEPATH,
        outdirtpl: str = DEFAULT_OUTDIRTPL,
        outtmpl: str = DEFAULT_OUTTMPL,
        format: Optional[str] = DEFAULT_FORMAT,
        missing_value: str = DEFAULT_MISSING_VALUE,
        retries: int = DEFAULT_RETRIES,
        cookiefile: Optional[str] = None,
    ) -> None:
        self.backend = backend
        self.records = records
        self.basepath = basepath
        self.outdirtpl = outdirtpl
        self.outtmpl = outtmpl
        self.format = format
        self.missing_value = missing_value
        self.retries = retries
        self.cookiefile = cookiefile

        self.writeannotations = False
        self.writesubtitles = False

    def _download_episode(self, episode: JsonDict) -> bool:
        in_season = is_in_season(episode)
        episode_id = int(episode["id"])

        if self.records.is_episode_complete(episode_id):
            logging.info("Episode %s was already downloaded", episode_id)
            return False

        if in_season:
            logging.debug(
                "Downloading show=%s season=%s episode=%s", episode["showId"], episode["seasonId"], episode["id"]
            )
        else:
            logging.debug("Downloading show=%s episode=%s", episode["showId"], episode["id"])

        dt = parse_datetime(episode["firstBroadcastdate"])
        if dt:
            year, month, day, hour, minute, second = tuple(
                map(str, (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second))
            )
        else:
            year, month, day, hour, minute, second = (self.missing_value,) * 6

        season = self.backend.get_season_info(episode)

        tpl_map = {
            "show_id": int(episode["showId"]),
            "show_name": sanitize_filename(episode["showName"]) or self.missing_value,
            "season_id": season.get("id") or self.missing_value,
            "season_name": season.get("name") or self.missing_value,
            "season_number": season.get("number") or self.missing_value,
            "episode_id": episode_id,
            "episode_name": sanitize_filename(episode["title"]) or self.missing_value,
            "episode_number": opt_int(episode["episode"]) or self.missing_value,
            "year": year,
            "month": month,
            "day": day,
            "hour": hour,
            "minute": minute,
            "second": second,
            "duration": episode["duration"],
        }

        all_done = True
        try:
            youtube_tokens = episode["youtubeTokens"]
            other_tokens = []
        except KeyError:
            try:
                youtube_tokens = [t["token"] for t in episode["tokens"] if t["type"] == "youtube"]
                other_tokens = [(t["type"], t["token"]) for t in episode["tokens"] if t["type"] != "youtube"]
            except KeyError:
                logging.warning("No Youtube tokens for episode %d: %s", episode_id, json.dumps(episode))
                return False

        if other_tokens:
            logging.warning(
                "Found %d non-youtube tokens for episode %d: %s", len(other_tokens), episode_id, other_tokens
            )

        for episode_part, youtube_token in enumerate(youtube_tokens):
            if self.records.is_part_complete(episode_id, episode_part):
                logging.info("Episode %d part %d was already downloaded", episode_id, episode_part)
                continue

            if not youtube_token:
                # fixme: should this set all_done = False ?
                logging.warning("Got empty Youtube token for episode %s part %s", episode_id, episode_part)
                continue

            url = youtube_token_to_url(youtube_token)
            tpl_map["episode_part"] = episode_part

            assert not set(OUTDIRTPL_KEYS) - set(tpl_map.keys())

            ydl_opts = {
                "outtmpl": str(self.basepath / self.outdirtpl.format_map(tpl_map) / self.outtmpl.format_map(tpl_map)),
                "format": self.format,
                "retries": self.retries,
                "writeannotations": self.writeannotations,
                "writesubtitles": self.writesubtitles,
                "cookiefile": self.cookiefile,
            }

            def error_too_many_requests(msg: str) -> None:
                logging.error(
                    "Downloading episode id=%s (%s) failed. HTTP Error 429: Too Many Requests: %s. Waiting for %s seconds.",
                    episode_id,
                    url,
                    msg,
                    TOO_MANY_REQUESTS_DELAY,
                )
                time.sleep(TOO_MANY_REQUESTS_DELAY)

            errors: Dict[str, Callable[..., None]] = {
                r"ERROR: Unsupported URL": lambda: logging.error(
                    "Downloading episode id=%s (%s) is not supported", episode_id, url
                ),  # UnsupportedError
                r"ERROR: Incomplete YouTube ID": lambda: logging.error(
                    "YouTube ID of episode id=%s (%s) looks incomplete", episode_id, youtube_token
                ),  # ExtractorError
                r"ERROR: Did not get any data blocks": lambda: logging.error(
                    "Downloading episode id=%s (%s) failed. Did not get any data blocks.", episode_id, url
                ),
                r"ERROR: [a-zA-Z0-9\-_]+: YouTube said: Unable to extract video data": lambda: logging.error(
                    "Downloading episode id=%s (%s) failed. Unable to extract video data.", episode_id, url
                ),  # ExtractorError
                r"ERROR: unable to download video data": lambda: logging.error(
                    "Downloading episode id=%s (%s) failed. Unable to download video data.", episode_id, url
                ),  # ExtractorError
                r"ERROR: giving up after (?P<num>[0-9]+) retries": lambda num: logging.error(
                    "Downloading episode id=%s (%s) failed. Max retries (%s) exceeded.", episode_id, url, num
                ),  # DownloadError
                r"ERROR: This video is not available in your country.": lambda: logging.error(
                    "Downloading episode id=%s (%s) failed. Video geo-blocked.", episode_id, url
                ),  # ExtractorError
                r"ERROR: Unable to download webpage: HTTP Error 429: Too Many Requests (?P<msg>.*)": error_too_many_requests,  # DownloadError
                r"ERROR: Video unavailable\nThis video contains content from (?P<owner>.*), who has blocked it on copyright grounds\.": lambda owner: logging.error(
                    "Downloading episode id=%s (%s) failed. Video blocked by %s on copyright grounds.",
                    episode_id,
                    url,
                    owner,
                ),  # DownloadError
                r"ERROR: Video unavailable\nThis video contains content from (?P<owner>.*), who has blocked it in your country on copyright grounds\.": lambda owner: logging.error(
                    "Downloading episode id=%s (%s) failed. Video blocked by %s in this country on copyright grounds.",
                    episode_id,
                    url,
                    owner,
                ),  # DownloadError
                r"ERROR: Video unavailable\nThis video is private\.": lambda owner: logging.error(
                    "Downloading episode id=%s (%s) failed. Video is private.", episode_id, url
                ),  # DownloadError
            }

            with YoutubeDL(ydl_opts) as ydl:
                try:
                    # retcode = ydl.download([url])
                    res = ydl.extract_info(url)
                    filename = ydl.prepare_filename(res)  # only works correctly if only one format is downloaded
                    relpath = fspath(Path(filename).relative_to(self.basepath))
                    logging.info("Downloaded E%sP%s (%s) to <%s>.", episode_id, episode_part, youtube_token, relpath)
                except UnavailableVideoError:
                    logging.error("Downloading episode id=%s (%s) failed. Format unavailable.", episode_id, url)
                except DownloadError as e:
                    all_done = False
                    errmsg = e.args[0]

                    for pat, logfunc in errors.items():
                        m = re.match(pat, errmsg)
                        if m:
                            logfunc(**m.groupdict())
                            break
                    else:
                        logging.error("errormsg: %r", errmsg)
                        raise
                else:
                    self.records.insert_part(episode_id, episode_part, youtube_token, filename, res)

        if all_done:
            self.records.insert_episode(episode_id)

        return True

    def download_episodes(self, episode_ids: Iterable[int]) -> None:
        for episode in self.backend.get_episodes(episode_ids):
            self._download_episode(episode)

    def download_seasons(self, season_ids: Iterable[int]) -> None:
        for episode in self.backend.get_episodes_by_season(season_ids):
            self._download_episode(episode)

    def download_shows(self, show_ids: Iterable[int], unsorted_only: bool = False) -> None:
        for episode in self.backend.get_episodes_by_show(show_ids, unsorted_only):
            self._download_episode(episode)

    def download_shows_by_name(self, show_names: Iterable[str], unsorted_only: bool = False) -> None:
        for episode in self.backend.get_episodes_by_show_name(show_names, unsorted_only):
            self._download_episode(episode)

    def download_all_shows(self, unsorted_only: bool = False) -> None:
        for episode in self.backend.get_all_episodes(unsorted_only):
            self._download_episode(episode)

    def download_bohnen(self, bohne_ids: Iterable[int], num: int = 1, exclusive: bool = False) -> None:
        for episode in self.backend.get_episodes_by_bohne(bohne_ids, num, exclusive):
            self._download_episode(episode)

    def download_bohnen_by_name(self, bohne_names: Iterable[str], num: int = 1, exclusive: bool = False) -> None:
        for episode in self.backend.get_episodes_by_bohne_name(bohne_names, num, exclusive):
            self._download_episode(episode)

    def download_blog_posts(self, blog_ids: Iterable[int]) -> None:
        for post in self.backend.get_posts(blog_ids):
            blog_id = post["id"]
            path = self.basepath / SINGLE_BLOG_TPL.format(blog_id=blog_id)

            with open(path, "x", encoding="utf-8") as fw:
                json.dump(post, fw, indent="\t", ensure_ascii=False)

    def download_all_blog_posts(self) -> None:
        path = self.basepath / ALL_BLOG_TPL

        with open(path, "x", encoding="utf-8") as fw:
            for post in self.backend.get_all_posts():
                json.dump(post, fw, indent=None, ensure_ascii=False)
                fw.write("\n")


def print_episode_short(episode: JsonDict, season: Optional[JsonDict] = None) -> None:
    if season is None:
        season_info = episode.get("seasonId", "")
    else:
        season_info = season.get("name", "") or season.get("number", "") or season.get("id", "")

    print(
        "id={} {} (show={} season={} ep={}) ({})".format(
            episode["id"],
            episode["title"],
            episode["showName"],
            season_info,
            episode.get("episode", ""),
            parse_datetime(episode["firstBroadcastdate"]),
        )
    )


def print_episode(episode: JsonDict, season: Optional[JsonDict] = None, limit: Optional[int] = None) -> None:
    print_episode_short(episode, season)
    print(episode["description"])
    for token in islice(episode["youtubeTokens"], limit):
        print(youtube_token_to_url(token))


def print_show_short(show: JsonDict) -> None:
    print(
        "id={} {} (genre={} seasons={} '{}')".format(
            show["id"], show["title"], show["genre"], len(show["seasons"]), show["statusPublicNote"] or ""
        )
    )


def print_show_long(show: JsonDict, limit: Optional[int] = None) -> None:
    print_show_short(show)

    if show["hasUnsortedEpisodes"]:
        print("This show contains episodes which are not categorized into a season")

    for season in islice(show["seasons"], limit):
        print("id={} #{} {}".format(season["id"], season["numeric"], season["name"]))
    if not show["seasons"]:
        print("This show doesn't not have any seasons")


def print_bohne_short(bohne: JsonDict) -> None:
    print("id={} {} (episodes={})".format(bohne["mgmtid"], bohne["name"], bohne["episodeCount"]))


def print_post_long(post: JsonDict) -> None:
    print_post_short(post)
    print(post["subtitle"])


def print_post_short(post: JsonDict) -> None:
    authors = ", ".join(a["name"] for a in post.get("authors", []))
    print("id={} {} by '{}' ({})".format(post["id"], post["title"], authors, parse_datetime(post["publishDate"])))


def sort_by_item(it: Iterable[JsonDict], key: Optional[str], limit: Optional[int] = None) -> Iterator[JsonDict]:
    if key:
        return islice(sorted(it, key=itemgetter(key)), limit)
    else:
        return islice(it, limit)


def find_in_columns(text: str, columns: Iterable[str]) -> Callable[[JsonDict], bool]:
    def filter(doc: JsonDict) -> bool:
        for c in columns:
            if text.lower() in doc[c].lower():
                return True

        return False

    return filter


def get_backend(args: Namespace) -> Backend:
    if args.backend == "live":
        return LiveBackend()
    elif args.backend == "local":
        return LocalBackend(args.db_path)
    else:
        raise ValueError(args.backend)


def download(args: Namespace) -> None:
    if args.record_path is None:
        records: Records = MemoryRecords()
    else:
        records = SqliteRecords(args.record_path)

    with get_backend(args) as backend, records:
        rbtv = RBTVDownloader(
            backend,
            records,
            args.basepath,
            args.outdirtpl,
            args.outtmpl,
            args.format,
            args.missing_value,
            args.retries,
            args.cookies,
        )

        if args.episode_id:
            rbtv.download_episodes(args.episode_id)
        elif args.season_id:
            rbtv.download_seasons(args.season_id)
        elif args.show_id:
            rbtv.download_shows(args.show_id, args.unsorted_only)
        elif args.show_name:
            rbtv.download_shows_by_name(args.show_name, args.unsorted_only)
        elif args.all_shows:
            rbtv.download_all_shows(args.unsorted_only)
        elif args.bohne_id:
            rbtv.download_bohnen(args.bohne_id, args.bohne_num, args.bohne_exclusive)
        elif args.bohne_name:
            rbtv.download_bohnen_by_name(args.bohne_name, args.bohne_num, args.bohne_exclusive)
        elif args.blog_id:
            rbtv.download_blog_posts(args.blog_id)
        elif args.all_blog:
            rbtv.download_all_blog_posts()


def browse(args: Namespace) -> None:
    with get_backend(args) as backend:
        if args.episode_id:
            for episode in backend.get_episodes(args.episode_id):
                season = backend.get_season_info(episode)
                print_episode(episode, season, args.limit)

        elif args.season_id:
            for episode in backend.get_episodes_by_season(args.season_id, args.sort_by, args.limit):
                season = backend.get_season_info(episode)
                print_episode_short(episode, season)

        elif args.show_id:
            show_ids: List[int] = []
            for show in backend.get_shows(args.show_id):
                print_show_long(show, args.limit)
                show_ids.append(show["id"])

            if args.episodes:
                for episode in backend.get_episodes_by_show(show_ids, args.unsorted_only, args.sort_by, args.limit):
                    season = backend.get_season_info(episode)
                    print_episode_short(episode, season)

        elif args.show_name:
            show_ids = []
            for show in backend.get_shows_by_name(args.show_name):
                print_show_long(show, args.limit)
                show_ids.append(show["id"])

            if args.episodes:
                for episode in backend.get_episodes_by_show(show_ids, args.unsorted_only, args.sort_by, args.limit):
                    season = backend.get_season_info(episode)
                    print_episode_short(episode, season)

        elif args.all_shows:
            for show in backend.get_all_shows(args.sort_by, args.limit):
                print_show_short(show)

            if args.episodes:
                for episode in backend.get_all_episodes(args.unsorted_only, args.sort_by, args.limit):
                    season = backend.get_season_info(episode)
                    print_episode_short(episode, season)

        elif args.all_bohnen:
            for bohne in backend.get_all_bohnen(args.sort_by, args.limit):
                print_bohne_short(bohne)

            if args.episodes:
                for episode in backend.get_all_episodes(args.unsorted_only, args.sort_by, args.limit):
                    season = backend.get_season_info(episode)
                    print_episode_short(episode, season)

        elif args.blog_id:
            for post in backend.get_posts(args.blog_id):
                print_post_long(post)

        elif args.all_blog:
            for post in backend.get_all_posts(args.sort_by, args.limit):
                print_post_short(post)

        elif args.bohne_id:
            for bohne in backend.get_bohnen(args.bohne_id):
                print_bohne_short(bohne)

            if args.episodes:
                for episode in backend.get_episodes_by_bohne(
                    args.bohne_id, args.bohne_num, args.bohne_exclusive, args.sort_by, args.limit
                ):
                    season = backend.get_season_info(episode)
                    print_episode_short(episode, season)

        elif args.bohne_name:
            for bohne in backend.get_bohnen_by_name(args.bohne_name):
                print_bohne_short(bohne)

            if args.episodes:
                for episode in backend.get_episodes_by_bohne_name(
                    args.bohne_name, args.bohne_num, args.bohne_exclusive, args.sort_by, args.limit
                ):
                    season = backend.get_season_info(episode)
                    print_episode_short(episode, season)

        elif args.search:
            shows, episodes, posts = backend.search(args.search)
            print(f"Shows ({len(shows)})")
            for show in islice(shows, args.limit):
                print_show_short(show)
            print(f"Episodes ({len(episodes)})")
            for episode in islice(episodes, args.limit):
                print_episode_short(episode)

            print(f"Blog posts ({len(posts)})")
            for post in islice(posts, args.limit):
                print_post_short(post)


def reorganize(args: Namespace) -> None:
    def get_untracked(basepath, records) -> Iterator[str]:
        for path in basepath.rglob("*"):
            if not path.is_file():
                continue

            relpath = fspath(path.relative_to(args.basepath))
            tracked = list(
                records.execute("SELECT episode_id, episode_part FROM parts WHERE local_path = ?", (relpath,))
            )
            if not bool(tracked):
                yield relpath

    with get_backend(args) as backend, SqliteRecords(args.record_path) as records:
        if args.subcommand == "list-incomplete-episodes":
            episodes_parts = {row[0] for row in records.execute("SELECT DISTINCT episode_id FROM parts;")}
            episodes_full = {row[0] for row in records.execute("SELECT DISTINCT episode_id FROM episodes;")}

            for episodes, message in [
                (episodes_parts - episodes_full, "Episodes with missing parts ({})"),
                (episodes_full - episodes_parts, "Completed episodes without a single part ({})"),
            ]:
                print(message.format(len(episodes)))
                for episode in backend.get_episodes(episodes):
                    season = backend.get_season_info(episode)
                    print_episode_short(episode, season)

        elif args.subcommand == "list-files":
            for episode_id, episode_part, youtube_token, local_path, info in records:
                print(episode_id, episode_part, youtube_token, local_path)

        elif args.subcommand == "forget-missing-files":
            forget: List[Tuple[int, int, str]] = []
            for episode_id, episode_part, _, local_path, _ in records:
                if not (args.basepath / local_path).exists():
                    forget.append((episode_id, episode_part, local_path))

            episodes: Dict[int, List[str]] = defaultdict(list)  # mypy ignore redefine
            for episode_id, _, local_path in forget:
                episodes[episode_id].append(local_path)

            print(f"Removing {len(forget)} parts belonging to {len(episodes)} episodes")
            for episode, filenames in zip(backend.get_episodes(episodes.keys()), episodes.values()):
                season = backend.get_season_info(episode)
                print_episode_short(episode, season)
                for filename in filenames:
                    print("\t" + filename)

            for episode_id, episode_part, _ in forget:
                records.remove_episode(episode_id)
                records.remove_part(episode_id, episode_part)

        elif args.subcommand == "list-untracked-files":
            for path in get_untracked(args.basepath, records):
                print(path)

        elif args.subcommand == "track-untracked-files":
            tokenp = re.compile(args.regex)

            def get_youtube_token_from_path(path: str):
                m = tokenp.match(path)
                if m:
                    return m.group(1)
                else:
                    return None

            token2path = defaultdict(set)
            token2episode = defaultdict(list)
            for path in get_untracked(args.basepath, records):
                youtube_token = get_youtube_token_from_path(path)
                if youtube_token is None:
                    logging.error(f"Could not extract youtube token from <{path}>")
                    continue
                token2path[youtube_token].add(path)

            for episode in backend.get_episodes_by_youtube_token(token2path.keys()):
                if len(episode["youtubeTokens"]) != len(set(episode["youtubeTokens"])):
                    logging.error("Found duplicate parts: %s", episode["youtubeTokens"])
                    season = backend.get_season_info(episode)
                    print_episode_short(episode, season)
                    continue

                for token in episode["youtubeTokens"]:
                    if token in token2path:
                        token2episode[token].append(episode)

            for token, paths in token2path.items():
                episodes: Optional[List[dict]] = token2episode.get(token)
                if episodes is None:
                    logging.error("Could not find YouTube token %s", token)
                    continue

                if len(paths) == 1:
                    path = list(paths)[0]
                    for episode in episodes:
                        episode_id = episode["id"]
                        episode_part = episode["youtubeTokens"].index(token)
                        try:
                            records.insert_part(episode_id, episode_part, token, path, None)
                        except sqlite3.IntegrityError as e:
                            logging.warning(
                                "Failed to insert %s, %s [%s] <%s>: %s", episode_id, episode_part, token, path, e
                            )
                            season = backend.get_season_info(episode)
                        # print_episode_short(episode, season)
                        # for path in paths:
                        #    print("\t" + path)
                else:
                    logging.warning(
                        "Found %s files and %s episodes for token %s. Requires manual fix.",
                        len(paths),
                        len(episodes),
                        token,
                    )
                    for path in paths:
                        print("\t" + path)


def main() -> None:
    show_params = "--show-id, --show-name or --all-shows"
    bohne_params = "--bohne-id or --bohne-name"

    parser = ArgumentParser(
        description="Simple downloader and browser for the Rocket Beans TV Mediathek.",
        formatter_class=ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--db-path", default=DEFAULT_DB_PATH, type=Path, help="Path to the database file for local backend"
    )
    parser.add_argument(
        "--backend",
        default=DEFAULT_BACKEND,
        choices=ALL_BACKENDS,
        help="Query data from online live api or from locally cached backend",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.required = True

    parser_a = subparsers.add_parser("download", help="download files", formatter_class=ArgumentDefaultsHelpFormatter)
    group = parser_a.add_mutually_exclusive_group(required=True)
    group.add_argument("--episode-id", metavar="ID", nargs="+", type=int, help="Download these episodes")
    group.add_argument("--season-id", metavar="ID", nargs="+", type=int, help="Download all episodes of these seasons")
    group.add_argument("--show-id", metavar="ID", nargs="+", type=int, help="Download all episodes of these shows")
    group.add_argument("--show-name", metavar="NAME", nargs="+", type=str, help="Download all episodes of these shows")
    group.add_argument("--all-shows", action="store_true", help="Download all episodes of all shows")
    group.add_argument("--bohne-id", metavar="ID", nargs="+", type=int, help="Download all episodes by these people")
    group.add_argument(
        "--bohne-name", metavar="NAME", nargs="+", type=str, help="Download all episodes by these people"
    )
    group.add_argument("--blog-id", metavar="ID", nargs="+", type=int, help="Download blog post")
    group.add_argument("--all-blog", action="store_true", help="Download all blog posts")
    parser_a.add_argument(
        "--unsorted-only",
        action="store_true",
        help=f"Only valid in combination with {show_params}. Downloads only unsorted episodes (episodes which are not categorized into seasons).",
    )
    parser_a.add_argument(
        "--bohne-num",
        metavar="N",
        type=posint,
        default=1,
        help=f"Download episodes with at least N of the people specified by {bohne_params} present at the same time",
    )
    parser_a.add_argument(
        "--bohne-exclusive",
        action="store_true",
        help=f"If given, don't allow people other than {bohne_params} to be present",
    )
    parser_a.add_argument("--basepath", metavar="PATH", type=Path, default=DEFAULT_BASEPATH, help="Base output folder")
    parser_a.add_argument(
        "--outdirtpl",
        default=DEFAULT_OUTDIRTPL,
        help=f"Output folder relative to base folder. Can include the following placeholders: {', '.join(OUTDIRTPL_KEYS)}",
    )
    parser_a.add_argument(
        "--outtmpl",
        default=DEFAULT_OUTTMPL,
        help="Output file path relative to output folder. Can include the same placeholders as '--outdirtpl' as well as youtube-dl placeholders. See youtube-dl output template: https://github.com/ytdl-org/youtube-dl#output-template",
    )
    parser_a.add_argument(
        "--format",
        default=DEFAULT_FORMAT,
        help="Video/audio format. Defaults to 'bestvideo+bestaudio' with fallback to 'best'. See youtube-dl format selection: https://github.com/ytdl-org/youtube-dl#format-selection",
    )
    parser_a.add_argument(
        "--missing-value", default=DEFAULT_MISSING_VALUE, help="Value used for --outdirtpl if field is not available."
    )
    parser_a.add_argument(
        "--record-path",
        metavar="PATH",
        default=DEFAULT_RECORD_PATH,
        type=Path,
        help="File path where successful downloads are recorded. These episodes will be skipped if downloaded again.",
    )
    parser_a.add_argument(
        "--retries", metavar="N", default=DEFAULT_RETRIES, type=int, help="Retry failed downloads N times."
    )
    parser_a.add_argument(
        "--cookies", type=is_file, default=None, help="File name where cookies should be read from and dumped to."
    )

    parser_b = subparsers.add_parser("browse", help="browse mediathek", formatter_class=ArgumentDefaultsHelpFormatter)
    group = parser_b.add_mutually_exclusive_group(required=True)
    group.add_argument("--episode-id", metavar="ID", nargs="+", type=int, help="Show episode info")
    group.add_argument("--season-id", metavar="ID", nargs="+", type=int, help="Show season info")
    group.add_argument("--show-id", metavar="ID", nargs="+", type=int, help="Show show info")
    group.add_argument("--show-name", metavar="NAME", nargs="+", type=str, help="Show show info")
    group.add_argument("--all-shows", action="store_true", help="Show a list of all shows")
    group.add_argument("--bohne-id", metavar="ID", nargs="+", type=int, help="Show bohne info")
    group.add_argument("--bohne-name", metavar="NAME", nargs="+", type=str, help="Show bohne info")
    group.add_argument("--all-bohnen", action="store_true", help="Show a list of all Bohnen")
    group.add_argument("--blog-id", metavar="ID", nargs="+", type=int, help="Show blog post info")
    group.add_argument("--all-blog", action="store_true", help="Show all blog posts")
    group.add_argument("--search", type=str, help="Search shows and episodes")
    parser_b.add_argument("--limit", metavar="N", type=int, default=None, help="Limit list output to N items")
    parser_b.add_argument(
        "--sort-by", type=str, choices=("id", "title", "showName", "firstBroadcastdate"), help="Sort output"
    )
    parser_b.add_argument(
        "--unsorted-only",
        action="store_true",
        help=f"Only valid in combination with {show_params}. Only shows unsorted episodes (episodes which are not categorized into seasons).",
    )
    parser_b.add_argument(
        "--bohne-num",
        metavar="N",
        type=posint,
        default=1,
        help=f"Show episodes with at least N of the people specified by {bohne_params} present at the same time",
    )
    parser_b.add_argument(
        "--bohne-exclusive",
        action="store_true",
        help=f"If given, don't allow people other than {bohne_params} to be present",
    )
    parser_b.add_argument(
        "--episodes", action="store_true", help="Also display episodes when applicable (shows, Bohnen, ...)"
    )

    parser_c = subparsers.add_parser(
        "dump", help="dump mediathek meta data for fast search", formatter_class=ArgumentDefaultsHelpFormatter
    )
    parser_c.add_argument(
        "--no-progress", dest="progress", action="store_false", help="Don't show progress when dumping database"
    )

    SUBCOMMANDS = (
        "list-incomplete-episodes",
        "list-files",
        "forget-missing-files",
        "list-untracked-files",
        "track-untracked-files",
    )
    parser_d = subparsers.add_parser(
        "reorganize",
        help="Rename or move already downloaded files to match the titles/seasons online",
        formatter_class=ArgumentDefaultsHelpFormatter,
    )
    parser_d.add_argument("subcommand", choices=SUBCOMMANDS, help="Reorganization command")
    parser_d.add_argument("--basepath", metavar="PATH", type=Path, default=DEFAULT_BASEPATH, help="Base output folder")
    parser_d.add_argument(
        "--record-path",
        metavar="PATH",
        default=DEFAULT_RECORD_PATH,
        type=Path,
        help="File path where successful downloads are recorded. These episodes will be skipped if downloaded again.",
    )
    parser_d.add_argument(
        "--regex",
        default=DEFAULT_TOKEN_REGEX,
        help="Regex pattern to extract the youtube id from files. Used with `track-untracked-files`.",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    if args.command == "download":
        if args.cookies:
            args.cookies = fspath(args.cookies)

        if args.bohne_num <= 0:
            parser.error("--bohne-num must be strictly greater than 0")

        if (args.bohne_num != 1 or args.bohne_exclusive) and not (args.bohne_id or args.bohne_name):
            parser.error(f"--bohne-num and --bohne-exclusive must be used with {bohne_params}")

        if args.unsorted_only and not (args.show_id or args.show_name or args.all_shows):
            parser.error(f"--unsorted-only must be used with {show_params}")

        try:
            download(args)
        except FileNotFoundError as e:
            parser.error(f"{e}. Run `dump` first.")

    elif args.command == "browse":
        if args.bohne_num <= 0:
            parser.error("--bohne-num must be strictly greater than 0")

        if (args.bohne_num != 1 or args.bohne_exclusive) and not (args.bohne_id or args.bohne_name):
            parser.error(f"--bohne-num and --bohne-exclusive must be used with {bohne_params}")

        if args.unsorted_only and not (args.show_id or args.show_name or args.all_shows):
            parser.error(f"--unsorted-only must be used with {show_params}")

        try:
            browse(args)
        except FileNotFoundError as e:
            parser.error(f"{e}. Run `dump` first.")

    elif args.command == "dump":
        if args.backend != "local":
            parser.error("`dump` requires `--backend local`")

        LocalBackend.create(args.db_path, verbose=args.progress)

    elif args.command == "reorganize":
        try:
            reorganize(args)
        except FileNotFoundError as e:
            parser.error(f"{e}. Run `dump` first.")


if __name__ == "__main__":
    main()
