"""Cloud-only video sampling at 1 fps. 2026-11-05 (Asia/Tokyo, UTC+09:00)."""
import base64
import math
import re
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager, closing
from pathlib import Path

WIDTH, HEIGHT, COLUMNS, ROWS = 640, 360, 5, 5
MAX_SECONDS, MAX_BYTES, MAX_SHEETS_BYTES = 7200, 768 * 1024 * 1024, 256 * 1024 * 1024
FRAME_SLOT = threading.BoundedSemaphore(1)


def validate_request(value, validate_url):
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


def frame_filter():
    return ('fps=1:start_time=0:round=up:eof_action=pass,'
            f'scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,'
            f'pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,showinfo,'
            f'tile={COLUMNS}x{ROWS}')


@contextmanager
def extract_sheets(value, session, progress):
    # This directory exists only on the cloud service and is removed after streaming.
    with tempfile.TemporaryDirectory(prefix='dot-bili-frames-') as directory:
        root = Path(directory)
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
            input_args = ['-i', str(root / 'part-0.mp4')]
        else:
            (root / 'parts.txt').write_text(''.join(f"file 'part-{i}.mp4'\n" for i in range(len(value['sources']))))
            input_args = ['-f', 'concat', '-safe', '1', '-i', str(root / 'parts.txt')]
        command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'info', '-threads', '1', '-filter_threads', '1', '-protocol_whitelist', 'file,pipe', *input_args,
                   '-t', str(min(MAX_SECONDS, value['duration_seconds'] + 1)), '-map', '0:v:0', '-an', '-sn', '-dn', '-vf', frame_filter(),
                   '-fps_mode', 'passthrough', '-c:v', 'mjpeg', '-q:v', '3', '-threads', '1', str(root / 'sheet-%04d.jpg')]
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
