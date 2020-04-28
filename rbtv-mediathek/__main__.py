import logging

from youtube_dl import YoutubeDL, DEFAULT_OUTTMPL
from rbtv import RBTVAPI, name_of_season

def youtube_tokens_to_urls(tokens):
	return ["https://www.youtube.com/watch?v={}".format(token) for token in tokens]

def one(seq):
	assert len(seq) == 1
	return seq[0]

class RBTVDownloader(object):

	def __init__(self, outdirtpl, format="22/best"):
		self.outdirtpl = outdirtpl
		self.format = format
		self.api = RBTVAPI()

	def _download_episode(self, episode):

		logging.debug("Downloading show=%s season=%s episode=%s", episode["showId"], episode["seasonId"], episode["id"])

		tpl_map = {
			"show": episode["showName"],
			"season": name_of_season(self.api.get_season(episode["showId"], episode["seasonId"])),
		}

		ydl_opts = {
			"outtmpl": self.outdirtpl.format(**tpl_map) + "/" + DEFAULT_OUTTMPL,
			"format": self.format,
		}

		with YoutubeDL(ydl_opts) as ydl:
			urls = youtube_tokens_to_urls(episode["youtubeTokens"])
			ydl.download(urls)

	def download_episode(self, episode_id):

		episode = one(self.api.get_episode(episode_id)["episodes"])
		self._download_episode(episode)

	def download_season(self, season_id):

		episodes_combined = self.api.get_episodes_by_season(season_id)

		for ep_combined in episodes_combined:
			for episode in ep_combined["episodes"]:
				self._download_episode(episode)

	def download_show(self, show_id):
		episodes_combined = self.api.get_episodes_by_show(show_id)

		for ep_combined in episodes_combined:
			for episode in ep_combined["episodes"]:
				self._download_episode(episode)

if __name__ == "__main__":

	from argparse import ArgumentParser

	parser = ArgumentParser()
	parser.add_argument("-v", "--verbose", action="store_true")
	subparsers = parser.add_subparsers(dest="command")
	subparsers.required = True

	parser_a = subparsers.add_parser("download", help="download files")
	group = parser_a.add_mutually_exclusive_group(required=True)
	group.add_argument("--episode", type=int, help="Download this episode")
	group.add_argument("--season", type=int, help="Download all episodes of this season")
	group.add_argument("--show", type=int, help="Download all episodes of this show")
	parser_a.add_argument("--format", default="22/best", help="youtube-dl format string")
	parser_a.add_argument("--outdirtpl", default="{show}/{season}")

	parser_b = subparsers.add_parser("browse", help="browse mediathek")
	group = parser_b.add_mutually_exclusive_group(required=True)
	group.add_argument("--episode", type=int, help="Show episode info")
	group.add_argument("--season", type=int, help="Show season info")
	group.add_argument("--show", type=int, help="Show show info")

	args = parser.parse_args()

	if args.verbose:
		logging.basicConfig(level=logging.DEBUG)
	else:
		logging.basicConfig(level=logging.INFO)

	if args.command == "download":

		rbtv = RBTVDownloader(args.outdirtpl, args.format)

		if args.episode:
			rbtv.download_episode(args.episode)
		elif args.season:
			rbtv.download_season(args.season)
		elif args.show:
			rbtv.download_show(args.show)

	if args.command == "browse":

		api = RBTVAPI()

		if args.episode:
			episode = one(api.get_episode(args.episode)["episodes"])
			print("#{} {}\n{}".format(episode["episode"], episode["title"], episode["description"]))

		elif args.season:
			for ep_combined in api.get_episodes_by_season(args.season):
				for episode in ep_combined["episodes"]:
					print("id={} #{} {}".format(episode["id"], episode["episode"], episode["title"]))

		elif args.show:
			show = api.get_show(args.show)
			print("{} ({})".format(show["title"], show["genre"]))
			for season in show["seasons"]:
				print("id={} #{} {}".format(season["id"], season["numeric"], season["name"]))
