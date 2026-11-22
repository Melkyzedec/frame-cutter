"""Download one video into a directory owned by the current import."""

from __future__ import annotations

import errno
import ipaddress
import math
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.client import HTTPException, HTTPSConnection
from importlib.util import find_spec
from pathlib import Path
from tempfile import gettempdir
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, parse_qs, urlsplit
from typing import BinaryIO
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, build_opener
from uuid import uuid4

from frame_engine import APP_DIR, FrameCutterError, OperationCancelled, require_ffmpeg


IMPORT_TOOLS = APP_DIR / "tools" / "link-import"
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com"}
IMPORT_TIMEOUT = 15 * 60
NETWORK_TIMEOUT = 10
MAX_IMPORT_BYTES = 1024**3
MIN_FREE_BYTES = 512 * 1024**2
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
IMPORT_FOLDER = re.compile(r"frame-cutter-v2-[0-9a-f]{32}")
IMPORT_MARKER = ".frame-cutter-import"
IMPORT_MARKER_CONTENT = b"Frame Cutter temporary import v2\n"
_active_imports: dict[Path, BinaryIO] = {}

if os.name == "nt":
    import msvcrt
else:
    import fcntl


@dataclass(frozen=True)
class VideoLink:
    url: str
    platform: str | None = None
    video_id: str | None = None
    portion: tuple[float, float] | None = None


def create_import_folder() -> Path:
    folder = Path(gettempdir()).resolve() / f"frame-cutter-v2-{uuid4().hex}"
    # On Windows inherit the ACL of the user's temporary directory.
    folder.mkdir(mode=0o770 if os.name == "nt" else 0o700)
    marker = (folder / IMPORT_MARKER).open("x+b", buffering=0)
    try:
        _lock_marker(marker)
        marker.write(IMPORT_MARKER_CONTENT)
    except OSError:
        marker.close()
        raise
    _active_imports[folder] = marker
    return folder


def _lock_marker(marker: BinaryIO) -> None:
    if os.name == "nt":
        msvcrt.locking(marker.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(marker, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _regular_temp_folder(folder: Path) -> bool:
    if folder.parent != Path(gettempdir()).resolve() or not IMPORT_FOLDER.fullmatch(folder.name):
        return False
    try:
        info = folder.lstat()
        reparse_point = getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        return stat.S_ISDIR(info.st_mode) and not reparse_point and folder.resolve() == folder
    except OSError:
        return False


def remove_import_folder(folder: Path) -> None:
    marker = _active_imports.pop(folder, None)
    if marker is not None:
        marker.close()
        if _regular_temp_folder(folder):
            shutil.rmtree(folder, ignore_errors=True)


def clean_abandoned_imports() -> None:
    try:
        for folder in Path(gettempdir()).resolve().glob("frame-cutter-v2-*"):
            if folder in _active_imports or not _regular_temp_folder(folder):
                continue
            marker_path = folder / IMPORT_MARKER
            try:
                info = marker_path.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_size != len(IMPORT_MARKER_CONTENT)
                        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    continue
                with marker_path.open("r+b", buffering=0) as marker:
                    _lock_marker(marker)
                    abandoned = marker.read(len(IMPORT_MARKER_CONTENT)) == IMPORT_MARKER_CONTENT
                if abandoned and _regular_temp_folder(folder):
                    shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                continue
    except OSError:
        return


def _https_url(text: str) -> SplitResult:
    try:
        url = urlsplit(text)
        if (url.scheme == "https" and url.hostname and url.username is None and url.password is None
                and url.port != 0 and not any(char.isspace() or ord(char) < 32 for char in text)
                and "\\" not in text):
            return url
    except ValueError:
        pass
    raise FrameCutterError("Collez un lien HTTPS valide, sans identifiant ni mot de passe.")


def parse_video_link(text: str, portion: tuple[float, float] | None = None) -> VideoLink:
    text = text.strip()
    url = _https_url(text)
    host = (url.hostname or "").rstrip(".")
    if portion is not None and not 0 <= portion[0] < portion[1] < math.inf:
        raise FrameCutterError("La fin doit être après le début.")
    if host not in YOUTUBE_HOSTS:
        if portion is not None:
            raise FrameCutterError("Les bornes à l’import sont disponibles pour YouTube. "
                                   "Choisissez le passage après l’ouverture de cette vidéo.")
        if host in INSTAGRAM_HOSTS:
            reel = re.fullmatch(r"/reel/([A-Za-z0-9_-]{1,32})/?", url.path)
            if reel is None or url.port not in {None, 443}:
                raise FrameCutterError("Collez le lien d’un Reel Instagram public, au format /reel/…")
            reel_id = reel.group(1)
            return VideoLink(f"https://www.instagram.com/reel/{reel_id}/", "instagram", reel_id)
        return VideoLink(url._replace(fragment="").geturl())

    path = url.path.strip("/").split("/")
    video_id = ""
    if host in {"youtu.be", "www.youtu.be"} and len(path) == 1:
        video_id = path[0]
    elif url.path == "/watch":
        ids = parse_qs(url.query).get("v", [])
        if len(ids) == 1:
            video_id = ids[0]
    elif len(path) == 2 and path[0] == "shorts":
        video_id = path[1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) or url.port not in {None, 443}:
        raise FrameCutterError("Collez le lien d’une vidéo YouTube individuelle (vidéo ou Short).")
    return VideoLink(f"https://www.youtube.com/watch?v={video_id}", "youtube", video_id, portion)


class _HttpsRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _connect_public(address: tuple[str, int], timeout: float | None = None,
                    source_address: tuple[str, int] | None = None) -> socket.socket:
    host, port = address
    deadline = time.monotonic() + (NETWORK_TIMEOUT if timeout is None else timeout)
    last_error = None
    for family, socktype, protocol, _, destination in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        ip = ipaddress.ip_address(destination[0].split("%", 1)[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Connexion expirée.")
        connection = socket.socket(family, socktype, protocol)
        try:
            connection.settimeout(remaining)
            if source_address is not None:
                connection.bind(source_address)
            # Connect to the checked address. HTTPSConnection keeps the original name for TLS.
            connection.connect(destination)
            return connection
        except OSError as exc:
            last_error = exc
            connection.close()
    if last_error is not None:
        raise last_error
    raise FrameCutterError("Ce lien ne mène pas à un serveur public. Ouvrez la vidéo comme fichier local.")


class _PublicHttpsConnection(HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_public


class _PublicHttpsHandler(HTTPSHandler):
    def https_open(self, request):
        return self.do_open(_PublicHttpsConnection, request, context=self._context)


def _check_download_budget(folder: Path, written: int = 0, incoming: int = 0) -> None:
    if written + incoming > MAX_IMPORT_BYTES:
        raise FrameCutterError("La vidéo dépasse la limite de téléchargement de 1 Go. "
                               "Essayez un extrait plus court ou ouvrez la vidéo depuis votre PC.")
    if shutil.disk_usage(folder).free - incoming < MIN_FREE_BYTES:
        raise FrameCutterError("Il n’y a pas assez d’espace libre pour continuer l’import. "
                               "Libérez de la place sur le disque et réessayez.")


def _check_platform_budget(folder: Path) -> None:
    with os.scandir(folder) as entries:
        size = 0
        for entry in entries:
            try:
                if entry.name != IMPORT_MARKER and entry.is_file(follow_symlinks=False):
                    size += entry.stat(follow_symlinks=False).st_size
            except FileNotFoundError:
                continue
    _check_download_budget(folder, size)


def _video_extension(header: bytes) -> str:
    if header[4:8] == b"ftyp":
        return ".mp4"
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        return ".mkv"
    if header.startswith(b"RIFF") and header[8:12] == b"AVI ":
        return ".avi"
    raise FrameCutterError("Ce lien doit ouvrir directement un fichier MP4, MOV, WebM, MKV ou AVI. "
                           "Pour une plateforme, collez un lien YouTube ou un lien de Reel Instagram.")


def _download_direct(url: str, folder: Path, cancel: threading.Event, deadline: float) -> Path:
    with build_opener(ProxyHandler({}), _PublicHttpsHandler(), _HttpsRedirect()).open(
        url, timeout=NETWORK_TIMEOUT,
    ) as response:
        if response.status != 200:
            raise FrameCutterError("Le serveur n’a pas fourni le fichier vidéo complet.")
        length = response.headers.get("Content-Length", "").strip()
        expected = None
        if length.isascii() and length.isdecimal():
            significant_digits = length.lstrip("0") or "0"
            expected = int(significant_digits) if len(significant_digits) <= 12 else MAX_IMPORT_BYTES + 1
            _check_download_budget(folder, incoming=expected)
        first_bytes = response.read(12)
        _check_interrupt(cancel, deadline)
        _check_download_budget(folder, incoming=len(first_bytes))
        path = folder / ("video-importee" + _video_extension(first_bytes))
        with path.open("xb") as output:
            output.write(first_bytes)
            while block := response.read1(256 * 1024):
                _check_interrupt(cancel, deadline)
                _check_download_budget(folder, output.tell(), len(block))
                output.write(block)
            if expected is not None and output.tell() != expected:
                raise FrameCutterError("Le téléchargement est incomplet. Réessayez.")
    return path


def _check_interrupt(cancel: threading.Event, deadline: float) -> None:
    if cancel.is_set():
        raise OperationCancelled("Import annulé.")
    if time.monotonic() >= deadline:
        raise FrameCutterError("L’import a dépassé 15 minutes. Réessayez avec un extrait plus court.")


def _stop_download(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        # yt-dlp may have started FFmpeg and Node processes of its own.
        try:
            taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
            subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=NO_WINDOW, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if process.poll() is None:
        process.kill()

def _download_platform(
    link: VideoLink, folder: Path, cancel: threading.Event, deadline: float, *, preserve_audio: bool = False,
) -> Path:
    youtube = link.platform == "youtube"
    platform_name = "YouTube" if youtube else "Instagram"
    if sys.version_info < (3, 11):
        raise FrameCutterError(f"L’import {platform_name} nécessite Python 3.11 ou plus récent.")
    env = os.environ.copy()
    if getattr(sys, "frozen", False):
        downloader = IMPORT_TOOLS / "yt-dlp.exe"
        if not downloader.is_file():
            raise FrameCutterError("Le composant d’import est absent. Réextrayez l’archive de Frame Cutter.")
        command = [str(downloader)]
    else:
        modules = ("yt_dlp", "yt_dlp_ejs") if youtube else ("yt_dlp",)
        if all((IMPORT_TOOLS / name / "__init__.py").is_file() for name in modules):
            env["PYTHONPATH"] = str(IMPORT_TOOLS)
        elif not all(find_spec(name) is not None for name in modules):
            raise FrameCutterError(f"Le composant d’import {platform_name} est absent. "
                                   "Installez les dépendances indiquées dans le README.")
        command = [sys.executable, "-B", "-m", "yt_dlp"]
    ffmpeg, _ = require_ffmpeg()
    name = f"{link.platform}-{link.video_id}" + ("-extrait" if link.portion else "")
    path = folder / f"{name}.mp4"
    output_template = (str(folder / name).replace("%", "%%") + ".%(ext)s" if preserve_audio
                       else str(path).replace("%", "%%"))
    command += [
        "--ignore-config", "--no-plugin-dirs", "--no-cache-dir", "--no-playlist",
        "--no-cookies", "--no-cookies-from-browser", "--use-extractors", link.platform,
        "--socket-timeout", str(NETWORK_TIMEOUT), "--retries", "1", "--extractor-retries", "0",
        "--max-filesize", str(MAX_IMPORT_BYTES),
        "--no-progress", "--ffmpeg-location", ffmpeg, "-o", output_template,
    ]
    if preserve_audio:
        command += ["--merge-output-format", "mkv", "--remux-video", "mkv"]
    if youtube:
        bundled_node = IMPORT_TOOLS / "node.exe"
        node = str(bundled_node) if bundled_node.is_file() else shutil.which("node")
        if node is None:
            raise FrameCutterError("L’import YouTube nécessite Node.js 22 ou plus récent dans le PATH.")
        match_filter = "!is_live & !is_upcoming"
        if link.portion is not None:
            start, end = link.portion
            match_filter += f" & duration >= {end:.6f}"
            command += ["--download-sections", f"*{start:.6f}-{end:.6f}", "--no-force-keyframes-at-cuts"]
        command += ["--js-runtimes", f"node:{node}", "--match-filter", match_filter]
        command += ["-f", "bv+ba/b/bv"] if preserve_audio else ["-f", "bv[ext=mp4]/b[ext=mp4]"]
        command += ["-S", "res:1080,br,fps"]
    else:
        command += ["-f", "bv+ba/b/bv"] if preserve_audio else ["-f", "b[ext=mp4]/bv[ext=mp4]"]
    with subprocess.Popen(command + ["--", link.url], cwd=folder, env=env, creationflags=NO_WINDOW,
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace") as process:
        try:
            while True:
                _check_interrupt(cancel, deadline)
                _check_platform_budget(folder)
                try:
                    output, _ = process.communicate(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            _stop_download(process)
    _check_platform_budget(folder)
    if process.returncode == 0:
        if not preserve_audio and path.is_file():
            return path
        if preserve_audio:
            for candidate in (folder / f"{name}.mkv", folder / f"{name}.mp4", folder / f"{name}.webm"):
                if candidate.is_file() and candidate.stat().st_size > 0:
                    return candidate
    failure = output.lower()
    if "max-filesize" in failure:
        _check_download_budget(folder, MAX_IMPORT_BYTES + 1)
    if any(message in failure for message in ("confirm you", "http error 429", "login required", "login is required",
                                               "rate-limit reached", "log in", "login_required")):
        raise FrameCutterError(f"{platform_name} a bloqué cet import ou demande une connexion. "
                               "Réessayez plus tard ou ouvrez un fichier local.")
    if not youtube:
        raise FrameCutterError("Impossible d’importer ce Reel Instagram. Vérifiez qu’il est public et lisible sans connexion.")
    raise FrameCutterError("Impossible d’importer cette vidéo YouTube. Vérifiez que la vidéo est publique, "
                           "que le direct est terminé et que les bornes sont dans sa durée.")


def download_video(
    link: VideoLink, folder: Path, cancel: threading.Event, *, preserve_audio: bool = False,
) -> Path:
    deadline = time.monotonic() + IMPORT_TIMEOUT
    _check_interrupt(cancel, deadline)
    try:
        _check_download_budget(folder)
        path = (_download_platform(link, folder, cancel, deadline, preserve_audio=preserve_audio) if link.platform
                else _download_direct(link.url, folder, cancel, deadline))
    except HTTPError as exc:
        _check_interrupt(cancel, deadline)
        raise FrameCutterError(f"Le serveur refuse l’accès à la vidéo (HTTP {exc.code}).") from None
    except (URLError, TimeoutError):
        _check_interrupt(cancel, deadline)
        raise FrameCutterError("Le serveur vidéo est injoignable ou le téléchargement a expiré.") from None
    except (HTTPException, ConnectionError):
        _check_interrupt(cancel, deadline)
        raise FrameCutterError("Le téléchargement a été interrompu. Réessayez.") from None
    except OSError as exc:
        _check_interrupt(cancel, deadline)
        if exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) == 112:
            raise FrameCutterError("Espace disque insuffisant pour télécharger la vidéo.") from None
        raise FrameCutterError("Impossible de télécharger ou d’enregistrer la vidéo.") from None
    _check_interrupt(cancel, deadline)
    return path


def save_video_copy(source: Path, destination: Path, cancel: threading.Event) -> Path:
    """Write a durable copy of a validated video without exposing partial files."""
    deadline = time.monotonic() + IMPORT_TIMEOUT
    _check_interrupt(cancel, deadline)
    try:
        source = source.resolve(strict=True)
        destination = destination.resolve(strict=True)
        if not source.is_file() or not destination.is_dir():
            raise FrameCutterError("Le fichier téléchargé ou le dossier choisi est indisponible.")
        if destination == source.parent or destination.is_relative_to(source.parent):
            raise FrameCutterError("Choisissez un dossier en dehors de l’import temporaire.")
        size = source.stat().st_size
        if size <= 0:
            raise FrameCutterError("Le fichier téléchargé est vide.")
        if shutil.disk_usage(destination).free < size + MIN_FREE_BYTES:
            raise FrameCutterError("Espace disque insuffisant pour enregistrer la copie.")
    except FrameCutterError:
        raise
    except OSError:
        raise FrameCutterError("Impossible d’accéder au fichier ou au dossier d’enregistrement.") from None

    staging = None
    output = None
    for _ in range(10):
        candidate = destination / f".frame-cutter-{uuid4().hex}.part"
        try:
            output = candidate.open("xb")
        except FileExistsError:
            continue
        except OSError as exc:
            if exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) == 112:
                raise FrameCutterError("Espace disque insuffisant pour enregistrer la vidéo.") from None
            raise FrameCutterError("Impossible de créer le fichier dans le dossier choisi.") from None
        staging = candidate
        break
    if staging is None or output is None:
        raise FrameCutterError("Impossible de créer un fichier temporaire dans le dossier choisi.")

    try:
        copied = 0
        with output, source.open("rb") as video:
            while True:
                _check_interrupt(cancel, deadline)
                block = video.read(256 * 1024)
                if not block:
                    break
                output.write(block)
                copied += len(block)
            output.flush()
            os.fsync(output.fileno())
        if copied != size:
            raise FrameCutterError("La copie enregistrée est incomplète.")
        _check_interrupt(cancel, deadline)

        for number in range(10_000):
            suffix = "" if number == 0 else f" ({number + 1})"
            target = destination / f"{source.stem}{suffix}{source.suffix}"
            try:
                if os.name == "nt":
                    staging.rename(target)
                else:
                    os.link(staging, target)
                    staging.unlink()
            except FileExistsError:
                continue
            staging = None
            return target
        raise FrameCutterError("Impossible de trouver un nom de fichier disponible dans ce dossier.")
    except OperationCancelled:
        staging.unlink(missing_ok=True)
        raise
    except FrameCutterError:
        staging.unlink(missing_ok=True)
        raise
    except OSError as exc:
        staging.unlink(missing_ok=True)
        if exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) == 112:
            raise FrameCutterError("Espace disque insuffisant pour enregistrer la vidéo.") from None
        raise FrameCutterError("Impossible de copier la vidéo dans le dossier choisi.") from None