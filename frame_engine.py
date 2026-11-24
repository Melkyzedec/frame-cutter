"""Local video probing and image extraction backed by FFmpeg."""

from __future__ import annotations

import errno
import json
import math
import os
import re
import shutil
import subprocess
import threading
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Sequence


APP_DIR = Path(__file__).resolve().parent
FFMPEG_DIR = APP_DIR / "tools" / "ffmpeg" / "bin"
_PREVIEW_DECODE_LOCK = threading.Lock()


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


def _find_binary(name: str) -> str | None:
    suffix = ".exe" if os.name == "nt" else ""
    local = FFMPEG_DIR / f"{name}{suffix}"
    if local.is_file():
        return str(local)
    return shutil.which(name) or shutil.which(f"{name}{suffix}")


def require_ffmpeg() -> tuple[str, str]:
    ffmpeg = _find_binary("ffmpeg")
    ffprobe = _find_binary("ffprobe")
    if ffmpeg and ffprobe:
        return ffmpeg, ffprobe
    missing = []
    if not ffmpeg:
        missing.append("ffmpeg.exe")
    if not ffprobe:
        missing.append("ffprobe.exe")
    raise FrameCutterError(
        "Le moteur vidéo FFmpeg est introuvable (" + ", ".join(missing) + ").\n\n"
        "Ajoutez ffmpeg.exe et ffprobe.exe dans le dossier tools\\ffmpeg\\bin "
        "à côté de l’application, ou installez FFmpeg et ajoutez-le au PATH. "
        "Téléchargement gratuit : https://ffmpeg.org/download.html"
    )


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
    path = path.resolve(strict=True)

    command = [
        ffprobe,
        "-v", "error",
        "-protocol_whitelist", "file",
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
        raise FrameCutterError(f"Impossible de lancer ffprobe : {exc}") from exc

    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelled("Analyse annulée.")
    if return_code != 0:
        detail = stderr.strip() or "Format vidéo non reconnu."
        raise FrameCutterError(f"Impossible de lire cette vidéo.\n{detail[-1200:]}")

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
    path = path.resolve(strict=True)
    ffmpeg, _ = require_ffmpeg()
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-protocol_whitelist", "file",
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
        raise FrameCutterError("Impossible de lancer l’aperçu vidéo.") from exc
    if cancel_event.is_set():
        raise OperationCancelled("Aperçu annulé.")
    if return_code != 0 or not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise FrameCutterError("Aucune image disponible à cette position.")
    return image


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
    if not stem or stem in {".", ".."} or "/" in stem or "\\" in stem or ".." in stem or "\x00" in stem:
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
            raise FrameCutterError(f"Impossible de créer le dossier de résultat. Choisissez un dossier accessible.\n{exc}") from exc


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
        raise FrameCutterError("Le dossier de destination n’existe pas ou n’est pas accessible.")
    source = source.resolve(strict=True)
    destination_parent = destination_parent.resolve(strict=True)

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
        "-protocol_whitelist", "file",
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
                    frame_count = int(current.get("frame", "0"))
                    current.clear()
            return_code = process.wait()
            stderr_thread.join()
    except OSError as exc:
        raise FrameCutterError(f"Impossible d’exécuter FFmpeg.\n{exc}", output_dir) from exc

    if cancel_event.is_set():
        raise OperationCancelled("Extraction annulée. Les images déjà créées sont conservées.", output_dir)
    if return_code != 0:
        detail = "\n".join(stderr_lines).strip() or "Erreur FFmpeg sans détail supplémentaire."
        if "no space left on device" in detail.lower():
            message = "Espace disque insuffisant. Libérez de la place ou choisissez un autre dossier."
        else:
            message = "L’extraction a échoué. Vérifiez la vidéo et l’accès au dossier de destination."
        raise FrameCutterError(f"{message}\n{detail[-1600:]}", output_dir)
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
