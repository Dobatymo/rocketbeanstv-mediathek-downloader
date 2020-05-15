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
                            (--episode-id ID [ID ...] | --season-id ID [ID ...] | --show-id ID [ID ...] | --show-name NAME [NAME ...] | --all-shows | --bohne-id ID [ID ...] | --bohne-name NAME [NAME ...] | --blog-id ID [ID ...] | --all-blog)
                            [--unsorted-only] [--basepath PATH]
                            [--outdirtpl OUTDIRTPL] [--outtmpl OUTTMPL]
                            [--format FORMAT] [--missing-value MISSING_VALUE]
                            [--record-path PATH] [--retries N]

optional arguments:
  -h, --help            show this help message and exit
  --episode-id ID [ID ...]
                        Download this episode (default: None)
  --season-id ID [ID ...]
                        Download all episodes of this season (default: None)
  --show-id ID [ID ...]
                        Download all episodes of this show (default: None)
  --show-name NAME [NAME ...]
                        Download all episodes of this show (default: None)
  --all-shows           Download all episodes of all shows (default: False)
  --bohne-id ID [ID ...]
                        Download all episodes by Bohne (default: None)
  --bohne-name NAME [NAME ...]
                        Download all episodes by Bohne (default: None)
  --blog-id ID [ID ...]
                        Download blog post (default: None)
  --all-blog            Download all blog posts (default: False)
  --unsorted-only       Only valid in combination with --show-id, --show-name
                        or --all-shows. Downloads only unsorted episodes
                        (episodes which are not categorized into seasons).
                        (default: False)
  --basepath PATH       Base output folder (default: .)
  --outdirtpl OUTDIRTPL
                        Output folder relative to base folder. Can include the
                        following placeholders: show_id, show_name, season_id,
                        season_name, season_number, episode_id, episode_name,
                        episode_number, year, month, day, hour, minute,
                        second, duration (default: {show_name}/{season_name})
  --outtmpl OUTTMPL     Output file path relative to output folder. Can
                        include the same placeholders as '--outdirtpl' as well
                        as youtube-dl placeholders. See youtube-dl output
                        template: https://github.com/ytdl-org/youtube-
                        dl#output-template (default: %(title)s-%(id)s.%(ext)s)
  --format FORMAT       Video/audio format. Defaults to 'bestvideo+bestaudio'
                        with fallback to 'best'. See youtube-dl format
                        selection: https://github.com/ytdl-org/youtube-
                        dl#format-selection (default: None)
  --missing-value MISSING_VALUE
                        Value used for --outdirtpl if field is not available.
                        (default: -)
  --record-path PATH    File path where successful downloads are recorded.
                        These episodes will be skipped if downloaded again.
                        (default: None)
  --retries N           Retry failed downloads N times. (default: 10)
```

#### Examples

- `rbtv-mediathek.pyz download --show-id 99 --basepath "C:/Download/RBTV" --format "22/best"`

This will download all episodes for the show "After Dark" in 720p mp4 format (or next best if not available) to `C:/Download/RBTV` with subfolders per show and season (eg. `C:\Download\RBTV\After Dark\Resident Evil Code - Veronica X`).

- `rbtv-mediathek.pyz download --show-id 112 --unsorted --outdirtpl ""`

This will download only unsorted episodes for the show "Kino+" to the current directory without subfolders.

- `rbtv-mediathek.pyz download --bohne-name "Simon"`

Download all episodes with Simon.

- `rbtv-mediathek.pyz download --bohne-name "Gregor" --outdirtpl "{year}" --outtmpl "{episode_id}-{episode_part}.%(ext)s"`

Download all episodes with Gregor, sort them by release year and use the episode id and part for the filename.

### `rbtv-mediathek.pyz browse --help`
```
usage: rbtv-mediathek.pyz browse [-h]
                          (--episode-id ID | --season-id ID | --show-id ID | --show-name NAME | --all-shows | --bohne-id ID | --bohne-name NAME | --all-bohnen | --blog-id ID | --all-blog | --search SEARCH)
                          [--limit N]

optional arguments:
  -h, --help         show this help message and exit
  --episode-id ID    Show episode info (default: None)
  --season-id ID     Show season info (default: None)
  --show-id ID       Show show info (default: None)
  --show-name NAME   Show show info (default: None)
  --all-shows        Show a list of all shows (default: False)
  --bohne-id ID      Show bohne info (default: None)
  --bohne-name NAME  Show bohne info (default: None)
  --all-bohnen       Show a list of all Bohnen (default: False)
  --blog-id ID       Show blog post info (default: None)
  --all-blog         Show all blog posts (default: False)
  --search SEARCH    Search shows and episodes (default: None)
  --limit N          Limit list output to N items (default: None)
```

#### Examples

- `rbtv-mediathek.pyz browse --search "resident evil"`

Find shows, episodes and blog posts related to resident evil.

- `rbtv-mediathek.pyz browse --show-name "After Dark"`

Print information about the show After Dark, including some information about all seasons.

## Missing features (which I intend to implement in the future)

- Does not retry failed downloads
- Some Bohnen cannot be distinguished by name (eg. there are two "Fabian"s)
- Download episodes by season name instead of ID
- Information about current events

## Support RBTV

<https://rocketbeans.tv/supporte-uns>
