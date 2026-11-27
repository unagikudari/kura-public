"""Offline browser-compatible viewing copies. Original bytes are never rewritten."""
import json
import math
import subprocess
from pathlib import Path

RECIPE = 'h264-420-tv-crf18-aac192-faststart-v1'


def probe(path: Path) -> dict:
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format',
                             '-of', 'json', str(path)], capture_output=True, text=True,
                            check=True, timeout=30)
    return json.loads(result.stdout)


class DurationUnavailable(ValueError):
    """No trustworthy duration: retain the original and report a skipped view."""


def duration(info: dict) -> float:
    def positive(value):
        try:
            value = float(value)
            return value if math.isfinite(value) and value > 0 else None
        except (TypeError, ValueError):
            return None
    container = positive(info.get('format', {}).get('duration'))
    if container is not None:
        return container
    streams = [value for stream in info.get('streams', [])
               if (value := positive(stream.get('duration'))) is not None]
    if streams:
        return max(streams)
    raise DurationUnavailable('neither container nor stream has a finite positive duration')


def probe_mp4(path: Path) -> tuple[int, int]:
    info = probe(path)
    streams = info['streams']
    video = [s for s in streams if s['codec_type'] == 'video']
    if ('mp4' not in info['format']['format_name'].split(',') or len(video) != 1
            or video[0]['codec_name'] != 'h264' or video[0]['pix_fmt'] != 'yuv420p'
            or any(s['codec_name'] != 'aac' for s in streams if s['codec_type'] == 'audio')):
        raise ValueError('view must be H.264 yuv420p / AAC MP4')
    return video[0]['width'], video[0]['height']


def encode(source: Path, output: Path) -> None:
    """Bound the encoder; pad odd dimensions by one pixel, never stretch or resize."""
    source_duration = duration(probe(source))
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-n', '-i', str(source),
                    '-map', '0:v:0', '-map', '0:a:0?', '-map_metadata', '-1',
                    '-c:v', 'libx264', '-threads', '2', '-preset', 'medium', '-crf', '18',
                    '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2,scale=in_range=auto:out_range=tv',
                    '-pix_fmt', 'yuv420p', '-color_range', 'tv',
                    '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart',
                    str(output)], capture_output=True, check=True, timeout=1800)
    probe_mp4(output)
    if abs(source_duration - duration(probe(output))) > 0.25:
        raise ValueError('view duration differs from original')
