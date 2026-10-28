"""yt-dlp Studio: giao diện web chạy cục bộ cho yt-dlp.

Chạy: python gui/server.py  (mở http://127.0.0.1:8765 trong trình duyệt)
"""
import hashlib
import hmac
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if (getattr(sys, 'frozen', False) and '--after-update' in sys.argv
        and os.environ.get('YTDLP_STUDIO_CLEAN') != '1'):
    # Bản cũ (<= 1.2.0) khởi động lại mà không reset môi trường PyInstaller, nên tiến trình này
    # đang dùng chung thư mục tạm của bản cũ (sẽ bị xoá khi bản cũ thoát). Chạy lại một bản sạch.
    # YTDLP_STUDIO_CLEAN đánh dấu bản đã sạch (PyInstaller tự xoá biến PYINSTALLER_RESET_ENVIRONMENT).
    subprocess.Popen([sys.executable, *sys.argv[1:]], close_fds=True,
                     env=dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1', YTDLP_STUDIO_CLEAN='1'),
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    sys.exit(0)

if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys._MEIPASS) / 'gui'
else:
    BASE_DIR = Path(__file__).resolve().parent
    sys.path.insert(0, str(BASE_DIR.parent))
    sys.path.insert(0, str(BASE_DIR))

import yt_dlp  # noqa: E402
from yt_dlp.utils import DownloadCancelled  # noqa: E402
from yt_dlp.version import __version__ as YTDLP_VERSION  # noqa: E402

import manager  # noqa: E402
from studio_version import STUDIO_VERSION, UPDATE_URL  # noqa: E402

# Máy nào có ổ D thì lưu ở D, không thì vào thư mục Downloads của người dùng
DEFAULT_FOLDER = r'D:\Downloads\yt-dlp' if os.path.isdir('D:\\') else str(Path.home() / 'Downloads' / 'yt-dlp')
PORT = int(os.environ.get('YTDLP_STUDIO_PORT') or 8765)  # đổi cổng chỉ để chạy thử
HAS_FFMPEG = shutil.which('ffmpeg') is not None
JS_RUNTIMES = {name: {} for name in ('deno', 'node', 'bun') if shutil.which(name)} or {'deno': {}}
QUALITIES = {'best', '2160', '1440', '1080', '720', '480', '360'}
AUDIO_FORMATS = {'mp3', 'm4a', 'best'}

# ---------------------------------------------------------------- Cấu hình truy cập từ xa
# studio-config.json nằm cạnh file exe. Có "tunnel" thì app chạy nền liên tục và tự bật cloudflared;
# mọi truy cập qua tên miền đều phải đăng nhập bằng mật khẩu.
CONFIG_PATH = (Path(sys.executable).parent if getattr(sys, 'frozen', False) else BASE_DIR.parent) / 'studio-config.json'
THUMB_DIR = CONFIG_PATH.parent / 'cache' / 'thumbs'


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding='utf-8')


def hash_password(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 200_000).hex()


def set_password(password):
    cfg = load_config()
    cfg['salt'] = os.urandom(16).hex()
    cfg['password_hash'] = hash_password(password, cfg['salt'])
    cfg['secret'] = os.urandom(32).hex()  # đổi mật khẩu = đăng xuất mọi thiết bị
    save_config(cfg)


config = load_config()
REMOTE_HOST = (config.get('remote_hostname') or '').lower()
AUTH_COOKIE = 'studio_auth'
# Những thao tác chỉ làm được ngay trên máy tính, không làm được qua web
LOCAL_ONLY = {'/api/open', '/api/library/reveal', '/api/settings', '/api/settings/password',
              '/api/settings/autostart', '/api/quit', '/api/update/restart'}
login_failures = []


def auth_token():
    return hmac.new(bytes.fromhex(config['secret']), b'studio-auth', 'sha256').hexdigest()


jobs = {}
jobs_lock = threading.Lock()
# Số cửa sổ giao diện đang mở (đếm qua kết nối /api/alive)
clients = {'count': 0, 'seen': False}


class QuietLogger:
    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


def pick_thumbnail(info):
    if info.get('thumbnail'):
        return info['thumbnail']
    thumbs = [t for t in info.get('thumbnails') or [] if t.get('url')]
    return thumbs[-1]['url'] if thumbs else None


def summarize_info(info):
    if info.get('_type') == 'playlist' or 'entries' in info:
        entries = []
        for e in info.get('entries') or []:
            if not e:
                continue
            entries.append({
                'title': e.get('title') or e.get('id'),
                'duration': e.get('duration'),
                'thumbnail': pick_thumbnail(e),
            })
        return {
            'kind': 'playlist',
            'title': info.get('title') or 'Playlist',
            'uploader': info.get('uploader') or info.get('channel'),
            'thumbnail': pick_thumbnail(info) or next((e['thumbnail'] for e in entries if e['thumbnail']), None),
            'count': info.get('playlist_count') or len(entries),
            'entries': entries[:200],
            'extractor': info.get('extractor_key'),
        }

    heights = set()
    audio_size = 0
    sizes = {}
    for f in info.get('formats') or []:
        size = f.get('filesize') or f.get('filesize_approx') or 0
        if f.get('vcodec') not in (None, 'none') and f.get('height'):
            heights.add(f['height'])
            sizes[f['height']] = max(sizes.get(f['height'], 0), size)
        elif f.get('acodec') not in (None, 'none') and f.get('vcodec') in (None, 'none'):
            audio_size = max(audio_size, size)
    return {
        'kind': 'video',
        'title': info.get('title'),
        'uploader': info.get('uploader') or info.get('channel'),
        'thumbnail': pick_thumbnail(info),
        'duration': info.get('duration'),
        'view_count': info.get('view_count'),
        'upload_date': info.get('upload_date'),
        'extractor': info.get('extractor_key'),
        'is_live': info.get('is_live'),
        'heights': sorted(heights, reverse=True),
        'sizes': {str(h): s for h, s in sizes.items()},
        'audio_size': audio_size,
        'subtitles': sorted((info.get('subtitles') or {}).keys())[:40],
    }


def extract(url):
    opts = {
        'quiet': True,
        'no_warnings': True,
        'logger': QuietLogger(),
        'extract_flat': 'in_playlist',
        'js_runtimes': JS_RUNTIMES,
        'skip_download': True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return summarize_info(ydl.sanitize_info(info))


def build_opts(job, options):
    mode = options.get('mode', 'video')
    quality = str(options.get('quality', 'best'))
    if quality not in QUALITIES:
        quality = 'best'
    audio_format = options.get('audio_format', 'mp3')
    if audio_format not in AUDIO_FORMATS:
        audio_format = 'mp3'
    folder = options.get('folder') or download_folder()
    os.makedirs(folder, exist_ok=True)

    postprocessors = []
    opts = {
        'quiet': True,
        'no_warnings': True,
        'logger': QuietLogger(),
        'paths': {'home': folder},
        'outtmpl': '%(title).180B [%(id)s].%(ext)s',
        'noplaylist': not options.get('playlist', True),
        'windowsfilenames': True,
        'progress_hooks': [lambda d: on_progress(job, d)],
        'postprocessor_hooks': [lambda d: on_postprocess(job, d)],
        'concurrent_fragment_downloads': 4,
        'js_runtimes': JS_RUNTIMES,
    }
    if options.get('playlist', True):
        # Video trong playlist được gom vào thư mục mang tên playlist, có số thứ tự
        opts['outtmpl'] = '%(playlist_title&{}/|)s%(playlist_index&{:03d} - |)s%(title).160B [%(id)s].%(ext)s'
        items = (options.get('playlist_items') or '').strip()
        if items:
            opts['playlist_items'] = items

    if mode == 'audio':
        if audio_format == 'm4a':
            opts['format'] = 'ba[ext=m4a]/ba/b'
        else:
            opts['format'] = 'ba/b'
        if HAS_FFMPEG and audio_format in ('mp3', 'm4a'):
            postprocessors.append({
                'key': 'FFmpegExtractAudio',
                'preferredcodec': audio_format,
                'preferredquality': '0' if audio_format == 'mp3' else None,
            })
    else:
        limit = '' if quality == 'best' else f'[height<={quality}]'
        if HAS_FFMPEG:
            opts['format'] = f'bv*{limit}+ba/b{limit}/bv*+ba/b'
            opts['merge_output_format'] = 'mp4/mkv'
            if options.get('compat'):
                # Ưu tiên H.264 + AAC để mở được trên mọi trình phát / điện thoại
                opts['format_sort'] = ['res', 'vcodec:h264', 'acodec:aac']
        else:
            opts['format'] = f'b{limit}/b'

    if options.get('subtitles'):
        opts['writesubtitles'] = True
        opts['subtitleslangs'] = ['vi.*', 'en.*']
        if HAS_FFMPEG and mode != 'audio':
            postprocessors.append({'key': 'FFmpegEmbedSubtitle', 'already_have_subtitle': False})

    if options.get('metadata') and HAS_FFMPEG:
        postprocessors.append({'key': 'FFmpegMetadata', 'add_metadata': True, 'add_chapters': True})
        if options.get('thumbnail'):
            opts['writethumbnail'] = True
            postprocessors.append({'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg', 'when': 'before_dl'})
            postprocessors.append({'key': 'EmbedThumbnail', 'already_have_thumbnail': False})

    browser = options.get('cookies_browser')
    if browser in ('chrome', 'edge', 'firefox', 'brave', 'opera', 'vivaldi'):
        opts['cookiesfrombrowser'] = (browser,)

    opts['postprocessors'] = postprocessors
    return opts, folder


def update_job(job, **fields):
    with jobs_lock:
        job.update(fields)
        job['updated'] = time.time()


def on_progress(job, d):
    if job['cancel']:
        raise DownloadCancelled('Đã huỷ')
    info = d.get('info_dict') or {}
    fields = {
        'current_title': info.get('title') or job.get('current_title'),
        'playlist_index': info.get('playlist_index'),
        'playlist_count': info.get('n_entries') or info.get('playlist_count'),
    }
    if not job.get('thumbnail'):
        fields['thumbnail'] = pick_thumbnail(info)
    if not job.get('title') or job['title'] == job['url']:
        fields['title'] = info.get('playlist_title') or info.get('title') or job['title']
    if d['status'] == 'downloading':
        total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
        done = d.get('downloaded_bytes') or 0
        if total:
            percent = done / total * 100
        elif d.get('fragment_count'):
            percent = (d.get('fragment_index') or 0) / d['fragment_count'] * 100
        else:
            percent = None
        fields.update(status='downloading', percent=percent, speed=d.get('speed'),
                      eta=d.get('eta'), downloaded=done, total=total)
    elif d['status'] == 'finished':
        fields.update(status='processing', percent=100, speed=None, eta=None)
    update_job(job, **fields)


def on_postprocess(job, d):
    if d['status'] == 'started':
        names = {
            'Merger': 'Đang ghép video + âm thanh',
            'FFmpegMerger': 'Đang ghép video + âm thanh',
            'FFmpegExtractAudio': 'Đang chuyển sang âm thanh',
            'FFmpegEmbedSubtitle': 'Đang nhúng phụ đề',
            'FFmpegMetadata': 'Đang ghi thông tin',
            'EmbedThumbnail': 'Đang nhúng ảnh bìa',
        }
        update_job(job, status='processing', stage=names.get(d.get('postprocessor'), 'Đang xử lý'))
    elif d['status'] == 'finished':
        info = d.get('info_dict') or {}
        path = info.get('filepath')
        if path:
            # Mỗi video giữ đường dẫn cuối cùng (sau khi ghép/chuyển đổi) để "Lưu về thiết bị"
            with jobs_lock:
                job.setdefault('_files', {})[info.get('id') or path] = path
            update_job(job, filepath=path)


def download_folder():
    return config.get('download_folder') or DEFAULT_FOLDER


def max_concurrent():
    try:
        return max(1, min(5, int(config.get('max_concurrent') or 2)))
    except (TypeError, ValueError):
        return 2


# Hàng chờ: chỉ cho tối đa max_concurrent() video tải cùng lúc, các video khác chờ lượt
queue_cond = threading.Condition()
running = {'count': 0}


def wait_for_slot(job):
    with queue_cond:
        while running['count'] >= max_concurrent():
            if job['cancel']:
                return False
            queue_cond.wait(1)
        running['count'] += 1
        return True


def release_slot():
    with queue_cond:
        running['count'] -= 1
        queue_cond.notify_all()


def run_job(job, options):
    if not wait_for_slot(job):
        update_job(job, status='cancelled')
        return
    try:
        _run_job(job, options)
    finally:
        release_slot()


def _run_job(job, options):
    try:
        opts, folder = build_opts(job, options)
        update_job(job, folder=folder, status='starting')
        with yt_dlp.YoutubeDL(opts) as ydl:
            code = ydl.download([job['url']])
        if job['cancel']:
            update_job(job, status='cancelled', speed=None, eta=None)
        elif code:
            update_job(job, status='error', error='Một số mục tải không thành công', speed=None, eta=None)
        else:
            update_job(job, status='done', percent=100, speed=None, eta=None, stage=None)
    except DownloadCancelled:
        update_job(job, status='cancelled', speed=None, eta=None)
    except Exception as e:  # noqa: BLE001
        msg = str(e).replace('\x1b[0;31m', '').replace('\x1b[0m', '')
        msg = msg.removeprefix('ERROR: ')
        update_job(job, status='cancelled' if job['cancel'] else 'error', error=msg[:500], speed=None, eta=None)


def start_job(url, options, preview=None):
    job = {
        'id': uuid.uuid4().hex[:10],
        'url': url,
        'title': (preview or {}).get('title') or url,
        'thumbnail': (preview or {}).get('thumbnail'),
        'mode': options.get('mode', 'video'),
        'quality': options.get('quality'),
        'audio_format': options.get('audio_format'),
        'status': 'queued',
        'percent': 0,
        'cancel': False,
        'created': time.time(),
        'updated': time.time(),
        'options': options,
    }
    with jobs_lock:
        jobs[job['id']] = job
    threading.Thread(target=run_job, args=(job, options), daemon=True).start()
    return job


def public_job(job):
    data = {k: v for k, v in job.items() if k != 'cancel' and not k.startswith('_')}
    data['file_names'] = [os.path.basename(p) for p in (job.get('_files') or {}).values()]
    return data


class Handler(BaseHTTPRequestHandler):
    server_version = 'yt-dlp-studio'

    def log_message(self, fmt, *args):
        pass

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get('Content-Length') or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode('utf-8'))

    def host(self):
        return (self.headers.get('Host') or '').split(':')[0].lower()

    def is_remote(self):
        # Đi qua Cloudflare thì luôn có Cf-Ray, kể cả khi Host bị đổi thành localhost
        return (self.host() not in ('127.0.0.1', 'localhost')
                or 'Cf-Ray' in self.headers or 'Cf-Connecting-Ip' in self.headers)

    def host_ok(self):
        return self.host() in ('127.0.0.1', 'localhost') or bool(REMOTE_HOST and self.host() == REMOTE_HOST)

    def authed(self):
        if not self.is_remote():
            return True
        if not config.get('password_hash') or not config.get('secret'):
            return False
        cookie = SimpleCookie(self.headers.get('Cookie') or '').get(AUTH_COOKIE)
        return bool(cookie) and hmac.compare_digest(cookie.value, auth_token())

    def send_page(self, name):
        body = (BASE_DIR / name).read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Frame-Options', 'DENY')
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, query):
        qs = urllib.parse.parse_qs(query)
        with jobs_lock:
            job = jobs.get((qs.get('id') or [''])[0])
            files = list((job or {}).get('_files', {}).values())
        try:
            fp = files[int((qs.get('n') or ['0'])[0])]
        except (ValueError, IndexError):
            return self.send_error(404)
        self.send_media(fp, attachment=True)

    def send_media(self, fp, attachment=False, content_type=None):
        """Gửi file, hỗ trợ Range để trình duyệt tua được video."""
        if not fp or not os.path.isfile(fp):
            return self.send_error(404)
        size = os.path.getsize(fp)
        start, end, status = 0, size - 1, 200
        m = re.fullmatch(r'bytes=(\d*)-(\d*)', (self.headers.get('Range') or '').strip())
        if m and (m[1] or m[2]):
            if m[1]:
                start, end = int(m[1]), int(m[2]) if m[2] else size - 1
            else:
                start = max(0, size - int(m[2]))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        if attachment:
            self.send_header('Content-Type', 'application/octet-stream')
            name = urllib.parse.quote(os.path.basename(fp))
            self.send_header('Content-Disposition', f"attachment; filename*=UTF-8''{name}")
        else:
            self.send_header('Content-Type', content_type or mimetypes.guess_type(fp)[0] or 'application/octet-stream')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - start + 1))
        if status == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.send_header('Cache-Control', 'private, max-age=3600' if content_type == 'image/jpeg' else 'no-store')
        self.end_headers()
        if self.command == 'HEAD':
            return
        try:
            with open(fp, 'rb') as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except OSError:
            pass  # người dùng huỷ tải / tua sang chỗ khác

    def library_path(self, query):
        rel = (urllib.parse.parse_qs(query).get('path') or [''])[0]
        target = manager.resolve_in(download_folder(), rel)
        return target if target and target.is_file() else None

    def manage(self, path, data):
        """Thao tác của Thư viện và Cài đặt."""
        root = download_folder()

        def targets():
            out = []
            for rel in data.get('paths') or [data.get('path')]:
                t = manager.resolve_in(root, rel or '')
                if not t or not t.is_file():
                    raise FileNotFoundError(rel)
                out.append(t)
            return out

        try:
            if path == '/api/library/delete':
                files = targets()
                if not manager.send_to_recycle_bin(files):
                    return self.send_json({'error': 'Không xoá được (file đang mở?)'}, 409)
                return self.send_json({'ok': True, 'deleted': len(files)})

            if path == '/api/library/rename':
                new = manager.rename(targets()[0], data.get('name'))
                return self.send_json({'ok': True, 'path': new.relative_to(Path(root).resolve()).as_posix()})

            if path == '/api/library/reveal':
                subprocess.Popen(['explorer', '/select,', str(targets()[0])])
                return self.send_json({'ok': True})

            if path == '/api/library/cleanup':
                files = manager.leftovers(root)
                ok = manager.send_to_recycle_bin(files)
                shutil.rmtree(THUMB_DIR, ignore_errors=True)
                return self.send_json({'ok': ok, 'deleted': len(files) if ok else 0})

            if path == '/api/settings':
                cfg = load_config()
                if 'download_folder' in data:
                    folder = str(data['download_folder'] or '').strip()
                    if not folder or not os.path.isabs(folder):
                        return self.send_json({'error': 'Hãy nhập đường dẫn đầy đủ, ví dụ D:\\Downloads\\yt-dlp'}, 400)
                    os.makedirs(folder, exist_ok=True)
                    cfg['download_folder'] = folder
                if 'max_concurrent' in data:
                    cfg['max_concurrent'] = max(1, min(5, int(data['max_concurrent'])))
                save_config(cfg)
                config.clear()
                config.update(cfg)
                with queue_cond:
                    queue_cond.notify_all()
                return self.send_json({'ok': True})

            if path == '/api/settings/password':
                password = str(data.get('password') or '')
                if len(password) < 8:
                    return self.send_json({'error': 'Mật khẩu cần ít nhất 8 ký tự'}, 400)
                set_password(password)
                config.clear()
                config.update(load_config())
                return self.send_json({'ok': True})

            if path == '/api/settings/autostart':
                if not manager.autostart_supported():
                    return self.send_json({'error': 'Chỉ dùng được với bản exe'}, 400)
                manager.set_autostart(bool(data.get('enabled')))
                return self.send_json({'ok': True, 'enabled': manager.autostart_enabled()})

            if path == '/api/quit':
                self.send_json({'ok': True})
                stop_tunnel()
                threading.Timer(0.5, lambda: os._exit(0)).start()
                return None
        except FileNotFoundError:
            return self.send_json({'error': 'Không tìm thấy file (có thể đã bị xoá hoặc đổi tên)'}, 404)
        except (ValueError, OSError) as e:
            return self.send_json({'error': str(e)}, 400)
        return self.send_error(404)

    def login(self, data):
        if not config.get('password_hash'):
            return self.send_json({'error': 'Chưa đặt mật khẩu cho truy cập từ xa'}, 403)
        now = time.time()
        with jobs_lock:
            login_failures[:] = [t for t in login_failures if now - t < 300]
            if len(login_failures) >= 5:
                return self.send_json({'error': 'Sai quá nhiều lần, thử lại sau 5 phút'}, 429)
        given = hash_password(str(data.get('password') or ''), config['salt'])
        if not hmac.compare_digest(given, config['password_hash']):
            with jobs_lock:
                login_failures.append(now)
            time.sleep(1)
            return self.send_json({'error': 'Sai mật khẩu'}, 401)
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Set-Cookie',
                         f'{AUTH_COOKIE}={auth_token()}; Max-Age=2592000; Path=/; HttpOnly; Secure; SameSite=Strict')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.host_ok():
            return self.send_error(403)
        path, _, query = self.path.partition('?')
        if not self.authed():
            if path in ('/', '/index.html'):
                return self.send_page('login.html')
            return self.send_json({'error': 'Cần đăng nhập'}, 401)
        if path in ('/', '/index.html'):
            self.send_page('index.html')
        elif path == '/api/file':
            self.send_file(query)
        elif path == '/api/library':
            root = download_folder()
            files = manager.list_files(root)
            self.send_json({'folder': root, 'files': files, 'disk': manager.disk_info(root),
                            'total_size': sum(f['size'] for f in files)})
        elif path == '/api/library/stream':
            self.send_media(self.library_path(query))
        elif path == '/api/library/download':
            self.send_media(self.library_path(query), attachment=True)
        elif path == '/api/library/thumb':
            fp = self.library_path(query)
            thumb = manager.thumbnail(fp, THUMB_DIR) if fp else None
            if not thumb:
                return self.send_error(404)
            self.send_media(str(thumb), content_type='image/jpeg')
        elif path == '/api/settings':
            root = download_folder()
            self.send_json({
                'download_folder': root,
                'max_concurrent': max_concurrent(),
                'disk': manager.disk_info(root),
                'leftovers': len(manager.leftovers(root)),
                'autostart': manager.autostart_enabled() if manager.autostart_supported() else None,
                'remote_host': REMOTE_HOST or None,
                'tunnel_configured': tunnel_enabled(),
                'tunnel_running': bool(tunnel['proc'] and tunnel['proc'].poll() is None),
                'has_password': bool(config.get('password_hash')),
                'studio_version': STUDIO_VERSION,
                'ytdlp_version': YTDLP_VERSION,
                'ffmpeg': HAS_FFMPEG,
                'app_path': sys.executable if getattr(sys, 'frozen', False) else None,
                'is_remote': self.is_remote(),
            })
        elif path == '/api/config':
            self.send_json({
                'version': YTDLP_VERSION,
                'ffmpeg': HAS_FFMPEG,
                'default_folder': download_folder(),
                'studio_version': STUDIO_VERSION,
                'remote': self.is_remote(),
            })
        elif path == '/api/update':
            with jobs_lock:
                self.send_json({**update, 'current': STUDIO_VERSION})
        elif path == '/api/alive':
            # Kết nối giữ mở suốt thời gian cửa sổ còn mở; đứt kết nối = cửa sổ đã đóng
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            with jobs_lock:
                clients['count'] += 1
                clients['seen'] = True
            try:
                while True:
                    self.wfile.write(b': ping\n\n')
                    self.wfile.flush()
                    time.sleep(3)
            except OSError:
                pass
            finally:
                with jobs_lock:
                    clients['count'] -= 1
        elif path == '/api/jobs':
            with jobs_lock:
                data = sorted((public_job(j) for j in jobs.values()), key=lambda j: j['created'], reverse=True)
            self.send_json(data)
        else:
            self.send_error(404)

    def do_POST(self):
        # Chặn request từ trang web khác: bắt buộc header riêng (trình duyệt sẽ preflight và bị từ chối)
        if not self.host_ok() or self.headers.get('X-Studio') != '1':
            return self.send_error(403)
        try:
            data = self.read_json()
        except ValueError:
            return self.send_json({'error': 'Dữ liệu không hợp lệ'}, 400)
        path = self.path.split('?')[0]

        if path == '/api/login':
            return self.login(data)
        if path == '/api/logout':
            self.send_response(200)
            self.send_header('Set-Cookie', f'{AUTH_COOKIE}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Strict')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if not self.authed():
            return self.send_json({'error': 'Cần đăng nhập'}, 401)
        remote = self.is_remote()
        if remote:
            # Từ xa không được chọn thư mục tuỳ ý trên máy hay mở Explorer
            (data.get('options') or {}).pop('folder', None)
            if path in LOCAL_ONLY:
                return self.send_json({'error': 'Chỉ làm được trên chính máy tính'}, 403)

        if path.startswith('/api/library/') or path.startswith('/api/settings') or path == '/api/quit':
            return self.manage(path, data)

        if path == '/api/info':
            url = (data.get('url') or '').strip()
            if not url:
                return self.send_json({'error': 'Chưa nhập link'}, 400)
            try:
                self.send_json(extract(url))
            except Exception as e:  # noqa: BLE001
                msg = str(e).replace('\x1b[0;31m', '').replace('\x1b[0m', '').removeprefix('ERROR: ')
                self.send_json({'error': msg[:600]}, 422)

        elif path == '/api/download':
            urls = [u.strip() for u in data.get('urls') or [] if u and u.strip()]
            if not urls:
                return self.send_json({'error': 'Chưa nhập link'}, 400)
            options = data.get('options') or {}
            preview = data.get('preview') if len(urls) == 1 else None
            created = [public_job(start_job(u, options, preview)) for u in urls[:50]]
            self.send_json(created)

        elif path == '/api/cancel':
            with jobs_lock:
                job = jobs.get(data.get('id'))
                if job:
                    job['cancel'] = True
            self.send_json({'ok': bool(job)})

        elif path == '/api/retry':
            with jobs_lock:
                old = jobs.get(data.get('id'))
            if not old:
                return self.send_json({'error': 'Không tìm thấy'}, 404)
            self.send_json(public_job(start_job(old['url'], old['options'], {'title': old['title'], 'thumbnail': old.get('thumbnail')})))

        elif path == '/api/update/check':
            threading.Thread(target=check_update, daemon=True).start()
            self.send_json({'ok': True})

        elif path == '/api/update/restart':
            if update['status'] != 'ready':
                return self.send_json({'error': 'Chưa có bản cập nhật sẵn sàng'}, 409)
            with jobs_lock:
                busy = any(j['status'] in ('queued', 'starting', 'downloading', 'processing') for j in jobs.values())
            if busy and not data.get('force'):
                return self.send_json({'error': 'Đang có video tải dở, hãy chờ tải xong'}, 409)
            self.send_json({'ok': True})
            restart_into_new_version()

        elif path == '/api/clear':
            with jobs_lock:
                for jid in [k for k, j in jobs.items() if j['status'] in ('done', 'error', 'cancelled')]:
                    del jobs[jid]
            self.send_json({'ok': True})

        elif path == '/api/open':
            with jobs_lock:
                job = jobs.get(data.get('id'))
            target = None
            if job:
                fp = job.get('filepath')
                target = fp if fp and os.path.exists(fp) else job.get('folder')
            elif data.get('folder') and os.path.isdir(data['folder']):
                target = data['folder']
            if not target or not os.path.exists(target):
                return self.send_json({'error': 'Thư mục chưa tồn tại (chưa tải gì vào đây)'}, 404)
            if os.path.isfile(target):
                subprocess.Popen(['explorer', '/select,', os.path.normpath(target)])
            else:
                os.startfile(os.path.normpath(target))
            self.send_json({'ok': True})

        else:
            self.send_error(404)


# ---------------------------------------------------------------- Tự cập nhật
# Bản mới được tải ngầm, kiểm tra SHA-256 rồi thay vào chỗ file exe đang chạy
# (Windows cho đổi tên exe đang chạy). Lần mở sau, hoặc khi bấm "Cập nhật ngay", sẽ chạy bản mới.
update = {'status': 'idle', 'latest': None, 'notes': '', 'error': None, 'progress': 0}
update_lock = threading.Lock()


def parse_version(v):
    return tuple(int(x) for x in re.findall(r'\d+', str(v))[:3])


def set_update(**fields):
    with jobs_lock:
        update.update(fields)


def fetch_manifest():
    req = urllib.request.Request(
        f'{UPDATE_URL}?t={int(time.time())}', headers={'User-Agent': f'yt-dlp-studio/{STUDIO_VERSION}'})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def check_update():
    if not update_lock.acquire(blocking=False):
        return
    try:
        if update['status'] == 'ready':
            return
        set_update(status='checking', error=None)
        manifest = fetch_manifest()
        latest = manifest['version']
        if parse_version(latest) <= parse_version(STUDIO_VERSION):
            set_update(status='latest', latest=latest)
            return
        set_update(status='available', latest=latest, notes=manifest.get('notes', ''))
        if not getattr(sys, 'frozen', False):
            return  # chạy từ mã nguồn: chỉ báo, không thay file

        exe = Path(sys.executable)
        new, old = exe.with_name(exe.stem + '.new.exe'), exe.with_name(exe.stem + '.old.exe')
        set_update(status='downloading', progress=0)
        req = urllib.request.Request(manifest['url'], headers={'User-Agent': f'yt-dlp-studio/{STUDIO_VERSION}'})
        digest = hashlib.sha256()
        with urllib.request.urlopen(req, timeout=60) as r, open(new, 'wb') as out:
            total = int(r.headers.get('Content-Length') or manifest.get('size') or 0)
            done = 0
            while chunk := r.read(1 << 16):
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if total:
                    set_update(progress=done / total * 100)
        if digest.hexdigest().lower() != manifest['sha256'].lower():
            new.unlink(missing_ok=True)
            raise ValueError('File cập nhật bị lỗi (sai mã kiểm tra), sẽ thử lại sau')
        old.unlink(missing_ok=True)
        os.replace(exe, old)
        os.replace(new, exe)
        set_update(status='ready', progress=100)
    except Exception as e:  # noqa: BLE001
        set_update(status='error', error=str(e)[:300])
    finally:
        update_lock.release()


def update_loop():
    if getattr(sys, 'frozen', False):
        old = Path(sys.executable).with_name(Path(sys.executable).stem + '.old.exe')
        for delay in (2, 5, 15, 40):  # bản cũ có thể chưa thoát hẳn ngay
            time.sleep(delay)
            try:
                old.unlink(missing_ok=True)
                break
            except OSError:
                pass
    else:
        time.sleep(3)
    while True:
        check_update()
        # Lỗi mạng thì thử lại sớm, bình thường kiểm tra 6 tiếng một lần
        time.sleep(1800 if update['status'] == 'error' else 6 * 3600)


def restart_into_new_version():
    keep = [a for a in sys.argv[1:] if a in ('--background', '--no-browser')]
    # PYINSTALLER_RESET_ENVIRONMENT: bản mới tự giải nén riêng, không dùng thư mục tạm của bản này
    subprocess.Popen([sys.executable, '--after-update', *keep], close_fds=True,
                     env=dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1', YTDLP_STUDIO_CLEAN='1'),
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    stop_tunnel()
    threading.Timer(0.5, lambda: os._exit(0)).start()


# ---------------------------------------------------------------- Cloudflare Tunnel
tunnel = {'proc': None, 'stop': False}


def tunnel_enabled():
    t = config.get('tunnel') or {}
    return bool(t.get('cloudflared') and os.path.isfile(t['cloudflared']) and t.get('credentials'))


def start_tunnel(port):
    """Giữ cloudflared chạy suốt thời gian app chạy; tự bật lại nếu nó bị tắt."""
    t = config['tunnel']
    args = [t['cloudflared'], 'tunnel', '--no-autoupdate', '--credentials-file', t['credentials'],
            'run', '--url', f'http://127.0.0.1:{port}', t['name']]

    def loop():
        while not tunnel['stop']:
            tunnel['proc'] = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                              creationflags=subprocess.CREATE_NO_WINDOW)
            tunnel['proc'].wait()
            time.sleep(10)

    threading.Thread(target=loop, daemon=True).start()


def stop_tunnel():
    tunnel['stop'] = True
    if tunnel['proc'] and tunnel['proc'].poll() is None:
        tunnel['proc'].terminate()


def studio_running(port):
    """Cổng đang bị chiếm bởi chính yt-dlp Studio (bản đã mở trước đó)?"""
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/config', timeout=1) as r:
            return r.headers.get('Server', '').startswith('yt-dlp-studio')
    except Exception:  # noqa: BLE001
        return False


def find_port(start=PORT):
    for port in range(start, start + 20):
        with socket.socket() as s:
            if s.connect_ex(('127.0.0.1', port)) != 0:
                return port, False
        if studio_running(port):
            return port, True
    raise RuntimeError('Không còn cổng trống')


def find_browser():
    candidates = [
        os.path.expandvars(r'%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe'),
        os.path.expandvars(r'%ProgramFiles%\Microsoft\Edge\Application\msedge.exe'),
        os.path.expandvars(r'%ProgramFiles%\Google\Chrome\Application\chrome.exe'),
        os.path.expandvars(r'%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe'),
        os.path.expandvars(r'%LocalAppData%\Google\Chrome\Application\chrome.exe'),
        os.path.expandvars(r'%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe'),
    ]
    return next((p for p in candidates if os.path.isfile(p)), None)


def open_window(url):
    """Mở giao diện thành cửa sổ ứng dụng riêng (không tab, không thanh địa chỉ)."""
    browser = find_browser()
    if browser:
        subprocess.Popen([browser, f'--app={url}', '--window-size=1100,860', '--new-window'])
    else:
        webbrowser.open(url)


def watchdog():
    """Tự tắt khi cửa sổ đã đóng và không còn video nào đang tải."""
    idle_since = None
    while True:
        time.sleep(2)
        with jobs_lock:
            busy = clients['count'] > 0 or any(
                j['status'] in ('queued', 'starting', 'downloading', 'processing') for j in jobs.values())
        if busy or not clients['seen']:
            idle_since = None
        elif idle_since is None:
            idle_since = time.time()
        elif time.time() - idle_since > 8:
            os._exit(0)


def main():
    # Bản exe không có console: tránh lỗi khi có thứ gì đó ghi ra stdout/stderr
    for name in ('stdout', 'stderr'):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, 'w', encoding='utf-8'))

    if '--set-password' in sys.argv:
        # yt-dlp-studio.exe --set-password "mật khẩu mới"
        set_password(sys.argv[sys.argv.index('--set-password') + 1])
        return

    after_update = '--after-update' in sys.argv
    if after_update:
        # Chờ bản cũ nhả cổng; cửa sổ đang mở sẽ tự kết nối lại và tải lại trang
        for _ in range(50):
            with socket.socket() as s:
                if s.connect_ex(('127.0.0.1', PORT)) != 0:
                    break
            time.sleep(0.2)
        clients['seen'] = True

    port, already = find_port()
    url = f'http://127.0.0.1:{port}'
    # --background: chạy nền khi khởi động Windows, không mở cửa sổ
    no_window = '--no-browser' in sys.argv or '--background' in sys.argv
    if already:
        if not no_window:
            open_window(url)
        return
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'yt-dlp Studio {STUDIO_VERSION} đang chạy tại {url}')
    threading.Thread(target=update_loop, daemon=True).start()
    if tunnel_enabled():
        # Có tunnel: chạy liên tục để truy cập được từ xa, không tự tắt khi đóng cửa sổ
        start_tunnel(port)
    elif not no_window:
        threading.Thread(target=watchdog, daemon=True).start()
    if not no_window and not after_update:
        threading.Timer(0.3, open_window, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
