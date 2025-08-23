#!/usr/bin/env python3
"""
Scripture Memorization Agent for Apple Reminders

- Uses AppleScript via `osascript` (no Shortcuts) from Python
- Lists: Backlog, Daily, Weekly, Monthly
- Backlog → Daily (no duplicates), due set to next morning 8:00 AM
- Cadence per Featherstone: Daily repeats then Weekly then Monthly
- Notes auto-fill:
    * Fetch from scripture API(s) based on the reminder title (single contiguous ref)
    * Multi-verse notes are formatted as separate paragraphs with one blank line between
"""

import os
import sys
import json
import calendar
import re
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
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
DAILY_REPEATS   = 7   # review daily for 7 days (set to 2 for quick testing)
WEEKLY_REPEATS  = 4   # review weekly for 4 weeks (set to 2 for quick testing)
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
# LDS canon capable; expects spaces as '+' in q=... (use urlencode → quote_plus)
NEPHI_API_BASE = "https://api.nephi.org/scriptures/"
# Bible-only fallback (KJV); supports refs like "John 3:16-17"
BIBLE_API_BASE = "https://bible-api.com/"

# ----- Auto-add frequency gate -----
AUTO_ADD_EVERY_N_DAYS = 0  # set to 1 for daily, 7 for weekly, 0 to disable gate


# ====================================================================
# AppleScript runner
# ====================================================================
def run_as(script: str, *args: str) -> str:
    return subprocess.run(
        ["osascript", "-e", script, *args],
        check=True, capture_output=True, text=True
    ).stdout.strip()


# ========= Config (JSON, no deps) =========
def _append_log(line: str) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {line}\n")
    except Exception:
        pass

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
        "every_n_days": 7
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
    },
    "maintenance": {
    "doctor_fix_cadence_days": 7  # 0 = disabled; run at most once every N days
    }

}

def _ensure_config_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)

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

def load_or_init_config() -> dict:
    _ensure_config_dir()
    cfg = _load_json(CONFIG_PATH)
    if cfg is None:
        cfg = DEFAULT_CONFIG
        _save_json(CONFIG_PATH, cfg)
        print(f"[config] created default config at {CONFIG_PATH}")
    return cfg

def _get_last_doctor_fix_date() -> Optional[datetime]:
    st = _load_state()
    iso = st.get("last_doctor_fix")
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except Exception:
        return None

def _set_last_doctor_fix_date(d: datetime) -> None:
    st = _load_state()
    st["last_doctor_fix"] = d.isoformat()
    _save_state(st)

def _run_scheduled_fix_if_due(now: datetime) -> None:
    """
    If maintenance cadence is configured and we're due, run a lightweight '--fix':
      - SID sweep on Daily/Weekly/Monthly
      - Title-change repair (SID-anchored)
    Records the run time in state to avoid re-running too frequently.
    """
    try:
        cfg = load_or_init_config()
        apply_config(cfg)
        cadence = int((cfg.get("maintenance", {}) or {}).get("doctor_fix_cadence_days", 0))
        if cadence <= 0:
            return

        last = _get_last_doctor_fix_date()
        if last and (now.date() - last.date()).days < cadence:
            return  # not due yet

        # Perform the same actions as doctor --fix, but quiet
        total_added = 0
        for ln in (DAILY, WEEKLY, MONTHLY):
            total_added += sid_sweep_for_list(ln)
        migrated = _doctor_title_change_repair()

        _append_log(f"scheduled-fix: SID+repair added_sids={total_added}, migrated={migrated}")
        _set_last_doctor_fix_date(now)
    except Exception as e:
        _append_log(f"scheduled-fix ERROR: {e}")


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
    MASTERED_REVIEW_MONTHS = list(cfg.get("mastered", {}).get("review_months",   DEFAULT_CONFIG["mastered"]["review_months"]))
    MASTERED_YEARLY_INTERVAL = int(cfg.get("mastered", {}).get("yearly_interval", DEFAULT_CONFIG["mastered"]["yearly_interval"]))

    global AUTO_ADD_EVERY_N_DAYS
    AUTO_ADD_EVERY_N_DAYS = int(cfg.get("auto_add", {}).get("every_n_days", DEFAULT_CONFIG["auto_add"]["every_n_days"]))

    # Verse Obfuscation
    global OBF_ENABLED, OBF_SEPARATOR, OBF_SCHEDULE, OBF_MIN_LEN, OBF_KEEP_FL, OBF_RESPECT_PUNCT
    obf = cfg.get("obfuscation", {}) or {}
    OBF_ENABLED = bool(obf.get("enabled", True))
    OBF_SEPARATOR = obf.get("separator", DEFAULT_CONFIG["obfuscation"]["separator"])
    OBF_SCHEDULE = list(obf.get("schedule", DEFAULT_CONFIG["obfuscation"]["schedule"]))
    OBF_MIN_LEN = int(obf.get("min_word_len", DEFAULT_CONFIG["obfuscation"]["min_word_len"]))
    OBF_KEEP_FL = bool(obf.get("keep_first_last", DEFAULT_CONFIG["obfuscation"]["keep_first_last"]))
    OBF_RESPECT_PUNCT = bool(obf.get("respect_punctuation", DEFAULT_CONFIG["obfuscation"]["respect_punctuation"]))

    global OBF_BUFFER_LINES, OBF_BUFFER_TOKEN
    OBF_BUFFER_LINES = int(obf.get("buffer_lines", DEFAULT_CONFIG["obfuscation"]["buffer_lines"]))
    OBF_BUFFER_TOKEN = str(obf.get("buffer_token", DEFAULT_CONFIG["obfuscation"]["buffer_token"]))


# ====================================================================
# Readers
# ====================================================================
def list_reminders(list_name: str):
    """
    Returns [{'id': str, 'name': str, 'body': str, 'completed': bool, 'due': str}]
    (Sanitizes CR/LF so each reminder is one line.)
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
    items = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("␞")
        if len(parts) != 5:
            continue  # defensive
        rid, name, body, completed, due = parts
        items.append({
            "id": rid,
            "name": name,
            "body": body,
            "completed": (completed.strip().lower() == "true"),
            "due": due
        })
    return items

# ====================================================================
# Writers
# ====================================================================
def set_notes(list_name: str, title: str, notes: str) -> bool:
    script = r'''
    on run argv
      set listName to item 1 of argv
      set theTitle to item 2 of argv
      set theBody to item 3 of argv
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
          set body of targetRem to theBody
          return "OK"
        end if
      end tell
    end run
    '''
    res = run_as(script, list_name, title, notes)
    return res == "OK"

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
    """Robust AppleScript date construction for next morning 08:00 local."""
    dt_target = (datetime.now() + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    y = str(dt_target.year)
    m = str(dt_target.month)   # 1-12
    d = str(dt_target.day)
    hh = str(dt_target.hour)   # 8
    mm = str(dt_target.minute) # 0

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


# ====================================================================
# Scripture HTTP helpers (no extra deps)
# ====================================================================

# --- Reference parsing & overlap detection ---
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
    b_clean = re.sub(r"\s{2,}", " ", b).strip()
    return b_clean

def parse_reference(ref: str):
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
    ref = re.sub(r"\s+", " ", ref)
    return ref

# Clean & format helpers for verses
_LEADING_VERSE_NUM_RE = re.compile(r"^\s*(\d+[:\u00A0\s]+)?(\d+)\s+")
_BRACKETED_NUM_RE = re.compile(r"^\s*\[?\d+\]?\s*")

def _clean_line(s: str) -> str:
    s = s.replace("\u00A0", " ")
    s = s.strip()
    s = _LEADING_VERSE_NUM_RE.sub("", s)
    s = _BRACKETED_NUM_RE.sub("", s)
    s = re.sub(r"^[a-z]\s+", "", s)  # strip leading footnote letters like 'a'
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


# ====================================================================
# ChatGPT fallback (optional)
# ====================================================================
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

def suggest_reference_via_chatgpt(topic: Optional[str] = None, exclusions: Optional[list[str]] = None) -> Optional[str]:
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
        "Do NOT include multiple disjoint references. "
        "Do NOT include commentary—reply with only the reference."
    )

    text = _openai_chat(user_prompt, model="gpt-4o-mini")
    ref = _extract_reference(text or "")
    return ref


# ====================================================================
# Notes fill (ID-based, normalized matching)
# ====================================================================
def _norm_title_key(t: str) -> str:
    return (t or "").strip().casefold().replace("–", "-")

def _contains_manual_override(note: str) -> bool:
    if not note:
        return False
    return "#manual_override" in note.casefold()

_SID_RE = re.compile(r"\[sid:([0-9a-fA-F-]{36})\]\s*$")

def _new_sid() -> str:
    return str(uuid.uuid4())

def _extract_sid(note: str) -> Optional[str]:
    if not note:
        return None
    match = re.search(r"\[sid:([0-9a-fA-F-]{36})\]", note)
    return match.group(1) if match else None

def _append_sid(note: str, sid: str) -> str:
    """
    Append the SID far down in the note body with blank spacer lines,
    without disturbing existing verse formatting.
    """
    base = (note or "").rstrip()
    spacer = "\n" * 8  # <-- canonical: 8 newline spacer
    return f"{base}{spacer}[sid:{sid}]"

def _contains_manual_override(note: str) -> bool:
    """True if the note includes '#manual_override' (case-insensitive)."""
    if not note:
        return False
    return "#manual_override" in note.casefold()


def ensure_notes_for(list_name: str, title: str) -> bool:
    """
    Ensure the reminder has canonical scripture text in its notes.
    Rules:
      - Prefer state.full_text if present; otherwise fetch via API.
      - If the note body is non-empty BUT contains '#manual_override' (any case),
        leave it untouched and return True.
      - If the note body is non-empty and NO '#manual_override', replace it with the canonical text.
      - Preserve existing [sid:...] if present (append it back at the very bottom with spacing).
      - For Monthly, this function is only called from fill_notes_for_monthly() when body is blank.
        Obfuscation/canonical layout is handled there, not here.
    """
    want = _norm_title_key(title)
    items = list_reminders(list_name)

    # Find by normalized title
    it = None
    for x in items:
        if _norm_title_key(x["name"]) == want:
            it = x
            break
    if not it:
        return False

    # Raw body (to preserve newlines and detect manual_override and SID)
    raw_body = get_body_by_id_raw(list_name, it["id"])
    sid = _extract_sid(raw_body)

    if _contains_manual_override(raw_body):
        return True

    rec = _get_or_init_record(title)
    canonical = (rec.get("full_text") or "").strip()
    if not canonical:
        canonical = (fetch_scripture_text(it["name"]) or fetch_scripture_text(title) or "").strip()
        if not canonical:
            return False
        _update_record(title, full_text=canonical, full_text_sha=_sha1(canonical))

    new_body = canonical
    if sid:
        new_body = _append_sid(new_body, sid)

    return set_body_by_id(list_name, it["id"], new_body)

def ensure_notes_for_by_id(list_name: str, rem_id: str, title: str) -> bool:
    """
    ID-based variant to avoid a list scan when we already have the reminder ID.
    Mirrors ensure_notes_for() behavior (state-first, respects #manual_override, preserves SID).
    """
    raw_body = get_body_by_id_raw(list_name, rem_id)
    sid = _extract_sid(raw_body)

    if _contains_manual_override(raw_body):
        return True

    rec = _get_or_init_record(title)
    canonical = (rec.get("full_text") or "").strip()
    if not canonical:
        # Try API only if state is empty
        canonical = (fetch_scripture_text(title) or "").strip()
        if not canonical:
            return False
        _update_record(title, full_text=canonical, full_text_sha=_sha1(canonical))

    new_body = canonical
    if sid:
        new_body = _append_sid(new_body, sid)

    return set_body_by_id(list_name, rem_id, new_body)


# ====================================================================
# Backlog → Daily
# ====================================================================
def _norm_title(t: str) -> str:
    return (t or "").strip().casefold().replace("–", "-")

def maybe_add_new_verse_from_backlog(topic: Optional[str] = None) -> Optional[str]:
    """
    1) Clean ALL exact duplicates in Backlog that already exist in Daily/Weekly/Monthly.
    2) Clean Backlog items that OVERLAP any existing title across lists (range-overlap).
    3) Move the FIRST remaining Backlog item into Daily.
       - Set due to next morning 08:00
       - Init cadence state with today's weekday (anchor)
       - Fill notes via API if blank
       - CSV log "moved-from-backlog"
    4) If Backlog is empty and frequency gate allows:
       - Ask ChatGPT once for a contiguous reference (optionally guided by `topic`)
       - Pass an exclusions list to avoid duplicates/overlaps
       - If it still overlaps, skip (no further model calls)
       - Create in Daily, set due 8:00, init state, fill notes
       - CSV log "chatgpt-added"
    Returns the moved/created title, or None if nothing done.
    """
    now = datetime.now()

    # Collect existing titles (normalized string compare for exact dup pass)
    daily_titles   = {_norm_title(x["name"]) for x in list_reminders(DAILY)}
    weekly_titles  = {_norm_title(x["name"]) for x in list_reminders(WEEKLY)}
    monthly_titles = {_norm_title(x["name"]) for x in list_reminders(MONTHLY)}
    exists_elsewhere = daily_titles | weekly_titles | monthly_titles

    # -------- Pass 1: clean exact duplicates from Backlog --------
    backlog_items = list_reminders(BACKLOG)
    for r in backlog_items:
        title = r["name"].strip()
        if _norm_title(title) in exists_elsewhere:
            delete_by_id(BACKLOG, r["id"])
            print(f"Cleaned duplicate from Backlog: {title}")

    # -------- Pass 2: clean overlapping Backlog items --------
    backlog_items = list_reminders(BACKLOG)  # refresh after exact dup cleanup
    for r in backlog_items:
        title = r["name"].strip()
        overlapping = ref_overlaps_anywhere(title)
        if overlapping and _norm_title(title) != _norm_title(overlapping):
            delete_by_id(BACKLOG, r["id"])
            print(f"Cleaned overlapping Backlog item: {title} (overlaps {overlapping})")

    # -------- Pass 3: move the first remaining Backlog item to Daily --------
    for r in list_reminders(BACKLOG):
        title = r["name"].strip()

        # Create in Daily
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

        # Delete from Backlog
        delete_by_id(BACKLOG, r["id"])

        # Due next morning 8:00 + init cadence + ensure notes
        set_due_next_morning_8am(DAILY, title)
        _get_or_init_record(title, anchor_weekday=now.weekday())
        ensure_notes_for(DAILY, title)
        _ensure_sid_for_title(DAILY, title)

        # CSV log
        next_due = next_morning_8am(now).strftime('%Y-%m-%d 08:00')
        _append_csv_event(title, "daily", "moved-from-backlog", next_due)

        print(f"New verse moved from Backlog → Daily (next review at 8:00 AM): {title}")
        return title

    # -------- Pass 4: Backlog empty → maybe ask ChatGPT --------
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
        print(f"[new-verse] ChatGPT suggested '{candidate}', but it overlaps existing '{overlapping}'. "
              f"Skipping add to avoid duplicates (no further calls).")
        return None

    # Create in Daily with empty body first
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

    # CSV log
    next_due = next_morning_8am(now).strftime('%Y-%m-%d 08:00')
    _append_csv_event(candidate, "daily", "chatgpt-added", next_due)

    print(f"[ChatGPT] Added new verse to Daily (next review at 8:00 AM): {candidate}")
    return candidate


# ====================================================================
# Cadence state helpers
# ====================================================================
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
    key = _norm_title(title)
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
    key = _norm_title(title)
    state = _load_state()
    rec = state["verses"].setdefault(key, {
        "title": title.strip(),
        "stage": "daily",
        "daily_count": 0,
        "weekly_count": 0,
        "monthly_count": 0,
        "mastered_count": 0,
        "anchor_weekday": datetime.now().weekday(),
    })
    rec.update(changes)
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

def _chatgpt_allowed_today(now: datetime) -> bool:
    if AUTO_ADD_EVERY_N_DAYS <= 0:
        return True
    last = _get_last_auto_added_date()
    if not last:
        return True
    return (now.date() - last.date()).days >= AUTO_ADD_EVERY_N_DAYS

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

def next_same_weekday_in_n_months_8am(anchor_weekday: int, base: datetime, months_ahead: int) -> datetime:
    target = _add_months(base, months_ahead)
    return _first_weekday_on_or_after(
        target.year,
        target.month,
        anchor_weekday,
        start_day=target.day
    )

# ====================================================================
# Cadence date helpers
# ====================================================================
def next_morning_8am(base: datetime) -> datetime:
    return (base + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)

def next_same_weekday_8am(anchor_weekday: int, base: datetime) -> datetime:
    days_ahead = (anchor_weekday - base.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    target = base + timedelta(days=days_ahead)
    return target.replace(hour=8, minute=0, second=0, microsecond=0)

# ====================================================================
# Advance-on-complete (cadence-aware)
# ====================================================================
def move_by_title(from_list: str, to_list: str, title: str) -> bool:
    wanted = title.strip().lower()
    items = list_reminders(from_list)
    match = next((x for x in items if x["name"].strip().lower() == wanted), None)
    if not match:
        return False

    # create in destination
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

    # delete original by ID (robust)
    delete_by_id(from_list, match["id"])
    return True

def advance_on_complete():
    now = datetime.now()

    # ===== Daily stage =====
    for r in list_reminders(DAILY):
        if not r["completed"]:
            continue
        title = r["name"]
        _maybe_migrate_state_on_touch(DAILY, r)  # opportunistic SID-based title migration (no extra I/O)
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
                # Move to Weekly
                move_by_title(DAILY, WEEKLY, title)
                wdue = next_same_weekday_8am(anchor, now)
                set_due(WEEKLY, title, wdue)
                mark_incomplete_by_title(WEEKLY, title)
                _update_record(title, stage="weekly", daily_count=DAILY_REPEATS, weekly_count=0, anchor_weekday=anchor)
                print(f"[Daily→Weekly] {title} scheduled {wdue.strftime('%m/%d/%Y 08:00')}")
                _append_csv_event(title, "weekly", "promoted", wdue.strftime('%Y-%m-%d 08:00'))

    # ===== Weekly stage =====
    for r in list_reminders(WEEKLY):
        if not r["completed"]:
            continue
        title = r["name"]
        _maybe_migrate_state_on_touch(WEEKLY, r)  # opportunistic SID-based title migration (no extra I/O)
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
            # Move to Monthly and init monthly_count
            move_by_title(WEEKLY, MONTHLY, title)
            mdue = next_same_weekday_in_n_months_8am(anchor, now, 1)
            set_due(MONTHLY, title, mdue)
            mark_incomplete_by_title(MONTHLY, title)
            _update_record(title, stage="monthly", weekly_count=WEEKLY_REPEATS, monthly_count=0, anchor_weekday=anchor)
            _ensure_canonical_monthly_note(title, now)
            if OBF_ENABLED:
                _roll_obf_salt(title)                  # NEW: new month → new pattern
                _refresh_monthly_obfuscation(title, now)
            print(f"[Weekly→Monthly] {title} scheduled {mdue.strftime('%m/%d/%Y 08:00')}")
            _append_csv_event(title, "monthly", "promoted", mdue.strftime('%Y-%m-%d 08:00'))

    # ===== Monthly stage =====
    for r in list_reminders(MONTHLY):
        if not r["completed"]:
            continue
        title = r["name"]
        _maybe_migrate_state_on_touch(MONTHLY, r) 
        rec = _get_or_init_record(title)
        anchor = rec.get("anchor_weekday", now.weekday())
        mcount = int(rec.get("monthly_count", 0)) + 1  # count this completion

        if mcount >= MONTHLY_REPEATS:
            # Graduate to Mastered
            move_by_title(MONTHLY, MASTERED, title)
            first_gap = MASTERED_REVIEW_MONTHS[0] if MASTERED_REVIEW_MONTHS else MASTERED_YEARLY_INTERVAL
            due = next_same_weekday_in_n_months_8am(anchor, now, first_gap)
            set_due(MASTERED, title, due)
            mark_incomplete_by_title(MASTERED, title)
            _update_record(title, stage="mastered", monthly_count=mcount, mastered_count=0, anchor_weekday=anchor)
            print(f"[Monthly→Mastered] {title} graduated after {MONTHLY_REPEATS} monthly reviews; next check {due.strftime('%m/%d/%Y 08:00')}")
            _append_csv_event(title, "mastered", "promoted", due.strftime('%Y-%m-%d 08:00'))
        else:
            # Stay Monthly
            due = next_same_weekday_in_n_months_8am(anchor, now, 1)
            set_due(MONTHLY, title, due)
            mark_incomplete_by_title(MONTHLY, title)
            _update_record(title, stage="monthly", monthly_count=mcount, anchor_weekday=anchor)
            _ensure_canonical_monthly_note(title, now)
            if OBF_ENABLED:
                _roll_obf_salt(title)                  # NEW: increment month → new pattern
                _refresh_monthly_obfuscation(title, now)
            print(f"[Monthly] Rescheduled {title} for {due.strftime('%m/%d/%Y 08:00')} ({mcount}/{MONTHLY_REPEATS})")
            _append_csv_event(title, "monthly", "rescheduled", due.strftime('%Y-%m-%d 08:00'))

    # ===== Mastered stage =====
    for r in list_reminders(MASTERED):
        if not r["completed"]:
            continue
        title = r["name"]
        _maybe_migrate_state_on_touch(MASTERED, r) # opportunistic SID-based title migration (no extra I/O)
        rec = _get_or_init_record(title)
        anchor = rec.get("anchor_weekday", now.weekday())
        k = int(rec.get("mastered_count", 0)) + 1  # increment mastered completions

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


def _weekday_name(ix: int) -> str:
    names = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    try:
        return names[int(ix) % 7]
    except Exception:
        return "?"

def _find_item_across_lists(title: str):
    for ln in [DAILY, WEEKLY, MONTHLY, MASTERED, BACKLOG]:
        items = list_reminders(ln)
        for it in items:
            if _norm_title(it["name"]) == _norm_title(title):
                return (ln, it)
    return (None, None)

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
    idx = {_norm_title(it["name"]): (ln, it) for (ln, it) in all_items}

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
        tracked_keys.add(_norm_title(title))
        stage = (rec.get("stage") or "?").ljust(9)
        counts = fmt_counts(rec).ljust(10)
        anchor = _weekday_name(rec.get("anchor_weekday", 0)).ljust(6)

        ln, it = idx.get(_norm_title(title), (None, None))
        list_name   = (ln or "-").ljust(10)
        completed   = ("True" if (it and it.get("completed")) else "False").ljust(9)
        due_display = (it.get("due") if it else "(missing)").strip() if it else "(missing)"

        tcol = (title[:35] + "…") if len(title) > 36 else title.ljust(36)
        print(f"{tcol} | {stage} | {counts} | {anchor} | {list_name} | {completed} | {due_display}")

    orphan_titles = []
    for (ln, it) in all_items:
        if _norm_title(it["name"]) not in tracked_keys:
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


# ====================================================================
# Debug / utilities
# ====================================================================
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
    Fill notes for Daily if blank (state-first; API fallback),
    then ensure a SID for any item we actually filled,
    and cache full_text in state from the note.
    """
    filled = 0
    sid_added = 0
    for it in list_reminders(DAILY):
        try:
            if not (it["body"] or "").strip():
                if ensure_notes_for_by_id(DAILY, it["id"], it["name"]):
                    filled += 1
                    # Reuse title-based SID helper for consistency (one list scan per filled item)
                    sid = _ensure_sid_for_title(DAILY, it["name"])
                    if sid:
                        sid_added += 1
                    _ingest_full_text_from_note(DAILY, it["id"], it["name"])
        except Exception as e:
            print(f"[daily] fill-notes error for '{it.get('name','?')}': {e}")
    print(f"Filled notes for {filled} item(s) in Daily; ensured SID on {sid_added}.")


def fill_notes_for_weekly():
    """Fill notes for Weekly if blank, then attach SID and cache full_text in state."""
    filled = 0
    for it in list_reminders(WEEKLY):
        try:
            if not (it["body"] or "").strip():
                if ensure_notes_for_by_id(WEEKLY, it["id"], it["name"]):
                    filled += 1
                    _ensure_sid_for_title(WEEKLY, it["name"])
                    _ingest_full_text_from_note(WEEKLY, it["id"], it["name"])
                    _maybe_migrate_state_on_touch(WEEKLY, it) 
        except Exception as e:
            print(f"[weekly] fill-notes error for '{it.get('name','?')}': {e}")
    print(f"Filled notes for {filled} item(s) in Weekly.")

def fill_notes_for_monthly():
    """Fill notes for Monthly if blank, then attach SID, cache full_text, and canonicalize with obfuscation."""
    filled = 0
    now = datetime.now()
    for it in list_reminders(MONTHLY):
        try:
            if not (it["body"] or "").strip():
                if ensure_notes_for_by_id(MONTHLY, it["id"], it["name"]):
                    filled += 1
                    _ensure_sid_for_title(MONTHLY, it["name"])
                    _ingest_full_text_from_note(MONTHLY, it["id"], it["name"])
                    _ensure_canonical_monthly_note(it["name"], now)
                    _maybe_migrate_state_on_touch(MONTHLY, it) 
        except Exception as e:
            print(f"[monthly] fill-notes error for '{it.get('name','?')}': {e}")
    print(f"Filled notes for {filled} item(s) in Monthly.")

def cli_test_fetch(ref: str) -> None:
    txt = fetch_scripture_text(ref)
    if not txt:
        print("(no text returned)")
        return
    preview = [line for line in txt.splitlines() if line.strip()]
    print("\n".join(preview[:6]))
    if len(preview) > 6:
        print("... (truncated)")

def existing_refs_across_all_lists() -> list[str]:
    titles = []
    for ln in [DAILY, WEEKLY, MONTHLY, BACKLOG]:
        try:
            titles += [x["name"] for x in list_reminders(ln)]
        except Exception:
            pass
    seen = set()
    out = []
    for t in titles:
        k = _norm_title(t)
        if k not in seen:
            seen.add(k)
            out.append(t)
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

def doctor():
    """
    Doctor checks:
      - Config / lists / API probe / OPENAI key / state
    If invoked with:  python scripture_agent.py doctor --fix
      - Run SID sweep on Daily/Weekly/Monthly
      - Run title-change repair (SID-anchored) on Daily/Weekly/Monthly
    """
    print("\n=== scripture_agent doctor ===")
    # Load config
    try:
        cfg = load_or_init_config()
        apply_config(cfg)
        print("[config] loaded config")
    except Exception as e:
        print(f"[config] ERROR: {e}")

    # Ensure lists
    print("[lists] ensuring all lists exist…")
    ensure_all_lists()

    # Probe API
    probe_ref = "1 Nephi 1:1"
    print(f"[nephi] test-fetch '{probe_ref}' …")
    txt = fetch_scripture_text(probe_ref)
    if txt:
        snippet = " ".join([ln.strip() for ln in txt.splitlines() if ln.strip()][:2])
        print(f"[nephi] OK  (preview: {snippet[:120]}{'…' if len(snippet)>120 else ''})")
    else:
        print("[nephi] WARN  could not retrieve sample passage; check network/API")

    # OpenAI key presence
    key = os.environ.get("OPENAI_API_KEY", "")
    print("[openai] " + ("OK  key present" if key else "WARN  key missing"))

    # State access
    st = _load_state()
    if isinstance(st, dict) and "verses" in st:
        print(f"[state] OK  {len(st.get('verses', {}))} tracked verse(s)")
    else:
        print("[state] WARN  could not read state file")

    # Fix mode?
    fix_mode = any(arg.strip() == "--fix" for arg in sys.argv[2:])
    if not fix_mode:
        print("=== doctor done (no --fix) ===\n")
        return

    print("\n=== doctor --fix repairs ===")
    # Fill missing note content first (prevents SID-only notes)
    for ln in (DAILY, WEEKLY, MONTHLY):
        try:
            for it in list_reminders(ln):
                if not (it.get("body") or "").strip():
                    _refresh_text_and_note(ln, it)
        except Exception as e:
            print(f"[fix] fill-missing {ln} ERROR: {e}")

    # A) SID sweep
    total_added = 0
    for ln in (DAILY, WEEKLY, MONTHLY):
        try:
            added = sid_sweep_for_list(ln)
            print(f"[fix] SID sweep {ln}: +{added}")
            total_added += added
        except Exception as e:
            print(f"[fix] SID sweep {ln} ERROR: {e}")
    _append_log(f"doctor --fix: SID sweep added {total_added} SID(s)")

    # B) Title-change repair (SID-anchored)
    try:
        migrated = _doctor_title_change_repair()
        print(f"[fix] Title-change repairs (SID-anchored): {migrated}")
        _append_log(f"doctor --fix: title-change repairs {migrated}")
    except Exception as e:
        print(f"[fix] Title-change repair ERROR: {e}")

    print("=== doctor --fix done ===\n")

def _doctor_title_change_repair() -> int:
    """
    Sweep Daily/Weekly/Monthly using sanitized bodies only.
    For any item whose SID maps to a *different* title in state, migrate the record
    to the current title and refresh canonical text for that new title, rebuilding the note.
    Returns count of migrations performed.
    """
    migrated = 0
    for ln in (DAILY, WEEKLY, MONTHLY):
        try:
            for it in list_reminders(ln):
                sid = _extract_sid_from_text(it.get("body",""))
                if not sid:
                    continue
                # Try migration
                moved = _migrate_state_title_by_sid(it.get("name",""), sid)
                if moved:
                    migrated += 1
                    # After migrating to the new title, refresh full_text & rebuild the note
                    _refresh_text_and_note(ln, it)
        except Exception as e:
            _append_log(f"[doctor_repair] ERROR scanning '{ln}': {e}")
    return migrated



# ====================================================================
# Verse Obfuscation helpers (UNIFIED)
# ====================================================================
def _extract_full_text(note: str) -> str:
    """
    Extract canonical FULL ORIGINAL TEXT from a note:
      - Take content AFTER the LAST obfuscation separator.
      - If that tail still contains separators (from past duplication), take only the content after the last one.
      - Strip ANY [sid:UUID] tokens that might have been embedded.
      - Trim leading/trailing dot-buffer and blank lines.
    If no separator exists, return entire note minus any [sid:...] tokens and spacer lines.
    """
    sep = globals().get("OBF_SEPARATOR", "\n\n______________________________\n")
    buf_token = str(globals().get("OBF_BUFFER_TOKEN", "."))
    s = (note or "")

    # Remove trailing SID footer if present
    s = re.sub(r"\s*\[sid:[0-9a-fA-F-]{36}\]\s*\Z", "", s.strip(), flags=re.MULTILINE)

    # Isolate "full text" region: take tail after the final separator;
    # if multiple separators exist in that tail (due to prior duplication), keep only after the last.
    if sep in s:
        tail = s.rsplit(sep, 1)[1]
        if sep in tail:
            tail = tail.rsplit(sep, 1)[1]
    else:
        tail = s

    # Strip ANY stray [sid:...] occurrences that might have leaked inside the text
    tail = re.sub(r"\[sid:[0-9a-fA-F-]{36}\]", "", tail)

    # Remove leading dot-buffer/blank lines and trailing blank lines
    lines = tail.splitlines()
    i = 0
    while i < len(lines) and (not lines[i].strip() or lines[i].strip() == buf_token):
        i += 1
    j = len(lines)
    while j > i and not lines[j - 1].strip():
        j -= 1

    return "\n".join(lines[i:j]).strip()



# Word regex and obfuscation
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z’']*")

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z’']*")

def _obfuscate_text(full_text: str, visible_ratio: float, seed: int) -> str:
    """
    Obfuscate ~ (1 - visible_ratio) of eligible words in full_text.
    - Eligible word: >= OBF_MIN_LEN letters.
    - If OBF_KEEP_FL is True -> keep first/last letters; otherwise replace FULL word letters with underscores.
    - Preserves punctuation and whitespace exactly.
    """
    vis = max(0.0, min(1.0, float(visible_ratio)))
    if vis >= 0.999:
        return full_text

    min_len = int(globals().get("OBF_MIN_LEN", 3))
    keep_first_last = bool(globals().get("OBF_KEEP_FL", False))

    # Gather eligible word spans
    spans = []
    for m in _WORD_RE.finditer(full_text):
        w = m.group(0)
        letters = sum(1 for c in w if c.isalpha())
        if letters >= min_len:
            spans.append((m.start(), m.end(), w))

    if not spans:
        return full_text

    # Choose how many to blank
    blank_frac = 1.0 - vis
    k = int(round(blank_frac * len(spans)))
    if k <= 0:
        return full_text

    rnd = random.Random(seed)
    to_blank = set(rnd.sample(range(len(spans)), k))

    # Build masked result
    out = []
    last = 0
    for idx, (s, e, w) in enumerate(spans):
        out.append(full_text[last:s])
        if idx in to_blank:
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
    """
    Map Monthly completion count to the configured visibility schedule.
    """
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

def _note_with_obfuscation(full_text: str, visible_ratio: float, seed: int) -> str:
    """
    Canonical Monthly note (WITHOUT SID appended):
      [OBFUSCATED TEXT]

      .        (N dot lines)
      .
      .
      .

      ______________________________
      [FULL ORIGINAL TEXT]
    """
    obf = _obfuscate_text(full_text.strip(), visible_ratio, seed)

    buf_lines = int(globals().get("OBF_BUFFER_LINES", 4))
    buf_token = str(globals().get("OBF_BUFFER_TOKEN", "."))
    sep = globals().get("OBF_SEPARATOR", "\n\n______________________________\n")

    # Build dot buffer (each line contains just the token)
    buffer_block = "\n".join(buf_token for _ in range(max(0, buf_lines)))
    parts = [obf]
    if buffer_block:
        parts.append(buffer_block)
    # Separator already includes leading/trailing newlines by config; keep consistent:
    parts.append(sep.rstrip("\n"))
    parts.append(full_text.strip())

    # Assemble with blank lines between major sections
    body = "\n\n".join(parts).rstrip()
    return body


def _weekly_seed_for(title: str, now: datetime) -> int:
    iso_year, iso_week, _ = now.isocalendar()
    return hash((title.casefold(), iso_year, iso_week)) & 0x7FFFFFFF

def _monthly_seed(title: str, rec: dict) -> int:
    sid = rec.get("sid") or title
    mcount = int(rec.get("monthly_count", 0))
    h = hashlib.sha1(f"{sid}|m|{mcount}".encode("utf-8")).hexdigest()[:8]
    return int(h, 16)

def _ensure_dual_note_for_monthly(title: str, now: datetime) -> bool:
    """
    Legacy helper; now delegates to the canonical Monthly note builder to avoid
    producing a second (duplicated) layout.
    """
    return _ensure_canonical_monthly_note(title, now)


def _refresh_monthly_obfuscation(title: str, now: datetime) -> None:
    """
    Single source of truth: delegate to the canonical Monthly builder.
    This avoids a second rewrite that used to duplicate sections.
    """
    try:
        _ensure_canonical_monthly_note(title, now)
    except Exception as e:
        _append_log(f"[obfuscate] ERROR for '{title}': {e}")


def _resolve_full_text_for(title: str, list_name: str, rem_id: str) -> Optional[str]:
    rec = _get_or_init_record(title)
    ft = (rec.get("full_text") or "").strip()
    if ft:
        return ft

    try:
        note_raw = get_body_by_id_raw(list_name, rem_id)
    except Exception:
        note_raw = ""
    ft = _extract_full_text(note_raw).strip()
    if not ft:
        ft = fetch_scripture_text(title) or ""
    if not ft:
        return None

    _update_record(title, full_text=ft, full_text_sha=_sha1(ft))
    return ft

def _ensure_canonical_monthly_note(title: str, now: datetime) -> bool:
    """
    Rebuild the Monthly note from the canonical source of truth:
      - Resolve full_text (state-first; else parse from note; else API).
      - Compute visible ratio and seed for this month.
      - Build a SINGLE clean canonical body (no duplicates).
      - Append SID at the very bottom using the standard spacer.
    """
    ln, it = _find_item_across_lists(title)
    if ln != MONTHLY or not it:
        return False

    # Resolve canonical full text
    full = _resolve_full_text_for(title, ln, it["id"])
    if not full:
        return False

    # Ratio & seed (seed may use obf_salt if present)
    rec = _get_or_init_record(title)
    ratio = _ratio_for_monthly_count(int(rec.get("monthly_count", 0)))
    seed = _monthly_seed(title, rec)

    # Ensure/obtain SID
    note_raw = get_body_by_id_raw(ln, it["id"])
    sid = _extract_sid(note_raw) or rec.get("sid") or _ensure_sid_for_title(ln, title)

    # Build canonical body WITHOUT SID, then append SID with spacer
    core = _note_with_obfuscation(full, ratio, seed)
    final_body = _append_sid(core, sid)

    # Only write if changed to avoid needless AppleScript writes
    if note_raw.strip() != final_body.strip():
        return set_body_by_id(ln, it["id"], final_body)
    return True




def _ingest_full_text_from_note(list_name: str, rem_id: str, title: str) -> None:
    try:
        note_raw = get_body_by_id_raw(list_name, rem_id)
    except Exception:
        note_raw = ""
    full = _extract_full_text(note_raw).strip()
    if full:
        _update_record(title, full_text=full, full_text_sha=_sha1(full))

def _roll_obf_salt(title: str) -> int:
    """
    Generate and persist a new random obfuscation salt for this verse.
    Called when entering Monthly or when monthly_count increments.
    """
    salt = random.getrandbits(32)
    _update_record(title, obf_salt=int(salt))
    return int(salt)

def _refresh_text_and_note(list_name: str, item: dict) -> bool:
    """
    For a given reminder item (already in hand), fetch canonical text for its *current title*,
    persist to state, and rebuild the note in the list's appropriate format.
    Respects #manual_override via ensure_notes_for().
    Returns True if we wrote note content.
    """
    title = (item.get("name") or "").strip()
    if not title:
        return False

    # Fetch canonical scripture text for the *current* title.
    txt = fetch_scripture_text(title) or ""
    if not txt:
        return False

    _update_record(title, full_text=txt, full_text_sha=_sha1(txt))

    # Rebuild note content:
    if list_name == MONTHLY:
        # canonical monthly format (obfuscated header + separator + full text + SID)
        ok = _ensure_canonical_monthly_note(title, datetime.now())
        if ok:
            _ensure_sid_for_title(MONTHLY, title)
        return bool(ok)
    else:
        # Daily/Weekly: ensure canonical text; this respects #manual_override
        ok = ensure_notes_for(list_name, title)
        if ok:
            _ensure_sid_for_title(list_name, title)
        return bool(ok)


# ====================================================================
# SID helpers
# ====================================================================
def _ensure_sid_for_title(list_name: str, title: str) -> Optional[str]:
    items = list_reminders(list_name)
    it = next((x for x in items if _norm_title(x["name"]) == _norm_title(title)), None)
    if not it:
        return None

    note_raw = get_body_by_id_raw(list_name, it["id"])
    sid = _extract_sid(note_raw)
    if not sid:
        sid = _new_sid()
        new_body = _append_sid(note_raw, sid)
        set_body_by_id(list_name, it["id"], new_body)

    rec = _get_or_init_record(title)
    if rec.get("sid") != sid:
        _update_record(title, sid=sid)

    return sid


def _sha1(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()

def sid_sweep_for_list(list_name: str) -> int:
    """
    Ensure every item in list_name has a SID, but never leave a 'SID-only' note.
    - If body is blank: fill text first (state-first -> API), then append SID.
    - If body has text and no SID: append SID.
    Returns the number of SIDs newly added.
    """
    added = 0
    for it in list_reminders(list_name):
        body = (it.get("body") or "").strip()
        has_sid = bool(_extract_sid_from_text(body))
        if has_sid:
            continue

        # If the body is empty, fill canonical text first (so SID isn't the only content).
        if not body:
            if _refresh_text_and_note(list_name, it):
                added += 1  # _ensure_sid_for_title() is called inside
            else:
                # Try a last-resort ensure (may no-op on manual_override)
                if ensure_notes_for(list_name, it.get("name","")):
                    _ensure_sid_for_title(list_name, it.get("name",""))
                    added += 1
            continue

        # Body has text but no SID → append SID only
        sid = _ensure_sid_for_title(list_name, it.get("name",""))
        if sid:
            added += 1

    return added


def _extract_sid_from_text(s: str) -> Optional[str]:
    """Extract [sid:UUID] from a sanitized (flattened) body string."""
    if not s:
        return None
    m = re.search(r"\[sid:([0-9a-fA-F-]{36})\]", s)
    return m.group(1) if m else None

def _sid_index_from_state() -> dict:
    """Return {sid: normalized_title_key} for all verses in state that have a sid."""
    st = _load_state()
    out = {}
    for k, rec in (st.get("verses") or {}).items():
        sid = rec.get("sid")
        if sid:
            out[sid] = k
    return out

def _migrate_state_title_by_sid(current_title: str, sid: str) -> bool:
    """
    If 'sid' exists in state under a different title key, move that record to 'current_title'.
    Returns True if a migration occurred.
    """
    if not sid:
        return False
    state = _load_state()
    verses = state.setdefault("verses", {})
    sid_map = { (rec.get("sid") or ""): key for key, rec in verses.items() if rec.get("sid") }

    old_key = sid_map.get(sid)
    new_key = _norm_title(current_title)
    if not old_key or old_key == new_key:
        return False

    # If a record already exists at new_key, do not overwrite; log and skip (safest behavior).
    if new_key in verses:
        _append_log(f"[migrate] SKIP merge: '{current_title}' already exists; keeping both (old key: {old_key})")
        return False

    rec = verses.get(old_key)
    if not rec:
        return False

    # Update canonical title; keep all counters/stage/anchor/full_text intact
    rec["title"] = current_title.strip()
    verses[new_key] = rec
    try:
        del verses[old_key]
    except Exception:
        pass

    _save_state(state)
    _append_log(f"[migrate] moved state '{old_key}' → '{new_key}' via SID {sid}")
    return True

def _maybe_migrate_state_on_touch(list_name: str, item: dict) -> bool:
    """
    Lightweight: use the item's sanitized body to grab SID (no extra raw read),
    and migrate state to the item's current title if needed. Returns True if migrated.
    """
    try:
        sid = _extract_sid_from_text(item.get("body", ""))
        if not sid:
            return False
        return _migrate_state_title_by_sid(item.get("name", ""), sid)
    except Exception as e:
        _append_log(f"[migrate_touch] ERROR for '{item.get('name','?')}' on '{list_name}': {e}")
        return False


# ====================================================================
# CSV logging
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
# Tiny CLI and run loop
# ====================================================================
def run_daily(topic_arg: Optional[str] = None):
    """
    One 'daily' run:
      1) advance_on_complete()  # handles D/W/M/M* rescheduling & promotions
      2) fill_notes_for_daily()
      3) fill_notes_for_weekly()
      4) fill_notes_for_monthly()
      5) maybe_add_new_verse_from_backlog()
      6) scheduled weekly '--fix' if due (config-driven; minimal overhead)
    """
    cfg = load_or_init_config()
    apply_config(cfg)

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
        fill_notes_for_weekly()
        _append_log("run-daily: fill_notes_for_weekly OK")
    except Exception as e:
        _append_log(f"run-daily: fill_notes_for_weekly ERROR: {e}")

    try:
        fill_notes_for_monthly()
        _append_log("run-daily: fill_notes_for_monthly OK")
    except Exception as e:
        _append_log(f"run-daily: fill_notes_for_monthly ERROR: {e}")

    try:
        moved_or_added = maybe_add_new_verse_from_backlog(topic=topic)
        if moved_or_added:
            _append_log(f"run-daily: new verse added/moved → {moved_or_added}")
        else:
            _append_log("run-daily: no new verse added/moved")
    except Exception as e:
        _append_log(f"run-daily: maybe_add_new_verse_from_backlog ERROR: {e}")

    # Scheduled maintenance (quiet, config-driven)
    try:
        _run_scheduled_fix_if_due(datetime.now())
    except Exception as e:
        _append_log(f"run-daily: scheduled-fix ERROR: {e}")

    _append_log("run-daily: done")




def ensure_all_lists_cmd():
    ensure_all_lists()

def print_status_cmd():
    print_status()

def debug_dump_cmd():
    debug_dump()

def dump_state_cmd():
    dump_state()

def cli_test_fetch_cmd(ref: str):
    cli_test_fetch(ref)

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
            cli_test_fetch_cmd(ref)

    elif cmd == "help":
        print("Usage:")
        print("  python scripture_agent.py new-verse    # Backlog → Daily (dedupe, due 8am, init state, fill notes via API)")
        print("  python scripture_agent.py advance      # Reschedule/move after you mark complete")
        print("  python scripture_agent.py fill-notes   # Fill notes for Daily/Weekly/Monthly if blank")
        print('  python scripture_agent.py test-fetch "Mosiah 2:21-22"')
        print("  python scripture_agent.py state        # Show cadence state file")
        print('  python scripture_agent.py new-verse [topic]   # Backlog or (if empty & allowed) ChatGPT')
        print("  python scripture_agent.py config       # Show merged config currently in use")
        print("  python scripture_agent.py status       # Show stages, counts, and next due for all verses")
        print("  python scripture_agent.py setup        # Create any missing lists from config")
        print("  python scripture_agent.py doctor       # Check lists, config, APIs, env")
        print('  python scripture_agent.py run-daily [topic]  # Advance, fill notes, then add new verse if needed')

    elif cmd == "config":
        cfg = load_or_init_config()
        apply_config(cfg)
        print(json.dumps(cfg, indent=2))

    elif cmd == "status":
        print_status_cmd()

    elif cmd == "setup":
        ensure_all_lists_cmd()

    elif cmd == "doctor":
        doctor()

    elif cmd == "run-daily":
        topic = " ".join(sys.argv[2:]).strip() if len(sys.argv) > 2 else None
        run_daily(topic_arg=topic)

    else:
        print(f"Unknown command: {cmd} (run 'help')")

if __name__ == "__main__":
    main()
