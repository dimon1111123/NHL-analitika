import math
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

# --- НАСТРОЙКИ TELEGRAM БОТА ---
TELEGRAM_BOT_TOKEN = "9129216316:AAH89qhYjPDZTRTpPTNbq-qcHznc1lDjrAY"
TELEGRAM_CHAT_IDS = [
    "-1004394620734",  # Твой Telegram-канал
    "233585248",       # Пользователь 1
    "6431073786"       # Пользователь 2
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Accept": "application/json"
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# Три сезона для анализа очных встреч (H2H за 3 года)
H2H_SEASONS = ["20262027", "20252026", "20242025"]
CURRENT_SEASON = "20262027"

# Рубежи для отслеживания юбилеев в НХЛ
CAREER_GOAL_MILESTONES = {50, 100, 150, 200, 250, 300, 350, 400, 450, 500, 600, 700, 800, 900}
CAREER_POINT_MILESTONES = {50, 100, 150, 200, 250, 300, 350, 400, 450, 500, 600, 700, 800, 900, 1000, 1100, 1200, 1300, 1400, 1500}
CAREER_ASSIST_MILESTONES = {50, 100, 150, 200, 250, 300, 400, 500, 600, 700, 800, 900, 1000}
CAREER_GP_MILESTONES = {100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100, 1200, 1300, 1400, 1500}
SEASON_GOAL_MILESTONES = {10, 20, 25, 30, 35, 40, 45, 50, 60, 70}


def as_dict(val):
    return val if isinstance(val, dict) else {}


def send_tg_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        r = SESSION.post(url, json=payload, timeout=12)
        if r.status_code == 429:
            retry_after = as_dict(as_dict(r.json()).get("parameters")).get("retry_after", 5)
            time.sleep(retry_after + 1)
            SESSION.post(url, json=payload, timeout=12)
        elif r.status_code == 400 and "chat not found" in r.text:
            print(f"[!] ID {chat_id} недоступен (бот не добавлен в канал/чат или не нажат /start). Пропуск.")
        elif r.status_code != 200:
            payload.pop("parse_mode", None)
            SESSION.post(url, json=payload, timeout=12)
    except Exception as e:
        print(f"[!] Ошибка сети Telegram ({chat_id}): {e}")


def send_long_tg_message(chat_id, lines, header_title=""):
    chunk = []
    chunk_len = 0
    for line in lines:
        if chunk_len + len(line) > 3500:
            send_tg_message(chat_id, "\n".join(chunk))
            time.sleep(1.2)
            chunk = [f"*{header_title} (продолжение состава)*\n────────────────────────"] if header_title else []
            chunk_len = len(chunk[0]) if chunk else 0
        chunk.append(line)
        chunk_len += len(line) + 1

    if chunk:
        send_tg_message(chat_id, "\n".join(chunk))
        time.sleep(1.2)


def poisson_at_least_one(lmbda):
    if lmbda <= 0:
        return 0.0
    return (1.0 - math.exp(-lmbda)) * 100.0


def poisson_over_line(lmbda, k_threshold):
    if lmbda <= 0:
        return 0.0
    min_k = int(math.floor(k_threshold)) + 1
    prob_under = sum((lmbda**k * math.exp(-lmbda)) / math.factorial(k) for k in range(min_k))
    return max(0.0, min(99.9, (1.0 - prob_under) * 100.0))


def extract_name(val):
    if isinstance(val, dict):
        return val.get("default", "")
    return str(val or "")


# =========================================================================
# 1. ПОЛУЧЕНИЕ РАСПИСАНИЯ БЛИЖАЙШЕГО ИГРОВОГО ДНЯ НХЛ
# =========================================================================
def fetch_upcoming_nhl_matches():
    matches = []
    url = "https://api-web.nhle.com/v1/schedule/now"
    try:
        res = SESSION.get(url, timeout=10).json()
        game_week = res.get("gameWeek", [])

        target_day = None
        for day_obj in game_week:
            games = day_obj.get("games", [])
            upcoming = [
                g for g in games
                if g.get("gameState") in ["FUT", "PRE", "LIVE", "CRIT"] and g.get("gameType", 2) == 2
            ]
            if upcoming:
                target_day = day_obj
                break

        if not target_day:
            return matches

        schedule_date_str = target_day.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))

        for g in target_day.get("games", []):
            if g.get("gameState") in ["OFF", "FINAL"] or g.get("gameType", 2) != 2:
                continue

            h_team = g.get("homeTeam", {})
            a_team = g.get("awayTeam", {})
            h_abbr = h_team.get("abbrev", "")
            a_abbr = a_team.get("abbrev", "")

            h_name = (extract_name(h_team.get("placeName")) + " " + extract_name(h_team.get("commonName"))).strip() or h_abbr
            a_name = (extract_name(a_team.get("placeName")) + " " + extract_name(a_team.get("commonName"))).strip() or a_abbr

            start_utc = g.get("startTimeUTC", "")
            time_msk = "Ночью МСК"
            if start_utc:
                try:
                    dt_utc = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
                    dt_msk = dt_utc + timedelta(hours=3)
                    time_msk = dt_msk.strftime("%d.%m в %H:%M МСК")
                except Exception:
                    pass

            matches.append({
                "home_abbr": h_abbr,
                "away_abbr": a_abbr,
                "home_name": h_name,
                "away_name": a_name,
                "time": time_msk,
                "game_date": schedule_date_str
            })
    except Exception as e:
        print(f"[!] Ошибка загрузки расписания НХЛ: {e}")

    return matches


# =========================================================================
# 2. АНАЛИЗ ПРОТОКОЛОВ PLAY-BY-PLAY И ГОЛЫ ПО ПЕРИОДАМ
# =========================================================================
_GAME_PBP_GOALS_CACHE = {}
_TEAM_PERIOD_SCORERS_CACHE = {}


def fetch_game_goal_events_by_period(game_id):
    if game_id in _GAME_PBP_GOALS_CACHE:
        return _GAME_PBP_GOALS_CACHE[game_id]

    events = []
    url = f"https://api-web.nhle.com/v1/gamecenter/{game_id}/play-by-play"
    try:
        r = SESSION.get(url, timeout=8)
        if r.status_code == 200:
            pbp = r.json()
            for play in pbp.get("plays", []):
                if play.get("typeDescKey") == "goal":
                    p_num = play.get("periodDescriptor", {}).get("number", 0)
                    if p_num in (1, 2, 3):
                        details = play.get("details", {})
                        scorer_id = details.get("scoringPlayerId")
                        a1_id = details.get("assist1PlayerId")
                        a2_id = details.get("assist2PlayerId")
                        assists = [aid for aid in (a1_id, a2_id) if aid]
                        events.append((p_num, scorer_id, assists))
    except Exception:
        pass

    _GAME_PBP_GOALS_CACHE[game_id] = events
    return events


def fetch_team_season_period_scoring(team_abbr):
    if team_abbr in _TEAM_PERIOD_SCORERS_CACHE:
        return _TEAM_PERIOD_SCORERS_CACHE[team_abbr]

    sched_url = f"https://api-web.nhle.com/v1/club-schedule-season/{team_abbr}/{CURRENT_SEASON}"
    completed_game_ids = []
    try:
        res = SESSION.get(sched_url, timeout=10).json()
        for g in res.get("games", []):
            if g.get("gameType") == 2 and g.get("gameState") in ["OFF", "FINAL"]:
                gid = g.get("id")
                if gid:
                    completed_game_ids.append(gid)
    except Exception:
        pass

    player_period_stats = defaultdict(lambda: {"goals": {1: 0, 2: 0, 3: 0}, "points": {1: 0, 2: 0, 3: 0}})

    with ThreadPoolExecutor(max_workers=16) as ex:
        futures = [ex.submit(fetch_game_goal_events_by_period, gid) for gid in completed_game_ids]
        for fut in as_completed(futures):
            events = fut.result()
            for p_num, scorer_id, assists in events:
                if scorer_id:
                    player_period_stats[scorer_id]["goals"][p_num] += 1
                    player_period_stats[scorer_id]["points"][p_num] += 1
                for aid in assists:
                    player_period_stats[aid]["points"][p_num] += 1

    _TEAM_PERIOD_SCORERS_CACHE[team_abbr] = player_period_stats
    return player_period_stats


# =========================================================================
# 3. КАРЬЕРНАЯ СТАТИСТИКА, ДНИ РОЖДЕНИЯ И H2H ИГРОКА ЗА 3 СЕЗОНА
# =========================================================================
_PLAYER_GAMELOG_CACHE = {}
_PLAYER_LANDING_CACHE = {}


def fetch_player_landing_profile(player_id):
    if player_id in _PLAYER_LANDING_CACHE:
        return _PLAYER_LANDING_CACHE[player_id]

    profile = {
        "birthDate": "",
        "career_gp": 0,
        "career_goals": 0,
        "career_assists": 0,
        "career_points": 0
    }

    url = f"https://api-web.nhle.com/v1/player/{player_id}/landing"
    try:
        r = SESSION.get(url, timeout=7)
        if r.status_code == 200:
            data = r.json()
            profile["birthDate"] = data.get("birthDate", "")
            reg_career = data.get("careerTotals", {}).get("regularSeason", {})
            if not reg_career:
                reg_career = data.get("featuredStats", {}).get("regularSeason", {}).get("career", {})

            if reg_career:
                profile["career_gp"] = int(reg_career.get("gamesPlayed", 0))
                profile["career_goals"] = int(reg_career.get("goals", 0))
                profile["career_assists"] = int(reg_career.get("assists", 0))
                profile["career_points"] = int(reg_career.get("points", 0))
            else:
                gp_sum = g_sum = a_sum = p_sum = 0
                for s in data.get("seasonTotals", []):
                    if s.get("leagueAbbrev") == "NHL" and s.get("gameTypeId") == 2:
                        gp_sum += int(s.get("gamesPlayed", 0))
                        g_sum += int(s.get("goals", 0))
                        a_sum += int(s.get("assists", 0))
                        p_sum += int(s.get("points", 0))
                profile["career_gp"] = gp_sum
                profile["career_goals"] = g_sum
                profile["career_assists"] = a_sum
                profile["career_points"] = p_sum
    except Exception:
        pass

    _PLAYER_LANDING_CACHE[player_id] = profile
    return profile


def check_player_milestones_and_birthday(landing_prof, season_goals, birth_date_fallback, game_date_str):
    notes = []
    bdate = landing_prof.get("birthDate") or birth_date_fallback or ""

    if bdate and len(bdate) >= 10 and game_date_str and len(game_date_str) >= 10:
        b_year = int(bdate[:4])
        b_mm_dd = bdate[5:10]
        g_year = int(game_date_str[:4])
        g_mm_dd = game_date_str[5:10]

        if b_mm_dd == g_mm_dd:
            age = g_year - b_year
            notes.append(f"🎂 *ИМЕНИННИК СЕГОДНЯ!* Исполняется `{age}` лет")

    c_goals = landing_prof.get("career_goals", 0)
    c_pts = landing_prof.get("career_points", 0)
    c_ast = landing_prof.get("career_assists", 0)
    c_gp = landing_prof.get("career_gp", 0)

    if (c_goals + 1) in CAREER_GOAL_MILESTONES:
        notes.append(f"🚨 *Следующий гол — {c_goals + 1}-й в карьере НХЛ!* (сейчас `{c_goals}`)")
    elif (c_goals + 2) in {100, 200, 300, 400, 500, 600, 700, 800, 900}:
        notes.append(f"🚨 *До {c_goals + 2}-го гола в карьере НХЛ осталось 2 шайбы* (сейчас `{c_goals}`)")

    if (c_pts + 1) in CAREER_POINT_MILESTONES:
        notes.append(f"💎 *Следующее очко — {c_pts + 1}-е в карьере НХЛ!* (сейчас `{c_pts}`)")
    elif (c_pts + 2) in {100, 200, 300, 400, 500, 600, 700, 800, 900, 1000}:
        notes.append(f"💎 *До {c_pts + 2}-го очка в карьере НХЛ осталось 2 балла* (сейчас `{c_pts}`)")

    if (c_ast + 1) in CAREER_ASSIST_MILESTONES:
        notes.append(f"🎯 *Следующий пас — {c_ast + 1}-й в карьере НХЛ!* (сейчас `{c_ast}`)")

    if (c_gp + 1) in CAREER_GP_MILESTONES:
        notes.append(f"🎉 *Проводит {c_gp + 1}-й юбилейный матч в НХЛ!*")

    if (season_goals + 1) in SEASON_GOAL_MILESTONES:
        notes.append(f"🔥 *Следующий гол — {season_goals + 1}-й в этом сезоне!* (сейчас `{season_goals}`)")

    return notes


def fetch_player_season_gamelog(player_id, season_code):
    cache_key = (player_id, season_code)
    if cache_key in _PLAYER_GAMELOG_CACHE:
        return _PLAYER_GAMELOG_CACHE[cache_key]

    url = f"https://api-web.nhle.com/v1/player/{player_id}/game-log/{season_code}/2"
    try:
        r = SESSION.get(url, timeout=7)
        if r.status_code == 200:
            logs = r.json().get("gameLog", [])
            _PLAYER_GAMELOG_CACHE[cache_key] = logs
            return logs
    except Exception:
        pass

    _PLAYER_GAMELOG_CACHE[cache_key] = []
    return []


def analyze_player_full_data(player_id, opp_abbr):
    current_logs = fetch_player_season_gamelog(player_id, CURRENT_SEASON)
    landing_prof = fetch_player_landing_profile(player_id)

    gp_curr = len(current_logs)
    games_with_point = sum(1 for g in current_logs if int(g.get("points", 0)) >= 1)
    games_with_goal = sum(1 for g in current_logs if int(g.get("goals", 0)) >= 1)

    h2h_gp = 0
    h2h_g = 0
    h2h_a = 0
    h2h_p = 0
    h2h_sog = 0
    h2h_pts_games = 0
    h2h_goal_games = 0

    for s_code in H2H_SEASONS:
        s_logs = current_logs if s_code == CURRENT_SEASON else fetch_player_season_gamelog(player_id, s_code)
        for g in s_logs:
            if g.get("opponentAbbrev") == opp_abbr:
                goals = int(g.get("goals", 0))
                assists = int(g.get("assists", 0))
                points = int(g.get("points", 0))
                shots = int(g.get("shots", 0))

                h2h_gp += 1
                h2h_g += goals
                h2h_a += assists
                h2h_p += points
                h2h_sog += shots
                if points >= 1:
                    h2h_pts_games += 1
                if goals >= 1:
                    h2h_goal_games += 1

    return {
        "gp_curr": gp_curr,
        "games_with_point": games_with_point,
        "games_with_goal": games_with_goal,
        "h2h_gp": h2h_gp,
        "h2h_g": h2h_g,
        "h2h_a": h2h_a,
        "h2h_p": h2h_p,
        "h2h_sog": h2h_sog,
        "h2h_pts_games": h2h_pts_games,
        "h2h_goal_games": h2h_goal_games,
        "landing": landing_prof
    }


# =========================================================================
# 4. АНАЛИЗ СОСТАВА КОМАНДЫ И РАСЧЁТ ПОТЕНЦИАЛА ИГРОКОВ
# =========================================================================
def fetch_active_roster_birthdates(abbr):
    roster_map = {}
    url = f"https://api-web.nhle.com/v1/roster/{abbr}/current"
    try:
        r = SESSION.get(url, timeout=8)
        if r.status_code == 200:
            data = r.json()
            for group in ("forwards", "defensemen"):
                for p in data.get(group, []):
                    pid = p.get("id")
                    if pid:
                        roster_map[pid] = p.get("birthDate", "")
    except Exception:
        pass
    return roster_map


def analyze_team_players_vs_opponent(team_abbr, team_name, opp_abbr, game_date_str):
    print(f"  ↳ Полный анализ состава НХЛ: {team_name} ({team_abbr}) против {opp_abbr}...", flush=True)

    roster_birthdates = fetch_active_roster_birthdates(team_abbr)
    period_scoring_map = fetch_team_season_period_scoring(team_abbr)

    stats_url = f"https://api-web.nhle.com/v1/club-stats/{team_abbr}/now"
    skaters = []

    try:
        r = SESSION.get(stats_url, timeout=10)
        if r.status_code == 200:
            skaters = r.json().get("skaters", [])
    except Exception as e:
        print(f"  [!] Ошибка загрузки состава {team_abbr}: {e}")
        return []

    candidates = []
    for sk in skaters:
        pid = sk.get("playerId")
        gp = int(sk.get("gamesPlayed", 0))
        if gp <= 0:
            continue
        if roster_birthdates and pid not in roster_birthdates:
            continue
        candidates.append(sk)

    logs_by_pid = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        fut_map = {
            ex.submit(analyze_player_full_data, sk.get("playerId"), opp_abbr): sk.get("playerId")
            for sk in candidates
        }
        for fut in as_completed(fut_map):
            pid = fut_map[fut]
            try:
                logs_by_pid[pid] = fut.result()
            except Exception:
                logs_by_pid[pid] = None

    analyzed_players = []

    for sk in candidates:
        pid = sk.get("playerId")
        first_name = extract_name(sk.get("firstName"))
        last_name = extract_name(sk.get("lastName"))
        full_name = f"{first_name} {last_name}".strip()
        pos = sk.get("positionCode", "F")

        gp = int(sk.get("gamesPlayed", 1))
        goals = int(sk.get("goals", 0))
        assists = int(sk.get("assists", 0))
        points = int(sk.get("points", 0))
        shots = int(sk.get("shots", 0))

        gpg = goals / gp
        ppg = points / gp
        sog_pg = shots / gp

        log_info = logs_by_pid.get(pid) or {
            "gp_curr": gp, "games_with_point": 0, "games_with_goal": 0,
            "h2h_gp": 0, "h2h_g": 0, "h2h_a": 0, "h2h_p": 0, "h2h_sog": 0,
            "h2h_pts_games": 0, "h2h_goal_games": 0, "landing": {}
        }

        landing_prof = log_info.get("landing", {})
        bdate_fallback = roster_birthdates.get(pid, "")

        motivation_notes = check_player_milestones_and_birthday(
            landing_prof=landing_prof,
            season_goals=goals,
            birth_date_fallback=bdate_fallback,
            game_date_str=game_date_str
        )

        p_goals_dict = period_scoring_map[pid]["goals"]
        g_p1, g_p2, g_p3 = p_goals_dict[1], p_goals_dict[2], p_goals_dict[3]
        reg_goals_total = g_p1 + g_p2 + g_p3

        best_p_num = max((1, 2, 3), key=lambda p_n: p_goals_dict[p_n])
        best_p_goals = p_goals_dict[best_p_num]
        best_p_share = round((best_p_goals / reg_goals_total) * 100, 1) if reg_goals_total > 0 else 0.0

        is_period_specialist = False
        if (reg_goals_total >= 3 and best_p_goals >= 2 and best_p_share >= 55.0) or (reg_goals_total == 2 and best_p_goals == 2):
            is_period_specialist = True

        period_specialist_score = (best_p_goals * (best_p_share / 100.0)) if is_period_specialist else 0.0

        h2h_gp = log_info["h2h_gp"]
        h2h_g = log_info["h2h_g"]
        h2h_a = log_info["h2h_a"]
        h2h_p = log_info["h2h_p"]
        h2h_sog = log_info["h2h_sog"]

        h2h_gpg = (h2h_g / h2h_gp) if h2h_gp > 0 else gpg
        h2h_ppg = (h2h_p / h2h_gp) if h2h_gp > 0 else ppg
        h2h_sog_pg = (h2h_sog / h2h_gp) if h2h_gp > 0 else sog_pg

        is_h2h_killer = False
        if h2h_gp >= 2 and (h2h_ppg >= 1.0 or h2h_gpg >= 0.55):
            is_h2h_killer = True

        w_sea, w_h2h = (0.75, 0.25) if h2h_gp >= 2 else (1.0, 0.0)

        exp_goals = (w_sea * gpg) + (w_h2h * h2h_gpg)
        exp_points = (w_sea * ppg) + (w_h2h * h2h_ppg)
        exp_sog = (w_sea * sog_pg) + (w_h2h * h2h_sog_pg)

        prob_goal = round(poisson_at_least_one(exp_goals), 1)
        prob_point = round(poisson_at_least_one(exp_points), 1)

        if exp_sog >= 3.6:
            sog_line = 3.5
        elif exp_sog >= 2.6:
            sog_line = 2.5
        else:
            sog_line = 1.5

        prob_sog_over = round(poisson_over_line(exp_sog, sog_line), 1)

        actual_gp = max(1, log_info["gp_curr"] if log_info["gp_curr"] > 0 else gp)
        pts_hit_rate = round((log_info["games_with_point"] / actual_gp) * 100, 1)
        goal_hit_rate = round((log_info["games_with_goal"] / actual_gp) * 100, 1)

        rating_score = (
            (prob_point * 0.45)
            + (prob_goal * 0.35)
            + (exp_sog * 6.0)
            + (12.0 if is_h2h_killer else 0.0)
            + (8.0 if motivation_notes else 0.0)
        )

        analyzed_players.append({
            "name": full_name,
            "pos": pos,
            "team_abbr": team_abbr,
            "team_name": team_name,
            "opp_abbr": opp_abbr,
            "gp": gp,
            "goals": goals,
            "assists": assists,
            "points": points,
            "shots": shots,
            "gpg": round(gpg, 2),
            "ppg": round(ppg, 2),
            "sog_pg": round(sog_pg, 2),
            "games_with_point": log_info["games_with_point"],
            "games_with_goal": log_info["games_with_goal"],
            "pts_hit_rate": pts_hit_rate,
            "goal_hit_rate": goal_hit_rate,
            "g_p1": g_p1,
            "g_p2": g_p2,
            "g_p3": g_p3,
            "reg_goals_total": reg_goals_total,
            "best_p_num": best_p_num,
            "best_p_goals": best_p_goals,
            "best_p_share": best_p_share,
            "is_period_specialist": is_period_specialist,
            "period_specialist_score": period_specialist_score,
            "h2h_gp": h2h_gp,
            "h2h_g": h2h_g,
            "h2h_a": h2h_a,
            "h2h_p": h2h_p,
            "h2h_sog_pg": round(h2h_sog_pg, 1) if h2h_gp > 0 else 0.0,
            "h2h_pts_games": log_info["h2h_pts_games"],
            "is_h2h_killer": is_h2h_killer,
            "motivation_notes": motivation_notes,
            "exp_goals": round(exp_goals, 2),
            "exp_points": round(exp_points, 2),
            "exp_sog": round(exp_sog, 2),
            "prob_goal": prob_goal,
            "prob_point": prob_point,
            "sog_line": sog_line,
            "prob_sog_over": prob_sog_over,
            "rating_score": rating_score
        })

    analyzed_players.sort(key=lambda x: x["rating_score"], reverse=True)
    return analyzed_players


# =========================================================================
# 5. ФОРМАТИРОВАНИЕ КАРТОЧЕК ИГРОКОВ НХЛ
# =========================================================================
def format_player_card(idx, p):
    if p["motivation_notes"]:
        milestone_line = f"   🎉 *Особая мотивация:* {' | '.join(p['motivation_notes'])}\n"
    else:
        milestone_line = ""

    if p["reg_goals_total"] > 0:
        spec_tag = f" ➡️ *ударный {p['best_p_num']}-й период ({p['best_p_share']}% голов)!*" if p["is_period_specialist"] else ""
        period_line = f"   ⏱️ *Голы по периодам:* `1П: {p['g_p1']}` | `2П: {p['g_p2']}` | `3П: {p['g_p3']}`{spec_tag}\n"
    else:
        period_line = ""

    if p["h2h_gp"] > 0:
        killer_tag = " 🔥 *[Киллер соперника!]*" if p["is_h2h_killer"] else ""
        h2h_line = (
            f"   ⚔️ *Личные встречи (3 года vs {p['opp_abbr']}):* `{p['h2h_gp']}` игр — "
            f"*{p['h2h_g']}Г + {p['h2h_a']}П = {p['h2h_p']} очк.* "
            f"(очки в `{p['h2h_pts_games']}` из `{p['h2h_gp']}` игр | ср. `{p['h2h_sog_pg']}` бр.){killer_tag}"
        )
    else:
        h2h_line = f"   ⚔️ *Личные встречи (3 года vs {p['opp_abbr']}):* _нет очных матчей_"

    return (
        f"{idx}. 👤 *{p['name']}* (`{p['pos']}`)\n"
        f"{milestone_line}"
        f"   📊 *Форма сезона ({p['gp']} игр):* *{p['goals']}Г + {p['assists']}П = {p['points']} очк.* "
        f"(ср. `{p['ppg']}` очк./матч | очки в `{p['games_with_point']}` из `{p['gp']}` игр — `{p['pts_hit_rate']}%`)\n"
        f"   🔫 *Броски и голы в сезоне:* `{p['shots']}` бр. (`{p['sog_pg']}`/матч) | "
        f"забивал в `{p['games_with_goal']}` из `{p['gp']}` игр (`{p['goal_hit_rate']}%`)\n"
        f"{period_line}"
        f"{h2h_line}\n"
        f"   🎯 *Потенциал на матч:* "
        f"Очко (ТБ 0.5): *{p['prob_point']}%* | "
        f"Гол (ТБ 0.5): *{p['prob_goal']}%* | "
        f"Броски: *ИТБ ({p['sog_line']})* — *{p['prob_sog_over']}%* (ожид. `~{p['exp_sog']}`)"
    )


def select_match_period_specialists(match_pool, max_count=3):
    specialists = [p for p in match_pool if p["is_period_specialist"]]
    if not specialists:
        specialists = [p for p in match_pool if p["best_p_goals"] >= 2 and p["best_p_share"] >= 45.0]
        for s in specialists:
            s["period_specialist_score"] = s["best_p_goals"] * (s["best_p_share"] / 100.0)

    specialists.sort(key=lambda x: (x["period_specialist_score"], x["best_p_goals"], x["goals"]), reverse=True)
    return specialists[:max_count]


# =========================================================================
# 6. ОСНОВНОЙ ЦИКЛ БОТА НХЛ И ОТПРАВКА В TELEGRAM
# =========================================================================
def main():
    print("🏒 Запуск бота НХЛ (Канал + Личка)...", flush=True)
    matches = fetch_upcoming_nhl_matches()

    if not matches:
        print("[i] На ближайший игровой день матчей НХЛ не обнаружено.", flush=True)
        for uid in TELEGRAM_CHAT_IDS:
            send_tg_message(uid, "📅 *На ближайшую ночь матчей НХЛ в расписании не обнаружено.*")
        return

    print(f"Найдено предстоящих матчей НХЛ: {len(matches)}. Начинаем разбор всех игроков...", flush=True)

    all_night_players = []

    for m in matches:
        h_abbr, a_abbr = m["home_abbr"], m["away_abbr"]
        h_name, a_name = m["home_name"], m["away_name"]
        game_date_str = m["game_date"]
        match_title = f"{h_name} vs {a_name}"

        print(f"\n⚔️ Разбор матча НХЛ: {match_title} ({m['time']})", flush=True)

        home_players = analyze_team_players_vs_opponent(h_abbr, h_name, a_abbr, game_date_str)
        away_players = analyze_team_players_vs_opponent(a_abbr, a_name, h_abbr, game_date_str)

        all_night_players.extend(home_players)
        all_night_players.extend(away_players)

        lines = [
            f"🏒 *NHL PLAYERS PRO | ПОЛНЫЙ РАЗБОР СОСТАВОВ*",
            f"⚔️ *Матч:* `{h_name}` vs `{a_name}`",
            f"⏰ *Начало:* {m['time']}",
            f"════════════════════════",
            f"🏠 *ВЕСЬ СОСТАВ: {h_name} ({h_abbr}) — {len(home_players)} игр.*"
        ]

        if home_players:
            for idx, p in enumerate(home_players, 1):
                lines.append(format_player_card(idx, p))
        else:
            lines.append("_Нет данных по игрокам_")

        lines.append("────────────────────────")
        lines.append(f"✈️ *ВЕСЬ СОСТАВ: {a_name} ({a_abbr}) — {len(away_players)} игр.*")

        if away_players:
            for idx, p in enumerate(away_players, 1):
                lines.append(format_player_card(idx, p))
        else:
            lines.append("_Нет данных по игрокам_")

        match_pool = home_players + away_players
        if match_pool:
            best_goal = max(match_pool, key=lambda x: x["prob_goal"])
            best_point = max(match_pool, key=lambda x: x["prob_point"])
            best_sog = max(match_pool, key=lambda x: x["exp_sog"])
            killers = [p for p in match_pool if p["is_h2h_killer"]]
            period_specs = select_match_period_specialists(match_pool, max_count=3)
            milestone_players = [p for p in match_pool if p["motivation_notes"]]

            lines.append("════════════════════════")
            lines.append("🔥 *ТОП-ВЫБОРЫ ПО ИГРОКАМ НА ЭТОТ МАТЧ:*")
            lines.append(f"• 💎 *Очко (ТБ 0.5):* `{best_point['name']}` ({best_point['team_abbr']}) — *{best_point['prob_point']}%*")
            lines.append(f"• 🚨 *Гол (ТБ 0.5):* `{best_goal['name']}` ({best_goal['team_abbr']}) — *{best_goal['prob_goal']}%*")
            lines.append(
                f"• 🎯 *Броски:* `{best_sog['name']}` ({best_sog['team_abbr']}) "
                f"*ИТБ ({best_sog['sog_line']})* — *{best_sog['prob_sog_over']}%* (ожид. `~{best_sog['exp_sog']}`)"
            )
            if killers:
                k_names = ", ".join(f"`{k['name']}` ({k['h2h_g']}Г+{k['h2h_a']}П в {k['h2h_gp']} игр)" for k in killers[:5])
                lines.append(f"• ⚔️ *Специалисты по сопернику (3 года):* {k_names}")

            if period_specs:
                lines.append("\n⏱️ *ПРОФИЛЬНЫЕ СНАЙПЕРЫ ПО ПЕРИОДАМ (1П / 2П / 3П):*")
                for ps in period_specs:
                    lines.append(
                        f"• 🎯 *{ps['name']}* (`{ps['team_abbr']}`): забивает часто именно в *{ps['best_p_num']}-м периоде* — "
                        f"*{ps['best_p_goals']} из {ps['reg_goals_total']} голов* (*{ps['best_p_share']}%*)! "
                        f"_(1П: {ps['g_p1']} | 2П: {ps['g_p2']} | 3П: {ps['g_p3']})_"
                    )

            if milestone_players:
                lines.append("\n🎂🏆 *ИМЕНИННИКИ И ЮБИЛЯРЫ МАТЧА (ОСОБАЯ МОТИВАЦИЯ):*")
                for mp in milestone_players:
                    lines.append(f"• ⭐ *{mp['name']}* (`{mp['team_abbr']}`): {' | '.join(mp['motivation_notes'])}")

        for uid in TELEGRAM_CHAT_IDS:
            send_long_tg_message(uid, lines, header_title=match_title)

    # =====================================================================
    # ИТОГОВЫЙ ТОП НОЧИ ПО ВСЕМ МАТЧАМ НХЛ
    # =====================================================================
    if all_night_players:
        top_goals = sorted(all_night_players, key=lambda x: x["prob_goal"], reverse=True)[:5]
        top_points = sorted(all_night_players, key=lambda x: x["prob_point"], reverse=True)[:5]
        top_shots = sorted(all_night_players, key=lambda x: x["exp_sog"], reverse=True)[:5]
        top_killers = sorted(
            [p for p in all_night_players if p["is_h2h_killer"]],
            key=lambda x: (x["h2h_p"] / max(1, x["h2h_gp"]), x["h2h_p"]),
            reverse=True
        )[:5]
        top_period_snipers = select_match_period_specialists(all_night_players, max_count=5)
        all_milestone_players = [p for p in all_night_players if p["motivation_notes"]]

        summary_lines = [
            "👑 *ЖЕЛЕЗО НОЧИ: ТОП ИГРОКОВ НХЛ (ИЗ ВСЕХ СОСТАВОВ)*",
            "════════════════════════",
            "💎 *ТОП-5 НА ОЧКО В МАТЧЕ (ТБ 0.5):*"
        ]
        for i, p in enumerate(top_points, 1):
            summary_lines.append(
                f"{i}. *{p['name']}* (`{p['team_abbr']}` vs `{p['opp_abbr']}`) — *{p['prob_point']}%*\n"
                f"   _Сезон: {p['points']} очк. в {p['gp']} игр (очки в {p['pts_hit_rate']}% матчей)_"
            )

        summary_lines.append("\n🚨 *ТОП-5 СНАЙПЕРОВ НА ГОЛ (ТБ 0.5):*")
        for i, p in enumerate(top_goals, 1):
            summary_lines.append(
                f"{i}. *{p['name']}* (`{p['team_abbr']}` vs `{p['opp_abbr']}`) — *{p['prob_goal']}%*\n"
                f"   _Сезон: {p['goals']} гол. в {p['gp']} игр | 3 года vs {p['opp_abbr']}: {p['h2h_g']} гол. в {p['h2h_gp']} игр_"
            )

        summary_lines.append("\n🎯 *ТОП-5 ПО БРОСКАМ В СТВОР (SOG):*")
        for i, p in enumerate(top_shots, 1):
            summary_lines.append(
                f"{i}. *{p['name']}* (`{p['team_abbr']}` vs `{p['opp_abbr']}`) — *ИТБ ({p['sog_line']})* (`{p['prob_sog_over']}%`)\n"
                f"   _Ожидание: ~{p['exp_sog']} бр. (Сезон: {p['sog_pg']}/матч)_"
            )

        if top_period_snipers:
            summary_lines.append("\n⏱️ *ГЛАВНЫЕ СНАЙПЕРЫ КОНКРЕТНЫХ ПЕРИОДОВ НХЛ:*")
            for i, ps in enumerate(top_period_snipers, 1):
                summary_lines.append(
                    f"{i}. *{ps['name']}* (`{ps['team_abbr']}`): "
                    f"забивает в *{ps['best_p_num']}-м периоде* — *{ps['best_p_goals']} из {ps['reg_goals_total']} голов ({ps['best_p_share']}%)* "
                    f"_(1П: {ps['g_p1']} | 2П: {ps['g_p2']} | 3П: {ps['g_p3']})_"
                )

        if top_killers:
            summary_lines.append("\n⚔️ *ГЛАВНЫЕ «КИЛЛЕРЫ» СОПЕРНИКОВ НХЛ:*")
            for i, p in enumerate(top_killers, 1):
                summary_lines.append(
                    f"{i}. *{p['name']}* (`{p['team_abbr']}` vs `{p['opp_abbr']}`): "
                    f"`{p['h2h_gp']}` игр — *{p['h2h_g']}Г + {p['h2h_a']}П = {p['h2h_p']} очк.*"
                )

        if all_milestone_players:
            summary_lines.append("\n🎂🏆 *ВСЕ ИМЕНИННИКИ И ЮБИЛЯРЫ ИГРОВОГО ДНЯ НХЛ:*")
            for i, mp in enumerate(all_milestone_players, 1):
                summary_lines.append(
                    f"{i}. *{mp['name']}* (`{mp['team_abbr']}`): {' | '.join(mp['motivation_notes'])}"
                )

        summary_lines.append("════════════════════════")
        summary_lines.append("📊 _Расчёт: сезон + голы по периодам + очные встречи (3 года) + дни рождения и юбилеи НХЛ._")

        for uid in TELEGRAM_CHAT_IDS:
            send_long_tg_message(uid, summary_lines, header_title="ТОП ИГРОКОВ НОЧИ")

    print("\n🎉 Готово! Все разборы игроков НХЛ отправлены в Telegram.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()