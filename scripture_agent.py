#!/usr/bin/env python3
"""
Scripture Memorization Agent for Apple Reminders

- Uses AppleScript via `osascript` (no Shortcuts) from Python
- Lists: Backlog, Daily, Weekly, Monthly, Mastered
- Backlog → Daily (no duplicates), due set to next morning 8:00 AM
- Cadence per Featherstone: Daily repeats then Weekly then Monthly, then Mastered refresh
- Notes auto-fill via scripture APIs
"""

import os
import sys
import json
import calendar
import re
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple
import subprocess
import random
import uuid
import hashlib
import csv


# ----- List names (top-level, no groups) -----
BACKLOG  = "Scripture Memorization - Backlog"
DAILY    = "Scripture Memorization - Daily"
WEEKLY   = "Scripture Memorization - Weekly"
MONTHLY  = "Scripture Memorization - Monthly"
MASTERED = "Scripture Memorization - Mastered"

# ----- Cadence thresholds (Featherstone-style) -----
DAILY_REPEATS   = 7
WEEKLY_REPEATS  = 4
MONTHLY_REPEATS = 24  # ~2 years in Monthly

# Mastered expanding refresh: then yearly thereafter
MASTERED_REVIEW_MONTHS = [3, 6, 12]
MASTERED_YEARLY_INTERVAL = 12

# ----- State file for cadence tracking -----
CONFIG_DIR  = os.path.expanduser("~/.scripture_agent")
STATE_PATH  = os.path.join(CONFIG_DIR, "state.json")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOG_PATH    = os.path.join(CONFIG_DIR, "agent.log")
CSV_PATH    = os.path.join(CONFIG_DIR, "progress.csv")

# ----- Scripture API endpoints -----
NEPHI_API_BASE = "https://api.nephi.org/scriptures/"  # LDS-capable
BIBLE_API_BASE = "https://bible-api.com/"             # KJV fallback

# ----- Auto-add frequency gate -----
AUTO_ADD_EVERY_N_DAYS = 0  # 1=daily, 7=weekly, 0=disable


# ====================================================================
# Shell / AppleScript helpers
# ====================================================================
def run_as(script: str, *args: str) -> str:
    return subprocess.run(
        ["osascript", "-e", script, *args],
        check=True, capture_output=True, text=True
    ).stdout.strip()

def _ensure_config_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)

def _append_log(line: str) -> None:
    _ensure_config_dir()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {line}\n")
    except Exception:
        pass

def _load_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"[config] failed to read {path}: {e}")
        return None

def _save_json(path: str, data: dict):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[config] failed to write {path}: {e}")


DEFAULT_CONFIG = {
    "lists": {
        "backlog":  "Scripture Memorization - Backlog",
        "daily":    "Scripture Memorization - Daily",
        "weekly":   "Scripture Memorization - Weekly",
        "monthly":  "Scripture Memorization - Monthly",
        "mastered": "Scripture Memorization - Mastered"
    },
    "cadence": {
        "daily_repeats":   7,
        "weekly_repeats":  4,
        "monthly_repeats": 24
    },
    "mastered": {
        "review_months":   [3, 6, 12],
        "yearly_interval": 12
    },
    "auto_add": {
        "every_n_days": 7,
        "topic_default": ""
    },
    "obfuscation": {
        "enabled": True,
        "separator": "\n\n______________________________\n",
        "schedule": [1.0, 0.75, 0.5, 0.35, 0.2],
        "min_word_len": 3,
        "keep_first_last": False,
        "respect_punctuation": True,
        "buffer_lines": 4,
        "buffer_token": "."
    }
}

def load_or_init_config() -> dict:
    _ensure_config_dir()
    cfg = _load_json(CONFIG_PATH)
    if cfg is None:
        cfg = DEFAULT_CONFIG
        _save_json(CONFIG_PATH, cfg)
        print(f"[config] created default config at {CONFIG_PATH}")
    return cfg

def apply_config(cfg: dict):
    global BACKLOG, DAILY, WEEKLY, MONTHLY, MASTERED
    BACKLOG  = cfg.get("lists", {}).get("backlog",  DEFAULT_CONFIG["lists"]["backlog"])
    DAILY    = cfg.get("lists", {}).get("daily",    DEFAULT_CONFIG["lists"]["daily"])
    WEEKLY   = cfg.get("lists", {}).get("weekly",   DEFAULT_CONFIG["lists"]["weekly"])
    MONTHLY  = cfg.get("lists", {}).get("monthly",  DEFAULT_CONFIG["lists"]["monthly"])
    MASTERED = cfg.get("lists", {}).get("mastered", DEFAULT_CONFIG["lists"]["mastered"])

    global DAILY_REPEATS, WEEKLY_REPEATS, MONTHLY_REPEATS
    DAILY_REPEATS   = int(cfg.get("cadence", {}).get("daily_repeats",   DEFAULT_CONFIG["cadence"]["daily_repeats"]))
    WEEKLY_REPEATS  = int(cfg.get("cadence", {}).get("weekly_repeats",  DEFAULT_CONFIG["cadence"]["weekly_repeats"]))
    MONTHLY_REPEATS = int(cfg.get("cadence", {}).get("monthly_repeats", DEFAULT_CONFIG["cadence"]["monthly_repeats"]))

    global MASTERED_REVIEW_MONTHS, MASTERED_YEARLY_INTERVAL
    MASTERED_REVIEW_MONTHS = list(cfg.get("mastered", {}).get("review_months", DEFAULT_CONFIG["mastered"]["review_months"]))
    MASTERED_YEARLY_INTERVAL = int(cfg.get("mastered", {}).get("yearly_interval", DEFAULT_CONFIG["mastered"]["yearly_interval"]))

    global AUTO_ADD_EVERY_N_DAYS
    AUTO_ADD_EVERY_N_DAYS = int(cfg.get("auto_add", {}).get("every_n_days", DEFAULT_CONFIG["auto_add"]["every_n_days"]))

    obf = cfg.get("obfuscation", {}) or {}
    global OBF_ENABLED, OBF_SEPARATOR, OBF_SCHEDULE, OBF_MIN_LEN, OBF_KEEP_FL, OBF_RESPECT_PUNCT
    OBF_ENABLED       = bool(obf.get("enabled", True))
    OBF_SEPARATOR     = obf.get("separator", DEFAULT_CONFIG["obfuscation"]["separator"])
    OBF_SCHEDULE      = list(obf.get("schedule", DEFAULT_CONFIG["obfuscation"]["schedule"]))
    OBF_MIN_LEN       = int(obf.get("min_word_len", DEFAULT_CONFIG["obfuscation"]["min_word_len"]))
    OBF_KEEP_FL       = bool(obf.get("keep_first_last", DEFAULT_CONFIG["obfuscation"]["keep_first_last"]))
    OBF_RESPECT_PUNCT = bool(obf.get("respect_punctuation", DEFAULT_CONFIG["obfuscation"]["respect_punctuation"]))
    global OBF_BUFFER_LINES, OBF_BUFFER_TOKEN
    OBF_BUFFER_LINES  = int(obf.get("buffer_lines", DEFAULT_CONFIG["obfuscation"]["buffer_lines"]))
    OBF_BUFFER_TOKEN  = str(obf.get("buffer_token", DEFAULT_CONFIG["obfuscation"]["buffer_token"]))


# ====================================================================
# Readers / Writers
# ====================================================================
def list_reminders(list_name: str) -> List[Dict[str, Any]]:
    """
    Returns [{'id': str, 'name': str, 'body': str, 'completed': bool, 'due': str}]
    """
    script = r'''
    on run argv
      set listName to item 1 of argv
      tell application "Reminders"
        if not (exists (list listName)) then return ""
        set theList to first list whose name is listName
        set out to ""
        repeat with r in reminders of theList
          set rid to id of r as text

          -- name (strip CR/LF -> space)
          set rname to name of r as text
          set AppleScript's text item delimiters to return
          set rname to text items of rname
          set AppleScript's text item delimiters to " "
          set rname to rname as text
          set AppleScript's text item delimiters to linefeed
          set rname to text items of rname
          set AppleScript's text item delimiters to " "
          set rname to rname as text
          set AppleScript's text item delimiters to ""

          -- body (strip CR/LF -> space)
          if body of r is missing value then
            set rbody to ""
          else
            set rbody to body of r as text
            set AppleScript's text item delimiters to return
            set rbody to text items of rbody
            set AppleScript's text item delimiters to " "
            set rbody to rbody as text
            set AppleScript's text item delimiters to linefeed
            set rbody to text items of rbody
            set AppleScript's text item delimiters to " "
            set rbody to rbody as text
            set AppleScript's text item delimiters to ""
          end if

          set rcompleted to completed of r as text

          -- due (strip CR/LF -> space)
          if due date of r is missing value then
            set rdue to ""
          else
            set rdue to (due date of r as string)
            set AppleScript's text item delimiters to return
            set rdue to text items of rdue
            set AppleScript's text item delimiters to " "
            set rdue to rdue as text
            set AppleScript's text item delimiters to linefeed
            set rdue to text items of rdue
            set AppleScript's text item delimiters to " "
            set rdue to rdue as text
            set AppleScript's text item delimiters to ""
          end if

          set out to out & rid & "␞" & rname & "␞" & rbody & "␞" & rcompleted & "␞" & rdue & linefeed
        end repeat
      end tell
      return out
    end run
    '''
    out = run_as(script, list_name)
    items: List[Dict[str, Any]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("␞")
        if len(parts) != 5:
            continue
        rid, name, body, completed, due = parts
        items.append({"id": rid, "name": name, "body": body, "completed": (completed.strip().lower() == "true"), "due": due})
    return items

def get_body_by_id_raw(list_name: str, rem_id: str) -> str:
    script = r'''
    on run argv
      set listName to item 1 of argv
      set rid to item 2 of argv
      tell application "Reminders"
        set theList to first list whose name is listName
        try
          set r to first reminder of theList whose id is rid
          if body of r is missing value then
            return ""
          else
            return (body of r as text)
          end if
        on error
          return ""
        end try
      end tell
    end run
    '''
    try:
        return run_as(script, list_name, rem_id)
    except Exception:
        return ""

def set_body_by_id(list_name: str, rem_id: str, body: str) -> bool:
    script = r'''
    on run argv
      set listName to item 1 of argv
      set rid to item 2 of argv
      set theBody to item 3 of argv
      tell application "Reminders"
        set theList to first list whose name is listName
        try
          set r to first reminder of theList whose id is rid
          set body of r to theBody
          return "OK"
        on error
          return "NOT_FOUND"
        end try
      end tell
    end run
    '''
    return run_as(script, list_name, rem_id, body) == "OK"

def set_due(list_name: str, title: str, due_dt: datetime) -> bool:
    due_str = due_dt.strftime("%m/%d/%Y %H:%M:%S")
    script = r'''
    on run argv
      set listName to item 1 of argv
      set theTitle to item 2 of argv
      set dueStr to item 3 of argv
      tell application "Reminders"
        set theList to first list whose name is listName
        set targetRem to missing value
        repeat with r in reminders of theList
          if (name of r as text) is theTitle then
            set targetRem to r
            exit repeat
          end if
        end repeat
        if targetRem is missing value then
          return "NOT_FOUND"
        else
          try
            set due date of targetRem to date dueStr
            return "OK"
          on error errMsg
            return "ERR:" & errMsg
          end try
        end if
      end tell
    end run
    '''
    res = run_as(script, list_name, title, due_str)
    return res == "OK"

def set_due_next_morning_8am(list_name: str, title: str) -> bool:
    dt_target = (datetime.now() + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    y, m, d = str(dt_target.year), str(dt_target.month), str(dt_target.day)
    hh, mm = str(dt_target.hour), str(dt_target.minute)
    script = r'''
    on run argv
      set listName to item 1 of argv
      set theTitle to item 2 of argv
      set y to (item 3 of argv) as integer
      set mIndex to (item 4 of argv) as integer
      set d to (item 5 of argv) as integer
      set hh to (item 6 of argv) as integer
      set mm to (item 7 of argv) as integer

      set monthList to {January, February, March, April, May, June, July, August, September, October, November, December}
      set theMonth to item mIndex of monthList

      tell application "Reminders"
        if not (exists (list listName)) then return "NOT_FOUND"
        set theList to first list whose name is listName
        set targetRem to missing value
        repeat with r in reminders of theList
          if (name of r as text) is theTitle then
            set targetRem to r
            exit repeat
          end if
        end repeat
        if targetRem is missing value then return "NOT_FOUND"
      end tell

      set theDate to (current date)
      set year of theDate to y
      set month of theDate to theMonth
      set day of theDate to d
      set hours of theDate to hh
      set minutes of theDate to mm
      set seconds of theDate to 0

      tell application "Reminders"
        set due date of targetRem to theDate
      end tell
      return "OK"
    end run
    '''
    res = run_as(script, list_name, title, y, m, d, hh, mm)
    return res == "OK"

def delete_by_id(list_name: str, rem_id: str) -> bool:
    script = r'''
    on run argv
      set listName to item 1 of argv
      set rid to item 2 of argv
      tell application "Reminders"
        set theList to first list whose name is listName
        try
          delete (first reminder of theList whose id is rid)
          return "OK"
        on error
          return "NOT_FOUND"
        end try
      end tell
    end run
    '''
    return run_as(script, list_name, rem_id) == "OK"

def mark_incomplete_by_id(list_name: str, rem_id: str) -> bool:
    script = r'''
    on run argv
      set listName to item 1 of argv
      set rid to item 2 of argv
      tell application "Reminders"
        set theList to first list whose name is listName
        try
          set r to first reminder of theList whose id is rid
          set completed of r to false
          return "OK"
        on error
          return "NOT_FOUND"
        end try
      end tell
    end run
    '''
    return run_as(script, list_name, rem_id) == "OK"

def mark_incomplete_by_title(list_name: str, title: str) -> bool:
    items = list_reminders(list_name)
    m = next((x for x in items if x["name"].strip() == title.strip()), None)
    if not m:
        return False
    return mark_incomplete_by_id(list_name, m["id"])

def get_tags_by_id(list_name: str, rem_id: str) -> List[str]:
    """
    Try to read tag names (macOS Reminders supports 'tag names' on newer versions).
    Returns lowercased tag list or [] if unavailable.
    """
    script = r'''
    on run argv
      set listName to item 1 of argv
      set rid to item 2 of argv
      tell application "Reminders"
        set theList to first list whose name is listName
        try
          set r to first reminder of theList whose id is rid
          try
            set tn to (tag names of r) as text
            return tn
          on error
            return ""
          end try
        on error
          return ""
        end try
      end tell
    end run
    '''
    try:
        raw = run_as(script, list_name, rem_id)
    except Exception:
        raw = ""
    parts = [t.strip().lower() for t in raw.split(",") if t.strip()]
    return parts


# ====================================================================
# Scripture HTTP helpers
# ====================================================================
_REF_PARSE = re.compile(
    r"^\s*([A-Za-z0-9&’' .\-]+?)\s+(\d+)\s*:\s*(\d+)(?:\s*[-–]\s*(\d+))?\s*$",
    re.IGNORECASE
)

def _normalize_book_name(book: str) -> str:
    b = (book or "").strip()
    b_cf = b.casefold().replace("–", "-").replace("—", "-")
    aliases = {
        "d&c": "Doctrine and Covenants",
        "d. & c.": "Doctrine and Covenants",
        "dc": "Doctrine and Covenants",
        "doctrine & covenants": "Doctrine and Covenants",
        "doctrine and covenants": "Doctrine and Covenants",
    }
    if b_cf in aliases:
        return aliases[b_cf]
    return re.sub(r"\s{2,}", " ", b).strip()

def parse_reference(ref: str) -> Optional[Tuple[str,int,int,int]]:
    m = _REF_PARSE.match((ref or "").replace("—", "-").replace("–", "-"))
    if not m:
        return None
    book = _normalize_book_name(m.group(1))
    chapter = int(m.group(2))
    v_start = int(m.group(3))
    v_end = int(m.group(4)) if m.group(4) else v_start
    if v_end < v_start:
        v_end = v_start
    return (book, chapter, v_start, v_end)

def ranges_overlap(a, b) -> bool:
    if a is None or b is None:
        return False
    (abook, ach, as_, ae) = a
    (bbook, bch, bs, be) = b
    if _normalize_book_name(abook).casefold() != _normalize_book_name(bbook).casefold():
        return False
    if ach != bch:
        return False
    return not (ae < bs or be < as_)

def _titles_across_all_lists() -> list:
    titles = []
    for ln in [DAILY, WEEKLY, MONTHLY, BACKLOG]:
        try:
            titles += [x["name"] for x in list_reminders(ln)]
        except Exception:
            pass
    return titles

def ref_overlaps_anywhere(candidate_ref: str) -> Optional[str]:
    c_parsed = parse_reference(candidate_ref)
    if not c_parsed:
        return None
    for t in _titles_across_all_lists():
        e_parsed = parse_reference(t)
        if e_parsed and ranges_overlap(c_parsed, e_parsed):
            return t
    return None

def _http_get_json(url: str, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = resp.read()
        return json.loads(data.decode("utf-8", errors="ignore"))
    except Exception:
        return None

def _normalize_reference_for_nephi(ref: str) -> str:
    ref = (ref or "").strip().replace("–", "-")
    return re.sub(r"\s+", " ", ref)

_LEADING_VERSE_NUM_RE = re.compile(r"^\s*(\d+[:\u00A0\s]+)?(\d+)\s+")
_BRACKETED_NUM_RE     = re.compile(r"^\s*\[?\d+\]?\s*")

def _clean_line(s: str) -> str:
    s = s.replace("\u00A0", " ")
    s = s.strip()
    s = _LEADING_VERSE_NUM_RE.sub("", s)
    s = _BRACKETED_NUM_RE.sub("", s)
    s = re.sub(r"^[a-z]\s+", "", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()

def _format_verses_paragraphs(verses: List[str]) -> str:
    cleaned = []
    for v in verses:
        if not v:
            continue
        c = _clean_line(v)
        if c:
            cleaned.append(c)
    return "\n\n".join(cleaned)

def _try_nephi_api(reference: str) -> Optional[str]:
    ref = _normalize_reference_for_nephi(reference)
    query = urllib.parse.urlencode({"q": ref})
    url = f"{NEPHI_API_BASE}?{query}"
    data = _http_get_json(url)
    if not data:
        return None
    verses: List[str] = []
    scr = data.get("scriptures")
    if isinstance(scr, list):
        for v in scr:
            t = v.get("text")
            if isinstance(t, str) and t.strip():
                verses.append(t)
    if not verses:
        return None
    return _format_verses_paragraphs(verses)

def _try_bible_api(reference: str) -> Optional[str]:
    q = urllib.parse.quote(reference.strip())
    url = f"{BIBLE_API_BASE}{q}"
    data = _http_get_json(url)
    if not data:
        return None
    verses_field = data.get("verses")
    verses: List[str] = []
    if isinstance(verses_field, list):
        for v in verses_field:
            t = v.get("text")
            if isinstance(t, str) and t.strip():
                verses.append(t)
    if not verses:
        return None
    return _format_verses_paragraphs(verses)

def fetch_scripture_text(reference: str) -> Optional[str]:
    ref = (reference or "").strip()
    if not ref:
        print("[fetch] empty reference, skipping")
        return None
    txt = _try_nephi_api(ref)
    if txt:
        print(f"[fetch] OK via LDS provider for '{ref}'")
        return txt
    else:
        print(f"[fetch] LDS provider had no result for '{ref}'")
    txt = _try_bible_api(ref)
    if txt:
        print(f"[fetch] OK via Bible-only provider for '{ref}'")
        return txt
    print(f"[fetch] no provider could resolve '{ref}'")
    return None

def _openai_chat(prompt: str, model: str = "gpt-4o-mini") -> Optional[str]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("[chatgpt] OPENAI_API_KEY not set; skipping")
        return None
    try:
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            data=json.dumps({
                "model": model,
                "messages": [
                    {"role": "system", "content": "You are a concise assistant that only replies with a single contiguous Latter-day Saint scripture reference in the format 'Book Chapter:Verse' or 'Book Chapter:Start-End'. No commentary."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.5,
            }).encode("utf-8")
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return (text or "").strip()
    except Exception as e:
        print(f"[chatgpt] error: {e}")
        return None

_REF_RE = re.compile(r"([A-Za-z0-9&’' .\-]+)\s+(\d+):(\d+(?:-\d+)?)")

def _extract_reference(s: str) -> Optional[str]:
    if not s:
        return None
    m = _REF_RE.search(s.replace("–","-"))
    if not m:
        return None
    book = re.sub(r"\s+", " ", m.group(1)).strip()
    ch   = m.group(2)
    vv   = m.group(3)
    return f"{book} {ch}:{vv}"

def suggest_reference_via_chatgpt(topic: Optional[str] = None, exclusions: Optional[List[str]] = None) -> Optional[str]:
    avoid_list = exclusions or []
    avoid_block = ""
    if avoid_list:
        joined = "\n".join(f"- {t}" for t in avoid_list[:50])
        avoid_block = (
            "Avoid suggesting any scripture that is the same as OR overlaps any of the following references "
            "(treat overlaps as sharing any verse in common):\n"
            f"{joined}\n\n"
        )
    user_prompt = (
        (f"Topic: {topic.strip()}\n" if topic else "") +
        avoid_block +
        "Return exactly ONE Latter-day Saint scripture reference that is a single contiguous passage (e.g., 'Mosiah 2:21-22'). "
        "Do NOT include multiple disjoint references. Do NOT include commentary—reply with only the reference."
    )
    text = _openai_chat(user_prompt, model="gpt-4o-mini")
    return _extract_reference(text or "")


# ====================================================================
# State helpers
# ====================================================================
def _norm_title_key(t: str) -> str:
    return (t or "").strip().casefold().replace("–", "-")

def _load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"verses": {}}

def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def _get_or_init_record(title: str, *, anchor_weekday: Optional[int] = None) -> dict:
    key = _norm_title_key(title)
    state = _load_state()
    rec = state["verses"].get(key)
    if rec is None:
        rec = {
            "title": title.strip(),
            "stage": "daily",
            "daily_count": 0,
            "weekly_count": 0,
            "monthly_count": 0,
            "mastered_count": 0,
            "anchor_weekday": anchor_weekday if anchor_weekday is not None else datetime.now().weekday(),
            "sid": None,
            "full_text": "",
            "full_text_sha": ""
        }
        state["verses"][key] = rec
        _save_state(state)
    return rec

def _update_record(title: str, **changes) -> None:
    key = _norm_title_key(title)
    state = _load_state()
    rec = state["verses"].setdefault(key, {
        "title": title.strip(),
        "stage": "daily",
        "daily_count": 0,
        "weekly_count": 0,
        "monthly_count": 0,
        "mastered_count": 0,
        "anchor_weekday": datetime.now().weekday(),
        "sid": None,
        "full_text": "",
        "full_text_sha": ""
    })
    rec.update(changes)
    _save_state(state)

def _find_record_by_sid(sid: str) -> Tuple[Optional[dict], Optional[str]]:
    if not sid:
        return None, None
    state = _load_state()
    for k, rec in state.get("verses", {}).items():
        if (rec or {}).get("sid") == sid:
            return rec, k
    return None, None

def _rename_state_key(old_key: str, new_key: str) -> None:
    if old_key == new_key:
        return
    state = _load_state()
    verses = state.get("verses", {})
    if old_key in verses:
        verses[new_key] = verses.pop(old_key)
        _save_state(state)

def _get_last_auto_added_date() -> Optional[datetime]:
    state = _load_state()
    iso = state.get("last_auto_added")
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except Exception:
        return None

def _set_last_auto_added_date(d: datetime) -> None:
    state = _load_state()
    state["last_auto_added"] = d.isoformat()
    _save_state(state)


# ====================================================================
# Obfuscation helpers (single, unified set)
# ====================================================================
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z’']*")

def _sha1(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()

def _extract_full_text(note: str) -> str:
    sep = globals().get("OBF_SEPARATOR", "\n\n______________________________\n")
    note = (note or "")
    parts = note.rsplit(sep, 1)
    return parts[1].strip() if len(parts) == 2 else note.strip()

def _obfuscate_text(full_text: str, visible_ratio: float, seed: int) -> str:
    """Blank out ~ (1 - visible_ratio) of eligible words; preserve punctuation and spacing."""
    if visible_ratio >= 0.999:
        return full_text
    min_len = int(globals().get("OBF_MIN_LEN", 3))
    keep_first_last = bool(globals().get("OBF_KEEP_FL", False))

    spans = []
    for m in _WORD_RE.finditer(full_text):
        w = m.group(0)
        letters = sum(1 for c in w if c.isalpha())
        if letters >= min_len:
            spans.append((m.start(), m.end(), w))

    if not spans:
        return full_text

    blank_frac = max(0.0, min(1.0, 1.0 - float(visible_ratio)))
    k = int(round(blank_frac * len(spans)))
    if k <= 0:
        return full_text

    rnd = random.Random(seed)
    to_blank_idx = set(rnd.sample(range(len(spans)), k))

    out = []
    last = 0
    for idx, (s, e, w) in enumerate(spans):
        out.append(full_text[last:s])
        if idx in to_blank_idx:
            if keep_first_last and len(w) >= 2:
                core = "".join("_" if c.isalpha() else c for c in w[1:-1])
                masked = w[0] + core + w[-1]
            else:
                masked = "".join("_" if c.isalpha() else c for c in w)
            out.append(masked)
        else:
            out.append(w)
        last = e
    out.append(full_text[last:])
    return "".join(out)

def _ratio_for_monthly_count(mcount: int) -> float:
    schedule = globals().get("OBF_SCHEDULE", [1.0, 0.75, 0.5, 0.35, 0.2])
    repeats = int(globals().get("MONTHLY_REPEATS", 24))
    if not schedule:
        return 1.0
    if len(schedule) == 1:
        return float(schedule[0])
    denom = max(1, repeats - 1)
    p = max(0.0, min(1.0, float(mcount) / float(denom)))
    steps = len(schedule) - 1
    x = p * steps
    i = int(x)
    if i >= steps:
        return float(schedule[-1])
    frac = x - i
    a = float(schedule[i]); b = float(schedule[i + 1])
    return a * (1.0 - frac) + b * frac

def _note_with_obfuscation(full_text: str, visible_ratio: float, seed: int, sid: Optional[str] = None) -> str:
    """
    Canonical Monthly note format:
      [OBFUSCATED TEXT]
      [N lines of buffer tokens]
      ______________________________
      [FULL ORIGINAL TEXT]
      (8 blank lines)
      [sid:...]
    """
    obf = _obfuscate_text(full_text, visible_ratio, seed)
    buf_lines = int(globals().get("OBF_BUFFER_LINES", 4))
    buf_token = str(globals().get("OBF_BUFFER_TOKEN", "."))
    sep = globals().get("OBF_SEPARATOR", "\n\n______________________________\n")

    # Build buffer: N lines of the buffer token, one per line (no extra commentary)
    buffer_block = "\n".join(buf_token for _ in range(max(0, buf_lines))).strip()
    parts = [obf.strip()]
    if buffer_block:
        parts.append(buffer_block)
    parts.append(sep + full_text.strip())

    body = "\n\n".join(parts)

    # Always push SID down with consistent spacing
    if sid:
        body = _append_sid(body, sid)

    return body



# ====================================================================
# SID helpers
# ====================================================================
_SID_RE = re.compile(r"\[sid:([0-9a-fA-F-]{36})\]\s*$")

def _new_sid() -> str:
    return str(uuid.uuid4())

def _extract_sid(note: str) -> Optional[str]:
    if not note:
        return None
    m = re.search(r"\[sid:([0-9a-fA-F-]{36})\]", note)
    return m.group(1) if m else None

def _append_sid(note: str, sid: str) -> str:
    """
    Append the SID far down in the note body with 8 blank spacer lines,
    without disturbing existing verse formatting.
    """
    base = (note or "").rstrip()
    spacer = "\n" * 8
    return f"{base}{spacer}[sid:{sid}]"


def _ensure_sid_for_title(list_name: str, title: str) -> Optional[str]:
    """
    Ensure the reminder in list_name with the given title has a [sid:UUID] footer,
    preserving original newline formatting in the note body.
    """
    # Locate item by normalized title
    items = list_reminders(list_name)
    it = next((x for x in items if _norm_title(x["name"]) == _norm_title(title)), None)
    if not it:
        return None

    # IMPORTANT: fetch RAW body (list_reminders flattens newlines)
    note_raw = get_body_by_id_raw(list_name, it["id"])
    sid = _extract_sid(note_raw)
    if not sid:
        sid = _new_sid()
        new_body = _append_sid(note_raw, sid)
        set_body_by_id(list_name, it["id"], new_body)

    # Mirror into state
    rec = _get_or_init_record(title)
    if rec.get("sid") != sid:
        _update_record(title, sid=sid)

    return sid




# ====================================================================
# Manual override detector
# ====================================================================
def _has_manual_override(list_name: str, item: Dict[str, Any]) -> bool:
    """
    True if:
      - Reminders 'tag names' contains 'manual_override' (case-insensitive), OR
      - '#manual_override' appears in the note body (raw) or title.
    """
    try:
        tags = get_tags_by_id(list_name, item["id"])
        if any(t == "manual_override" or t == "#manual_override" for t in tags):
            return True
    except Exception:
        pass
    try:
        raw = get_body_by_id_raw(list_name, item["id"]) or ""
        if "#manual_override" in raw:
            return True
    except Exception:
        pass
    return "#manual_override" in (item.get("name") or "")


# ====================================================================
# Title-change refresh (SID-anchored)
# ====================================================================
def _ensure_plain_body_with_sid(full_text: str, sid: str) -> str:
    """
    Plain (non-Monthly) body builder: scripture text + 8 blank lines + [sid:...].
    """
    body = (full_text or "").rstrip()
    return _append_sid(body, sid)


def _monthly_seed(title: str, rec: dict) -> int:
    sid = rec.get("sid") or title
    mcount = int(rec.get("monthly_count", 0))
    h = hashlib.sha1(f"{sid}|m|{mcount}".encode("utf-8")).hexdigest()[:8]
    return int(h, 16)

def _handle_title_change_if_needed(list_name: str, item: Dict[str, Any], now: datetime) -> None:
    """
    If the reminder's title changed (vs state via SID), fetch new text, update state,
    and rewrite the note (Monthly = canonical obfuscated; others = plain + [sid]).
    NOTE: If the note body is empty, we defer all work (including SID creation) until
    after content is added by the fill-notes step.
    """
    # Skip manual overrides entirely
    if _has_manual_override(list_name, item):
        return

    # Read current raw body
    raw = get_body_by_id_raw(list_name, item["id"])
    sid = _extract_sid(raw) or None

    # If the note is currently empty, defer SID creation and any title-refresh
    # logic until after content exists (the fill-notes step will add text).
    if not (raw or "").strip():
        return

    # If no SID but content exists, add SID now and refresh raw
    if not sid:
        sid = _ensure_sid_for_title(list_name, item["name"])
        raw = get_body_by_id_raw(list_name, item["id"])

    # Locate the state record via SID; if none, bind current title to this SID
    rec, old_key = _find_record_by_sid(sid) if sid else (None, None)
    current_title = item["name"].strip()
    if rec is None:
        rec = _get_or_init_record(current_title)
        if sid:
            _update_record(current_title, sid=sid)

    # If the title changed vs what state has, rename state key and refresh content
    if _norm_title_key(rec.get("title", "")) != _norm_title_key(current_title):
        new_key = _norm_title_key(current_title)

        # Determine old key and rename if needed
        if old_key is None:
            old_key = _norm_title_key(rec.get("title", ""))
        if old_key and old_key != new_key:
            _rename_state_key(old_key, new_key)

        # Ensure the state's stored 'title' field matches current_title
        state = _load_state()
        verses = state.get("verses", {})
        if new_key not in verses:
            # Initialize if missing
            _get_or_init_record(current_title)
            state = _load_state()
            verses = state.get("verses", {})
        try:
            verses[new_key]["title"] = current_title
            _save_state(state)
        except Exception:
            pass  # defensive: don't block the rest

        # Fetch new canonical text for the new title
        new_text = fetch_scripture_text(current_title)
        if not new_text:
            return  # leave existing note/body intact if fetch failed

        _update_record(current_title, full_text=new_text, full_text_sha=_sha1(new_text))

        # Rewrite the note according to stage
        rec = _get_or_init_record(current_title)
        rec_stage = rec.get("stage", "daily")
        if list_name == MONTHLY or rec_stage == "monthly":
            ratio = _ratio_for_monthly_count(int(rec.get("monthly_count", 0)))
            seed = _monthly_seed(current_title, rec)
            new_body = _note_with_obfuscation(new_text, ratio, seed, sid=sid)
        else:
            new_body = _ensure_plain_body_with_sid(new_text, sid or _new_sid())

        set_body_by_id(list_name, item["id"], new_body)




# ====================================================================
# Backlog → Daily
# ====================================================================
def existing_refs_across_all_lists() -> List[str]:
    titles = []
    for ln in [DAILY, WEEKLY, MONTHLY, BACKLOG]:
        try:
            titles += [x["name"] for x in list_reminders(ln)]
        except Exception:
            pass
    seen = set(); out = []
    for t in titles:
        k = _norm_title_key(t)
        if k not in seen:
            seen.add(k); out.append(t)
    return out

def ensure_list_exists(list_name: str) -> bool:
    script = r'''
    on run argv
      set listName to item 1 of argv
      tell application "Reminders"
        if not (exists (list listName)) then
          make new list with properties {name:listName}
        end if
      end tell
      return "OK"
    end run
    '''
    try:
        return run_as(script, list_name) == "OK"
    except Exception:
        return False

def ensure_all_lists() -> None:
    for ln in [BACKLOG, DAILY, WEEKLY, MONTHLY, MASTERED]:
        ok = ensure_list_exists(ln)
        print(f"[setup] {( 'OK ' if ok else 'ERR')}  {ln}")

def ensure_notes_for(list_name: str, title: str) -> bool:
    """
    If the reminder's notes are blank, fill them.
    Priority:
      1) state.full_text (if present)
      2) API fetch (nephi → bible-api fallback)
    Matching is normalized (casefolded, dash-normalized). Writes by ID.
    """
    want = _norm_title_key(title)
    items = list_reminders(list_name)

    # find by normalized title
    m = None
    for x in items:
        if _norm_title_key(x["name"]) == want:
            m = x
            break
    if not m:
        return False

    # If notes already present, nothing to do
    if (m["body"] or "").strip():
        return True

    # Prefer cached state.full_text when available (avoids API calls)
    rec = _get_or_init_record(m["name"])
    text = (rec.get("full_text") or "").strip()

    # If state doesn't have it, fall back to API
    if not text:
        text = fetch_scripture_text(m["name"]) or fetch_scripture_text(title) or ""

    if not text.strip():
        return False  # no content available

    return set_body_by_id(list_name, m["id"], text.strip())


def maybe_add_new_verse_from_backlog(topic: Optional[str] = None) -> Optional[str]:
    now = datetime.now()

    daily_titles   = {_norm_title_key(x["name"]) for x in list_reminders(DAILY)}
    weekly_titles  = {_norm_title_key(x["name"]) for x in list_reminders(WEEKLY)}
    monthly_titles = {_norm_title_key(x["name"]) for x in list_reminders(MONTHLY)}
    exists_elsewhere = daily_titles | weekly_titles | monthly_titles

    # Pass 1: clean exact duplicates from Backlog
    for r in list_reminders(BACKLOG):
        title = r["name"].strip()
        if _norm_title_key(title) in exists_elsewhere:
            delete_by_id(BACKLOG, r["id"])
            print(f"Cleaned duplicate from Backlog: {title}")

    # Pass 2: clean overlapping Backlog items
    for r in list_reminders(BACKLOG):
        title = r["name"].strip()
        overlapping = ref_overlaps_anywhere(title)
        if overlapping and _norm_title_key(title) != _norm_title_key(overlapping):
            delete_by_id(BACKLOG, r["id"])
            print(f"Cleaned overlapping Backlog item: {title} (overlaps {overlapping})")

    # Pass 3: move first remaining Backlog item → Daily
    for r in list_reminders(BACKLOG):
        title = r["name"].strip()
        create_script = r'''
        on run argv
          set listName to item 1 of argv
          set theTitle to item 2 of argv
          set theBody to item 3 of argv
          tell application "Reminders"
            set theList to first list whose name is listName
            make new reminder at end of reminders of theList with properties {name:theTitle, body:theBody}
          end tell
        end run
        '''
        run_as(create_script, DAILY, title, r["body"])
        delete_by_id(BACKLOG, r["id"])

        set_due_next_morning_8am(DAILY, title)
        _get_or_init_record(title, anchor_weekday=now.weekday())
        ensure_notes_for(DAILY, title)
        _ensure_sid_for_title(DAILY, title)

        next_due = next_morning_8am(now).strftime('%Y-%m-%d 08:00')
        _append_csv_event(title, "daily", "moved-from-backlog", next_due)
        print(f"New verse moved from Backlog → Daily (next review at 8:00 AM): {title}")
        return title

    # Pass 4: Backlog empty → maybe ChatGPT
    if not _chatgpt_allowed_today(now):
        print("[new-verse] Backlog empty, but frequency gate prevents auto-add today.")
        return None

    exclusions = existing_refs_across_all_lists()
    candidate = suggest_reference_via_chatgpt(topic=topic, exclusions=exclusions)
    if not candidate:
        print("[new-verse] ChatGPT did not provide a usable reference.")
        return None

    overlapping = ref_overlaps_anywhere(candidate)
    if overlapping:
        print(f"[new-verse] ChatGPT suggested '{candidate}', but it overlaps existing '{overlapping}'. Skipping.")
        return None

    create_script = r'''
    on run argv
      set listName to item 1 of argv
      set theTitle to item 2 of argv
      tell application "Reminders"
        set theList to first list whose name is listName
        make new reminder at end of reminders of theList with properties {name:theTitle, body:""}
      end tell
    end run
    '''
    run_as(create_script, DAILY, candidate)
    set_due_next_morning_8am(DAILY, candidate)
    _get_or_init_record(candidate, anchor_weekday=now.weekday())
    ensure_notes_for(DAILY, candidate)
    _ensure_sid_for_title(DAILY, candidate)
    _set_last_auto_added_date(now)

    next_due = next_morning_8am(now).strftime('%Y-%m-%d 08:00')
    _append_csv_event(candidate, "daily", "chatgpt-added", next_due)
    print(f"[ChatGPT] Added new verse to Daily (next review at 8:00 AM): {candidate}")
    return candidate


# ====================================================================
# Cadence date helpers
# ====================================================================
def next_morning_8am(base: datetime) -> datetime:
    return (base + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)

def _first_weekday_on_or_after(year: int, month: int, weekday: int, start_day: int = 1) -> datetime:
    d = datetime(year, month, max(1, start_day), 8, 0, 0)
    delta = (weekday - d.weekday()) % 7
    return d + timedelta(days=delta)

def _add_months(dt: datetime, months: int) -> datetime:
    y = dt.year + (dt.month - 1 + months) // 12
    m = (dt.month - 1 + months) % 12 + 1
    last_day = calendar.monthrange(y, m)[1]
    day = min(dt.day, last_day)
    return dt.replace(year=y, month=m, day=day)

def next_same_weekday_8am(anchor_weekday: int, base: datetime) -> datetime:
    days_ahead = (anchor_weekday - base.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    target = base + timedelta(days=days_ahead)
    return target.replace(hour=8, minute=0, second=0, microsecond=0)

def next_same_weekday_in_n_months_8am(anchor_weekday: int, base: datetime, months_ahead: int) -> datetime:
    target = _add_months(base, months_ahead)
    return _first_weekday_on_or_after(target.year, target.month, anchor_weekday, start_day=target.day)


# ====================================================================
# Progress CSV
# ====================================================================
def _append_csv_event(title: str, stage: str, action: str, next_due: str = "") -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        newfile = not os.path.exists(CSV_PATH)
        with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if newfile:
                w.writerow(["timestamp", "title", "stage", "action", "next_due"])
            w.writerow([ts, title, stage, action, next_due])
    except Exception as e:
        _append_log(f"[csv] ERROR {e}")


# ====================================================================
# Monthly note canonicalization + obfuscation refresh
# ====================================================================
def _resolve_full_text_for(title: str, list_name: str, rem_id: str) -> Optional[str]:
    rec = _get_or_init_record(title)
    ft = (rec.get("full_text") or "").strip()
    if ft:
        return ft
    note_raw = get_body_by_id_raw(list_name, rem_id)
    ft = _extract_full_text(note_raw).strip()
    if not ft:
        ft = fetch_scripture_text(title) or ""
    if not ft:
        return None
    _update_record(title, full_text=ft, full_text_sha=_sha1(ft))
    return ft

def _ensure_canonical_monthly_note(title: str, now: datetime) -> bool:
    """
    If item is in Monthly, rebuild the note canonically from state.full_text:
      [obfuscated] + dot buffer + SEPARATOR + [full text] + blank space + [sid:...]
    """
    ln, it = _find_item_across_lists(title)
    if ln != MONTHLY or not it:
        return False

    # Resolve canonical full text (prefer state; else from note; else API)
    full = _resolve_full_text_for(title, ln, it["id"])
    if not full:
        return False

    # Ratio & seed for this monthly count
    rec = _get_or_init_record(title)
    ratio = _ratio_for_monthly_count(int(rec.get("monthly_count", 0)))
    seed = _monthly_seed(title, rec)

    # Ensure we have/keep a SID
    note_raw = get_body_by_id_raw(ln, it["id"])
    sid = _extract_sid(note_raw) or rec.get("sid") or _ensure_sid_for_title(ln, title)

    # Build canonical body and write
    final_body = _note_with_obfuscation(full, ratio, seed, sid=sid)
    return set_body_by_id(ln, it["id"], final_body)



def _ensure_dual_note_for_monthly(title: str, now: datetime) -> bool:
    ln, it = _find_item_across_lists(title)
    if ln != MONTHLY or not it:
        return False
    if _has_manual_override(ln, it):
        return False

    full = _resolve_full_text_for(title, ln, it["id"])
    if not full:
        return False
    rec = _get_or_init_record(title)
    ratio = _ratio_for_monthly_count(int(rec.get("monthly_count", 0)))
    seed  = _monthly_seed(title, rec)
    raw   = get_body_by_id_raw(ln, it["id"])
    sid   = _extract_sid(raw) or rec.get("sid") or _ensure_sid_for_title(ln, title)
    new_body = _note_with_obfuscation(full, ratio, seed, sid=sid)
    return set_body_by_id(MONTHLY, it["id"], new_body)

def _refresh_monthly_obfuscation(title: str, now: datetime) -> None:
    if not OBF_ENABLED:
        return
    try:
        _ensure_dual_note_for_monthly(title, now)
    except Exception as e:
        _append_log(f"[obfuscate] ERROR for '{title}': {e}")

def _ensure_canonical_monthly_note(title: str, now: datetime) -> bool:
    """
    If item is in Monthly, rebuild the note canonically from state.full_text:
      [obfuscated] + dot buffer + SEPARATOR + [full text] + 8 blank lines + [sid:...]
    Optimizations:
      - Use cached raw body reads
      - Skip set_body_by_id if the computed body is identical
    """
    ln, it = _find_item_across_lists(title)
    if ln != MONTHLY or not it:
        return False

    # Resolve canonical full text (prefer state; else from note; else API)
    full = _resolve_full_text_for(title, ln, it["id"])
    if not full:
        return False

    rec = _get_or_init_record(title)
    ratio = _ratio_for_monthly_count(int(rec.get("monthly_count", 0)))
    seed = _monthly_seed(title, rec)

    # Use cached raw body; ensure we have/keep a SID
    note_raw = _get_body_raw_cached(ln, it["id"])
    sid = _extract_sid(note_raw) or rec.get("sid") or _ensure_sid_for_title(ln, title)

    # Build target canonical body
    final_body = _note_with_obfuscation(full, ratio, seed, sid=sid)

    # If already identical, avoid a write
    if note_raw == final_body:
        return True

    ok = set_body_by_id(ln, it["id"], final_body)
    if ok:
        _get_body_raw_cached(ln, it["id"], force_refresh=True)
    return ok

# ====================================================================
# Move / find utilities
# ====================================================================
def _find_item_across_lists(title: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    for ln in [DAILY, WEEKLY, MONTHLY, MASTERED, BACKLOG]:
        items = list_reminders(ln)
        for it in items:
            if _norm_title_key(it["name"]) == _norm_title_key(title):
                return (ln, it)
    return (None, None)

def move_by_title(from_list: str, to_list: str, title: str) -> bool:
    wanted = title.strip().lower()
    items = list_reminders(from_list)
    match = next((x for x in items if x["name"].strip().lower() == wanted), None)
    if not match:
        return False
    create_script = r'''
    on run argv
      set listName to item 1 of argv
      set theTitle to item 2 of argv
      set theBody to item 3 of argv
      tell application "Reminders"
        set theList to first list whose name is listName
        make new reminder at end of reminders of theList with properties {name:theTitle, body:theBody}
      end tell
    end run
    '''
    run_as(create_script, to_list, match["name"], match["body"])
    delete_by_id(from_list, match["id"])
    return True


# ====================================================================
# Advance-on-complete (cadence-aware) + manual override + title refresh
# ====================================================================
def advance_on_complete():
    now = datetime.now()

    # ===== Daily stage =====
    for r in list_reminders(DAILY):
        if _has_manual_override(DAILY, r):
            continue
        _handle_title_change_if_needed(DAILY, r, now)
        if not r["completed"]:
            continue
        title = r["name"]
        rec = _get_or_init_record(title)
        anchor = rec.get("anchor_weekday", now.weekday())

        if rec.get("stage") not in ("weekly", "monthly", "mastered"):
            dcount = int(rec.get("daily_count", 0))
            if dcount + 1 < DAILY_REPEATS:
                due = next_morning_8am(now)
                set_due(DAILY, title, due)
                mark_incomplete_by_title(DAILY, title)
                _update_record(title, stage="daily", daily_count=dcount + 1, anchor_weekday=anchor)
                print(f"[Daily] Rescheduled {title} for {due.strftime('%m/%d/%Y 08:00')}; day {dcount+1}/{DAILY_REPEATS}")
                _append_csv_event(title, "daily", "rescheduled", due.strftime('%Y-%m-%d 08:00'))
            else:
                move_by_title(DAILY, WEEKLY, title)
                wdue = next_same_weekday_8am(anchor, now)
                set_due(WEEKLY, title, wdue)
                mark_incomplete_by_title(WEEKLY, title)
                _update_record(title, stage="weekly", daily_count=DAILY_REPEATS, weekly_count=0, anchor_weekday=anchor)
                print(f"[Daily→Weekly] {title} scheduled {wdue.strftime('%m/%d/%Y 08:00')}")
                _append_csv_event(title, "weekly", "promoted", wdue.strftime('%Y-%m-%d 08:00'))

    # ===== Weekly stage =====
    for r in list_reminders(WEEKLY):
        if _has_manual_override(WEEKLY, r):
            continue
        _handle_title_change_if_needed(WEEKLY, r, now)
        if not r["completed"]:
            continue
        title = r["name"]
        rec = _get_or_init_record(title)
        anchor = rec.get("anchor_weekday", now.weekday())
        wcount = int(rec.get("weekly_count", 0))

        if wcount + 1 < WEEKLY_REPEATS:
            wdue = next_same_weekday_8am(anchor, now)
            set_due(WEEKLY, title, wdue)
            mark_incomplete_by_title(WEEKLY, title)
            _update_record(title, stage="weekly", weekly_count=wcount + 1, anchor_weekday=anchor)
            print(f"[Weekly] Rescheduled {title} for {wdue.strftime('%m/%d/%Y 08:00')}; week {wcount+1}/{WEEKLY_REPEATS}")
            _append_csv_event(title, "weekly", "rescheduled", wdue.strftime('%Y-%m-%d 08:00'))
        else:
            move_by_title(WEEKLY, MONTHLY, title)
            mdue = next_same_weekday_in_n_months_8am(anchor, now, 1)
            set_due(MONTHLY, title, mdue)
            mark_incomplete_by_title(MONTHLY, title)
            _update_record(title, stage="monthly", weekly_count=WEEKLY_REPEATS, monthly_count=0, anchor_weekday=anchor)
            _ensure_canonical_monthly_note(title, now)
            if OBF_ENABLED:
                _refresh_monthly_obfuscation(title, now)
            print(f"[Weekly→Monthly] {title} scheduled {mdue.strftime('%m/%d/%Y 08:00')}")
            _append_csv_event(title, "monthly", "promoted", mdue.strftime('%Y-%m-%d 08:00'))

    # ===== Monthly stage =====
    for r in list_reminders(MONTHLY):
        if _has_manual_override(MONTHLY, r):
            continue
        _handle_title_change_if_needed(MONTHLY, r, now)
        if not r["completed"]:
            continue
        title = r["name"]
        rec = _get_or_init_record(title)
        anchor = rec.get("anchor_weekday", now.weekday())
        mcount = int(rec.get("monthly_count", 0)) + 1

        if mcount >= MONTHLY_REPEATS:
            move_by_title(MONTHLY, MASTERED, title)
            first_gap = MASTERED_REVIEW_MONTHS[0] if MASTERED_REVIEW_MONTHS else MASTERED_YEARLY_INTERVAL
            due = next_same_weekday_in_n_months_8am(anchor, now, first_gap)
            set_due(MASTERED, title, due)
            mark_incomplete_by_title(MASTERED, title)
            _update_record(title, stage="mastered", monthly_count=mcount, mastered_count=0, anchor_weekday=anchor)
            print(f"[Monthly→Mastered] {title} graduated after {MONTHLY_REPEATS} monthly reviews; next {due.strftime('%m/%d/%Y 08:00')}")
            _append_csv_event(title, "mastered", "promoted", due.strftime('%Y-%m-%d 08:00'))
        else:
            due = next_same_weekday_in_n_months_8am(anchor, now, 1)
            set_due(MONTHLY, title, due)
            mark_incomplete_by_title(MONTHLY, title)
            _update_record(title, stage="monthly", monthly_count=mcount, anchor_weekday=anchor)
            _ensure_canonical_monthly_note(title, now)
            if OBF_ENABLED:
                _refresh_monthly_obfuscation(title, now)
            print(f"[Monthly] Rescheduled {title} for {due.strftime('%m/%d/%Y 08:00')} ({mcount}/{MONTHLY_REPEATS})")
            _append_csv_event(title, "monthly", "rescheduled", due.strftime('%Y-%m-%d 08:00'))

    # ===== Mastered stage =====
    for r in list_reminders(MASTERED):
        if _has_manual_override(MASTERED, r):
            continue
        _handle_title_change_if_needed(MASTERED, r, now)
        if not r["completed"]:
            continue
        title = r["name"]
        rec = _get_or_init_record(title)
        anchor = rec.get("anchor_weekday", now.weekday())
        k = int(rec.get("mastered_count", 0)) + 1

        if k < 1:
            gap = MASTERED_REVIEW_MONTHS[0] if MASTERED_REVIEW_MONTHS else MASTERED_YEARLY_INTERVAL
        elif k <= len(MASTERED_REVIEW_MONTHS):
            gap = MASTERED_REVIEW_MONTHS[k - 1]
        else:
            gap = MASTERED_YEARLY_INTERVAL

        due = next_same_weekday_in_n_months_8am(anchor, now, gap)
        set_due(MASTERED, title, due)
        mark_incomplete_by_title(MASTERED, title)
        _update_record(title, stage="mastered", mastered_count=k, anchor_weekday=anchor)
        schedule_label = f"next in {gap} mo" if k <= len(MASTERED_REVIEW_MONTHS) else "next yearly"
        print(f"[Mastered] Rescheduled {title} ({schedule_label}); completed {k} mastered review(s)")
        _append_csv_event(title, "mastered", "rescheduled", due.strftime('%Y-%m-%d 08:00'))


# ====================================================================
# Status / doctor / debug
# ====================================================================
def _weekday_name(ix: int) -> str:
    names = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    try:
        return names[int(ix) % 7]
    except Exception:
        return "?"

def print_status():
    state = _load_state()
    recs = state.get("verses", {})

    all_items = []
    for ln in [DAILY, WEEKLY, MONTHLY, MASTERED, BACKLOG]:
        try:
            for it in list_reminders(ln):
                all_items.append((ln, it))
        except Exception:
            pass
    idx = {_norm_title_key(it["name"]): (ln, it) for (ln, it) in all_items}

    print("\n=== STATUS ===============================================")
    print("Title                                 | Stage     | D/W/M/M* | Anchor | List       | Completed | Due")
    print("----------------------------------------------------------+-----------+------------+--------+------------+-----------+------------------------------")

    def fmt_counts(rec: dict) -> str:
        d = int(rec.get("daily_count", 0))
        w = int(rec.get("weekly_count", 0))
        m = int(rec.get("monthly_count", 0))
        k = int(rec.get("mastered_count", 0))
        return f"D:{d}/{DAILY_REPEATS}-W:{w}/{WEEKLY_REPEATS}-M:{m}/{MONTHLY_REPEATS}-M*:{k}"

    tracked_keys = set()
    for key, rec in sorted(recs.items(), key=lambda kv: kv[1].get("title","")):
        title = rec.get("title") or ""
        tracked_keys.add(_norm_title_key(title))
        stage = (rec.get("stage") or "?").ljust(9)
        counts = fmt_counts(rec).ljust(10)
        anchor = _weekday_name(rec.get("anchor_weekday", 0)).ljust(6)

        ln, it = idx.get(_norm_title_key(title), (None, None))
        list_name   = (ln or "-").ljust(10)
        completed   = ("True" if (it and it.get("completed")) else "False").ljust(9)
        due_display = (it.get("due") if it else "(missing)").strip() if it else "(missing)"

        tcol = (title[:35] + "…") if len(title) > 36 else title.ljust(36)
        print(f"{tcol} | {stage} | {counts} | {anchor} | {list_name} | {completed} | {due_display}")

    orphan_titles = []
    for (ln, it) in all_items:
        if _norm_title_key(it["name"]) not in tracked_keys:
            orphan_titles.append((ln, it))

    if orphan_titles:
        print("\nOrphans (exist in Reminders but not in state):")
        for (ln, it) in orphan_titles:
            print(f"  - [{ln}] {it['name']} | completed={it['completed']} | due={it['due']!r}")

    stale = [recs[k]["title"] for k in recs.keys() if k not in idx]
    if stale:
        print("\nStale (tracked in state but missing from Reminders):")
        for t in stale:
            print(f"  - {t}")
    print("===========================================================\n")

def debug_dump():
    for ln in [DAILY, WEEKLY, MONTHLY, BACKLOG, MASTERED]:
        items = list_reminders(ln)
        print(f"\n== {ln} ==")
        for it in items:
            print(f"  - {it['name']}  | completed={it['completed']}  | due={it['due']!r}")

def dump_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            print(f"\n[STATE] {STATE_PATH}\n" + f.read())
    except Exception:
        print("\n[STATE] (no state file yet)")

def fill_notes_for_daily():
    """
    Daily:
      - Skip #manual_override.
      - If blank, fill notes (prefer state.full_text, fallback API), then ensure SID.
      - If non-blank:
          * If state.full_text missing AND no #manual_override, fetch canonical text,
            overwrite body, ensure SID, cache full_text.
          * If SID missing, ensure SID.
    """
    filled = 0
    now = datetime.now()

    for it in list_reminders(DAILY):
        try:
            if _has_manual_override(DAILY, it):
                continue

            # Title-change refresh acts only if body has content
            _handle_title_change_if_needed(DAILY, it, now)

            raw = get_body_by_id_raw(DAILY, it["id"])
            has_body = bool((raw or "").strip())

            if not has_body:
                # Fill from state.full_text first; fallback to API inside ensure_notes_for()
                if ensure_notes_for(DAILY, it["name"]):
                    _ensure_sid_for_title(DAILY, it["name"])
                    filled += 1
                continue

            # Non-blank: if state.full_text missing and not manual_override, replace with canonical scripture
            rec = _get_or_init_record(it["name"])
            if not (rec.get("full_text") or "").strip():
                # Only replace if the user's note does NOT opt-out
                if "#manual_override" not in (raw or "").casefold() and "#manual_override" not in (it.get("name","").casefold()):
                    fetched = fetch_scripture_text(it["name"]) or ""
                    if fetched.strip():
                        set_body_by_id(DAILY, it["id"], fetched.strip())
                        _ensure_sid_for_title(DAILY, it["name"])
                        _update_record(it["name"], full_text=fetched.strip(), full_text_sha=_sha1(fetched.strip()))
                        continue  # done with this item

            # If we already have content but no SID (e.g., SID was deleted), add one
            if not _extract_sid(raw):
                _ensure_sid_for_title(DAILY, it["name"])

        except Exception as e:
            print(f"[daily] fill-notes error for '{it.get('name','?')}': {e}")

    print(f"Filled notes for {filled} item(s) in Daily.")



def _ingest_full_text_from_note(list_name: str, rem_id: str, title: str) -> None:
    try:
        note_raw = get_body_by_id_raw(list_name, rem_id)
    except Exception:
        note_raw = ""
    full = _extract_full_text(note_raw).strip()
    if full:
        _update_record(title, full_text=full, full_text_sha=_sha1(full))

def fill_notes_for_weekly():
    """
    Weekly:
      - Skip #manual_override.
      - Title-change refresh if body has content.
      - If blank, fill notes (prefer state.full_text, fallback API), then ensure SID + cache full_text.
      - If non-blank:
          * If state.full_text missing AND no #manual_override, fetch canonical text,
            overwrite body, ensure SID, cache full_text.
          * If SID missing, ensure SID.
    """
    filled = 0
    now = datetime.now()

    for it in list_reminders(WEEKLY):
        try:
            if _has_manual_override(WEEKLY, it):
                continue

            _handle_title_change_if_needed(WEEKLY, it, now)

            raw = get_body_by_id_raw(WEEKLY, it["id"])
            has_body = bool((raw or "").strip())

            if not has_body:
                if ensure_notes_for(WEEKLY, it["name"]):
                    filled += 1
                    _ensure_sid_for_title(WEEKLY, it["name"])
                    _ingest_full_text_from_note(WEEKLY, it["id"], it["name"])
                continue

            rec = _get_or_init_record(it["name"])
            if not (rec.get("full_text") or "").strip():
                if "#manual_override" not in (raw or "").casefold() and "#manual_override" not in (it.get("name","").casefold()):
                    fetched = fetch_scripture_text(it["name"]) or ""
                    if fetched.strip():
                        set_body_by_id(WEEKLY, it["id"], fetched.strip())
                        _ensure_sid_for_title(WEEKLY, it["name"])
                        _update_record(it["name"], full_text=fetched.strip(), full_text_sha=_sha1(fetched.strip()))
                        continue

            if not _extract_sid(raw):
                _ensure_sid_for_title(WEEKLY, it["name"])

            if not (rec.get("full_text") or "").strip():
                _ingest_full_text_from_note(WEEKLY, it["id"], it["name"])

        except Exception as e:
            print(f"[weekly] fill-notes error for '{it.get('name','?')}': {e}")

    print(f"Filled notes for {filled} item(s) in Weekly.")


def fill_notes_for_monthly():
    """
    Monthly:
      - Skip #manual_override.
      - Title-change refresh if body has content.
      - If blank, fill notes (prefer state.full_text, fallback API), ensure SID, cache full_text, canonicalize.
      - If non-blank:
          * If state.full_text missing AND no #manual_override, fetch canonical text,
            overwrite body, ensure SID, cache full_text, then canonicalize.
          * Else ensure SID and state.full_text, then canonicalize (keeps schedule).
    """
    filled = 0
    now = datetime.now()

    for it in list_reminders(MONTHLY):
        try:
            if _has_manual_override(MONTHLY, it):
                continue

            _handle_title_change_if_needed(MONTHLY, it, now)

            raw = get_body_by_id_raw(MONTHLY, it["id"])
            has_body = bool((raw or "").strip())

            if not has_body:
                if ensure_notes_for(MONTHLY, it["name"]):
                    filled += 1
                    _ensure_sid_for_title(MONTHLY, it["name"])
                    _ingest_full_text_from_note(MONTHLY, it["id"], it["name"])
                    _ensure_canonical_monthly_note(it["name"], now)
                continue

            rec = _get_or_init_record(it["name"])
            replaced = False
            if not (rec.get("full_text") or "").strip():
                if "#manual_override" not in (raw or "").casefold() and "#manual_override" not in (it.get("name","").casefold()):
                    fetched = fetch_scripture_text(it["name"]) or ""
                    if fetched.strip():
                        set_body_by_id(MONTHLY, it["id"], fetched.strip())
                        _ensure_sid_for_title(MONTHLY, it["name"])
                        _update_record(it["name"], full_text=fetched.strip(), full_text_sha=_sha1(fetched.strip()))
                        replaced = True

            # Ensure SID if still missing
            if not replaced and not _extract_sid(raw):
                _ensure_sid_for_title(MONTHLY, it["name"])

            # Ensure full_text cached if still missing (extract from note)
            rec = _get_or_init_record(it["name"])
            if not (rec.get("full_text") or "").strip():
                _ingest_full_text_from_note(MONTHLY, it["id"], it["name"])

            # Always canonicalize in Monthly
            _ensure_canonical_monthly_note(it["name"], now)

        except Exception as e:
            print(f"[monthly] fill-notes error for '{it.get('name','?')}': {e}")

    print(f"Filled notes for {filled} item(s) in Monthly.")

# --- per-run cache for raw note reads (list_name, rem_id) -> raw_body
_RAW_BODY_CACHE: dict[tuple[str, str], str] = {}

def _get_body_raw_cached(list_name: str, rem_id: str, *, force_refresh: bool = False) -> str:
    """
    Single-run cache for get_body_by_id_raw. Avoids repeated AppleScript reads.
    Use force_refresh=True after you write to the body to keep cache coherent.
    """
    key = (list_name, rem_id)
    if not force_refresh and key in _RAW_BODY_CACHE:
        return _RAW_BODY_CACHE[key]
    raw = get_body_by_id_raw(list_name, rem_id)
    _RAW_BODY_CACHE[key] = raw
    return raw


def doctor():
    print("\n=== scripture_agent doctor ===")
    try:
        cfg = load_or_init_config()
        apply_config(cfg)
        print("[config] loaded config")
    except Exception as e:
        print(f"[config] ERROR: {e}")

    print("[lists] ensuring all lists exist…")
    ensure_all_lists()

    # API reachability probe
    probe_ref = "1 Nephi 1:1"
    print(f"[nephi] test-fetch '{probe_ref}' …")
    txt = fetch_scripture_text(probe_ref)
    if txt:
        snippet = " ".join([ln.strip() for ln in txt.splitlines() if ln.strip()][:2])
        print(f"[nephi] OK  (preview: {snippet[:120]}{'…' if len(snippet)>120 else ''})")
    else:
        print("[nephi] WARN  could not retrieve sample passage; check network/API")

    # OpenAI key presence (no call, just a heads-up)
    key = os.environ.get("OPENAI_API_KEY", "")
    if key:
        print("[openai] OK  OPENAI_API_KEY present")
    else:
        print("[openai] WARN  OPENAI_API_KEY not set (ChatGPT fallback won’t run)")

    # Integrity audit (+ optional repair with --fix)
    fix = any(arg.strip().lower() == "--fix" for arg in sys.argv[2:])
    _ensure_state_integrity(fix=fix)

    # Final state count
    st = _load_state()
    if isinstance(st, dict) and "verses" in st:
        print(f"[state] OK  {len(st.get('verses', {}))} tracked verse(s)")
    else:
        print("[state] WARN  could not read state file")

    print("=== doctor done ===\n")



def _is_canonical_monthly_note(note: str) -> bool:
    """
    Heuristic check for the canonical Monthly layout:
      [OBFUSCATED TEXT]

      .         (buffer token lines)
      .
      .
      ______________________________
      [FULL ORIGINAL TEXT]


      [sid:UUID]
    We require: separator present, [sid:...] present, and at least one buffer token line.
    """
    if not (note or "").strip():
        return False
    if "[sid:" not in note:
        return False

    sep = globals().get("OBF_SEPARATOR", "\n\n______________________________\n")
    if sep not in note:
        return False

    buf_token = str(globals().get("OBF_BUFFER_TOKEN", "."))
    if buf_token not in note:
        return False

    # Basic shape: something before sep and something after sep
    parts = note.rsplit(sep, 1)
    if len(parts) != 2:
        return False
    if not parts[0].strip() or not parts[1].strip():
        return False

    return True


def _ensure_state_integrity(fix: bool = False) -> None:
    """
    Scan Daily/Weekly/Monthly/Mastered for structural issues and (optionally) repair:
      - Missing SID (only checked when note HAS body text)
      - Missing state.full_text (we DO NOT count it as missing if it can be
        derived from the current note body; with --fix we will cache it)
      - Monthly notes not in canonical layout

    Prints a concise summary at the end.
    """
    lists = [DAILY, WEEKLY, MONTHLY, MASTERED]
    now = datetime.now()
    counts = {
        "missing_sid": 0,
        "missing_full_text": 0,
        "non_canonical_monthly": 0,
        "fixed": 0
    }

    for ln in lists:
        for it in list_reminders(ln):
            try:
                # Respect manual override completely
                if "_has_manual_override" in globals() and _has_manual_override(ln, it):
                    continue

                title = it["name"].strip()
                rec = _get_or_init_record(title)

                # Raw note body and quick flags
                raw = get_body_by_id_raw(ln, it["id"])
                has_body = bool((raw or "").strip())
                sid = _extract_sid(raw)

                # (1) Missing SID: only consider when the note HAS body text
                if has_body and not sid:
                    counts["missing_sid"] += 1
                    if fix:
                        _ensure_sid_for_title(ln, title)
                        counts["fixed"] += 1
                        # refresh values after fix
                        raw = get_body_by_id_raw(ln, it["id"])
                        has_body = bool((raw or "").strip())
                        sid = _extract_sid(raw)

                # (2) Missing full_text in state:
                #     If the note HAS body text and we can derive full_text from it,
                #     do NOT count as missing. With --fix, cache it into state.
                if not (rec.get("full_text") or "").strip():
                    derived = ""
                    if has_body:
                        derived = (_extract_full_text(raw) or "").strip()

                    if derived:
                        # We can derive full_text from the note → not "missing".
                        if fix:
                            _update_record(title, full_text=derived, full_text_sha=_sha1(derived))
                            counts["fixed"] += 1
                    else:
                        # Truly missing: blank note (or un-derivable)
                        counts["missing_full_text"] += 1
                        if fix:
                            # Only fetch on --fix; dry runs should not mutate or spend network.
                            fetched = fetch_scripture_text(title) or ""
                            if fetched:
                                _update_record(title, full_text=fetched, full_text_sha=_sha1(fetched))
                                counts["fixed"] += 1

                # (3) Monthly canonical layout drift
                if ln == MONTHLY and has_body and not _is_canonical_monthly_note(raw):
                    counts["non_canonical_monthly"] += 1
                    if fix:
                        # Rebuild canonical body (threads SID, ratio, seed)
                        if _ensure_canonical_monthly_note(title, now):
                            counts["fixed"] += 1

            except Exception as e:
                print(f"[doctor] error on '{it.get('name','?')}' in [{ln}]: {e}")

    # Clarify that missing_sid only considers items with body text.
    print(
        "[doctor] integrity: "
        f"missing_sid={counts['missing_sid']} (only checked when note has text)  "
        f"missing_full_text={counts['missing_full_text']}  "
        f"monthly_noncanonical={counts['non_canonical_monthly']}  "
        f"fixed={counts['fixed']}"
    )




# ====================================================================
# run-daily orchestration
# ====================================================================
def _chatgpt_allowed_today(now: datetime) -> bool:
    if AUTO_ADD_EVERY_N_DAYS <= 0:
        return True
    last = _get_last_auto_added_date()
    if not last:
        return True
    return (now.date() - last.date()).days >= AUTO_ADD_EVERY_N_DAYS

def run_daily(topic_arg: Optional[str] = None):
    """
    One 'daily' run (lightweight):
      1) advance_on_complete()
      2) fill_notes_for_daily()
      3) maybe_add_new_verse_from_backlog()

    NOTE: Weekly/Monthly maintenance can be run manually via:
      - python3 scripture_agent.py fill-notes
      - python3 scripture_agent.py doctor [--fix]
    """
    cfg = load_or_init_config()  # ensure config loaded
    apply_config(cfg)

    # Pick topic: CLI beats config; empty string means "no topic"
    cfg_topic = (cfg.get("auto_add", {}) or {}).get("topic_default", "") or None
    topic = topic_arg if (topic_arg and topic_arg.strip()) else cfg_topic

    _append_log("run-daily: start")

    try:
        advance_on_complete()
        _append_log("run-daily: advance_on_complete OK")
    except Exception as e:
        _append_log(f"run-daily: advance_on_complete ERROR: {e}")

    try:
        fill_notes_for_daily()
        _append_log("run-daily: fill_notes_for_daily OK")
    except Exception as e:
        _append_log(f"run-daily: fill_notes_for_daily ERROR: {e}")

    try:
        moved_or_added = maybe_add_new_verse_from_backlog(topic=topic)
        if moved_or_added:
            _append_log(f"run-daily: new verse added/moved → {moved_or_added}")
        else:
            _append_log("run-daily: no new verse added/moved")
    except Exception as e:
        _append_log(f"run-daily: maybe_add_new_verse_from_backlog ERROR: {e}")

    _append_log("run-daily: done")


# ====================================================================
# Manual override helpers
# ====================================================================
def _has_tag_by_id(list_name: str, rem_id: str, tag_name: str) -> Optional[bool]:
    """
    Return True if the reminder has a Reminders tag with the given name.
    Return False if tags are supported but not present.
    Return None if tags are not supported on this macOS/Reminders version.
    """
    script = r'''
    on run argv
      set listName to item 1 of argv
      set rid to item 2 of argv
      set tname to item 3 of argv
      try
        tell application "Reminders"
          if not (exists (list listName)) then return "NO_LIST"
          set theList to first list whose name is listName
          set r to first reminder of theList whose id is rid
          try
            set tagNames to {}
            repeat with tg in tags of r
              set end of tagNames to (name of tg as text)
            end repeat
            if tagNames contains tname then
              return "YES"
            else
              return "NO"
            end if
          on error
            -- Property 'tags' may not exist on older macOS versions
            return "NO_TAGS"
          end try
        end tell
      on error
        return "ERR"
      end try
    end run
    '''
    try:
        res = run_as(script, list_name, rem_id, tag_name)
    except Exception:
        return None
    if res == "YES":
        return True
    if res in ("NO", "NO_LIST"):
        return False
    if res in ("NO_TAGS", "ERR"):
        return None
    return None

def _has_manual_override(list_name: str, item: Dict[str, Any]) -> bool:
    """
    Returns True if this reminder should be ignored by the agent.
    Detection:
      - '#manual_override' anywhere in title (case-insensitive)
      - '#manual_override' anywhere in note body (case-insensitive)
    No Reminders tag API calls (removed for performance).
    """
    try:
        title = (item.get("name") or "")
        if "#manual_override" in title.casefold():
            return True

        # Quick check: flattened body
        flat = (item.get("body") or "").casefold()
        if "#manual_override" in flat:
            return True

        # Fallback: raw note body (ensures we catch manual_override even if not visible in flattened body)
        raw = get_body_by_id_raw(list_name, item["id"])
        if "#manual_override" in (raw or "").casefold():
            return True

    except Exception:
        return False

    return False



# ====================================================================
# CLI
# ====================================================================
def cli_test_fetch(ref: str) -> None:
    txt = fetch_scripture_text(ref)
    if not txt:
        print("(no text returned)")
        return
    preview = [line for line in txt.splitlines() if line.strip()]
    print("\n".join(preview[:6]))
    if len(preview) > 6:
        print("... (truncated)")

def ensure_all_lists_cmd():
    ensure_all_lists()

def main():
    cfg = load_or_init_config()
    apply_config(cfg)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "new-verse":
        cfg = load_or_init_config()
        apply_config(cfg)
        cfg_topic = (cfg.get("auto_add", {}) or {}).get("topic_default", "") or None
        cli_topic = " ".join(sys.argv[2:]).strip() if len(sys.argv) > 2 else None
        topic = cli_topic if (cli_topic and cli_topic.strip()) else cfg_topic
        maybe_add_new_verse_from_backlog(topic=topic)
        debug_dump()

    elif cmd == "advance":
        advance_on_complete()
        debug_dump()

    elif cmd == "fill-notes":
        fill_notes_for_daily()
        fill_notes_for_weekly()
        fill_notes_for_monthly()
        debug_dump()

    elif cmd == "state":
        dump_state()

    elif cmd == "test-fetch":
        ref = " ".join(sys.argv[2:]).strip()
        if not ref:
            print('Usage: python scripture_agent.py test-fetch "Book Chapter:Verse[-Verse]"')
        else:
            cli_test_fetch(ref)

    elif cmd == "config":
        cfg = load_or_init_config()
        apply_config(cfg)
        print(json.dumps(cfg, indent=2))

    elif cmd == "status":
        print_status()

    elif cmd == "setup":
        ensure_all_lists_cmd()

    elif cmd == "doctor":
        doctor()

    elif cmd == "run-daily":
        topic = " ".join(sys.argv[2:]).strip() if len(sys.argv) > 2 else None
        run_daily(topic_arg=topic)

    else:
        print("Usage:")
        print("  python scripture_agent.py new-verse [topic]")
        print("  python scripture_agent.py advance")
        print("  python scripture_agent.py fill-notes")
        print('  python scripture_agent.py test-fetch "Mosiah 2:21-22"')
        print("  python scripture_agent.py state")
        print("  python scripture_agent.py config")
        print("  python scripture_agent.py status")
        print("  python scripture_agent.py setup")
        print("  python scripture_agent.py doctor")
        print("  python scripture_agent.py run-daily [topic]")

if __name__ == "__main__":
    main()
