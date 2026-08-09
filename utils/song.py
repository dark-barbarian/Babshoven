from datetime import datetime, timedelta
from typing import NotRequired, TypedDict


class Song(TypedDict):
    """TypedDict for song metadata."""

    archive_id: str
    id: str
    filename: str
    title: str
    song_link: str
    duration_string: str
    duration: int
    starting_time: NotRequired[datetime]
    passed_time: NotRequired[timedelta]
    passed_time_until_pause: NotRequired[timedelta]
