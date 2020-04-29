# Rocket Beans TV Mediathek downloader and browser

## Installation

Install Python 3.5 (or newer) from <https://www.python.org/downloads/>.
If you downloaded the `rbtv-mediathek.pyz` file from the release section, just call it from the command line.
Otherwise install the dependencies with `py -m pip install -r requirements.txt` and run `rbtv-mediathek/__main__.py`.

For 1080p support you also need FFmpeg <https://www.ffmpeg.org/download.html> or avconv <https://libav.org/download/> in %PATH%.

## Usage

### `rbtv-mediathek.pyz download --help`
```
usage: __main__.py download [-h]
                            (--episode-id EPISODE_ID | --season-id SEASON_ID | --show-id SHOW_ID)
                            [--basepath BASEPATH] [--outdirtpl OUTDIRTPL]
                            [--outtmpl OUTTMPL] [--format FORMAT]

optional arguments:
  -h, --help            show this help message and exit
  --episode-id EPISODE_ID
                        Download this episode (default: None)
  --season-id SEASON_ID
                        Download all episodes of this season (default: None)
  --show-id SHOW_ID     Download all episodes of this show (default: None)
  --basepath BASEPATH   Base output folder (default: .)
  --outdirtpl OUTDIRTPL
                        Output folder relative to base folder. Can include the
                        following placeholders: show_id, show_name, season_id,
                        season_name, season_number, episode_id, episode_name,
                        episode_number, year, month, day, hour, minute, second
                        (default: {show_name}/{season_name})
  --outtmpl OUTTMPL     Output file path relative to output folder. See
                        youtube-dl output template: https://github.com/ytdl-
                        org/youtube-dl#output-template (default:
                        %(title)s-%(id)s.%(ext)s)
  --format FORMAT       Video/audio format. Defaults to 'bestvideo+bestaudio'
                        with fallback to 'best'. See youtube-dl format
                        selection: https://github.com/ytdl-org/youtube-
                        dl#format-selection (default: None)
```

Example: `rbtv-mediathek.pyz download --show-id 99 --basepath "C:/Download/RBTV" --format "22/best"`
This will download all episodes for the show "After Dark" in 720p mp4 format (or next best if not available) to `C:/Download/RBTV` with subfolders per show and season (eg. `G:\PUBLIC_ARCHIVE\Rocket Beans TV\After Dark\Resident Evil Code - Veronica X`).

### `rbtv-mediathek.pyz browse --help`
```
usage: __main__.py browse [-h]
                          (--episode-id EPISODE_ID | --season-id SEASON_ID | --show-id SHOW_ID)

optional arguments:
  -h, --help            show this help message and exit
  --episode-id EPISODE_ID
                        Show episode info (default: None)
  --season-id SEASON_ID
                        Show season info (default: None)
  --show-id SHOW_ID     Show show info (default: None)
```

## Missing features (which I intend to implement in the future)

- Does not download episodes belonging to show if they don't belong to a season
- Does not retry failed download
- Download episodes by host
- Download episodes by show/season name instead of ID
- Events

## Support RBTV

<https://rocketbeans.tv/supporte-uns>
