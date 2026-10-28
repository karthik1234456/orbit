"""Pure time parsing functions for reminders and timers with injectable clock."""

from datetime import datetime, timedelta
import re
from typing import Optional, Union

# Word numbers up to twenty
WORD_NUMBERS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}

# Unit multipliers in seconds
UNIT_SECONDS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
}


def parse_relative(
    number: Union[int, float, str, None],
    unit: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Union[timedelta, datetime, None]:
    """Parse relative duration into a timedelta or datetime if now is provided.

    Supports:
      - seconds, minutes, hours
      - "an hour", "half an hour", "a minute"
      - word numbers "one" to "twenty"
      - integer numbers from 1 up to 999
    Returns None for 0, negative, > 999, or unknown format.
    """
    if number is None and (unit is None or unit == ""):
        return None

    if unit == "":
        unit = None

    td: Optional[timedelta] = None

    # Handle combined string passed as first argument, e.g. parse_relative("5 minutes")
    if unit is None and isinstance(number, str):
        raw = number.strip().lower()
        if raw in ("an hour", "1 hour", "one hour"):
            td = timedelta(hours=1)
        elif raw in ("half an hour", "half hour", "a half hour"):
            td = timedelta(minutes=30)
        elif raw in ("a minute", "1 minute", "one minute"):
            td = timedelta(minutes=1)
        elif raw in ("a second", "1 second", "one second"):
            td = timedelta(seconds=1)
        else:
            # Match "<num> <unit>"
            m = re.match(r"^(\S+)\s+(.+)$", raw)
            if not m:
                return None
            num_part, unit_part = m.group(1), m.group(2)
            return parse_relative(num_part, unit_part, now=now)

    if td is None:
        # Unit normalization
        if unit is None:
            return None

        u_clean = str(unit).strip().lower()
        # Check special phrases when unit contains articles
        if u_clean in ("an hour", "hour", "hours", "hr", "hrs", "h"):
            unit_key = "hour"
        elif u_clean in ("a minute", "minute", "minutes", "min", "mins", "m"):
            unit_key = "minute"
        elif u_clean in ("a second", "second", "seconds", "sec", "secs", "s"):
            unit_key = "second"
        else:
            return None

        # Number normalization
        n_val: Optional[float] = None
        if isinstance(number, (int, float)):
            n_val = float(number)
        elif isinstance(number, str):
            num_str = number.strip().lower()
            if num_str in ("a", "an"):
                n_val = 1.0
            elif num_str == "half":
                n_val = 0.5
            elif num_str == "half an" and unit_key == "hour":
                n_val = 0.5
            elif num_str in WORD_NUMBERS:
                n_val = float(WORD_NUMBERS[num_str])
            else:
                try:
                    n_val = float(num_str)
                except ValueError:
                    return None
        elif number is None:
            if u_clean.startswith("a ") or u_clean.startswith("an "):
                n_val = 1.0
            else:
                return None

        if n_val is None or n_val <= 0:
            return None

        # Must be at most 999
        if n_val > 999:
            return None

        total_secs = n_val * UNIT_SECONDS[unit_key]
        td = timedelta(seconds=total_secs)

    if td is None:
        return None
    if now is not None:
        return now + td
    return td


# NOTE on Daylight-Saving Time (DST) Handling:
# Standard library zoneinfo-free approach: We compute local naive datetimes and attach
# the system local timezone offset using datetime.astimezone() or the caller's tzinfo.
# Limitation: In standard library without zoneinfo/pytz, during the exact 1-hour "spring forward"
# transition gap (non-existent local wall clock times) or "fall back" ambiguity (repeated hour),
# the system C-level tzset/localtime determines the offset, which may round or choose standard/daylight
# based on current OS settings. This is standard and avoids external dependencies or zoneinfo database requirements.

_CLOCK_PATTERN = re.compile(
    r"^(\d{1,2})(?::(\d{2}))?(?::(\d{2}))?\s*(am|pm)?$",
    re.IGNORECASE,
)


def parse_clock(
    text: str,
    now: Optional[datetime] = None,
) -> Optional[datetime]:
    """Parse a local clock expression into the next occurrence as a datetime.

    Supported patterns:
      - "5:30 pm", "17:30", "5 pm", "5:30am", "17:00", "5:00 pm", "14:00:10"
      - "tomorrow at 5:30 pm", "tomorrow at 17:30", "tomorrow at 5 pm"
      - "at 5:30 pm", "at 17:30"
    No other date formats are accepted (returns None).

    The returned datetime is:
      - Tomorrow if "tomorrow at ..." is specified.
      - Today if the target time is at least 5 seconds ahead of `now`.
      - Otherwise tomorrow at that time.
    """
    if not text or not isinstance(text, str):
        return None

    raw = text.strip()
    is_tomorrow = False
    if re.match(r"^tomorrow\s+at\s+", raw, re.IGNORECASE):
        is_tomorrow = True
        raw = re.sub(r"^tomorrow\s+at\s+", "", raw, flags=re.IGNORECASE).strip()
    elif re.match(r"^at\s+", raw, re.IGNORECASE):
        raw = re.sub(r"^at\s+", "", raw, flags=re.IGNORECASE).strip()

    m = _CLOCK_PATTERN.match(raw)
    if not m:
        return None

    hour_str, min_str, sec_str, am_pm_str = m.group(1), m.group(2), m.group(3), m.group(4)

    try:
        hour = int(hour_str)
        minute = int(min_str) if min_str is not None else 0
        second = int(sec_str) if sec_str is not None else 0
    except (ValueError, TypeError):
        return None

    if minute < 0 or minute > 59 or second < 0 or second > 59:
        return None

    if am_pm_str:
        ap = am_pm_str.lower()
        if hour < 1 or hour > 12:
            return None
        if ap == "pm" and hour < 12:
            hour += 12
        elif ap == "am" and hour == 12:
            hour = 0
    else:
        # 24-hour time
        if hour < 0 or hour > 23:
            return None

    # Reference now
    ref_now = now if now is not None else datetime.now().astimezone()
    tz = ref_now.tzinfo

    today_target = ref_now.replace(
        hour=hour,
        minute=minute,
        second=second,
        microsecond=0,
    )

    if is_tomorrow:
        target = today_target + timedelta(days=1)
    else:
        # Check if today is still ahead by at least 5 seconds
        if (today_target - ref_now).total_seconds() >= 5.0:
            target = today_target
        else:
            target = today_target + timedelta(days=1)

    if tz is not None and target.tzinfo != tz:
        target = target.replace(tzinfo=tz)

    return target
