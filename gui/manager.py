"""Quản lý file đã tải và các thiết lập hệ thống của yt-dlp Studio."""
import ctypes
import hashlib
import mimetypes
import os
import re
import shutil
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

VIDEO_EXTS = {'.mp4', '.mkv', '.webm', '.mov', '.avi', '.flv', '.m4v', '.ts'}
AUDIO_EXTS = {'.mp3', '.m4a', '.opus', '.ogg', '.wav', '.flac', '.aac', '.weba'}
LEFTOVER_PATTERNS = ('*.part', '*.part-Frag*', '*.ytdl', '*.temp.*')
MAX_FILES = 3000

mimetypes.add_type('video/x-matroska', '.mkv')
mimetypes.add_type('audio/ogg', '.opus')
mimetypes.add_type('audio/mp4', '.m4a')


# ---------------------------------------------------------------- Đường dẫn an toàn
def resolve_in(root, rel):
    """Trả về đường dẫn tuyệt đối của rel nằm TRONG root, hoặc None nếu thoát ra ngoài."""
    if not rel or '\0' in rel:
        return None
    root_path = Path(root).resolve()
    target = (root_path / rel).resolve()
    if target != root_path and root_path not in target.parents:
        return None
    return target


# ---------------------------------------------------------------- Danh sách file
def list_files(root):
    root_path = Path(root)
    files = []
    if not root_path.is_dir():
        return files
    for dirpath, dirnames, filenames in os.walk(root_path):
        depth = len(Path(dirpath).relative_to(root_path).parts)
        dirnames[:] = [d for d in dirnames if not d.startswith('.')] if depth < 3 else []
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            kind = 'video' if ext in VIDEO_EXTS else 'audio' if ext in AUDIO_EXTS else None
            if not kind or '.temp.' in name or name.endswith('.part'):
                continue
            full = Path(dirpath) / name
            try:
                st = full.stat()
            except OSError:
                continue
            files.append({
                'path': full.relative_to(root_path).as_posix(),
                'name': name,
                'folder': Path(dirpath).relative_to(root_path).as_posix() if depth else '',
                'kind': kind,
                'ext': ext[1:],
                'size': st.st_size,
                'mtime': st.st_mtime,
                'playable': ext in ('.mp4', '.webm', '.m4v', '.mp3', '.m4a', '.opus', '.ogg', '.wav', '.flac', '.aac'),
            })
            if len(files) >= MAX_FILES:
                return files
    return files


def disk_info(root):
    try:
        anchor = Path(root).anchor or root
        usage = shutil.disk_usage(anchor if os.path.exists(anchor) else '.')
        return {'total': usage.total, 'free': usage.free, 'drive': anchor}
    except OSError:
        return {}


def leftovers(root):
    root_path = Path(root)
    if not root_path.is_dir():
        return []
    found = set()
    for pattern in LEFTOVER_PATTERNS:
        found.update(p for p in root_path.rglob(pattern) if p.is_file())
    return sorted(found)


# ---------------------------------------------------------------- Ảnh thu nhỏ
def thumbnail(file_path, cache_dir):
    """Tạo (hoặc lấy từ cache) ảnh JPG thu nhỏ bằng ffmpeg. Trả về đường dẫn ảnh hoặc None."""
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        return None
    st = file_path.stat()
    key = hashlib.sha1(f'{file_path}|{st.st_size}|{st.st_mtime}'.encode()).hexdigest()
    out = Path(cache_dir) / f'{key}.jpg'
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    is_audio = file_path.suffix.lower() in AUDIO_EXTS
    # Video: lấy khung hình ở giây thứ 3; âm thanh: lấy ảnh bìa đã nhúng (nếu có)
    seek = [] if is_audio else ['-ss', '3']
    cmd = [ffmpeg, '-hide_banner', '-loglevel', 'error', *seek, '-i', str(file_path),
           '-map', '0:v:0', '-frames:v', '1', '-vf', 'scale=480:-2', '-q:v', '4', '-y', str(out)]
    try:
        subprocess.run(cmd, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        if not out.exists() and seek:  # video ngắn hơn 3 giây
            cmd.remove('-ss')
            cmd.remove('3')
            subprocess.run(cmd, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out if out.exists() else None


# ---------------------------------------------------------------- Xoá vào Thùng rác
class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ('hwnd', wintypes.HWND), ('wFunc', wintypes.UINT), ('pFrom', wintypes.LPCWSTR),
        ('pTo', wintypes.LPCWSTR), ('fFlags', ctypes.c_uint16), ('fAnyOperationsAborted', wintypes.BOOL),
        ('hNameMappings', ctypes.c_void_p), ('lpszProgressTitle', wintypes.LPCWSTR),
    ]


def send_to_recycle_bin(paths):
    """Đưa file vào Thùng rác (khôi phục được). Trả về True nếu thành công."""
    if not paths:
        return True
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    op = _SHFILEOPSTRUCTW(
        wFunc=FO_DELETE,
        pFrom='\0'.join(str(p) for p in paths) + '\0',  # danh sách kết thúc bằng 2 ký tự null
        fFlags=FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI,
    )
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0 and not op.fAnyOperationsAborted


# ---------------------------------------------------------------- Đổi tên
INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def rename(file_path, new_name):
    new_name = INVALID_NAME.sub('', (new_name or '').strip()).rstrip('. ')
    if not new_name:
        raise ValueError('Tên mới không hợp lệ')
    if not new_name.lower().endswith(file_path.suffix.lower()):
        new_name += file_path.suffix
    target = file_path.with_name(new_name)
    if target.exists():
        raise ValueError('Đã có file trùng tên')
    file_path.rename(target)
    return target


# ---------------------------------------------------------------- Hộp chọn thư mục
def pick_folder(initial=''):
    """Mở hộp chọn thư mục của Windows (nổi trên cùng). Trả về đường dẫn, hoặc '' nếu bấm Huỷ."""
    initial = str(initial or '').replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8;"
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$d.Description = 'Chọn thư mục lưu video';"
        "$d.ShowNewFolderButton = $true;"
        f"if (Test-Path -LiteralPath '{initial}') {{ $d.SelectedPath = '{initial}' }};"
        "$owner = New-Object System.Windows.Forms.Form -Property @{TopMost = $true; ShowInTaskbar = $false};"
        "if ($d.ShowDialog($owner) -eq 'OK') { $d.SelectedPath }"
    )
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-STA', '-Command', script], capture_output=True,
                           timeout=900, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return ''
    return r.stdout.decode('utf-8', 'replace').strip()


# ---------------------------------------------------------------- Khởi động cùng Windows
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_NAME = 'yt-dlp Studio'


def autostart_supported():
    return getattr(sys, 'frozen', False)


def autostart_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_NAME)
            return os.path.normcase(sys.executable) in os.path.normcase(value)
    except OSError:
        return False


def set_autostart(enabled):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, f'"{sys.executable}" --background')
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass
