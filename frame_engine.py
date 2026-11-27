"""Local video probing and image extraction backed by FFmpeg."""

from __future__ import annotations

import errno
import json
import logging
import math
import os
import re
import subprocess
import threading
import traceback
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable, Iterator, Sequence


APP_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger("frame_cutter")
FFMPEG_DIR = APP_DIR / "tools" / "ffmpeg" / "bin"
_PREVIEW_DECODE_LOCK = threading.Lock()
_FFMPEG_LOG_PREFIX_RE = re.compile(r"^\s*\[[^\]]+@\s*(?:0x)?[0-9a-fA-F]+\]")
_FFMPEG_NOISE_PREFIXES = (
    "error opening input",
    "ffmpeg version",
    "built with",
    "configuration:",
    "libav",
)
_FILE_URI_RE = re.compile(r"""file://[^\s'"<>|\r\n]+""", re.IGNORECASE)
_QUOTED_SYSTEM_PATH_RE = re.compile(
    r"""(['"])(?:(?<![A-Za-z0-9+.-])[A-Za-z]:(?!//)[\\/]|\\\\|/(?:Users|home|root|tmp|var|etc|opt|usr|mnt|media|Volumes|private)/)[^'"\r\n]+\1"""
)
# A path may contain spaces: it runs until a quote or guillemet, a ": " separator or the end of the line.
_PATH_BODY = r"""[^\r\n'"<>|«»]*?(?=\s*[«»'"<>|]|\s+:\s|:\s|\s*$)"""
_WIN_SYSTEM_PATH_RE = re.compile(
    r"""(?<![A-Za-z0-9+.-])(?:[A-Za-z]:(?!//)[\\/]|\\\\)""" + _PATH_BODY
)
_RELATIVE_PATH_RE = re.compile(r"""(?<![\w.~%\\/-])(?:\.{1,2}|~)[\\/]""" + _PATH_BODY)
# A bare relative Windows path ("dossier\video.mp4", "%USERPROFILE%\..."). The application's own
# "tools\..." folder, quoted in its help messages, is kept.
_BACKSLASH_PATH_RE = re.compile(r"""(?<![\w.~%\\/-])(?!tools\\)[^\s'"<>|«»\\/:]+\\""" + _PATH_BODY)
_UNIX_SYSTEM_PATH_RE = re.compile(
    r"""(?<![:/\w.~-])/(?:(?:Users|home|root|tmp|var|etc|opt|usr|mnt|media|Volumes|private)/|(?:[\w.-]+/)+[\w.-])"""
    + _PATH_BODY
)
_UNIX_SINGLE_FILE_RE = re.compile(r"""(?<![:/\w.~-])/[\w-][\w.-]*\.[A-Za-z0-9]{1,8}\b""")
# Containers FFmpeg may open: the video formats offered by the file dialog, and the exported images.
# Playlists and indirections (hls, concat, tty, lavfi…) are refused because they can read other files.
FORMAT_WHITELIST = "mov,matroska,webm,avi,mpeg,mpegts,asf,image2,png_pipe,jpeg_pipe"
_FFMPEG_ERROR_TABLE = (
    (
        "no space left on device",
        {
            "probe": "Espace disque insuffisant pour analyser cette vidéo.",
            "extract": "Espace disque insuffisant. Libérez de la place ou choisissez un autre dossier.",
        },
    ),
    (
        "format not on whitelist",
        {
            "probe": "Impossible de lire cette vidéo : format de fichier non pris en charge. "
                     "Choisissez une vidéo MP4, MOV, MKV, WebM ou AVI.",
            "extract": "L’extraction a échoué : format de fichier non pris en charge. "
                       "Choisissez une vidéo MP4, MOV, MKV, WebM ou AVI.",
        },
    ),
    (
        "not on whitelist",
        {
            "probe": "Impossible de lire cette vidéo : le fichier renvoie vers une source externe non prise en charge, "
                     "bloquée par sécurité. Ouvrez directement le fichier vidéo.",
            "extract": "L’extraction a échoué : le fichier renvoie vers une source externe non prise en charge, "
                       "bloquée par sécurité. Ouvrez directement le fichier vidéo.",
        },
    ),
    (
        "permission denied",
        {
            "probe": "Impossible de lire cette vidéo : accès au fichier refusé. "
                     "Copiez la vidéo dans un dossier personnel, par exemple Vidéos, puis réessayez.",
            "extract": "L’extraction a échoué : accès au fichier ou au dossier refusé. "
                       "Choisissez un autre dossier de sortie.",
        },
    ),
)
_FFMPEG_DEFAULT_MESSAGES = {
    "probe": "Impossible de lire cette vidéo. Vérifiez que le fichier est une vidéo valide.",
    "extract": "L’extraction a échoué. Vérifiez la vidéo et l’accès au dossier de destination.",
}


def sanitize_user_error(message: str, fallback: str = "Une erreur technique est survenue.") -> str:
    """Strip local filesystem paths and raw FFmpeg log lines from user-facing messages."""
    kept_lines: list[str] = []
    for raw_line in message.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if _FFMPEG_LOG_PREFIX_RE.match(stripped) or stripped.lower().startswith(_FFMPEG_NOISE_PREFIXES):
            continue
        cleaned = _FILE_URI_RE.sub("ce chemin", raw_line)
        cleaned = _QUOTED_SYSTEM_PATH_RE.sub("ce chemin", cleaned)
        cleaned = _WIN_SYSTEM_PATH_RE.sub("ce chemin", cleaned)
        cleaned = _RELATIVE_PATH_RE.sub("ce chemin", cleaned)
        cleaned = _BACKSLASH_PATH_RE.sub("ce chemin", cleaned)
        cleaned = _UNIX_SYSTEM_PATH_RE.sub("ce chemin", cleaned)
        cleaned = _UNIX_SINGLE_FILE_RE.sub("ce chemin", cleaned)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
        if cleaned and re.search(r"[A-Za-z0-9À-ÿ]", cleaned):
            kept_lines.append(cleaned)
    return "\n".join(kept_lines) if kept_lines else fallback


def describe_exception(exc: BaseException) -> str:
    """Summarize an exception for the log without absolute paths or raw tracebacks."""
    frames = traceback.extract_tb(exc.__traceback__)[-6:]
    where = " > ".join(f"{Path(frame.filename).name}:{frame.name}:{frame.lineno}" for frame in frames)
    detail = sanitize_user_error(str(exc), fallback="")
    summary = type(exc).__name__ + (f": {detail}" if detail else "")
    return f"{summary} [{where}]" if where else summary


def log_unexpected_error(context: str, exc: BaseException) -> None:
    """Record an unexpected failure. The user only sees a generic message."""
    LOGGER.error("Erreur inattendue (%s) : %s", context, describe_exception(exc))


def format_os_error(exc: OSError, fallback: str) -> str:
    """Format an OSError without exposing raw OS details or filesystem paths."""
    if exc.errno is not None or getattr(exc, "winerror", None) is not None:
        return fallback
    return sanitize_user_error(str(exc), fallback=fallback)


def _classify_ffmpeg_error(stderr: str, *, context: str) -> str:
    """Map raw FFmpeg/ffprobe stderr output to a safe French user error message."""
    lowered = stderr.lower()
    for token, messages in _FFMPEG_ERROR_TABLE:
        if token in lowered:
            return messages[context]
    return _FFMPEG_DEFAULT_MESSAGES[context]


class FrameCutterError(RuntimeError):
    """An error that can be shown directly to the user."""

    def __init__(self, message: str, output_dir: Path | None = None) -> None:
        super().__init__(message)
        self.output_dir = output_dir


class OperationCancelled(FrameCutterError):
    """An operation stopped at the user's request."""


@dataclass(frozen=True)
class VideoInfo:
    duration: float
    width: int | None
    height: int | None
    frame_rate: str | None


def find_executable(name: str) -> str | None:
    """Search the absolute PATH entries only, never the current directory.

    shutil.which() also looks in the current directory on Windows and follows relative PATH
    entries, so a planted ffmpeg.exe or node.exe could be run instead of the real one.
    """
    filename = f"{name}.exe" if os.name == "nt" and not name.lower().endswith(".exe") else name
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or not os.path.isabs(entry):
            continue
        candidate = Path(entry) / filename
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _find_binary(name: str) -> str | None:
    suffix = ".exe" if os.name == "nt" else ""
    local = FFMPEG_DIR / f"{name}{suffix}"
    if local.is_file():
        return str(local)
    return find_executable(name)


VIDEO_COMPONENT_MISSING = (
    "Le composant vidéo est absent : Frame Cutter ne peut pas lire de vidéo.\n\n"
    "Téléchargez FFmpeg gratuitement sur https://ffmpeg.org/download.html et copiez tout le contenu "
    "de son dossier « bin » dans le dossier tools\\ffmpeg\\bin de Frame Cutter, puis relancez l’application."
)


def require_ffmpeg() -> tuple[str, str]:
    ffmpeg = _find_binary("ffmpeg")
    ffprobe = _find_binary("ffprobe")
    if ffmpeg and ffprobe:
        return ffmpeg, ffprobe
    raise FrameCutterError(VIDEO_COMPONENT_MISSING)


@contextmanager
def _video_process(
    command: list[str], cancel_event: threading.Event | None, *, binary_output: bool = False,
) -> Iterator[subprocess.Popen]:
    with subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=not binary_output, encoding=None if binary_output else "utf-8",
        errors=None if binary_output else "replace", bufsize=-1 if binary_output else 1,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ) as process:
        finished = threading.Event()
        watcher = None
        if cancel_event is not None:
            def stop_on_cancel() -> None:
                # Progress and metadata reads can block while the child is silent.
                while not finished.wait(0.05):
                    if cancel_event.is_set():
                        process.kill()
                        return

            watcher = threading.Thread(target=stop_on_cancel)
            watcher.start()
        try:
            yield process
        finally:
            finished.set()
            if watcher is not None:
                watcher.join()
            if process.poll() is None:
                process.kill()


def probe_video(path: Path, cancel_event: threading.Event | None = None) -> VideoInfo:
    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelled("Analyse annulée.")
    _, ffprobe = require_ffmpeg()
    if not path.is_file():
        raise FrameCutterError("Le fichier vidéo sélectionné n’existe plus.")
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise FrameCutterError("Impossible d’accéder au fichier vidéo sélectionné.") from exc

    command = [
        ffprobe,
        "-v", "error",
        "-protocol_whitelist", "file", "-format_whitelist", FORMAT_WHITELIST,
        "-show_entries", "format=duration:stream=codec_type,width,height,avg_frame_rate,r_frame_rate",
        "-of", "json",
        "-i", str(path),
    ]
    try:
        with _video_process(command, cancel_event) as process:
            stdout, stderr = process.communicate(timeout=30)
            return_code = process.returncode
    except subprocess.TimeoutExpired as exc:
        raise FrameCutterError("La lecture des informations de la vidéo a dépassé 30 secondes.") from exc
    except OSError as exc:
        raise FrameCutterError("Impossible de lancer l’outil d’analyse vidéo. "
                               "Réinstallez le composant vidéo, puis réessayez.") from exc

    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelled("Analyse annulée.")
    if return_code != 0:
        raise FrameCutterError(_classify_ffmpeg_error(stderr, context="probe"))

    try:
        metadata = json.loads(stdout)
        duration = float(metadata.get("format", {}).get("duration", 0))
        stream = next(item for item in metadata.get("streams", []) if item.get("codec_type") == "video")
    except (ValueError, TypeError, json.JSONDecodeError, StopIteration) as exc:
        raise FrameCutterError("La vidéo ne fournit pas de durée ou de piste image exploitable.") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise FrameCutterError("La durée de cette vidéo est inconnue ou nulle.")
    rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    if rate in (None, "0/0", "N/A"):
        rate = None
    return VideoInfo(
        duration=duration,
        width=_optional_int(stream.get("width")),
        height=_optional_int(stream.get("height")),
        frame_rate=str(rate) if rate else None,
    )


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def read_preview_frame(
    path: Path, seconds: float, width: int, height: int, cancel_event: threading.Event,
) -> bytes:
    if cancel_event.is_set():
        raise OperationCancelled("Aperçu annulé.")
    if not path.is_file() or not math.isfinite(seconds) or seconds < 0:
        raise FrameCutterError("La vidéo ou la position de l’aperçu est invalide.")
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise FrameCutterError("Impossible d’accéder au fichier vidéo sélectionné.") from exc
    ffmpeg, _ = require_ffmpeg()
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-protocol_whitelist", "file", "-format_whitelist", FORMAT_WHITELIST,
    ]
    # An image file has no seekable timeline, even for a seek to zero.
    if seconds:
        command += ["-ss", f"{seconds:.6f}"]
    command += [
        "-i", str(path),
        "-map", "0:v:0", "-frames:v", "1", "-an", "-sn", "-dn",
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,setsar=1",
        "-c:v", "png", "-f", "image2pipe", "pipe:1",
    ]
    try:
        with _PREVIEW_DECODE_LOCK:
            if cancel_event.is_set():
                raise OperationCancelled("Aperçu annulé.")
            with _video_process(command, cancel_event, binary_output=True) as process:
                image, _ = process.communicate(timeout=10)
                return_code = process.returncode
    except subprocess.TimeoutExpired as exc:
        raise FrameCutterError("La lecture de l’aperçu a dépassé 10 secondes.") from exc
    except OSError as exc:
        raise FrameCutterError("Impossible de lancer l’aperçu vidéo. "
                               "Réinstallez le composant vidéo, puis rouvrez la vidéo.") from exc
    if cancel_event.is_set():
        raise OperationCancelled("Aperçu annulé.")
    if return_code != 0 or not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise FrameCutterError("Aucune image disponible à cette position.")
    return image


def format_frame_rate(rate: str | None) -> str | None:
    """Turn a raw rate ("25/1", "30000/1001") into "25 images/s" or "29,97 images/s"."""
    if not rate or len(rate) > 32:
        return None
    try:
        value = Fraction(rate.strip())
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    if not 0 < value <= 10_000:
        return None
    number = f"{float(value):.2f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{number} images/s"


def format_duration(seconds: float) -> str:
    if not math.isfinite(seconds) or seconds <= 0:
        return "00:00"
    whole = max(0, int(seconds + 0.5))
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def parse_position(text: str) -> float:
    text = text.strip().replace(",", ".")
    if not re.fullmatch(r"(?:[0-9]+:){0,2}[0-9]+(?:\.[0-9]{1,6})?", text):
        raise ValueError(text)
    parts = text.split(":")
    seconds = float(parts[-1])
    if len(parts) > 1:
        minutes = int(parts[-2])
        if seconds >= 60 or (len(parts) == 3 and minutes >= 60):
            raise ValueError(text)
        seconds += 60 * minutes
    if len(parts) == 3:
        seconds += 3600 * int(parts[0])
    if not math.isfinite(seconds):
        raise ValueError(text)
    return seconds


def validate_portion(duration: float, start: float, end: float | None) -> tuple[float, float]:
    if not math.isfinite(duration) or duration <= 0:
        raise FrameCutterError("La durée de la vidéo est inconnue ou nulle.")
    end = duration if end is None else end
    if not 0 <= start < end <= duration:
        raise FrameCutterError("Choisissez un début avant la fin, dans la durée de la vidéo.")
    return start, end


def validate_portions(duration: float, portions: Sequence[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    if not portions:
        raise FrameCutterError("Ajoutez au moins un passage à extraire.")
    ordered = tuple(sorted(validate_portion(duration, start, end) for start, end in portions))
    for (_, previous_end), (start, _) in zip(ordered, ordered[1:]):
        if start < previous_end:
            raise FrameCutterError("Ce passage chevauche un passage déjà ajouté. Ajustez les bornes.")
    return ordered


def _available_output_dir(parent: Path, stem: str) -> Path:
    # Separators are refused, so ".." can only be a whole name; "mon..clip" stays valid.
    if not stem or stem in {".", ".."} or "/" in stem or "\\" in stem or "\x00" in stem:
        raise FrameCutterError("Nom de dossier de résultat invalide.")
    resolved_parent = parent.resolve()
    base = resolved_parent / f"{stem}_frames"
    candidate = base
    suffix = 2
    while True:
        if candidate.resolve().parent != resolved_parent:
            raise FrameCutterError("Nom de dossier de résultat invalide.")
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            candidate = resolved_parent / f"{stem}_frames_{suffix}"
            suffix += 1
        except OSError as exc:
            if exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) == 112:
                raise FrameCutterError("Espace disque insuffisant. Libérez de la place ou choisissez un autre dossier.") from exc
            raise FrameCutterError("Impossible de créer le dossier de résultat. Choisissez un dossier accessible.") from exc


def extract_frames(
    source: Path,
    destination_parent: Path,
    fps: float,
    image_format: str,
    duration: float,
    progress: Callable[[float], None],
    cancel_event: threading.Event,
    *,
    start: float = 0.0,
    end: float | None = None,
    portions: Sequence[tuple[float, float]] | None = None,
    interval_seconds: float | None = None,
) -> Path:
    if cancel_event.is_set():
        raise OperationCancelled("Extraction annulée.")
    ffmpeg, _ = require_ffmpeg()
    if interval_seconds is None:
        if not 1 <= fps <= 60:
            raise FrameCutterError("La cadence doit être comprise entre 1 et 60 images par seconde.")
    elif not math.isfinite(interval_seconds) or not 1 <= interval_seconds <= 3600:
        raise FrameCutterError("L’intervalle doit être compris entre 1 et 3 600 secondes.")
    if image_format not in {"JPEG", "PNG"}:
        raise FrameCutterError("Format de sortie non pris en charge.")
    if portions is None:
        portions = (validate_portion(duration, start, end),)
    else:
        portions = validate_portions(duration, portions)
    if not source.is_file():
        raise FrameCutterError("Le fichier vidéo sélectionné n’existe plus.")
    if not destination_parent.is_dir():
        raise FrameCutterError("Le dossier de destination n’existe pas ou n’est pas accessible. "
                               "Choisissez un autre dossier.")
    try:
        source = source.resolve(strict=True)
        destination_parent = destination_parent.resolve(strict=True)
    except OSError as exc:
        raise FrameCutterError("Impossible d’accéder au fichier vidéo ou au dossier de destination.") from exc

    output_dir = _available_output_dir(destination_parent, source.stem)
    extension = "jpg" if image_format == "JPEG" else "png"
    total_duration = sum(end - start for start, end in portions)
    elapsed = 0.0
    next_frame = 1
    for index, (start, end) in enumerate(portions, 1):
        try:
            next_frame = _extract_portion(
                ffmpeg, source, output_dir, fps, extension, duration, start, end, next_frame,
                lambda seconds: progress(min(99.0, (elapsed + seconds) / total_duration * 100)),
                cancel_event, interval_seconds=interval_seconds,
            )
        except OperationCancelled:
            if next_frame == 1 and output_dir.is_dir() and not any(output_dir.iterdir()):
                try:
                    output_dir.rmdir()
                except OSError:
                    pass
            raise
        except FrameCutterError as exc:
            if next_frame == 1 and output_dir.is_dir() and not any(output_dir.iterdir()):
                try:
                    output_dir.rmdir()
                except OSError:
                    pass
                if exc.output_dir == output_dir:
                    exc = FrameCutterError(str(exc), None)
            if len(portions) == 1:
                raise exc
            raise FrameCutterError(f"Passage {index} sur {len(portions)}. {exc}", output_dir) from exc
        elapsed += end - start
    progress(100.0)
    return output_dir


def _extract_portion(
    ffmpeg: str, source: Path, output_dir: Path, fps: float, extension: str, duration: float,
    start: float, end: float, first_frame: int, progress_seconds: Callable[[float], None],
    cancel_event: threading.Event, *, interval_seconds: float | None = None,
) -> int:
    if cancel_event.is_set():
        raise OperationCancelled("Extraction annulée. Les images déjà créées sont conservées.", output_dir)
    portion_duration = end - start
    # FFmpeg interprets percent signs across the whole output pattern.
    escaped_output_dir = str(output_dir).replace("%", "%%")
    filename_pattern = f"{escaped_output_dir}{os.sep}frame_%06d.{extension}"
    command = [
        ffmpeg,
        "-hide_banner", "-loglevel", "error", "-nostdin", "-nostats", "-progress", "pipe:1", "-n",
        "-protocol_whitelist", "file", "-format_whitelist", FORMAT_WHITELIST,
    ]
    if start:
        command += ["-ss", f"{start:.6f}"]
    command += ["-i", str(source), "-map", "0:v:0", "-an", "-sn", "-dn"]
    filters = (
        f"fps=1/{interval_seconds:g}:start_time=0:round=up"
        if interval_seconds is not None else f"fps={fps:.6g}"
    )
    if start or end < duration:
        # Exclude out-of-range source frames before fps can select one of them.
        filters = f"trim=start=0:end={portion_duration:.6f},setpts=PTS-STARTPTS,{filters}"
    command += ["-vf", filters]
    if extension == "jpg":
        command += ["-c:v", "mjpeg", "-q:v", "2"]
    else:
        command += ["-c:v", "png", "-compression_level", "3"]
    command += ["-start_number", str(first_frame), "-f", "image2", filename_pattern]

    stderr_lines: deque[str] = deque(maxlen=80)
    try:
        with _video_process(command, cancel_event) as process:
            def drain_stderr() -> None:
                assert process.stderr is not None
                for line in process.stderr:
                    stderr_lines.append(line.rstrip())

            stderr_thread = threading.Thread(target=drain_stderr)
            stderr_thread.start()
            current: dict[str, str] = {}
            frame_count = 0
            assert process.stdout is not None
            for line in process.stdout:
                key, sep, value = line.strip().partition("=")
                if not sep:
                    continue
                current[key] = value
                if key == "progress":
                    progress_seconds(min(portion_duration, _progress_seconds(current)))
                    frame_value = current.get("frame", "")
                    # FFmpeg may report "N/A" before the first frame: keep the last known count.
                    if frame_value.isascii() and frame_value.isdecimal():
                        frame_count = int(frame_value)
                    current.clear()
            return_code = process.wait()
            stderr_thread.join()
    except OSError as exc:
        raise FrameCutterError("Impossible de lancer le composant vidéo pour l’extraction. "
                               "Réinstallez-le, puis réessayez.", output_dir) from exc

    if cancel_event.is_set():
        raise OperationCancelled("Extraction annulée. Les images déjà créées sont conservées.", output_dir)
    if return_code != 0:
        detail = "\n".join(stderr_lines).strip()
        lowered = detail.lower()
        if frame_count == 0 and "could not open encoder before eof" in lowered and "received no packets" in lowered:
            raise FrameCutterError("Aucune image exportée. Ajustez la portion ou le rythme.", output_dir)
        raise FrameCutterError(_classify_ffmpeg_error(detail, context="extract"), output_dir)
    if frame_count == 0 or not (output_dir / f"frame_{first_frame + frame_count - 1:06d}.{extension}").is_file():
        raise FrameCutterError("Aucune image exportée. Ajustez la portion ou le rythme.", output_dir)
    return first_frame + frame_count


def _progress_seconds(progress_data: dict[str, str]) -> float:
    value = progress_data.get("out_time")
    if not value:
        return 0.0
    match = re.fullmatch(r"(\d+):(\d+):(\d+(?:\.\d+)?)", value)
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
