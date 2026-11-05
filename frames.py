"""Cloud-only video sampling at 1 fps. 2026-11-05 (Asia/Tokyo, UTC+09:00)."""
import base64
import json
import math
import re
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager, closing
from pathlib import Path

WIDTH, HEIGHT, COLUMNS, ROWS = 960, 540, 3, 3
MAX_SECONDS, MAX_BYTES, MAX_SHEETS_BYTES = 7200, 768 * 1024 * 1024, 256 * 1024 * 1024
FRAME_SLOT = threading.BoundedSemaphore(1)


def validate_request(value, validate_url, single=False):
    sources = value.get('sources')
    duration = value.get('duration_seconds')
    if not isinstance(sources, list) or not 1 <= len(sources) <= 100 or not isinstance(duration, (int, float)) or isinstance(duration, bool) or not math.isfinite(duration) or not 0 < duration <= MAX_SECONDS:
        raise ValueError('invalid_frame_request')
    for source in sources:
        if not isinstance(source, str):
            raise ValueError('invalid_frame_source')
        u = validate_url(source)
        if not u.hostname.endswith(('.bilivideo.com', '.bilivideo.cn', '.biliapi.net')):
            raise ValueError('frames_require_video_cdn')
    if single:
        second = value.get('second')
        if not isinstance(second, int) or isinstance(second, bool) or not 0 <= second < duration:
            raise ValueError('invalid_frame_second')


def frame_filter(duration=None):
    return ('fps=1:start_time=0:round=up:eof_action=pass,'
            + (f'trim=end_frame={math.ceil(duration)},' if duration is not None else '') +
            f'scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,'
            f'pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,'
            "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf:"
            "text='%{pts\\:hms}':x=18:y=h-th-18:fontsize=30:fontcolor=white:"
            'box=1:boxcolor=black@0.75:boxborderw=8,showinfo,'
            f'tile={COLUMNS}x{ROWS}')


def prepare_video(value, session, root, progress):
    total, last_progress = 0, 0
    for index, url in enumerate(value['sources']):
        progress({'type': 'progress', 'stage': 'downloading', 'segment': index + 1})
        with closing(session.get(url, headers={'Referer': 'https://www.bilibili.com/', 'Accept-Encoding': 'identity'}, allow_redirects=False, timeout=180, stream=True)) as response:
            if response.status_code != 200 or total + int(response.headers.get('content-length', '0')) > MAX_BYTES:
                raise ValueError('video_source_unavailable_or_too_large')
            with (root / f'part-{index}.mp4').open('wb') as output:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise ValueError('video_exceeds_frame_budget')
                    output.write(chunk)
                    if time.monotonic() - last_progress > 5:
                        progress({'type': 'progress', 'stage': 'downloading', 'bytes': total})
                        last_progress = time.monotonic()
    if len(value['sources']) == 1:
        return ['-i', str(root / 'part-0.mp4')]
    (root / 'parts.txt').write_text(''.join(f"file 'part-{i}.mp4'\n" for i in range(len(value['sources']))))
    return ['-f', 'concat', '-safe', '1', '-i', str(root / 'parts.txt')]


class SourceResolutionError(ValueError):
    def __init__(self, width, height):
        self.width, self.height = width, height
        super().__init__('source_resolution_below_1080p')


def extract_frame(value, session):
    with tempfile.TemporaryDirectory(prefix='dot-bili-original-') as directory:
        root = Path(directory)
        input_args = prepare_video(value, session, root, lambda _value: None)
        probe = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'json', str(root / 'part-0.mp4')], check=True, capture_output=True, timeout=30)
        source = json.loads(probe.stdout)['streams'][0]
        width, height = int(source['width']), int(source['height'])
        if width <= 0 or height <= 0 or min(1920 / width, 1080 / height) > 1.0001:
            raise SourceResolutionError(width, height)
        output = root / 'frame.png'
        subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', '1', '-filter_threads', '1', '-protocol_whitelist', 'file,pipe', '-ss', str(value['second']), *input_args, '-map', '0:v:0', '-frames:v', '1', '-an', '-sn', '-dn', '-vf', 'scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1', '-c:v', 'png', '-pix_fmt', 'rgb24', '-threads', '1', '-compression_level', '3', str(output)], check=True, capture_output=True, timeout=120)
        data = output.read_bytes()
        if not 24 < len(data) <= 8 * 1024 * 1024 or data[:8] != b'\x89PNG\r\n\x1a\n':
            raise ValueError('invalid_full_hd_frame')
        return {'type': 'frame', 'second': value['second'], 'width': 1920, 'height': 1080, 'source_width': width, 'source_height': height, 'upscaled': False, 'mime_type': 'image/png', 'timestamp_rule': 'accurate_seek_at_requested_second', 'data': base64.b64encode(data).decode('ascii')}


@contextmanager
def extract_sheets(value, session, progress):
    # This directory exists only on the cloud service and is removed after streaming.
    with tempfile.TemporaryDirectory(prefix='dot-bili-frames-') as directory:
        root = Path(directory)
        input_args = prepare_video(value, session, root, progress)
        last_progress = 0
        command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'info', '-threads', '1', '-filter_threads', '1', '-protocol_whitelist', 'file,pipe', *input_args,
                   '-t', str(min(MAX_SECONDS, value['duration_seconds'] + 1)), '-map', '0:v:0', '-an', '-sn', '-dn', '-vf', frame_filter(),
                   '-fps_mode', 'passthrough', '-c:v', 'mjpeg', '-q:v', '2', '-threads', '1', str(root / 'sheet-%04d.jpg')]
        count = [0]
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors='replace')
        def consume_log():
            for line in process.stderr:
                match = re.search(r'Parsed_showinfo.*\bn:\s*(\d+)', line)
                if match:
                    count[0] = max(count[0], int(match[1]) + 1)
        reader = threading.Thread(target=consume_log, daemon=True)
        reader.start()
        started = time.monotonic()
        try:
            while process.poll() is None:
                if time.monotonic() - started > 600:
                    raise TimeoutError('cloud_frame_extraction_timeout')
                if time.monotonic() - last_progress > 5:
                    progress({'type': 'progress', 'stage': 'extracting', 'frames': count[0]})
                    last_progress = time.monotonic()
                time.sleep(0.5)
            reader.join(timeout=2)
            sheets = sorted(root.glob('sheet-*.jpg'))
            if process.returncode != 0 or not 0 < count[0] <= MAX_SECONDS or len(sheets) != math.ceil(count[0] / (COLUMNS * ROWS)):
                raise ValueError('cloud_frame_extraction_failed')
            if sum(p.stat().st_size for p in sheets) > MAX_SHEETS_BYTES or any(p.stat().st_size > 6 * 1024 * 1024 for p in sheets):
                raise ValueError('frame_images_exceed_budget')
            yield {'type': 'manifest', 'interval_seconds': 1, 'frame_count': count[0], 'sheet_count': len(sheets), 'tile_width': WIDTH, 'tile_height': HEIGHT, 'columns': COLUMNS, 'rows': ROWS}, sheets
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            process.stderr.close()


def encoded_sheet(index, path):
    return {'type': 'sheet', 'index': index, 'data': base64.b64encode(path.read_bytes()).decode('ascii')}


def stream_sheets(value, session, send):
    """Publish each complete JPEG before the remaining video finishes decoding."""
    expected = math.ceil(value['duration_seconds'])
    manifest = {'type': 'manifest', 'interval_seconds': 1, 'frame_count': expected,
                'sheet_count': math.ceil(expected / 9), 'tile_width': WIDTH,
                'tile_height': HEIGHT, 'columns': COLUMNS, 'rows': ROWS,
                'progressive': True, 'timestamps_burned_in': True}
    with tempfile.TemporaryDirectory(prefix='dot-bili-progressive-') as directory:
        root = Path(directory)
        input_args = prepare_video(value, session, root, send)
        send(manifest)
        command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'info', '-threads', '1',
                   '-filter_threads', '1', '-protocol_whitelist', 'file,pipe', *input_args,
                   '-map', '0:v:0', '-an', '-sn', '-dn', '-vf', frame_filter(value['duration_seconds']),
                   '-fps_mode', 'passthrough', '-c:v', 'mjpeg', '-q:v', '2', '-threads', '1',
                   '-atomic_writing', '1', str(root / 'sheet-%04d.jpg')]
        count = [0]
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors='replace')
        def consume_log():
            for line in process.stderr:
                match = re.search(r'Parsed_showinfo.*\bn:\s*(\d+)', line)
                if match:
                    count[0] = max(count[0], int(match[1]) + 1)
        reader = threading.Thread(target=consume_log, daemon=True)
        reader.start()
        started, last_progress, index, total = time.monotonic(), 0, 0, 0
        def flush_sheets():
            nonlocal index, total
            # image2 atomic_writing renames only fully closed JPEGs.
            while (path := root / f'sheet-{index + 1:04d}.jpg').is_file():
                size = path.stat().st_size
                total += size
                if not 0 < size <= 6 * 1024 * 1024 or total > MAX_SHEETS_BYTES or index >= manifest['sheet_count']:
                    raise ValueError('frame_images_exceed_budget')
                send(encoded_sheet(index, path))
                index += 1
        try:
            while process.poll() is None:
                flush_sheets()
                if time.monotonic() - started > 600:
                    raise TimeoutError('cloud_frame_extraction_timeout')
                if time.monotonic() - last_progress > 5:
                    send({'type': 'progress', 'stage': 'extracting', 'frames': count[0]})
                    last_progress = time.monotonic()
                time.sleep(0.1)
            reader.join(timeout=2)
            flush_sheets()
            if process.returncode != 0 or not max(1, expected - 2) <= count[0] <= expected or index != math.ceil(count[0] / 9):
                raise ValueError('cloud_frame_extraction_failed')
            send({**manifest, 'type': 'coverage', 'frame_count': count[0], 'sheet_count': index})
            send({'type': 'done'})
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            process.stderr.close()
