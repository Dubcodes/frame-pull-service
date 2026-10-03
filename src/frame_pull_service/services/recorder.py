"""Durable FFmpeg recorder boundary for normal Frame Pull source chunks."""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import RecordingSession
from .operations import get_value


ACTIVE_SESSION_STATES = ("recording", "stopping", "finalizing", "uncertain")
CHUNK_SEQUENCE = re.compile(r"_(\d+)\.ts$", re.I)
URL_PATTERN = re.compile(r"https?://[^\s'\"]+")


def redact_input(value: str) -> str:
    """Expose configuration state without retaining endpoint details."""
    return "Configured" if value else "Not configured"


@dataclass(frozen=True)
class RecorderState:
    session_id: int | None = None
    state: str = "idle"
    active_path: Path | None = None
    active_chunk_sequence: int | None = None
    started_at: datetime | None = None
    planned_end_at: datetime | None = None
    pid: int | None = None
    health: str = "unconfigured"
    error: str | None = None
    input_state: str = "Not configured"
    free_space_gb: float | None = None
    stop_after_chunk: bool = False

    @property
    def running(self) -> bool:
        return self.state in {"recording", "stopping", "finalizing"}

    def public(self) -> dict:
        result = asdict(self)
        result["active_path"] = self.active_path.name if self.active_path else None
        result["running"] = self.running
        return result


class RecorderAdapter:
    """Stable recorder contract. Implementations must never expose input secrets."""
    name = "external"

    def status(self, _session: Session | None = None) -> RecorderState:
        return RecorderState(health="unconfigured")

    def start(self, _session: Session, _race_day=None, _planned_end=None, *, manual: bool = True) -> RecorderState:
        raise RuntimeError("No recorder input is configured")

    def stop_after_current_chunk(self, _session: Session) -> RecorderState:
        return self.status(_session)

    def stop(self, _session: Session, *, hard: bool = False) -> RecorderState:
        return self.status(_session)

    def current_output(self, session: Session | None = None) -> Path | None:
        return self.status(session).active_path

    def health(self, session: Session | None = None) -> str:
        return self.status(session).health


class FFmpegRecorder(RecorderAdapter):
    """One long-lived FFmpeg segmenter, writing parser-compatible Trackside chunks."""
    name = "ffmpeg"

    def __init__(self, settings: Settings, popen=subprocess.Popen, disk_usage=shutil.disk_usage):
        self.settings = settings
        self._popen = popen
        self._disk_usage = disk_usage
        self._children: dict[int, subprocess.Popen] = {}
        self._log_threads: dict[int, threading.Thread] = {}

    def _session(self, session: Session) -> RecordingSession | None:
        return session.execute(
            select(RecordingSession)
            .where(RecordingSession.status.in_(ACTIVE_SESSION_STATES))
            .order_by(RecordingSession.id.desc())
        ).scalars().first()

    def _free_space_gb(self) -> float | None:
        try:
            return round(self._disk_usage(self.settings.resolved_recorder_output_dir).free / 1024 ** 3, 2)
        except OSError:
            return None

    def _ffmpeg_available(self) -> bool:
        executable = self.settings.recorder_ffmpeg_path
        return bool(Path(executable).exists() if Path(executable).is_absolute() else shutil.which(executable))

    def _configured(self, session: Session) -> tuple[bool, str]:
        if not bool(get_value(session, "recorder_enabled", self.settings.recorder_enabled)):
            return False, "Recorder is disabled."
        if not self.settings.recorder_input:
            return False, "Recorder input is not configured."
        profile_error = self._profile_error()
        if profile_error:
            return False, profile_error
        if not self._ffmpeg_available():
            return False, "FFmpeg executable is unavailable."
        free = self._free_space_gb()
        if free is None:
            return False, "Recorder output filesystem is unavailable."
        minimum = float(get_value(session, "recorder_min_free_space_gb", self.settings.recorder_min_free_space_gb))
        if free < minimum:
            return False, f"Free disk space is below the {minimum:g} GB recorder threshold."
        return True, ""

    def _chunk_pattern(self) -> Path:
        # The historical parser accepts the optional numeric suffix. Using the
        # actual seconds avoids FFmpeg's incompatible strftime/index expansion
        # while retaining an accurate chunk-start timestamp and unique rolls.
        return self.settings.resolved_recorder_output_dir / "trackside_%Y%m%d-%H%M_%S.ts"

    def _profile_error(self) -> str | None:
        profile = self.settings.recorder_profile.strip().lower()
        if profile == "generic":
            return None
        if profile != "trackside_hls":
            return f"Unsupported recorder profile: {profile or 'unset'}."
        if not self.settings.recorder_origin or not self.settings.recorder_referer:
            return "Trackside HLS requires configured Origin and Referer headers."
        if self.settings.recorder_program is None or self.settings.recorder_program < 0:
            return "Trackside HLS requires an explicit non-negative program number."
        if self.settings.recorder_reconnect_max_retries < 0 or self.settings.recorder_reconnect_delay_total_max < 0:
            return "Trackside HLS reconnect limits must be non-negative."
        return None

    def _trackside_headers(self) -> str:
        """Return FFmpeg's required CRLF-terminated HTTP header block."""
        return f"Origin: {self.settings.recorder_origin}\r\nReferer: {self.settings.recorder_referer}\r\n"

    def _input_options(self) -> list[str]:
        if self.settings.recorder_profile.strip().lower() != "trackside_hls":
            return []
        return [
            "-readrate", "1.0",
            "-fflags", "+discardcorrupt",
            "-rw_timeout", "15000000",
            "-reconnect", "1",
            "-reconnect_at_eof", "1",
            "-reconnect_on_network_error", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "10",
            "-reconnect_max_retries", str(self.settings.recorder_reconnect_max_retries),
            "-reconnect_delay_total_max", str(self.settings.recorder_reconnect_delay_total_max),
            "-headers", self._trackside_headers(),
        ]

    def _mapping_options(self) -> list[str]:
        if self.settings.recorder_profile.strip().lower() == "trackside_hls":
            program = self.settings.recorder_program
            if program is None:
                raise RuntimeError("Trackside HLS program mapping is not configured.")
            return ["-map", f"0:p:{program}:v:0", "-map", f"0:p:{program}:a:0"]
        return ["-map", "0"]

    def _command(self, chunk_minutes: int) -> list[str]:
        source = self.settings.recorder_input
        # `-n` makes an unexpected path collision fail rather than silently
        # replacing a previously captured interval.
        command = [self.settings.recorder_ffmpeg_path, "-hide_banner", "-n"]
        # A local file is only used by synthetic qualification. Live URLs are not
        # looped and are never committed to source control.
        if Path(source).is_file():
            command.extend(["-stream_loop", "-1", "-re"])
        command.extend(self._input_options())
        command.extend(["-i", source])
        command.extend(self._mapping_options())
        command.extend(["-c", "copy"])
        if self.settings.recorder_extra_args:
            command.extend(shlex.split(self.settings.recorder_extra_args))
        command.extend([
            "-f", "segment", "-segment_time", str(max(1, chunk_minutes * 60)),
            "-reset_timestamps", "1", "-strftime", "1",
            str(self._chunk_pattern()),
        ])
        return command

    def _log_command(self, command: list[str]) -> str:
        redacted: list[str] = []
        hide_next = False
        for part in command:
            if hide_next:
                redacted.append("<redacted>")
                hide_next = False
            elif part == self.settings.recorder_input:
                redacted.append(redact_input(part))
            elif part == "-headers":
                redacted.append(part)
                hide_next = True
            else:
                redacted.append(part)
        return " ".join(shlex.quote(part) for part in redacted)

    def _sanitize_log_line(self, line: bytes) -> bytes:
        text = line.decode("utf-8", "replace")
        for value in (self.settings.recorder_input, self.settings.recorder_origin, self.settings.recorder_referer):
            if value:
                text = text.replace(value, "<redacted>")
        return URL_PATTERN.sub("<redacted-url>", text).encode("utf-8", "replace")

    def _drain_log(self, process: subprocess.Popen, path: Path) -> None:
        if process.stderr is None:
            return
        try:
            with path.open("ab") as handle:
                # FFmpeg progress uses carriage returns rather than lines. A
                # chunked read prevents its stderr pipe from back-pressuring
                # segment rollover on Windows.
                while chunk := process.stderr.read1(4096):
                    handle.write(self._sanitize_log_line(chunk))
                    handle.flush()
        finally:
            process.stderr.close()

    @staticmethod
    def _sequence(path: Path | None) -> int | None:
        match = CHUNK_SEQUENCE.search(path.name) if path else None
        return int(match.group(1)) if match else None

    def _latest_chunk(self) -> Path | None:
        output = self.settings.resolved_recorder_output_dir
        try:
            chunks = [path for path in output.glob("trackside_*.ts") if path.is_file()]
            return max(chunks, key=lambda item: item.stat().st_mtime, default=None)
        except OSError:
            return None

    def _state(self, row: RecordingSession | None) -> RecorderState:
        free = self._free_space_gb()
        if row is None:
            configured = bool(self.settings.recorder_input) and self._ffmpeg_available()
            return RecorderState(health="idle" if configured else "unconfigured", input_state=redact_input(self.settings.recorder_input), free_space_gb=free)
        active = Path(row.active_path) if row.active_path else None
        health = "healthy" if row.status == "recording" else row.status
        return RecorderState(row.id, row.status, active, row.active_chunk_sequence, row.started_at, row.planned_end_at,
                             row.ffmpeg_pid, health, row.error_summary, redact_input(self.settings.recorder_input), free,
                             row.stop_after_chunk)

    def status(self, session: Session | None = None) -> RecorderState:
        return self._state(self._session(session)) if session else RecorderState(health="unknown", input_state=redact_input(self.settings.recorder_input), free_space_gb=self._free_space_gb())

    def start(self, session: Session, race_day=None, planned_end=None, *, manual: bool = True) -> RecorderState:
        existing = self._session(session)
        if existing:
            raise RuntimeError("A recorder session is already active or requires operator reconciliation.")
        allowed, reason = self._configured(session)
        if not allowed:
            raise RuntimeError(reason)
        output = self.settings.resolved_recorder_output_dir
        output.mkdir(parents=True, exist_ok=True)
        chunk_minutes = float(get_value(session, "recorder_chunk_minutes", self.settings.recorder_chunk_minutes))
        started = datetime.now()
        row = RecordingSession(race_day_id=getattr(race_day, "id", None), status="starting", adapter_name=self.name,
                               started_at=started, planned_start_at=started, planned_end_at=planned_end,
                               manual=manual, log_path=str(self.settings.data_dir / "logs" / f"recorder_{started:%Y%m%d_%H%M%S}.log"))
        session.add(row)
        session.flush()
        command = self._command(chunk_minutes)
        try:
            log = Path(row.log_path)
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as handle:
                handle.write(f"{started.isoformat()} recorder start: {self._log_command(command)}\n")
            process = self._popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, shell=False)
        except OSError as exc:
            row.status, row.error_summary, row.stopped_at = "failed", redact_input(str(exc)), datetime.now()
            session.commit()
            raise RuntimeError("FFmpeg recorder could not be started.") from exc
        row.status, row.ffmpeg_pid = "recording", process.pid
        self._children[process.pid] = process
        thread = threading.Thread(target=self._drain_log, args=(process, log), daemon=True, name=f"recorder-log-{process.pid}")
        self._log_threads[process.pid] = thread
        thread.start()
        session.commit()
        return self._state(row)

    def refresh(self, session: Session) -> RecorderState:
        row = self._session(session)
        if row is None:
            return self.status(session)
        process = self._children.get(row.ffmpeg_pid or -1)
        if process is None:
            if row.ffmpeg_pid and _pid_alive(row.ffmpeg_pid):
                row.status, row.error_summary, row.recovery_checked_at = "uncertain", "Recorder process ownership cannot be proven after restart.", datetime.now()
            elif row.status in {"recording", "stopping", "finalizing"}:
                row.status, row.error_summary, row.stopped_at, row.recovery_checked_at = "failed", "Recorder process is no longer running.", datetime.now(), datetime.now()
            session.commit()
            return self._state(row)
        exit_code = process.poll()
        if exit_code is not None:
            self._children.pop(process.pid, None)
            thread = self._log_threads.pop(process.pid, None)
            if thread:
                thread.join(timeout=2)
            if process.stdin:
                process.stdin.close()
            row.exit_code, row.stopped_at = exit_code, datetime.now()
            if row.status in {"stopping", "finalizing"} and exit_code == 0:
                row.status = "closed"
            elif exit_code == 0:
                row.status, row.error_summary = "source_ended", "Recorder input ended unexpectedly before an operator stop."
            else:
                row.status, row.error_summary = "failed", f"FFmpeg exited with code {exit_code}."
            row.active_path = None
            session.commit()
            return self._state(row)
        free = self._free_space_gb()
        minimum = float(get_value(session, "recorder_min_free_space_gb", self.settings.recorder_min_free_space_gb))
        if free is not None and free < minimum:
            row.error_summary = f"Free disk space fell below the {minimum:g} GB recorder threshold."
            return self.stop(session)
        newest = self._latest_chunk()
        previous = Path(row.active_path) if row.active_path else None
        if newest and newest != previous:
            row.active_path, row.active_chunk_sequence = str(newest.resolve()), self._sequence(newest)
            row.active_chunk_started_at = datetime.fromtimestamp(newest.stat().st_mtime)
            if row.stop_after_chunk and previous is not None:
                return self.stop(session)
            session.commit()
        return self._state(row)

    def stop_after_current_chunk(self, session: Session) -> RecorderState:
        row = self._session(session)
        if row is None or row.status != "recording":
            return self.status(session)
        row.stop_after_chunk = True
        session.commit()
        return self._state(row)

    def stop(self, session: Session, *, hard: bool = False) -> RecorderState:
        row = self._session(session)
        if row is None:
            return self.status(session)
        process = self._children.get(row.ffmpeg_pid or -1)
        if process is None:
            row.status, row.error_summary, row.recovery_checked_at = "uncertain", "Cannot stop an unowned recorder PID automatically.", datetime.now()
            session.commit()
            return self._state(row)
        if process.poll() is None:
            if hard:
                process.terminate()
            elif process.stdin:
                try:
                    process.stdin.write(b"q\n")
                    process.stdin.flush()
                except OSError:
                    process.terminate()
        row.status, row.stop_after_chunk = "stopping", False
        session.commit()
        return self._state(row)

    def recover(self, session: Session) -> RecorderState:
        """Never kill a persisted PID: another process may have reused it."""
        return self.refresh(session)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
