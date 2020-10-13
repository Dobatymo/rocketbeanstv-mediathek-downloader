import logging, re, json
from datetime import datetime
from pathlib import Path
from itertools import islice
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter, ArgumentTypeError
from collections import defaultdict
from typing import TYPE_CHECKING

from youtube_dl import YoutubeDL, DEFAULT_OUTTMPL
from youtube_dl.utils import sanitize_filename, DownloadError
from rbtv import RBTVAPI, name_of_season, batch_iter, parse_datetime

if TYPE_CHECKING:
	from typing import Optional, Tuple, Iterable, Iterator, Union, Dict, Set, TextIO, Sequence, TypeVar
	T = TypeVar("T")

__version__ = "0.4"

DEFAULT_BASEPATH = Path(".")
DEFAULT_OUTDIRTPL = "{show_name}/{season_name}"
DEFAULT_MISSING_VALUE = "-"
DEFAULT_RETRIES = 10

# similar to "bestvideo+bestaudio/best", but with improved fallback to "best"
# if separate streams are not possible
DEFAULT_FORMAT = None

OUTDIRTPL_KEYS = ("show_id", "show_name", "season_id", "season_name",
	"season_number", "episode_id", "episode_name", "episode_number", "year",
	"month", "day", "hour", "minute", "second", "duration")
SINGLE_BLOG_TPL = "blog-{blog_id}.json"
ALL_BLOG_TPL = "blog-posts.jl"

def youtube_token_to_url(token):
	# type: (str, ) -> str

	return "https://www.youtube.com/watch?v={}".format(token)

def one(seq):
	# type: (Sequence[T], ) -> T

	if len(seq) != 1:
		raise ValueError("Input must be of length 1")
	return seq[0]

def opt_int(s):
	# type: (Optional[str], ) -> Optional[int]

	if s is None:
		return None
	else:
		return int(s)

def posint(s):
	# type: (str, ) -> int

	number = int(s)

	if number <= 0:
		msg = "{0} is not strictly greater than 0".format(s)
		raise ArgumentTypeError(msg)

	return number

def episode_iter(eps_combined):
	return batch_iter(eps_combined, "episodes")

class RBTVDownloader(object):

	all = "all"

	def __init__(self, basepath=DEFAULT_BASEPATH, outdirtpl=DEFAULT_OUTDIRTPL, outtmpl=DEFAULT_OUTTMPL,
		format=DEFAULT_FORMAT, missing_value=DEFAULT_MISSING_VALUE, record_path=None, retries=DEFAULT_RETRIES):
		# type: (Path, str, str, Optional[str], str, Optional[str], int) -> None

		self.basepath = basepath
		self.outdirtpl = outdirtpl
		self.outtmpl = outtmpl
		self.format = format
		self.missing_value = missing_value
		self.retries = retries
		self.writeannotations = False
		self.writesubtitles = False

		if record_path:
			self.downloaded_episodes = set(self._parse_record_file(record_path))
			self.record_file = open(record_path, "a", encoding="utf-8")  # type: Optional[TextIO]
		else:
			self.downloaded_episodes = set()
			self.record_file = None

		self.api = RBTVAPI()

	def close(self):
		# type: () -> None

		if self.record_file:
			self.record_file.close()

	def __enter__(self):
		# type: () -> RBTVDownloader

		return self

	def __exit__(self, *args):
		self.close()

	@classmethod
	def _parse_record_file(cls, path):
		# type: (str, ) -> Iterator[Union[int, Tuple[int, int]]]

		try:
			with open(path, "r", encoding="utf-8") as fr:
				for line in fr:
					episode_id, episode_part = line.rstrip("\n").split(" ")
					if episode_part == cls.all:
						yield int(episode_id)
					else:
						yield int(episode_id), int(episode_part)
		except FileNotFoundError:
			return

	def _record_id(self, episode_id, episode_part=None):
		# type: (int, Optional[int]) -> None

		if episode_part:
			self.downloaded_episodes.add((episode_id, episode_part))
		else:
			self.downloaded_episodes.add(episode_id)

		if self.record_file:
			self.record_file.write("{} {}\n".format(episode_id, episode_part or self.all))
			self.record_file.flush()

	def _check_record(self, episode_id, episode_part=None):
		# type: (int, Optional[int]) -> bool

		if episode_part:
			return (episode_id, episode_part) in self.downloaded_episodes
		else:
			return episode_id in self.downloaded_episodes

	def _download_episode(self, episode):
		# type: (dict, ) -> bool

		in_season = bool(episode.get("seasonId"))
		episode_id = int(episode["id"])

		if self._check_record(episode_id):
			logging.info("Episode %s was already downloaded", episode_id)
			return False

		if in_season:
			logging.debug("Downloading show=%s season=%s episode=%s", episode["showId"], episode["seasonId"], episode["id"])
		else:
			logging.debug("Downloading show=%s episode=%s", episode["showId"], episode["id"])

		dt = parse_datetime(episode["firstBroadcastdate"])
		if dt:
			year, month, day, hour, minute, second = tuple(map(str, (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second))) # Tuple
		else:
			year, month, day, hour, minute, second = (self.missing_value, ) * 6

		if in_season:
			season = self.api.get_season(episode["showId"], episode["seasonId"])
			season_id = episode["seasonId"]
			season_name = sanitize_filename(name_of_season(season))  # type: Optional[str]
			season_number = opt_int(season["numeric"])  # type: Optional[int]
		else:
			season_id = self.missing_value
			season_name = None
			season_number = None

		tpl_map = {
			"show_id": int(episode["showId"]),
			"show_name": sanitize_filename(episode["showName"]) or self.missing_value,
			"season_id": season_id,
			"season_name": season_name or self.missing_value,
			"season_number": season_number or self.missing_value,
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
		for episode_part, youtube_token in enumerate(episode["youtubeTokens"]):

			if self._check_record(episode_id, episode_part):
				logging.info("Episode %s part %s was already downloaded", episode_id, episode_part)
				continue

			if not youtube_token:
				# fixme: should this set all_done = False ?
				logging.warning("Got empty Youtube token for episode %s part %s", episode_id, episode_part)
				continue

			url = youtube_token_to_url(youtube_token)
			tpl_map["episode_part"] = episode_part

			ydl_opts = {
				"outtmpl": str(self.basepath / self.outdirtpl.format(**tpl_map) / self.outtmpl.format(**tpl_map)),
				"format": self.format,
				"retries": self.retries,
				"writeannotations": self.writeannotations,
				"writesubtitles": self.writesubtitles,
			}

			errors = {
				r"ERROR: Unsupported URL": lambda: logging.error("Downloading episode id=%s (%s) is not supported", episode["id"], url),  # UnsupportedError
				r"ERROR: Incomplete YouTube ID": lambda: logging.error("YouTube ID of episode id=%s (%s) looks incomplete", episode["id"], youtube_token),  # ExtractorError
				r"ERROR: Did not get any data blocks": lambda: logging.error("Downloading episode id=%s (%s) failed. Did not get any data blocks.", episode["id"], url),
				r"ERROR: [a-zA-Z0-9\-_]+: YouTube said: Unable to extract video data": lambda: logging.error("Downloading episode id=%s (%s) failed. Unable to extract video data.", episode["id"], url),  # ExtractorError
				r"ERROR: unable to download video data": lambda: logging.error("Downloading episode id=%s (%s) failed. Unable to download video data.", episode["id"], url),  # ExtractorError
				r"ERROR: giving up after [0-9]+ retries": lambda: logging.error("Downloading episode id=%s (%s) failed. Max retries exceeded.", episode["id"], url),  # DownloadError
			}

			with YoutubeDL(ydl_opts) as ydl:
				try:
					ydl.download([url])
				except DownloadError as e:
					all_done = False
					errmsg = e.args[0]

					for pat, logfunc in errors.items():
						if re.match(pat, errmsg):
							logfunc()
							break
					else:
						raise
				else:
					self._record_id(episode_id, episode_part)

		if all_done:
			self._record_id(episode_id)

		return True

	def _print_episode(self, episode):
		# type: (dict, ) -> None

		episode_id = int(episode["id"])
		title = episode["title"]
		hosts = [self.api.bohne_id_to_name(bohne_id) for bohne_id in episode["hosts"]]
		print("id={}: {} ({})".format(episode_id, title, ", ".join(hosts)))

	def _download_episodes(self, episodes):
		# type: (Iterable[dict], ) -> None

		for episode in episodes:
			self._download_episode(episode)

	def _print_episodes(self, episodes):
		# type: (Iterable[dict], ) -> None

		for episode in episodes:
			self._print_episode(episode)

	def download_episode(self, episode_id, dry=False):
		# type: (int, bool) -> None

		episode = one(self.api.get_episode(episode_id)["episodes"])
		
		if dry:
			self._print_episode(episode)
		else:
			self._download_episode(episode)

	def download_season(self, season_id, dry=False):
		# type: (int, bool) -> None

		for episode in episode_iter(self.api.get_episodes_by_season(season_id)):
			if dry:
				self._print_episode(episode)
			else:
				self._download_episode(episode)

	def download_show(self, show_id, unsorted_only=False, dry=False):
		# type: (int, bool, bool) -> None

		if unsorted_only:
			for episode in episode_iter(self.api.get_unsorted_episodes_by_show(show_id)):
				if dry:
					self._print_episode(episode)
				else:
					self._download_episode(episode)
			# fixme: logging.warning("No unsorted episodes found for show id=%s", show_id)

		else:
			for episode in episode_iter(self.api.get_episodes_by_show(show_id)):
				if dry:
					self._print_episode(episode)
				else:
					self._download_episode(episode)

	def download_show_by_name(self, show_name, unsorted_only=False, dry=False):
		# type: (str, bool, bool) -> None

		show_id = self.api.show_name_to_id(show_name)
		self.download_show(show_id, unsorted_only, dry)

	def download_all_shows(self, unsorted_only=False, dry=False):
		# type: (bool, bool) -> None

		for show in self.api.get_shows_mini():
			show_id = show["id"]
			self.download_show(show_id, unsorted_only, dry)

	def _iter_episodes_for_bohnen(self, bohne_ids):
		# type: (Iterable[int], ) -> Iterator[dict]

		for bohne_id in bohne_ids:
			for episode in episode_iter(self.api.get_episodes_by_bohne(bohne_id)):
				yield episode

	@staticmethod
	def filter_sets(bohnen, bohne_ids, num, exclusive):
		# type: (Dict[int, Set[int]], Set[int], int, bool) -> Iterator[int]

		for episode_id, ids in bohnen.items():
			if len(bohne_ids & ids) >= num: # at last n people are in this episode
				if not exclusive or not (ids - bohne_ids): # nobody else in this episode if exclusive
					yield episode_id

	def _get_episodes_for_bohnen(self, bohne_ids, num, exclusive):
		# type: (Set[int], int, bool) -> Iterable[dict]

		if num == 1 and not exclusive: # low memory fast path for common options
			return self._iter_episodes_for_bohnen(bohne_ids)
		else:
			episodes = {}  # type: Dict[int, dict]

			for bohne_id in bohne_ids:
				for episode in episode_iter(self.api.get_episodes_by_bohne(bohne_id)):
					episode_id = int(episode["id"])
					episodes[episode_id] = episode

			bohnen = {ep_id: set(ep["hosts"]) for ep_id, ep in episodes.items()}

			return [episodes[episode_id] for episode_id in self.filter_sets(bohnen, bohne_ids, num, exclusive)]

	def download_bohnen(self, bohne_ids, num=1, exclusive=False, dry=False):
		# type: (Iterable[int], int, bool, bool) -> None

		episodes = self._get_episodes_for_bohnen(set(bohne_ids), num, exclusive)
		if dry:
			self._print_episodes(episodes)
		else:
			self._download_episodes(episodes)

	def download_bohnen_by_name(self, bohne_names, num=1, exclusive=False, dry=False):
		# type: (Iterable[str], int, bool, bool) -> None

		bohne_ids = [self.api.bohne_name_to_id(bohne_name) for bohne_name in bohne_names]
		self.download_bohnen(bohne_ids, num, exclusive, dry)

	def download_blog_post(self, blog_id, dry=False):
		# type: (int, bool) -> None

		post = self.api.get_blog_post(blog_id)
		path = self.basepath / SINGLE_BLOG_TPL.format(blog_id=blog_id)

		with open(path, "xt", encoding="utf-8") as fw:
			json.dump(post, fw, indent="\t", ensure_ascii=False)

	def download_blog_posts(self, dry=False):
		# type: (bool, ) -> None

		path = self.basepath / ALL_BLOG_TPL

		with open(path, "xt", encoding="utf-8") as fw:
			for post in self.api.get_blog_posts():
				json.dump(post, fw, indent=None, ensure_ascii=False)
				fw.write("\n")

def main():
	# type: () -> None

	show_params = "--show-id, --show-name or --all-shows"
	bohne_params = "--bohne-id or --bohne-name"

	parser = ArgumentParser(
		description="Simple downloader and browser for the Rocket Beans TV Mediathek.",
		formatter_class=ArgumentDefaultsHelpFormatter
	)
	parser.add_argument("-v", "--verbose", action="store_true")
	parser.add_argument("--version", action="version", version=__version__)
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
	group.add_argument("--bohne-name", metavar="NAME", nargs="+", type=str, help="Download all episodes by these people")
	group.add_argument("--blog-id", metavar="ID", nargs="+", type=int, help="Download blog post")
	group.add_argument("--all-blog", action="store_true", help="Download all blog posts")
	parser_a.add_argument("--unsorted-only", action="store_true", help="Only valid in combination with {}. Downloads only unsorted episodes (episodes which are not categorized into seasons).".format(show_params))
	parser_a.add_argument("--bohne-num", metavar="n", type=posint, default=1, help="Download episodes with at least n of the people specified by {} present at the same time".format(bohne_params))
	parser_a.add_argument("--bohne-exclusive", action="store_true", help="If given, don't allow people other than {} to be present".format(bohne_params))
	parser_a.add_argument("--basepath", metavar="PATH", type=Path, default=DEFAULT_BASEPATH, help="Base output folder")
	parser_a.add_argument("--outdirtpl", default=DEFAULT_OUTDIRTPL, help="Output folder relative to base folder. Can include the following placeholders: {}".format(", ".join(OUTDIRTPL_KEYS)))
	parser_a.add_argument("--outtmpl", default=DEFAULT_OUTTMPL, help="Output file path relative to output folder. Can include the same placeholders as '--outdirtpl' as well as youtube-dl placeholders. See youtube-dl output template: https://github.com/ytdl-org/youtube-dl#output-template")
	parser_a.add_argument("--format", default=DEFAULT_FORMAT, help="Video/audio format. Defaults to 'bestvideo+bestaudio' with fallback to 'best'. See youtube-dl format selection: https://github.com/ytdl-org/youtube-dl#format-selection")
	parser_a.add_argument("--missing-value", default=DEFAULT_MISSING_VALUE, help="Value used for --outdirtpl if field is not available.")
	parser_a.add_argument("--record-path", metavar="PATH", default=None, type=Path, help="File path where successful downloads are recorded. These episodes will be skipped if downloaded again.")
	parser_a.add_argument("--retries", metavar="N", default=DEFAULT_RETRIES, type=int, help="Retry failed downloads N times.")
	parser_a.add_argument("--dry", action="store_true", help="Download actually download the files. Just display a list.")

	parser_b = subparsers.add_parser("browse", help="browse mediathek", formatter_class=ArgumentDefaultsHelpFormatter)
	group = parser_b.add_mutually_exclusive_group(required=True)
	group.add_argument("--episode-id", metavar="ID", type=int, help="Show episode info")
	group.add_argument("--season-id", metavar="ID", type=int, help="Show season info")
	group.add_argument("--show-id", metavar="ID", type=int, help="Show show info")
	group.add_argument("--show-name", metavar="NAME", type=str, help="Show show info")
	group.add_argument("--all-shows", action="store_true", help="Show a list of all shows")
	group.add_argument("--bohne-id", metavar="ID", type=int, help="Show bohne info")
	group.add_argument("--bohne-name", metavar="NAME", type=str, help="Show bohne info")
	group.add_argument("--all-bohnen", action="store_true", help="Show a list of all Bohnen")
	group.add_argument("--blog-id", metavar="ID", type=int, help="Show blog post info")
	group.add_argument("--all-blog", action="store_true", help="Show all blog posts")
	group.add_argument("--search", type=str, help="Search shows and episodes")
	parser_b.add_argument("--limit", metavar="N", type=int, default=None, help="Limit list output to N items")

	args = parser.parse_args()

	if args.verbose:
		logging.basicConfig(level=logging.DEBUG)
	else:
		logging.basicConfig(level=logging.INFO)

	if args.command == "download":

		if args.bohne_num <= 0:
			parser.error("--bohne-num must be strictly greater than 0")

		if (args.bohne_num != 1 or args.bohne_exclusive) and not (args.bohne_id or args.bohne_name):
			parser.error("--bohne-num and --bohne-exclusive must be used with {}".format(bohne_params))

		if args.unsorted_only and not (args.show_id or args.show_name or args.all_shows):
			parser.error("--unsorted-only must be used with {}".format(show_params))

		with RBTVDownloader(args.basepath, args.outdirtpl, args.outtmpl, args.format, args.missing_value, args.record_path) as rbtv:

			if args.episode_id:
				for episode_id in args.episode_id:
					rbtv.download_episode(episode_id, args.dry)
			elif args.season_id:
				for season_id in args.season_id:
					rbtv.download_season(season_id, args.dry)
			elif args.show_id:
				for show_id in args.show_id:
					rbtv.download_show(show_id, args.unsorted_only, args.dry)
			elif args.show_name:
				for show_name in args.show_name:
					rbtv.download_show_by_name(show_name, args.unsorted_only, args.dry)
			elif args.all_shows:
				rbtv.download_all_shows(args.unsorted_only, args.dry)
			elif args.bohne_id:
				rbtv.download_bohnen(args.bohne_id, args.bohne_num, args.bohne_exclusive, args.dry)
			elif args.bohne_name:
				rbtv.download_bohnen_by_name(args.bohne_name, args.bohne_num, args.bohne_exclusive, args.dry)
			elif args.blog_id:
				for blog_id in args.blog_id:
					rbtv.download_blog_post(blog_id, args.dry)
			elif args.all_blog:
				rbtv.download_blog_posts(args.dry)

	if args.command == "browse":

		api = RBTVAPI()

		if args.episode_id:
			episode = one(api.get_episode(args.episode_id)["episodes"])
			print("#{} {}\n{}".format(episode["episode"], episode["title"], episode["description"]))
			for token in islice(episode["youtubeTokens"], args.limit):
				print(youtube_token_to_url(token))

		elif args.season_id:
			for episode in islice(episode_iter(api.get_episodes_by_season(args.season_id)), args.limit):
				print("id={} #{} {}".format(episode["id"], episode["episode"], episode["title"]))

		elif args.show_id or args.show_name:

			if args.show_name:
				show_id = api.show_name_to_id(args.show_name)
			else:
				show_id = args.show_id

			show = api.get_show(show_id)
			print("{} (genre={})".format(show["title"], show["genre"]))
			if show["hasUnsortedEpisodes"]:
				print("This show contains episodes which are not categorized into a season")

			for season in islice(show["seasons"], args.limit):
				print("id={} #{} {}".format(season["id"], season["numeric"], season["name"]))
			if not show["seasons"]:
				print("This show doesn't not have any seasons")

		elif args.all_shows:
			for show in islice(api.get_shows_mini(), args.limit):
				print("id={} {}".format(show["id"], show["title"]))

		elif args.bohne_id or args.bohne_name:

			if args.bohne_name:
				bohne_id = api.bohne_name_to_id(args.bohne_name)
			else:
				bohne_id = args.bohne_id

			bohne = api.get_bohne_portrait(bohne_id)
			print("{} (episodes={})".format(bohne["name"], bohne["episodeCount"]))

			for episode in islice(episode_iter(api.get_episodes_by_bohne(bohne_id)), args.limit):
				print("id={} #{} {}".format(episode["id"], episode["episode"], episode["title"]))

		elif args.all_bohnen:
			for bohne in islice(api.get_bohnen_portraits(), args.limit):
				print("id={} {} (episodes={})".format(bohne["mgmtid"], bohne["name"], bohne["episodeCount"]))

		elif args.blog_id:
			post = api.get_blog_post_preview(args.blog_id)
			authors = ", ".join(a["name"] for a in post["authors"])
			print("{} by {} published on {}\n{}".format(post["title"], authors, parse_datetime(post["publishDate"]), post["subtitle"]))

		elif args.all_blog:
			for post in islice(api.get_blog_posts_preview(), args.limit):
				authors = ", ".join(a["name"] for a in post["authors"])
				print("id={} {} by {} published on {}".format(post["id"], post["title"], authors, parse_datetime(post["publishDate"])))

		elif args.search:
			result = api.search(args.search)
			print("Shows ({})".format(len(result["shows"])))
			for show in islice(result["shows"], args.limit):
				print("id={} {}".format(show["id"], show["title"]))
			print("Episodes ({})".format(len(result["episodes"])))
			for episode in islice(result["episodes"], args.limit):
				print("id={} {} (show={}) ({})".format(episode["id"], episode["title"], episode["showName"], parse_datetime(episode["firstBroadcastdate"])))
			print("Blog posts ({})".format(len(result["blog"])))
			for post in islice(result["blog"], args.limit):
				print("id={} {} ({})".format(post["id"], post["title"], parse_datetime(post["publishDate"])))

if __name__ == "__main__":
	main()