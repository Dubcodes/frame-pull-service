"""Independent LoveRacing calendar context; never an identity authority."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CalendarMeeting, CalendarRace, RaceDay

LOVERACING_BASE_URL = "https://loveracing.nz/"
LOVERACING_CALENDAR_URL = "https://loveracing.nz/RaceInfo.aspx"
USER_AGENT = "TracksideFramePull/2.2 (local review context)"


class CalendarProviderError(RuntimeError):
    pass


@dataclass
class CalendarRunner:
    number: int | None = None
    horse: str | None = None
    jockey: str | None = None
    trainer: str | None = None
    scratched: bool = False


@dataclass
class CalendarRacePayload:
    race_number: int
    name: str | None = None
    scheduled_time: datetime | None = None
    source_url: str | None = None
    runners: list[CalendarRunner] = field(default_factory=list)


@dataclass
class CalendarMeetingPayload:
    track: str
    source_url: str
    race_date: date | None = None
    races: list[CalendarRacePayload] = field(default_factory=list)
    source: str = "loveracing"
    retrieved_at: datetime = field(default_factory=datetime.utcnow)


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.parts: list[str] = []
    def handle_data(self, data):
        value = " ".join(unescape(data).split())
        if value: self.parts.append(value)
    def text(self) -> str:
        return "\n".join(self.parts)


def _text(html: str) -> str:
    parser = _TextExtractor(); parser.feed(html); return parser.text()


def _parse_date(value: str) -> date | None:
    match = re.search(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(20\d{2})\b", value)
    if not match: return None
    months = {name: index for index, name in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
    month = months.get(match.group(2).lower()[:3])
    return date(int(match.group(3)), month, int(match.group(1))) if month else None


def _parse_time(value: str, race_date: date | None) -> datetime | None:
    match = re.search(r"\b(\d{1,2}):(\d{2})\s*([ap]m)?\b", value, re.I)
    if not match or race_date is None: return None
    hour, minute, suffix = int(match.group(1)), int(match.group(2)), (match.group(3) or "").lower()
    if suffix == "pm" and hour < 12: hour += 12
    if suffix == "am" and hour == 12: hour = 0
    return datetime.combine(race_date, time(hour, minute))


def _track_from_text(text: str) -> str | None:
    match = re.search(r"Race Meeting for\s+.+?\s+at\s+(.+?)\s+on\s+\d{1,2}\s+[A-Za-z]{3,9}\s+20\d{2}", text, re.I)
    if match: return " ".join(match.group(1).split())
    match = re.search(r"(?:Meeting|Venue|Track)\s*:?\s*([A-Za-z][A-Za-z '\-]{2,60})", text, re.I)
    return " ".join(match.group(1).split()) if match else None


def discover_meeting_urls(html: str) -> list[str]:
    urls, seen = [], set()
    for href in re.findall(r'href=["\']([^"\']*?/RaceInfo/\d+/Meeting-Overview\.aspx[^"\']*)', html, re.I):
        url = urljoin(LOVERACING_BASE_URL, href)
        if url.lower() not in seen:
            seen.add(url.lower()); urls.append(url)
    return urls


def parse_meeting_html(html: str, source_url: str) -> CalendarMeetingPayload:
    text = _text(html); race_date = _parse_date(text); track = _track_from_text(text)
    if not track: raise CalendarProviderError("LoveRacing meeting did not expose a trustworthy track name")
    races: list[CalendarRacePayload] = []
    for match in re.finditer(r"\bRace\s+(\d{1,2})\b([^\n]{0,240})", text, re.I):
        number, detail = int(match.group(1)), match.group(2)
        name = re.sub(r"\b\d{1,2}:\d{2}\s*(?:[ap]m)?\b.*$", "", detail, flags=re.I).strip(" -:") or None
        races.append(CalendarRacePayload(race_number=number, name=name, scheduled_time=_parse_time(detail, race_date), source_url=source_url))
    unique = {race.race_number: race for race in races}
    if not unique: raise CalendarProviderError("LoveRacing meeting did not expose any races")
    return CalendarMeetingPayload(track=track, source_url=source_url, race_date=race_date, races=[unique[key] for key in sorted(unique)])


class RaceCalendarProvider:
    """Provider boundary. Calendar context informs review but never identity."""
    name = "unconfigured"
    def meetings_for_day(self, _race_date: date) -> list[CalendarMeetingPayload]: return []


class LoveRacingCalendarProvider(RaceCalendarProvider):
    name = "loveracing"
    def __init__(self, fetch_html=None, timeout_seconds: int = 20):
        self.fetch_html = fetch_html or self._fetch_html; self.timeout_seconds = timeout_seconds
    def _fetch_html(self, url: str) -> str:
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
        with urlopen(request, timeout=self.timeout_seconds) as response:
            return response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")
    def meetings_for_day(self, race_date: date) -> list[CalendarMeetingPayload]:
        try: calendar_html = self.fetch_html(LOVERACING_CALENDAR_URL)
        except Exception as exc: raise CalendarProviderError(f"LoveRacing calendar unavailable: {exc}") from exc
        parsed = []
        for url in discover_meeting_urls(calendar_html):
            try:
                item = parse_meeting_html(self.fetch_html(url), url)
                if item.race_date == race_date: parsed.append(item)
            except Exception:
                continue
        return parsed


def _runner_context(runner: CalendarRunner) -> dict:
    return {"number": runner.number, "horse": runner.horse, "jockey": runner.jockey, "trainer": runner.trainer, "scratched": runner.scratched}


def upsert_meeting_context(session: Session, race_day_id: int, payload: CalendarMeetingPayload) -> CalendarMeeting:
    track = payload.track.strip()
    meeting = session.execute(select(CalendarMeeting).where(CalendarMeeting.race_day_id == race_day_id, CalendarMeeting.track == track)).scalar_one_or_none()
    if meeting is None:
        meeting = CalendarMeeting(race_day_id=race_day_id, track=track); session.add(meeting); session.flush()
    meeting.source, meeting.source_url, meeting.source_timestamp = payload.source, payload.source_url, payload.retrieved_at
    meeting.refreshed_at, meeting.last_attempted_at, meeting.provider_status, meeting.last_error = datetime.utcnow(), datetime.utcnow(), "ok", None
    for row in payload.races:
        item = session.execute(select(CalendarRace).where(CalendarRace.meeting_id == meeting.id, CalendarRace.race_number == row.race_number)).scalar_one_or_none()
        if item is None: item = CalendarRace(meeting_id=meeting.id, race_number=row.race_number); session.add(item)
        item.name, item.scheduled_time = row.name, row.scheduled_time
        item.context = {"source_url": row.source_url or payload.source_url, "runners": [_runner_context(runner) for runner in row.runners]}
    return meeting


def refresh_race_day(session: Session, race_day: RaceDay, provider: RaceCalendarProvider) -> dict:
    existing = session.execute(select(CalendarMeeting).where(CalendarMeeting.race_day_id == race_day.id)).scalars().all(); attempted = datetime.utcnow()
    try: meetings = provider.meetings_for_day(race_day.race_date)
    except CalendarProviderError as exc:
        for meeting in existing: meeting.last_attempted_at, meeting.provider_status, meeting.last_error = attempted, "error", str(exc)
        session.commit(); return {"ok": False, "provider": provider.name, "error": str(exc), "meetings": len(existing), "preserved_cache": bool(existing)}
    for payload in meetings: upsert_meeting_context(session, race_day.id, payload)
    session.commit(); return {"ok": True, "provider": provider.name, "meetings": len(meetings), "preserved_cache": False}
