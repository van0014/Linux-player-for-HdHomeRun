# HDHomeRun Viewer

A small Linux desktop app for HDHomeRun tuners. SiliconDust's own viewer apps
don't cover Linux, so this fills the gap with a channel list, a now/next guide,
and mpv for playback.

- Channel list with **what's on now and next**, in plain terms ("30 mins left", "in 45 mins")
- **Change channel without restarting the player**: double-click or press Enter in the list,
  or press Up/Down (also PgUp/PgDn) inside the mpv window
- On-screen display of channel name, now and next whenever the channel changes
- Frees the tuner when you stop or close; works with any number of tuners
- Guide cached for 24 hours; the guide is optional and the app works without it
- Pure Python standard library plus `mpv`: no pip packages

## Requirements

```
sudo apt install mpv python3-tk
```

An HDHomeRun on the same network (tested on a Connect Duo, HDHR5-2DT, on Debian with PipeWire).

## Install

```
git clone <this repo>
cd hdhr-viewer
./install.sh
```

This installs `~/.local/bin/hdhr-viewer` and an application-menu entry, so you can
launch it without a terminal. Or just run `./hdhr_viewer.py`.

## Configuration

`~/.config/hdhr-viewer/config.json` is created on first run:

```json
{
  "host": "hdhomerun.local",
  "xmltv_source": "",
  "mpv_args": []
}
```

- `host`: tuner hostname or IP. Use the IP if mDNS (`.local`) is slow or unreliable.
- `xmltv_source`: URL or file path of an XMLTV guide. Blank uses HDHomeRun's own guide.
- `mpv_args`: extra mpv flags, applied last so they override the defaults. Example for AMD/Intel
  GPUs: `["--hwdec=vaapi"]`.

## Guide data

With `xmltv_source` blank, the app reads the tuner's `DeviceAuth` and requests SiliconDust's
XMLTV guide. Their documentation says a paid DVR guide subscription is required. In practice
it returned data for the author's Australian Connect Duo without one, but that may differ by
device and region and may change. If the guide is unavailable, the channel list still works,
and you can point `xmltv_source` at any XMLTV feed instead. Channels are matched by name,
then by number.

Please don't hammer the guide endpoint: the cache is there to keep it to about one fetch a day.

## Files

| Path | Purpose |
|---|---|
| `~/.config/hdhr-viewer/config.json` | settings |
| `~/.cache/hdhr-viewer/guide.xml` | cached guide |
| `~/.cache/hdhr-viewer/mpv.log` | mpv output, for troubleshooting |

## Troubleshooting

- **No window / nothing plays**: check `~/.cache/hdhr-viewer/mpv.log`.
- **"Tuner busy" or a channel won't open**: all tuners are in use by another client.
- **Stutter**: first check the tuner's `http://<host>/status.json`. If signal and symbol quality are
  100%, try `"--hwdec=vaapi"` or `"--hwdec=no"` in `mpv_args`.
- **Guide channels unmatched**: the status bar shows how many matched. Compare your channel
  names with the `<display-name>` entries in the cached guide.
- Channel switching with Up/Down doesn't wrap at the ends of the list.

## Licence

MIT
