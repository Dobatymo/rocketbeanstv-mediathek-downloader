# Rocket Beans TV Mediathek downloader and browser

## Installation

Install Python 3.5 (or newer) from <https://www.python.org/downloads/>.
If you downloaded the `rbtv-mediathek.pyz` file from the release section, just call it from the command line.
Otherwise install the dependencies with `py -m pip install -r requirements.txt` and run `rbtv-mediathek/__main__.py`.

For 1080p support you also need FFmpeg <https://www.ffmpeg.org/download.html> or avconv <https://libav.org/download/> in %PATH%.

## Usage

### `rbtv-mediathek.pyz download --help`
```
usage: rbtv-mediathek.pyz download [-h]
                                   (--episode-id EPISODE_ID | --season-id SEASON_ID | --show-id SHOW_ID | --show-name SHOW_NAME | --all-shows | --bohne-id BOHNE_ID | --bohne-name BOHNE_NAME)
                                   [--unsorted-only] [--basepath BASEPATH]
                                   [--outdirtpl OUTDIRTPL] [--outtmpl OUTTMPL]
                                   [--format FORMAT]
                                   [--missing-value MISSING_VALUE]
                                   [--record-path RECORD_PATH]

optional arguments:
  -h, --help            show this help message and exit
  --episode-id EPISODE_ID
                        Download this episode (default: None)
  --season-id SEASON_ID
                        Download all episodes of this season (default: None)
  --show-id SHOW_ID     Download all episodes of this show (default: None)
  --show-name SHOW_NAME
                        Download all episodes of this show (default: None)
  --all-shows           Download all episodes of all shows (default: False)
  --bohne-id BOHNE_ID   Download all episodes by Bohne (default: None)
  --bohne-name BOHNE_NAME
                        Download all episodes by Bohne (default: None)
  --unsorted-only       Only valid in combination with --show-id, --show-name
                        or --all-shows. Downloads only unsorted episodes
                        (episodes which are not categorized into seasons).
                        (default: False)
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
  --missing-value MISSING_VALUE
                        Value used for --outdirtpl if field is not available.
                        (default: -)
  --record-path RECORD_PATH
                        File path where successful downloads are recorded.
                        These episodes will be skipped if downloaded again.
                        (default: None)
```

#### Examples

- `rbtv-mediathek.pyz download --show-id 99 --basepath "C:/Download/RBTV" --format "22/best"`

This will download all episodes for the show "After Dark" in 720p mp4 format (or next best if not available) to `C:/Download/RBTV` with subfolders per show and season (eg. `C:\Download\RBTV\After Dark\Resident Evil Code - Veronica X`).

- `rbtv-mediathek.pyz download --show 112 --unsorted --outdirtpl ""`

This will download only unsorted episodes for the show "Kino+" to the current directory without subfolders.

- `rbtv-mediathek.pyz download --bohne-name "Simon"`

Download all episodes with Simon.

### `rbtv-mediathek.pyz browse --help`
```
usage: rbtv-mediathek.pyz browse [-h]
                                 (--episode-id EPISODE_ID | --season-id SEASON_ID | --show-id SHOW_ID | --show-name SHOW_NAME | --all-shows | --bohne-id BOHNE_ID | --bohne-name BOHNE_NAME | --all-bohnen)
                                 [--preview]

optional arguments:
  -h, --help            show this help message and exit
  --episode-id EPISODE_ID
                        Show episode info (default: None)
  --season-id SEASON_ID
                        Show season info (default: None)
  --show-id SHOW_ID     Show show info (default: None)
  --show-name SHOW_NAME
                        Show show info (default: None)
  --all-shows           Show a list of all shows (default: False)
  --bohne-id BOHNE_ID   Show bohne info (default: None)
  --bohne-name BOHNE_NAME
                        Show bohne info (default: None)
  --all-bohnen          Show a list of all Bohnen (default: False)
  --preview             Don't output full information. Much faster. (default:
                        False)
```

## Missing features (which I intend to implement in the future)

- Does not retry failed downloads
- Download episodes by season name instead of ID
- information about current events

## Support RBTV

<https://rocketbeans.tv/supporte-uns>
