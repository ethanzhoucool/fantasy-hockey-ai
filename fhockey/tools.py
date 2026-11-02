"""Read-only reports that replace the old browser scripts: league snapshot (snapshot.js), trade offers and
trade_eval.py input (trade_state.js), and chat threads (chat_state.js)."""
import datetime as dt

from .league import TERMINAL_TRADE, team_label
from .players import INJ_SHORT, POS_BY_DEFAULT, js_round, stat

SNAPSHOT_COLUMNS = "team|name|pos|inj|espnProjAvg|seasonAvg|gp|pctOwned|lastNews|playerId|status"


def _iso_date(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d") if ms else ""


def snapshot_row(team, p, status, season):
    pr, sa = stat(p, season, 1), stat(p, season, 0)
    gp = js_round(sa["appliedTotal"] / sa["appliedAverage"]) if sa and sa.get("appliedAverage") else 0
    fmt = lambda s: f"{s['appliedAverage']:.2f}" if s and s.get("appliedAverage") is not None else ""
    return "|".join(str(x) for x in (
        team, p.get("fullName", ""), POS_BY_DEFAULT.get(p.get("defaultPositionId"), "F"),
        INJ_SHORT.get(p.get("injuryStatus"), ""), fmt(pr), fmt(sa), gp,
        js_round((p.get("ownership") or {}).get("percentOwned") or 0), _iso_date(p.get("lastNewsDate")),
        p.get("id"), status))


def build_snapshot(client, now=None, fa_limit=60):
    """League file for trades/trade_scan.py and trades/robust.py: every rostered player plus the top free agents."""
    now = now or dt.datetime.now(dt.timezone.utc)
    league = client.all_rosters()
    sp = league["scoringPeriodId"]
    out = [f"# pulled {now.strftime('%Y-%m-%dT%H:%M:%S')}Z period {sp} "
           f"(ESPN mRoster+kona_player_info; FA = top {fa_limit} FA/waivers by %owned)",
           f"# {SNAPSHOT_COLUMNS}"]
    for t in league.get("teams") or []:
        for e in (t.get("roster") or {}).get("entries") or []:
            out.append(snapshot_row(t["id"], e["playerPoolEntry"]["player"], "ONTEAM", client.season))
    for x in client.free_agents(sp, limit=fa_limit).get("players") or []:
        out.append(snapshot_row("FA", x["player"], x.get("status"), client.season))
    return "\n".join(out) + "\n", sp


# ---------------------------------------------------------------- trades


def _trade_pos(p):
    d = p.get("defaultPositionId")
    return "D" if d == 4 else "G" if d == 5 else "F"


def _trade_inj(e):
    if e.get("lineupSlotId") == 8:
        return "IR"
    return INJ_SHORT.get(e["playerPoolEntry"]["player"].get("injuryStatus"), "")


def league_rosters(client):
    league = client.all_rosters()
    rosters, names = {}, {}
    for t in league.get("teams") or []:
        rows = []
        for e in (t.get("roster") or {}).get("entries") or []:
            p = e["playerPoolEntry"]["player"]
            names[p["id"]] = p.get("fullName", str(p["id"]))
            rows.append((p.get("fullName", ""), _trade_pos(p), _trade_inj(e)))
        rosters[t["id"]] = rows
    return league, rosters, names


def pending_offers(client, team_id, cur, now_ms, names=None, back=3):
    """Pending, unexpired TRADE_PROPOSALs that involve team_id, from the last `back` scoring periods."""
    names = names or {}
    offers, seen = [], set()
    for sp in range(max(1, cur - back), cur + 1):
        for t in client.transactions(sp).get("transactions") or []:
            if t.get("type") != "TRADE_PROPOSAL" or t.get("status") != "PENDING" or t.get("id") in seen:
                continue
            if t.get("expirationDate") and t["expirationDate"] < now_ms:
                continue
            it = t.get("items") or []
            if not any(i.get("fromTeamId") == team_id or i.get("toTeamId") == team_id for i in it):
                continue
            seen.add(t.get("id"))
            partner = next((x for x in (i.get("toTeamId") if i.get("fromTeamId") == team_id else i.get("fromTeamId")
                                        for i in it if i.get("type") != "DROP") if x and x != team_id), None)
            offers.append({
                "id": t.get("id"), "mine": t.get("teamId") == team_id, "partner": partner,
                "expires": t.get("expirationDate"), "sp": sp,
                "give": [names.get(i["playerId"], i["playerId"]) for i in it
                         if i.get("fromTeamId") == team_id and i.get("type") != "DROP"],
                "get": [names.get(i["playerId"], i["playerId"]) for i in it if i.get("toTeamId") == team_id],
                "drop": [names.get(i["playerId"], i["playerId"]) for i in it
                         if i.get("fromTeamId") == team_id and i.get("type") == "DROP"],
            })
    return offers


def offers_report(client, team_id, now_ms):
    league, rosters, names = league_rosters(client)
    teams = {t["id"]: team_label(t) for t in league.get("teams") or []}
    cur = league["scoringPeriodId"]
    offers = pending_offers(client, team_id, cur, now_ms, names)
    lines = [f"period {cur} | {len(offers)} pending offer(s)"]
    for o in offers:
        exp = dt.datetime.fromtimestamp(o["expires"] / 1000).strftime("%m/%d %H:%M") if o["expires"] else "?"
        lines.append(f"{o['id']}: {'MY OFFER to' if o['mine'] else 'OFFER TO ME from'} {teams.get(o['partner'], o['partner'])}"
                     f" (team {o['partner']}) | I give {' + '.join(map(str, o['give']))} | I get {' + '.join(map(str, o['get']))}"
                     + (f" | I drop {' + '.join(map(str, o['drop']))}" if o["drop"] else "")
                     + f" | expires {exp}")
    return "\n".join(lines), offers


def trade_input(client, team_id, now_ms, offer_id=None, partner=None, give=(), get=(), drops=()):
    """trade_eval.py input (MINE/THEIRS/GIVE/GET/FA lines) for a pending offer or a hypothetical deal."""
    league, rosters, names = league_rosters(client)
    cur = league["scoringPeriodId"]
    if offer_id is not None:
        offers = pending_offers(client, team_id, cur, now_ms, names)
        o = next((x for x in offers if str(x["id"]) == str(offer_id)), None)
        if not o:
            raise ValueError(f"no pending offer {offer_id}")
        partner, give, get, drops = o["partner"], o["give"], o["get"], o["drop"]
    if partner is None:
        raise ValueError("need --offer or --partner")
    fa = client.players({"filterStatus": {"value": ["FREEAGENT"]}, "filterSlotIds": {"value": [3, 4]}, "limit": 8,
                         "sortPercOwned": {"sortPriority": 1, "sortAsc": False}}, cur)
    out = [f"MINE|{n}|{p}|{i}" for n, p, i in rosters.get(team_id, [])]
    out += [f"THEIRS|{n}|{p}|{i}" for n, p, i in rosters.get(int(partner), [])]
    out += [f"GIVE|{n}" for n in give] + [f"GET|{n}" for n in get] + [f"DROP|{n}" for n in drops]
    out += [f"FA|{x['player']['fullName']}|{_trade_pos(x['player'])}" for x in fa.get("players") or []]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- chat


def chat_report(client, view, since_ms, me_bare):
    """Chat threads with messages newer than since_ms; marks threads whose last message is not Ethan's."""
    r = client.communication()
    who = lambda a: "ESPN" if a == "LM" else "ETHAN" if (a or "").strip("{}").upper() == me_bare else (
        view.member_name(a) or f"team {view.member_team(a) or '?'}")
    fmt = lambda ms: dt.datetime.fromtimestamp(ms / 1000).strftime("%m/%d %H:%M")
    out = []
    for tp in r.get("topics") or []:
        if not str(tp.get("type", "")).startswith("CHAT"):
            continue
        msgs = sorted((m for m in tp.get("messages") or [] if m.get("content") and not m.get("isDeleted")),
                      key=lambda m: m.get("date", 0))
        if not msgs:
            continue
        if tp["type"] == "CHAT_ALL_MEMBERS":
            thread = "LEAGUE CHAT"
        else:
            others = [v for v in tp.get("viewableBy") or [] if (v or "").strip("{}").upper() != me_bare]
            thread = "DM " + "/".join(who(v) for v in others)
        fresh = [m for m in msgs if m.get("date", 0) > since_ms]
        last = msgs[-1]
        needs = bool(fresh) and who(last.get("author")) != "ETHAN"
        out.append(f"## {thread} | last {fmt(last['date'])} by {who(last.get('author'))}{' | NEEDS REPLY' if needs else ''}")
        if fresh:
            first = msgs.index(fresh[0])
            for m in msgs[max(0, first - 6):]:
                out.append(f"{'*' if m['date'] > since_ms else ' '} {fmt(m['date'])} {who(m.get('author'))}: {m['content']}")
    return "\n".join(out) or "no chats"


def is_terminal_trade(status):
    return status in TERMINAL_TRADE
