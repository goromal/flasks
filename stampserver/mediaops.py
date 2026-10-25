import math
import os
import shutil
import subprocess
import tempfile

import cv2
from PIL import Image

from fileops import IdentityConflictError, build_stamped, identity_index, identity_key, parse_stamped, unique_suffixed_name
from imageops import fill_white_rect, image_format, is_image, pad_image, save_image


def _rectangle(operation, width, height):
    x = int(operation['x'])
    y = int(operation['y'])
    rect_width = int(operation['width'])
    rect_height = int(operation['height'])
    if x < 0 or y < 0 or rect_width <= 0 or rect_height <= 0 or x + rect_width > width or y + rect_height > height:
        raise ValueError('Edit rectangle is outside the media bounds')
    return x, y, rect_width, rect_height


def _render_image(source, output, filename, operations):
    with Image.open(source) as opened:
        image = opened.copy()
    for operation in operations:
        kind = operation['type']
        if kind == 'rotate':
            degrees = int(operation['degrees'])
            if degrees not in (90, 180, 270):
                raise ValueError('Unsupported image rotation')
            image = image.rotate(-degrees, expand=True)
        elif kind in ('crop', 'whiteout'):
            x, y, width, height = _rectangle(operation, *image.size)
            if kind == 'crop':
                image = image.crop((x, y, x + width, y + height))
            else:
                image = fill_white_rect(image, x, y, width, height)
        elif kind == 'pad':
            sides = [int(operation[side]) for side in ('top', 'bottom', 'left', 'right')]
            if min(sides) < 0 or not any(sides):
                raise ValueError('Padding must be non-negative and nonempty')
            image = pad_image(image, *sides)
        else:
            raise ValueError(f'Unsupported image edit: {kind}')
    save_image(image, output, image_format(filename))


def _segments(points, duration):
    if not isinstance(points, list) or not points:
        raise ValueError('No edit points provided')
    sorted_points = sorted(points, key=lambda point: float(point['time']))
    for point in sorted_points:
        time = float(point['time'])
        if point['type'] not in ('start', 'end') or not math.isfinite(time) or time < 0 or time > duration:
            raise ValueError('Invalid edit point')
    if sorted_points[0]['type'] == 'end':
        sorted_points.insert(0, {'type': 'start', 'time': 0})
    if sorted_points[-1]['type'] == 'start':
        sorted_points.append({'type': 'end', 'time': duration})
    if len(sorted_points) % 2:
        raise ValueError('Edit points must alternate between start and end')
    segments = []
    for start, end in zip(sorted_points[::2], sorted_points[1::2]):
        start_time, end_time = float(start['time']), float(end['time'])
        if start['type'] != 'start' or end['type'] != 'end' or end_time <= start_time:
            raise ValueError('Edit points must alternate between start and end')
        segments.append((start_time, end_time))
    return segments


def _render_video(source, output, operations):
    capture = cv2.VideoCapture(source)
    if not capture.isOpened():
        raise ValueError('Could not open video file')
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = capture.get(cv2.CAP_PROP_FPS)
    frames = capture.get(cv2.CAP_PROP_FRAME_COUNT)
    capture.release()
    duration = frames / fps if fps > 0 else 0
    filters = []
    points = None
    for operation in operations:
        kind = operation['type']
        if kind == 'crop':
            x, y, crop_width, crop_height = _rectangle(operation, width, height)
            filters.append(f'crop={crop_width}:{crop_height}:{x}:{y}')
            width, height = crop_width, crop_height
        elif kind == 'rotate':
            degrees = int(operation['degrees'])
            rotation = {90: 'transpose=1', 180: 'vflip,hflip', 270: 'transpose=2'}
            if degrees not in rotation:
                raise ValueError('Unsupported video rotation')
            filters.append(rotation[degrees])
            if degrees in (90, 270):
                width, height = height, width
        elif kind == 'trim':
            if points is not None:
                raise ValueError('Only one set of edit points is supported')
            points = operation['points']
        else:
            raise ValueError(f'Unsupported video edit: {kind}')
    command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', source]
    if points is None:
        command += ['-vf', ','.join(filters), '-c:a', 'copy']
    else:
        segments = _segments(points, duration)
        probe = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-select_streams', 'a', '-show_entries', 'stream=codec_type',
             '-of', 'default=noprint_wrappers=1', source], capture_output=True, text=True
        )
        has_audio = 'codec_type=audio' in probe.stdout
        parts = []
        for index, (start, end) in enumerate(segments):
            video_filters = filters + [f'trim=start={start}:end={end}', 'setpts=PTS-STARTPTS']
            parts.append(f"[0:v]{','.join(video_filters)}[v{index}]")
            if has_audio:
                parts.append(f'[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{index}]')
        streams = ''.join(f'[v{index}]' + (f'[a{index}]' if has_audio else '') for index in range(len(segments)))
        parts.append(f'{streams}concat=n={len(segments)}:v=1:a={int(has_audio)}[vout]' + ('[aout]' if has_audio else ''))
        command += ['-filter_complex', ';'.join(parts), '-map', '[vout]']
        if has_audio:
            command += ['-map', '[aout]']
    if output.lower().endswith(('.mp4', '.mov')):
        command += ['-movflags', '+faststart']
    result = subprocess.run(command + [output, '-y'], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f'ffmpeg error: {result.stderr[-500:]}')


def edit_and_stamp(directory, filename, new_path, operations, copy):
    source = os.path.join(directory, filename)
    if not os.path.isfile(source):
        raise FileNotFoundError(filename)
    _, base = parse_stamped(filename)
    target_name = build_stamped(new_path, base)
    identities = identity_index(os.listdir(directory))
    if copy:
        if operations or target_name == filename or os.path.lexists(os.path.join(directory, target_name)) or identity_key(target_name) in identities:
            target_name = unique_suffixed_name(directory, target_name, '_edited' if operations else '_copy')
    elif target_name != filename:
        if os.path.lexists(os.path.join(directory, target_name)):
            raise FileExistsError(target_name)
        holders = identities.get(identity_key(target_name), set()) - {filename}
        if holders:
            raise IdentityConflictError(target_name, sorted(holders)[0])
    target = os.path.join(directory, target_name)
    if not operations:
        if target_name != filename:
            if copy:
                shutil.copy2(source, target)
            else:
                os.rename(source, target)
        return target_name
    if not isinstance(operations, list):
        raise ValueError('Invalid edit list')
    fd, temporary = tempfile.mkstemp(dir=directory, suffix=os.path.splitext(filename)[1])
    os.close(fd)
    try:
        if is_image(filename):
            _render_image(source, temporary, filename, operations)
        elif filename.lower().endswith(('.mp4', '.mov')):
            _render_video(source, temporary, operations)
        else:
            raise ValueError('This file cannot be edited')
        os.replace(temporary, target)
        if not copy and target_name != filename:
            os.remove(source)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    return target_name
