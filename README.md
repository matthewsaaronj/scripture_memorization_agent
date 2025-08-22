
# Scripture Memorization Agent

A Python-based agent that automates scripture memorization using **Apple Reminders**.

For years I looked for an application that would let me use the same scripture memorization program I learned as a missionary decades ago. Nothing quite fit the bill. I eventually used a manual process with Apple Reminders, which worked reasonably well — but took too much time to maintain.

So I decided to build this tool from scratch.

This script fully automates the **Vaughn J. Featherstone–style scripture memorization program**:
- **Backlog**: new verses waiting to be introduced.  
- **Daily**: reviewed every day for a week.  
- **Weekly**: reviewed weekly for a month.  
- **Monthly**: reviewed monthly for 2 years.  
- **Mastered**: long-term retention, checked every 3–12 months.  

The agent takes care of scheduling, rescheduling, duplicate detection, scripture text lookup, and even adding new verses automatically if your backlog is empty.

---

## Features

- Automatic scheduling: Daily → Weekly → Monthly → Mastered, with counters and state tracking.  
- Featherstone program logic: Counters for daily/weekly/monthly repeats before moving forward.  
- Notes auto-fill: Scripture text pulled automatically from [nephi.org](https://api.nephi.org) and [bible-api.com](https://bible-api.com).  
- Duplicate/overlap detection: Prevents adding the same verse twice, even if ranges overlap (e.g., `2 Nephi 2:25` vs. `2 Nephi 2:25–27`).  
- ChatGPT fallback: Suggests a doctrinally important scripture (optionally by topic) if your backlog is empty.  
- Configurable: All settings stored in `~/.scripture_agent/config.json`.  
- Logging: Keeps both text logs (`agent.log`) and CSV logs (`progress.csv`) for auditing progress.  
- Automated runs: Integrated with `launchd` to run every morning, even if you forget.  

---

## Project Structure

- `scripture_agent.py` – the main script (monolithic but stable).  
- `~/.scripture_agent/` – config, logs, state, and CSV files are stored here.  

---

## Installation

1. Clone the repo:  

   ```bash
   git clone https://github.com/yourusername/scripture-memorization-agent.git
   cd scripture-memorization-agent
   ```

2. Ensure Python 3.9+ is installed (macOS).  

3. Install dependencies:  
   *(no external Python deps — everything uses standard library)*  

4. Test that it runs:  

   ```bash
   python3 scripture_agent.py help
   ```

---

## Configuration

Your config file lives at:  

```
~/.scripture_agent/config.json
```

Example:  

```json
{
  "lists": {
    "backlog": "Scripture Memorization - Backlog",
    "daily": "Scripture Memorization - Daily",
    "weekly": "Scripture Memorization - Weekly",
    "monthly": "Scripture Memorization - Monthly",
    "mastered": "Scripture Memorization - Mastered"
  },
  "cadence": {
    "daily_repeats": 7,
    "weekly_repeats": 4,
    "monthly_repeats": 24
  },
  "mastered": {
    "review_months": [3, 6, 12],
    "yearly_interval": 12
  },
  "auto_add": {
    "every_n_days": 7,
    "topic_default": "faith"
  }
}
```

Tip: Set `"topic_default"` to guide ChatGPT when it adds new verses (e.g., `"faith"`, `"repentance"`, `"grace"`).

---

## Usage

### Daily run (automated by `launchd`):

```bash
python3 scripture_agent.py run-daily
```

This will:
1. Advance completed verses to the next stage.  
2. Fill missing notes with scripture text.  
3. Add a new verse from Backlog (or ChatGPT if empty).  

### Manual commands:

```bash
python3 scripture_agent.py new-verse    # Move one from Backlog → Daily
python3 scripture_agent.py advance      # Reschedule after marking items complete
python3 scripture_agent.py fill-notes   # Fill missing notes for Daily list
python3 scripture_agent.py status       # Show verse stages, counters, and next due dates
python3 scripture_agent.py doctor       # Run diagnostics (lists, APIs, config, OpenAI key)
```

---

## Logs

- `~/.scripture_agent/agent.log` – plain text log of each run.  
- `~/.scripture_agent/progress.csv` – CSV file of events (timestamp, verse, stage, action, next due).  

---

## Automation

To run automatically each morning, create a `launchd` job:  
- Save a `.plist` in `~/Library/LaunchAgents/` (see runbook).  
- Example runs at 6:30 AM daily, resuming after wake if asleep.  

---

## Why This Project Exists

This tool is deeply personal. For decades I’ve wanted a simple way to follow the scripture memorization program I learned as a missionary. I tried carrying 3x5 notecards as I did as a missionary, a number of scripture memorization applications, and then eventually Apple Reminders by hand. But nothing was sustainable long-term.

Now I have something that automates the flow, ensures I never lose track of cadence, and still integrates seamlessly with Apple Reminders (which syncs across my devices).

It’s a blend of technology and discipleship — making it easier to treasure up the word of God, consistently and joyfully.

---

## License

MIT License. Use freely and adapt as you see fit.
