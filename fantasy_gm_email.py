"""
Fantasy GM Email Assistant
---------------------------
Runs on a schedule via GitHub Actions. Fetches your Sleeper league data,
asks Claude for a recommendation (lineup optimization or draft pick),
and emails it to you. Sleeper's API is read-only, so this tool can't
submit lineup changes or draft picks for you automatically -- it gets
the recommendation to your inbox in time for you to make the move
yourself in the Sleeper app.

Required environment variables (set as GitHub Secrets):
  SLEEPER_USERNAME   - your Sleeper username
  ANTHROPIC_API_KEY  - your Anthropic API key
  GMAIL_ADDRESS       - the Gmail address to send FROM
  GMAIL_APP_PASSWORD  - a Gmail App Password (not your normal password)

Optional:
  TO_EMAIL     - where to send the email (defaults to GMAIL_ADDRESS)
  SEASON       - defaults to current year
  LEAGUE_NAME  - exact league name, if you have more than one league
  MODE         - "lineup" or "draft" (defaults to auto-detect)
"""

import os
import sys
import smtplib
import ssl
from email.mime.text import MIMEText
from datetime import datetime, timezone
import requests

SLEEPER_USERNAME = os.environ["SLEEPER_USERNAME"]
SEASON = os.environ.get("SEASON", str(datetime.now().year))
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
TO_EMAIL = os.environ.get("TO_EMAIL", GMAIL_ADDRESS)
LEAGUE_NAME_FILTER = os.environ.get("LEAGUE_NAME", "").strip()
FORCE_MODE = os.environ.get("MODE", "").strip().lower()

SL = "https://api.sleeper.app/v1"


def sget(path):
    r = requests.get(SL + path, timeout=20)
    r.raise_for_status()
    return r.json()


def get_league():
    user = sget(f"/user/{SLEEPER_USERNAME}")
    leagues = sget(f"/user/{user['user_id']}/leagues/nfl/{SEASON}")
    if not leagues:
        raise SystemExit(f"No {SEASON} NFL leagues found for {SLEEPER_USERNAME}")
    if LEAGUE_NAME_FILTER:
        for l in leagues:
            if l["name"].strip().lower() == LEAGUE_NAME_FILTER.lower():
                return user, l
        raise SystemExit(f"No league named '{LEAGUE_NAME_FILTER}' found")
    # Prefer an active league (drafting or in season) over a completed one
    for status in ("drafting", "pre_draft", "in_season", "complete"):
        for l in leagues:
            if l["status"] == status:
                return user, l
    return user, leagues[0]


def player_name(players, pid):
    p = players.get(str(pid))
    if not p:
        return f"#{pid}"
    return (f"{p.get('first_name','')} {p.get('last_name','')}".strip()) or f"#{pid}"


def call_claude(system, user_msg, max_tokens=900):
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": max_tokens,
            "system": system,
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            "messages": [{"role": "user", "content": user_msg}],
        },
        timeout=90,
    )
    resp.raise_for_status()
    data = resp.json()
    text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
    return "\n\n".join(text_blocks).strip() or "(No response text returned.)"


def send_email(subject, body):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = TO_EMAIL
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [TO_EMAIL], msg.as_string())


def load_players_minimal(roster_ids_needed):
    # The full Sleeper player DB is ~5MB; fine for a scheduled job (unlike a browser tab).
    all_players = sget("/players/nfl")
    return {pid: all_players.get(pid, {}) for pid in roster_ids_needed}


def run_lineup_mode(user, league, players_cache):
    week = league.get("settings", {}).get("leg", 1)
    rosters = sget(f"/league/{league['league_id']}/rosters")
    users = sget(f"/league/{league['league_id']}/users")
    my_roster = next((r for r in rosters if r.get("owner_id") == user["user_id"]), None)
    if not my_roster:
        raise SystemExit("Could not find your roster in this league.")

    all_ids = list(set((my_roster.get("players") or []) + (my_roster.get("starters") or [])))
    players = load_players_minimal(all_ids)

    starters = [player_name(players, pid) for pid in (my_roster.get("starters") or [])]
    bench_ids = [p for p in (my_roster.get("players") or []) if p not in (my_roster.get("starters") or [])]
    bench = [player_name(players, pid) for pid in bench_ids]

    ctx = f"""LEAGUE: {league.get('name')} | Week {week} | Season {SEASON}
STARTERS: {', '.join(starters) or 'none set'}
BENCH: {', '.join(bench) or 'none'}"""

    reco = call_claude(
        "You are an expert fantasy football GM. Be sharp, direct, specific -- name players. "
        "Search for current injury news and start/sit advice before answering.",
        ctx + "\n\nGive me this week's optimal lineup. List who should start at each position, "
        "flag any injury/bye concerns, and note any bench player who should be starting instead. "
        "Keep it under 300 words, plain text, no markdown symbols.",
        max_tokens=700,
    )

    subject = f"Fantasy GM: Week {week} Lineup — {league.get('name')}"
    body = f"Your Week {week} lineup recommendation for {league.get('name')}:\n\n{reco}\n\n" \
           f"— Set this in Sleeper before kickoff. This email was generated automatically."
    send_email(subject, body)
    print("Sent lineup email.")


def run_draft_mode(user, league, players_cache):
    drafts = sget(f"/league/{league['league_id']}/drafts")
    draft = next((d for d in drafts if d.get("status") == "drafting"), None)
    if not draft:
        print("No active draft right now. Skipping.")
        return

    draft_id = draft["draft_id"]
    picks = sget(f"/draft/{draft_id}/picks")
    draft_order = draft.get("draft_order") or {}
    my_user_id = user["user_id"]
    my_slot = draft_order.get(my_user_id)
    if my_slot is None:
        print("Could not determine your draft slot. Skipping.")
        return

    settings = draft.get("settings", {})
    teams = settings.get("teams", len(draft_order) or 10)
    picks_made = len(picks)
    current_round = picks_made // teams
    pos_in_round = picks_made % teams
    # Snake draft: even rounds go 1..N, odd rounds go N..1
    slot_on_clock = (pos_in_round + 1) if current_round % 2 == 0 else (teams - pos_in_round)

    picks_until_me = (slot_on_clock - my_slot) % teams
    if picks_until_me > 2:
        print(f"Not close to your pick yet ({picks_until_me} picks away). Skipping email.")
        return

    drafted_names = [p.get("metadata", {}).get("first_name", "") + " " + p.get("metadata", {}).get("last_name", "")
                      for p in picks]
    drafted_str = ", ".join([n.strip() for n in drafted_names if n.strip()])

    # Pull current roster (if any -- rookie/startup drafts may have little or nothing yet)
    # so the recommendation is grounded in team timeline, not just best-player-available.
    roster_summary = "No existing roster data available."
    try:
        rosters = sget(f"/league/{league['league_id']}/rosters")
        my_roster = next((r for r in rosters if r.get("owner_id") == user["user_id"]), None)
        if my_roster and my_roster.get("players"):
            ids = my_roster["players"]
            players = load_players_minimal(ids)
            names = [player_name(players, pid) for pid in ids]
            wins = my_roster.get("settings", {}).get("wins", 0)
            losses = my_roster.get("settings", {}).get("losses", 0)
            roster_summary = f"Current roster ({wins}-{losses} last season): {', '.join(names)}"
    except Exception:
        pass

    reco = call_claude(
        "You are my dynasty fantasy football GM, not a redraft assistant. Dynasty means I keep this "
        "roster indefinitely across future seasons, so evaluate every player on long-term asset value, "
        "not just next season's fantasy points. Weigh: player age and career-stage trajectory, my team's "
        "actual competitive window (rebuilding vs. contending, based on my current roster strength and "
        "recent record), positional depth I already have vs. still need, and how a pick's dynasty value "
        "compares to trading it away. Never just default to redraft ADP or 'best player available' -- "
        "explain the roster-construction and timeline reasoning behind each recommendation. "
        "Search for current dynasty rookie rankings, dynasty startup ADP, and player age/situation before answering.",
        f"League: {league.get('name')} | {teams} teams | Round {current_round + 1}\n"
        f"{roster_summary}\n"
        f"Already drafted by the league so far: {drafted_str or 'nobody yet'}\n\n"
        f"It's about to be my turn (I'm {picks_until_me} picks away). Recommend my top 3 available "
        f"targets right now. For each, state: the player, their dynasty value/age context, and WHY they "
        f"fit my team's specific timeline and roster needs right now -- not just that they're the highest "
        f"ranked player left. If my roster suggests I should be rebuilding, say so and prioritize accordingly. "
        f"Keep it under 250 words, plain text, no markdown symbols.",
        max_tokens=600,
    )

    subject = f"Fantasy GM: You're on the clock soon! ({league.get('name')})"
    body = f"Round {current_round + 1}, {picks_until_me} picks until your turn.\n\n{reco}\n\n" \
           f"— Head to Sleeper to make your pick."
    send_email(subject, body)
    print("Sent draft email.")


def main():
    user, league = get_league()
    mode = FORCE_MODE or ("draft" if league.get("status") == "drafting" else "lineup")
    print(f"League: {league.get('name')} | status: {league.get('status')} | mode: {mode}")

    if mode == "draft":
        run_draft_mode(user, league, {})
    else:
        run_lineup_mode(user, league, {})


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
