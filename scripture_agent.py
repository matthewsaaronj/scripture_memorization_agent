#!/usr/bin/env python3
"""
Scripture Memorization Agent for Apple Reminders

- Uses AppleScript via `osascript` (no Shortcuts) from Python
- Lists: Backlog, Daily, Weekly, Monthly
- Backlog → Daily (no duplicates), due set to next morning 8:00 AM
- Cadence per Featherstone: Daily repeats then Weekly then Monthly
- Notes auto-fill from local cache file (can be swapped to API later)
"""

import os
import sys
import json
import subprocess
from datetime import datetime, timedelta
from typing import Optional

# ----- List names (top-level, no groups) -----
BACKLOG  = "Scripture Memorization - Backlog"
DAILY    = "Scripture Memorization - Daily"
WEEKLY   = "Scripture Memorization - Weekly"
MONTHLY  = "Scripture Memorization - Monthly"

# ----- Cadence thresholds (Featherstone-style) -----
DAILY_REPEATS   = 7   # review daily for 7 days (set to 2 for quick testing)
WEEKLY_REPEATS  = 4   # review weekly for 4 weeks (set to 2 for quick testing)

# ----- State file for cadence tracking -----
STATE_PATH = os.path.expanduser("~/.scripture_agent/state.json")

# ----- Local verse cache for notes autofill (simple text file) -----
VERSE_CACHE_PATH = os.path.expanduser("~/.scripture_agent/verses.txt")
# Each line:  <reference>::<plain verse text>
# Example:
# John 3:16::For God so loved the world...

# ----- AppleScript runner -----
def run_as(script: str, *args: str) -> str:
    return subprocess.run(
        ["osascript", "-e", script, *args],
        check=True, capture_output=True, text=True
    ).stdout.strip()

# ----- Readers -----
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

# ----- Writers -----
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


def _norm_title_key(t: str) -> str:
    # normalize for matching: trim, casefold, unify en-dash to hyphen
    return (t or "").strip().casefold().replace("–", "-")

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


# ----- Move (copy → delete by ID) -----
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

# ----- Notes autofill (local-cache for now) -----
def _read_local_verse_cache() -> dict:
    os.makedirs(os.path.dirname(VERSE_CACHE_PATH), exist_ok=True)
    if not os.path.exists(VERSE_CACHE_PATH):
        open(VERSE_CACHE_PATH, "a", encoding="utf-8").close()
        return {}
    cache = {}
    with open(VERSE_CACHE_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "::" not in line:
                continue
            ref, text = line.split("::", 1)
            cache[ref.strip()] = text.strip()
    return cache

def fetch_scripture_text(reference: str) -> Optional[str]:
    """For now: look up from local cache file. Returns plain verse text or None."""
    cache = _read_local_verse_cache()
    return cache.get(reference.strip())

def ensure_notes_for(list_name: str, title: str) -> bool:
    """
    If the reminder's notes are blank, fetch text from cache and set notes.
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

    if (m["body"] or "").strip():
        return True  # already has notes

    text = fetch_scripture_text(m["name"]) or fetch_scripture_text(title)
    if not text:
        return False  # no cached text yet

    return set_body_by_id(list_name, m["id"], text.strip())


# ----- Backlog → Daily (clean duplicates, move one, set due 8am, init state) -----
def _norm_title(t: str) -> str:
    return (t or "").strip().casefold().replace("–", "-")

def maybe_add_new_verse_from_backlog() -> Optional[str]:
    """
    1) Clean ALL duplicates in Backlog that already exist in Daily/Weekly/Monthly.
    2) Move the FIRST remaining Backlog item into Daily.
       - Set due date to next morning at 08:00.
       - Initialize cadence state with anchor weekday.
       - Fill notes from local cache if blank (no-op if not present yet).
    Returns the moved title, or None if nothing moved.
    """
    daily_titles   = {_norm_title(x["name"]) for x in list_reminders(DAILY)}
    weekly_titles  = {_norm_title(x["name"]) for x in list_reminders(WEEKLY)}
    monthly_titles = {_norm_title(x["name"]) for x in list_reminders(MONTHLY)}
    exists_elsewhere = daily_titles | weekly_titles | monthly_titles

    # Pass 1: clean all duplicates from Backlog
    backlog_items = list_reminders(BACKLOG)
    for r in backlog_items:
        title = r["name"].strip()
        if _norm_title(title) in exists_elsewhere:
            delete_by_id(BACKLOG, r["id"])
            print(f"Cleaned duplicate from Backlog: {title}")

    # Pass 2: move the first remaining Backlog item to Daily
    for r in list_reminders(BACKLOG):
        title = r["name"].strip()

        # create in Daily
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

        # delete original from Backlog
        delete_by_id(BACKLOG, r["id"])

        # robust due time: next morning 8:00 AM
        set_due_next_morning_8am(DAILY, title)

        # init cadence state with today's weekday (anchor)
        _get_or_init_record(title, anchor_weekday=datetime.now().weekday())

        # fill notes if we have it in the local cache
        ensure_notes_for(DAILY, title)

        print(f"New verse moved from Backlog → Daily (next review at 8:00 AM): {title}")
        return title

    print("No eligible Backlog items to move (either empty or all were duplicates and cleaned).")
    return None

# ----- Cadence state helpers -----
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
            "stage": "daily",       # daily | weekly | monthly
            "daily_count": 0,
            "weekly_count": 0,
            "anchor_weekday": anchor_weekday if anchor_weekday is not None else datetime.now().weekday()
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
        "anchor_weekday": datetime.now().weekday()
    })
    rec.update(changes)
    _save_state(state)

# ----- Cadence date helpers -----
def next_morning_8am(base: datetime) -> datetime:
    return (base + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)

def next_same_weekday_8am(anchor_weekday: int, base: datetime) -> datetime:
    days_ahead = (anchor_weekday - base.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    target = base + timedelta(days=days_ahead)
    return target.replace(hour=8, minute=0, second=0, microsecond=0)

# ----- Advance-on-complete (cadence-aware) -----
def advance_on_complete():
    now = datetime.now()

    # ===== Daily stage =====
    for r in list_reminders(DAILY):
        if not r["completed"]:
            continue
        title = r["name"]
        rec = _get_or_init_record(title)
        anchor = rec.get("anchor_weekday", now.weekday())

        if rec.get("stage") != "weekly" and rec.get("stage") != "monthly":
            dcount = int(rec.get("daily_count", 0))
            if dcount + 1 < DAILY_REPEATS:
                due = next_morning_8am(now)
                set_due(DAILY, title, due)
                mark_incomplete_by_title(DAILY, title)
                _update_record(title, stage="daily", daily_count=dcount + 1, anchor_weekday=anchor)
                print(f"[Daily] Rescheduled {title} for {due.strftime('%m/%d/%Y 08:00')}; day {dcount+1}/{DAILY_REPEATS}")
            else:
                move_by_title(DAILY, WEEKLY, title)
                wdue = next_same_weekday_8am(anchor, now)
                set_due(WEEKLY, title, wdue)
                mark_incomplete_by_title(WEEKLY, title)
                _update_record(title, stage="weekly", daily_count=DAILY_REPEATS, weekly_count=0, anchor_weekday=anchor)
                print(f"[Daily→Weekly] {title} scheduled {wdue.strftime('%m/%d/%Y 08:00')}")

    # ===== Weekly stage =====
    for r in list_reminders(WEEKLY):
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
        else:
            move_by_title(WEEKLY, MONTHLY, title)
            mdue = (now + timedelta(days=30)).replace(hour=8, minute=0, second=0, microsecond=0)
            set_due(MONTHLY, title, mdue)
            mark_incomplete_by_title(MONTHLY, title)
            _update_record(title, stage="monthly", weekly_count=WEEKLY_REPEATS, anchor_weekday=anchor)
            print(f"[Weekly→Monthly] {title} scheduled {mdue.strftime('%m/%d/%Y 08:00')}")

    # ===== Monthly stage =====
    for r in list_reminders(MONTHLY):
        if not r["completed"]:
            continue
        title = r["name"]
        mdue = (now + timedelta(days=30)).replace(hour=8, minute=0, second=0, microsecond=0)
        set_due(MONTHLY, title, mdue)
        mark_incomplete_by_title(MONTHLY, title)
        _update_record(title, stage="monthly")
        print(f"[Monthly] Rescheduled {title} for {mdue.strftime('%m/%d/%Y 08:00')}")

# ----- Debug / utilities -----
def debug_dump():
    for ln in [DAILY, WEEKLY, MONTHLY, BACKLOG]:
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
    """Try to fill notes for any Daily reminders with blank notes (from local cache)."""
    filled = 0
    for it in list_reminders(DAILY):
        if not it["body"].strip():
            if ensure_notes_for(DAILY, it["name"]):
                filled += 1
    print(f"Filled notes for {filled} item(s) in Daily.")

# ----- Tiny CLI -----
def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "new-verse":
        maybe_add_new_verse_from_backlog()
        debug_dump()

    elif cmd == "advance":
        advance_on_complete()
        debug_dump()

    elif cmd == "fill-notes":
        fill_notes_for_daily()
        debug_dump()

    elif cmd == "state":
        dump_state()

    elif cmd == "help":
        print("Usage:")
        print("  python scripture_agent.py new-verse   # Backlog → Daily (dedupe, due 8am, init state, fill notes if cached)")
        print("  python scripture_agent.py advance     # Reschedule/move after you mark complete")
        print("  python scripture_agent.py fill-notes  # Fill notes from local cache (Daily)")
        print("  python scripture_agent.py state       # Show cadence state file")
    else:
        print(f"Unknown command: {cmd} (run 'help')")

if __name__ == "__main__":
    main()
