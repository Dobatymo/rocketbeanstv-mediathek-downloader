import logging, re
from datetime import datetime
from pathlib import Path

from youtube_dl import YoutubeDL, DEFAULT_OUTTMPL
from youtube_dl.utils import sanitize_filename, DownloadError
from rbtv import RBTVAPI, name_of_season

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

	def __init__(self, basepath=DEFAULT_BASEPATH, outdirtpl=DEFAULT_OUTDIRTPL, outtmpl=DEFAULT_OUTTMPL, format=DEFAULT_FORMAT, missing_value=DEFAULT_MISSING_VALUE):
		# type: (Path, str, str, str, str) -> None

		self.basepath = basepath
		self.outdirtpl = outdirtpl
		self.outtmpl = outtmpl
		self.format = format
		self.missing_value = missing_value

		self.api = RBTVAPI()

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
		# type: (dict, ) -> None

		in_season = "seasonId" in episode

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
			"episode_id": int(episode["id"]),
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
			except DownloadError as e:
				if e.args[0].startswith("ERROR: Unsupported URL"): # UnsupportedError
					logging.exception("Downloading episode id=%s (%r) is not supported", episode["id"], episode["youtubeTokens"])
				elif e.args[0].startswith("ERROR: Incomplete YouTube ID"): # ExtractorError
					logging.exception("YouTube ID of episode id=%s (%r) looks incomplete", episode["id"], episode["youtubeTokens"])
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

if __name__ == "__main__":

	from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter

	parser = ArgumentParser(
		description="Simple downloader for the Rocket Beans TV Mediathek.",
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
	parser_a.add_argument("--unsorted-only", action="store_true", help="Only valid in combination with --show-id. Downloads only unsorted episodes (episodes which are not categorized into seasons).")
	parser_a.add_argument("--basepath", type=Path, default=DEFAULT_BASEPATH, help="Base output folder")
	parser_a.add_argument("--outdirtpl", default=DEFAULT_OUTDIRTPL, help="Output folder relative to base folder. Can include the following placeholders: {}".format(", ".join(OUTDIRTPL_KEYS)))
	parser_a.add_argument("--outtmpl", default=DEFAULT_OUTTMPL, help="Output file path relative to output folder. See youtube-dl output template: https://github.com/ytdl-org/youtube-dl#output-template")
	parser_a.add_argument("--format", default=DEFAULT_FORMAT, help="Video/audio format. Defaults to 'bestvideo+bestaudio' with fallback to 'best'. See youtube-dl format selection: https://github.com/ytdl-org/youtube-dl#format-selection")
	parser_a.add_argument("--missing-value", default=DEFAULT_MISSING_VALUE, help="Value used for --outdirtpl if field is not available.")

	parser_b = subparsers.add_parser("browse", help="browse mediathek", formatter_class=ArgumentDefaultsHelpFormatter)
	group = parser_b.add_mutually_exclusive_group(required=True)
	group.add_argument("--episode-id", type=int, help="Show episode info")
	group.add_argument("--season-id", type=int, help="Show season info")
	group.add_argument("--show-id", type=int, help="Show show info")

	args = parser.parse_args()

	if args.verbose:
		logging.basicConfig(level=logging.DEBUG)
	else:
		logging.basicConfig(level=logging.INFO)

	if args.command == "download":

		if args.unsorted_only and not args.show_id:
			parser.error("--unsorted-only must be used together with --show-id")

		rbtv = RBTVDownloader(args.basepath, args.outdirtpl, args.outtmpl, args.format, args.missing_value)

		if args.episode_id:
			rbtv.download_episode(args.episode_id)
		elif args.season_id:
			rbtv.download_season(args.season_id)
		elif args.show_id:
			rbtv.download_show(args.show_id, args.unsorted_only)

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

		elif args.show_id:
			show = api.get_show(args.show_id)
			print("{} (genre={})".format(show["title"], show["genre"]))
			if show["hasUnsortedEpisodes"]:
				print("This show contains episodes which are not categorized into a season")

			for season in show["seasons"]:
				print("id={} #{} {}".format(season["id"], season["numeric"], season["name"]))
			if not show["seasons"]:
				print("This show doesn't not have any seasons")
