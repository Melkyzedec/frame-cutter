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
from urllib.parse import SplitResult, parse_qs, quote, urlsplit
from typing import BinaryIO
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, build_opener
from uuid import uuid4

from frame_engine import APP_DIR, FrameCutterError, OperationCancelled, find_executable, require_ffmpeg


IMPORT_TOOLS = APP_DIR / "tools" / "link-import"
# Must match the yt-dlp pin of requirements-import.txt (checked by the tests).
YT_DLP_VERSION = "2026.10.26"
_YT_DLP_VERSION_RE = re.compile(r"""^__version__\s*=\s*['"]([^'"\r\n]*)['"]\s*$""", re.M)
_SHOWN_VERSION_RE = re.compile(r"[0-9]{4}\.[0-9]{2}\.[0-9]{2}(?:\.[0-9A-Za-z]+)*")
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
NETWORK_FAILURE = "Connexion impossible ou interrompue. Vérifiez votre accès Internet puis réessayez."
ADVANCED_SETUP = "Suivez la section « Import depuis un lien (avancé) » du mode d’emploi, puis relancez Frame Cutter."
NODE_MISSING = ("L’import YouTube nécessite Node.js 22 ou plus récent. Installez-le depuis https://nodejs.org "
                "avec les options proposées, puis relancez Frame Cutter.")
# Lower-case fragments of yt-dlp output that mean the network, not the video, is at fault.
_NETWORK_FAILURE_MARKERS = (
    "unable to download webpage", "unable to download api page", "failed to resolve", "getaddrinfo failed",
    "name or service not known", "temporary failure in name resolution", "nodename nor servname",
    "no address associated with hostname", "connection refused", "connection reset", "connection aborted",
    "remote end closed connection", "network is unreachable", "no route to host", "timed out",
    "errno 11001", "errno 11002", "winerror 10051", "winerror 10053", "winerror 10054", "winerror 10060",
    "winerror 10061", "winerror 10065",
)

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


def _literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return the address written in the link, including legacy IPv4 forms (127.1, 0x7f.0.0.1, 2130706433)."""
    candidate = host.split("%", 1)[0]
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        pass
    if not re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)(?:\.(?:0[xX][0-9a-fA-F]+|[0-9]+)){0,3}", candidate):
        return None
    try:
        return ipaddress.IPv4Address(socket.inet_aton(candidate))
    except OSError:
        return None


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and not ip.is_reserved and not getattr(ip, "is_site_local", False)


def parse_video_link(text: str, portion: tuple[float, float] | None = None) -> VideoLink:
    text = text.strip()
    url = _https_url(text)
    host = (url.hostname or "").rstrip(".")
    literal = _literal_ip(host)
    if literal is not None and not _is_public_ip(literal):
        raise FrameCutterError("Ce lien ne mène pas à un serveur public. Ouvrez la vidéo comme fichier local.")
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
        # Percent-encode non-ASCII characters; existing escapes (%XX) are kept as they are.
        return VideoLink(url._replace(
            path=quote(url.path, safe="/%:@!$&'()*+,;=~"),
            query=quote(url.query, safe="=&%/:?@!$'()*+,;~"),
            fragment="",
        ).geturl())

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
        if not _is_public_ip(ipaddress.ip_address(destination[0].split("%", 1)[0])):
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

def _source_yt_dlp_version(package_dir: Path | None) -> str | None:
    """Read yt_dlp/version.py as text: nothing from the package is imported or run."""
    if package_dir is None:
        return None
    try:
        text = (package_dir / "version.py").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    match = _YT_DLP_VERSION_RE.search(text)
    return match.group(1) if match else None


def _frozen_yt_dlp_version(downloader: Path) -> str | None:
    try:
        completed = subprocess.run(
            [str(downloader), "--version"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15, creationflags=NO_WINDOW, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def _require_pinned_yt_dlp(found: str | None) -> None:
    if found == YT_DLP_VERSION:
        return
    shown = found if found and _SHOWN_VERSION_RE.fullmatch(found) else "inconnue"
    raise FrameCutterError(f"Le composant d’import des liens YouTube et Instagram est en version {shown}, alors que "
                           f"la version attendue ({YT_DLP_VERSION}) est requise. Réinstallez-le. {ADVANCED_SETUP}")


def _platform_downloader(youtube: bool) -> tuple[list[str], dict[str, str]]:
    """Return the pinned yt-dlp command and its environment, or explain what is missing."""
    if sys.version_info < (3, 11):
        raise FrameCutterError("L’import YouTube et Instagram nécessite Python 3.11 ou plus récent. "
                               "Installez une version récente de Python, puis relancez Frame Cutter.")
    env = os.environ.copy()
    if getattr(sys, "frozen", False):
        downloader = IMPORT_TOOLS / "yt-dlp.exe"
        if not downloader.is_file():
            raise FrameCutterError("Le composant d’import est absent. Réextrayez l’archive de Frame Cutter.")
        _require_pinned_yt_dlp(_frozen_yt_dlp_version(downloader))
        return [str(downloader)], env
    modules = ("yt_dlp", "yt_dlp_ejs") if youtube else ("yt_dlp",)
    if all((IMPORT_TOOLS / name / "__init__.py").is_file() for name in modules):
        env["PYTHONPATH"] = str(IMPORT_TOOLS)
        package_dir = IMPORT_TOOLS / "yt_dlp"
    elif not all(find_spec(name) is not None for name in modules):
        raise FrameCutterError("Le composant d’import des liens YouTube et Instagram n’est pas installé. "
                               + ADVANCED_SETUP)
    else:
        locations = list(getattr(find_spec("yt_dlp"), "submodule_search_locations", None) or [])
        package_dir = Path(locations[0]) if locations else None
    _require_pinned_yt_dlp(_source_yt_dlp_version(package_dir))
    # -P: the import folder used as cwd must not be added to sys.path.
    return [sys.executable, "-B", "-P", "-m", "yt_dlp"], env


def _node_runtime() -> str | None:
    bundled_node = IMPORT_TOOLS / "node.exe"
    return str(bundled_node) if bundled_node.is_file() else find_executable("node")


def platform_problems() -> dict[str, str | None]:
    """Say, for each platform, why its links cannot be imported (None: usable).

    Only files are inspected (and the bundled downloader asked for its version in the frozen build):
    nothing is downloaded and no link is opened.
    """
    checked: dict[bool | None, str | None] = {}

    def downloader_problem(youtube: bool) -> str | None:
        # The frozen build has a single downloader for both platforms: ask its version only once.
        key = None if getattr(sys, "frozen", False) else youtube
        if key not in checked:
            try:
                _platform_downloader(youtube)
                checked[key] = None
            except FrameCutterError as exc:
                checked[key] = str(exc)
        return checked[key]

    youtube = downloader_problem(True)
    if youtube is None and _node_runtime() is None:
        youtube = NODE_MISSING
    return {"youtube": youtube, "instagram": downloader_problem(False)}


def is_youtube_link(text: str) -> bool:
    """True when the text looks like a YouTube address (the link itself is checked on import)."""
    try:
        host = (urlsplit(text.strip()).hostname or "").rstrip(".").lower()
    except ValueError:
        return False
    return host in YOUTUBE_HOSTS


def _is_network_failure(output: str) -> bool:
    if "http error" in output:
        return False
    return any(marker in output for marker in _NETWORK_FAILURE_MARKERS)


def _download_platform(
    link: VideoLink, folder: Path, cancel: threading.Event, deadline: float, *, preserve_audio: bool = False,
) -> Path:
    youtube = link.platform == "youtube"
    platform_name = "YouTube" if youtube else "Instagram"
    command, env = _platform_downloader(youtube)
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
        node = _node_runtime()
        if node is None:
            raise FrameCutterError(NODE_MISSING)
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
    if _is_network_failure(failure):
        raise FrameCutterError(NETWORK_FAILURE)
    if not youtube:
        raise FrameCutterError("Impossible d’importer ce Reel Instagram. Vérifiez qu’il est public et lisible sans connexion.")
    raise FrameCutterError("Impossible d’importer cette vidéo YouTube. Vérifiez que la vidéo est publique, "
                           "que le direct est terminé et que les bornes sont dans sa durée.")


def _refusal_message(code: int) -> str:
    if code in (404, 410):
        return "Cette vidéo est introuvable à cette adresse. Vérifiez le lien dans votre navigateur, puis réessayez."
    if code in (401, 403, 407):
        return ("Le site refuse de fournir cette vidéo : elle est privée ou protégée. "
                "Vérifiez le lien, ou ouvrez une copie enregistrée sur votre PC.")
    if code == 429:
        return "Le site limite les téléchargements pour le moment. Réessayez dans quelques minutes."
    return "Le site de la vidéo ne répond pas correctement pour le moment. Réessayez plus tard."


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
        raise FrameCutterError(_refusal_message(exc.code)) from None
    except UnicodeError:
        _check_interrupt(cancel, deadline)
        raise FrameCutterError("L’adresse du lien contient des caractères non pris en charge. "
                               "Vérifiez le lien ou ouvrez la vidéo comme fichier local.") from None
    except (URLError, TimeoutError):
        _check_interrupt(cancel, deadline)
        raise FrameCutterError(NETWORK_FAILURE) from None
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