import logging, re
from datetime import datetime
from pathlib import Path

from youtube_dl import YoutubeDL, DEFAULT_OUTTMPL
from youtube_dl.utils import sanitize_filename, DownloadError
from rbtv import RBTVAPI, name_of_season

__version__ = "0.2"

DEFAULT_BASEPATH = Path(".")
DEFAULT_OUTDIRTPL = "{show_name}/{season_name}"
DEFAULT_MISSING_VALUE = "-"

# similar to "bestvideo+bestaudio/best", but with improved fallback to "best"
# if separate streams are not possible
DEFAULT_FORMAT = None

OUTDIRTPL_KEYS = ("show_id", "show_name", "season_id", "season_name",
	"season_number", "episode_id", "episode_name", "episode_number", "year",
	"month", "day", "hour", "minute", "second")

def youtube_tokens_to_urls(tokens):
	for token in tokens:
		if token:
			yield "https://www.youtube.com/watch?v={}".format(token)
		else:
			logging.warning("Got empty Youtube token from API")

def one(seq):
	assert len(seq) == 1
	return seq[0]

def opt_int(s):
	if s:
		return int(s)
	else:
		return s

class RBTVDownloader(object):

	def __init__(self, basepath=DEFAULT_BASEPATH, outdirtpl=DEFAULT_OUTDIRTPL, outtmpl=DEFAULT_OUTTMPL,
		format=DEFAULT_FORMAT, missing_value=DEFAULT_MISSING_VALUE, record_path=None):
		# type: (Path, str, str, str, str) -> None

		self.basepath = basepath
		self.outdirtpl = outdirtpl
		self.outtmpl = outtmpl
		self.format = format
		self.missing_value = missing_value

		if record_path:
			try:
				with open(record_path, "r", encoding="utf-8") as fr:
					self.downloaded_episodes = set(int(episode_id.rstrip("\n")) for episode_id in fr if episode_id)
			except FileNotFoundError:
				self.downloaded_episodes = set()
			self.record_file = open(record_path, "a", encoding="utf-8")
		else:
			self.downloaded_episodes = set()
			self.record_file = None

		self.api = RBTVAPI()

	def close(self):
		if self.record_file:
			self.record_file.close()

	def __enter__(self):
		return self

	def __exit__(self, *args):
		self.close()

	def _record_id(self, episode_id):
		self.downloaded_episodes.add(episode_id)
		if self.record_file:
			self.record_file.write("{}\n".format(episode_id))
			self.record_file.flush()

	def _parse_broadcast_date(self, datestr):
		# type: (Optional[str], ) -> tuple

		if not datestr:
			return (self.missing_value, ) * 6

		if datestr.endswith("Z"):
			datestr = datestr[:-1] + "+00:00"

		try:
			dt = datetime.fromisoformat(datestr)
			return (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
		except ValueError:
			return (self.missing_value, ) * 6

	def _download_episode(self, episode):
		# type: (dict, ) -> bool

		in_season = "seasonId" in episode
		episode_id = int(episode["id"])

		if episode_id in self.downloaded_episodes:
			logging.info("Episode %s was already downloaded", episode_id)
			return False

		if in_season:
			logging.debug("Downloading show=%s season=%s episode=%s", episode["showId"], episode["seasonId"], episode["id"])
		else:
			logging.debug("Downloading show=%s episode=%s", episode["showId"], episode["id"])

		year, month, day, hour, minute, second = self._parse_broadcast_date(episode["firstBroadcastdate"])

		if in_season:
			season = self.api.get_season(episode["showId"], episode["seasonId"])
			season_id = int(episode["seasonId"])
			season_name = sanitize_filename(name_of_season(season))
			season_number = opt_int(season["numeric"])
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
		}

		ydl_opts = {
			"outtmpl": str(self.basepath / self.outdirtpl.format(**tpl_map) / self.outtmpl),
			"format": self.format,
		}

		with YoutubeDL(ydl_opts) as ydl:
			urls = list(youtube_tokens_to_urls(episode["youtubeTokens"]))
			try:
				ydl.download(urls)
				self._record_id(episode_id)
				return True
			except DownloadError as e:
				errmsg = e.args[0]
				if errmsg.startswith("ERROR: Unsupported URL"): # UnsupportedError
					logging.exception("Downloading episode id=%s (%r) is not supported", episode["id"], episode["youtubeTokens"])
				elif errmsg.startswith("ERROR: Incomplete YouTube ID"): # ExtractorError
					logging.exception("YouTube ID of episode id=%s (%r) looks incomplete", episode["id"], episode["youtubeTokens"])
				elif errmsg.startswith("ERROR: Did not get any data blocks"):
					logging.exception("Downloading episode id=%s (%r) failed. YouTube did not return data.", episode["id"], episode["youtubeTokens"])
				elif errmsg.startswith("ERROR: unable to download video data"):
					logging.exception("Downloading episode id=%s (%r) failed. YouTube did not return data.", episode["id"], episode["youtubeTokens"])
				else:
					raise

	def download_episode(self, episode_id):
		# type: (int, ) -> None

		episode = one(self.api.get_episode(episode_id)["episodes"])
		self._download_episode(episode)

	def download_season(self, season_id):
		# type: (int, ) -> None

		episodes_combined = self.api.get_episodes_by_season(season_id)

		for ep_combined in episodes_combined:
			for episode in ep_combined["episodes"]:
				self._download_episode(episode)

	def download_show(self, show_id, unsorted_only=False):
		# type: (int, bool) -> None

		if unsorted_only:
			for ep_combined in self.api.get_unsorted_episodes_by_show(show_id):
				for episode in ep_combined["episodes"]:
					self._download_episode(episode)
			# fixme: logging.warning("No unsorted episodes found for show id=%s", show_id)

		else:
			for ep_combined in self.api.get_episodes_by_show(show_id):
				for episode in ep_combined["episodes"]:
					self._download_episode(episode)

	def download_show_by_name(self, show_name, unsorted_only=False):
		# type: (str, bool) -> None

		show_id = self.api.show_name_to_id(show_name)
		self.download_show(show_id, unsorted_only)

	def download_all_shows(self, unsorted_only=False):
		for show in self.api.get_shows_mini():
			show_id = show["id"]
			self.download_show(show_id, unsorted_only)

	def download_bohne(self, bohne_id):
		# type: (int, ) -> None

		for ep_combined in self.api.get_episodes_by_bohne(bohne_id):
			for episode in ep_combined["episodes"]:
				self._download_episode(episode)

	def download_bohne_by_name(self, bohne_name):
		# type: (str, ) -> None

		bohne_id = self.api.bohne_name_to_id(bohne_name)
		self.download_bohne(bohne_id)


if __name__ == "__main__":

	from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter

	show_params = "--show-id, --show-name or --all-shows"

	parser = ArgumentParser(
		description="Simple downloader and browser for the Rocket Beans TV Mediathek.",
		formatter_class=ArgumentDefaultsHelpFormatter
	)
	parser.add_argument("-v", "--verbose", action="store_true")
	subparsers = parser.add_subparsers(dest="command")
	subparsers.required = True

	parser_a = subparsers.add_parser("download", help="download files", formatter_class=ArgumentDefaultsHelpFormatter)
	group = parser_a.add_mutually_exclusive_group(required=True)
	group.add_argument("--episode-id", type=int, help="Download this episode")
	group.add_argument("--season-id", type=int, help="Download all episodes of this season")
	group.add_argument("--show-id", type=int, help="Download all episodes of this show")
	group.add_argument("--show-name", type=str, help="Download all episodes of this show")
	group.add_argument("--all-shows", action="store_true", help="Download all episodes of all shows")
	group.add_argument("--bohne-id", type=int, help="Download all episodes by Bohne")
	group.add_argument("--bohne-name", type=str, help="Download all episodes by Bohne")
	parser_a.add_argument("--unsorted-only", action="store_true", help="Only valid in combination with {}. Downloads only unsorted episodes (episodes which are not categorized into seasons).".format(show_params))
	parser_a.add_argument("--basepath", type=Path, default=DEFAULT_BASEPATH, help="Base output folder")
	parser_a.add_argument("--outdirtpl", default=DEFAULT_OUTDIRTPL, help="Output folder relative to base folder. Can include the following placeholders: {}".format(", ".join(OUTDIRTPL_KEYS)))
	parser_a.add_argument("--outtmpl", default=DEFAULT_OUTTMPL, help="Output file path relative to output folder. See youtube-dl output template: https://github.com/ytdl-org/youtube-dl#output-template")
	parser_a.add_argument("--format", default=DEFAULT_FORMAT, help="Video/audio format. Defaults to 'bestvideo+bestaudio' with fallback to 'best'. See youtube-dl format selection: https://github.com/ytdl-org/youtube-dl#format-selection")
	parser_a.add_argument("--missing-value", default=DEFAULT_MISSING_VALUE, help="Value used for --outdirtpl if field is not available.")
	parser_a.add_argument("--record-path", default=None, type=Path, help="File path where successful downloads are recorded. These episodes will be skipped if downloaded again.")

	parser_b = subparsers.add_parser("browse", help="browse mediathek", formatter_class=ArgumentDefaultsHelpFormatter)
	group = parser_b.add_mutually_exclusive_group(required=True)
	group.add_argument("--episode-id", type=int, help="Show episode info")
	group.add_argument("--season-id", type=int, help="Show season info")
	group.add_argument("--show-id", type=int, help="Show show info")
	group.add_argument("--show-name", type=str, help="Show show info")
	group.add_argument("--all-shows", action="store_true", help="Show a list of all shows")
	group.add_argument("--bohne-id", type=int, help="Show bohne info")
	group.add_argument("--bohne-name", type=str, help="Show bohne info")
	group.add_argument("--all-bohnen", action="store_true", help="Show a list of all Bohnen")
	parser_b.add_argument("--preview", action="store_true", help="Don't output full information. Much faster.")

	args = parser.parse_args()

	if args.verbose:
		logging.basicConfig(level=logging.DEBUG)
	else:
		logging.basicConfig(level=logging.INFO)

	if args.command == "download":

		if args.unsorted_only and not (args.show_id or args.show_name or args.all_shows):
			parser.error("--unsorted-only must be used together with {}".format(show_params))

		with RBTVDownloader(args.basepath, args.outdirtpl, args.outtmpl, args.format, args.missing_value, args.record_path) as rbtv:

			if args.episode_id:
				rbtv.download_episode(args.episode_id)
			elif args.season_id:
				rbtv.download_season(args.season_id)
			elif args.show_id:
				rbtv.download_show(args.show_id, args.unsorted_only)
			elif args.show_name:
				rbtv.download_show_by_name(args.show_name, args.unsorted_only)
			elif args.all_shows:
				rbtv.download_all_shows(args.unsorted_only)
			elif args.bohne_id:
				rbtv.download_bohne(args.bohne_id)
			elif args.bohne_name:
				rbtv.download_bohne_by_name(args.bohne_name)

	if args.command == "browse":

		api = RBTVAPI()

		if args.episode_id:
			episode = one(api.get_episode(args.episode_id)["episodes"])
			print("#{} {}\n{}".format(episode["episode"], episode["title"], episode["description"]))
			for url in youtube_tokens_to_urls(episode["youtubeTokens"]):
				print(url)

		elif args.season_id:
			for ep_combined in api.get_episodes_by_season(args.season_id):
				for episode in ep_combined["episodes"]:
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

			for season in show["seasons"]:
				print("id={} #{} {}".format(season["id"], season["numeric"], season["name"]))
			if not show["seasons"]:
				print("This show doesn't not have any seasons")

		elif args.all_shows:
			for show in api.get_shows_mini():
				print("id={} {}".format(show["id"], show["title"]))

		elif args.bohne_id or args.bohne_name:

			if args.bohne_name:
				bohne_id = api.bohne_name_to_id(args.bohne_name)
			else:
				bohne_id = args.bohne_id

			bohne = api.get_bohne_portrait(bohne_id)
			print("{} (episodes={})".format(bohne["name"], bohne["episodeCount"]))

			if args.preview:
				for episode in api.get_episodes_by_bohne_preview(bohne_id)["episodes"]:
					print("id={} {}".format(episode["id"], episode["title"]))

			else:
				for ep_combined in api.get_episodes_by_bohne(bohne_id):
					for episode in ep_combined["episodes"]:
						print("id={} #{} {}".format(episode["id"], episode["episode"], episode["title"]))

		elif args.all_bohnen:
			for bohne in api.get_bohnen_portraits():
				print("id={} {}".format(bohne["mgmtid"], bohne["name"]))
