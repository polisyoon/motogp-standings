import os
import re
import json
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# 전역 캐시 (파일 기반)
standings_cache = {}

#######################
# 헬퍼 함수 (색상 추출)
#######################
def extract_border_left(style_str: str) -> str:
    match_hex = re.search(r"border-left\s*:\s*\d+px\s+solid\s+(#[0-9A-Fa-f]{6})", style_str)
    if match_hex:
        return match_hex.group(1)
    match_rgb = re.search(
        r"border-left\s*:\s*\d+px\s+solid\s+rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)",
        style_str, re.IGNORECASE
    )
    if match_rgb:
        r_val = int(match_rgb.group(1))
        g_val = int(match_rgb.group(2))
        b_val = int(match_rgb.group(3))
        return f"rgb({r_val},{g_val},{b_val})"
    return ""

def extract_background_color(style_str: str) -> str:
    match_hex = re.search(r"background-color\s*:\s*(#[0-9A-Fa-f]{6})", style_str)
    if match_hex:
        return match_hex.group(1)
    match_rgb = re.search(
        r"background-color\s*:\s*rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)",
        style_str, re.IGNORECASE
    )
    if match_rgb:
        r_val = int(match_rgb.group(1))
        g_val = int(match_rgb.group(2))
        b_val = int(match_rgb.group(3))
        return f"rgb({r_val},{g_val},{b_val})"
    return ""

def extract_hex_anywhere(text: str) -> str:
    match_hex = re.search(r"#([0-9A-Fa-f]{6})", text)
    if match_hex:
        return f"#{match_hex.group(1)}"
    return ""

def extract_rider_color(rider_info: dict) -> str:
    color = rider_info.get("team_color") or rider_info.get("rider_color")
    if color:
        return color
    style_str = rider_info.get("style", "")
    if style_str:
        c_border = extract_border_left(style_str)
        if c_border:
            return c_border
        c_bg = extract_background_color(style_str)
        if c_bg:
            return c_bg
    c_hex = extract_hex_anywhere(str(rider_info))
    if c_hex:
        return c_hex
    return "#ddd"

#######################
# MotoGP API 호출 함수
#######################
def fetch(url):
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()

def fetch_seasons():
    data = fetch("https://api.motogp.pulselive.com/motogp/v1/results/seasons")
    return data if isinstance(data, list) else []

def fetch_categories(season_id):
    url = f"https://api.motogp.pulselive.com/motogp/v1/results/categories?seasonUuid={season_id}"
    data = fetch(url)
    return data if isinstance(data, list) else []

def fetch_events(season_id):
    url = f"https://api.motogp.pulselive.com/motogp/v1/results/events?seasonUuid={season_id}&isFinished=true"
    try:
        data = fetch(url)
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            print(f"  => 404 Not Found for season {season_id} in events, treating as no events.")
            data = []
        else:
            print(f"  => HTTP Error for season {season_id}: {e}")
            raise
    return data if isinstance(data, list) else []

def fetch_standings_api(season_id, category_id):
    url = f"https://api.motogp.pulselive.com/motogp/v1/results/standings?seasonUuid={season_id}&categoryUuid={category_id}"
    data = fetch(url)
    return data if "classification" in data else {"classification": []}

def fetch_session_classification(session_id):
    url = f"https://api.motogp.pulselive.com/motogp/v1/results/session/{session_id}/classification?test=false"
    data = fetch(url)
    return data if "classification" in data else {}

def fetch_sessions(event_id, category_id):
    url = f"https://api.motogp.pulselive.com/motogp/v1/results/sessions?eventUuid={event_id}&categoryUuid={category_id}"
    data = fetch(url)
    return [s for s in data if s.get("type", "").upper() in ["SPR", "RAC"]]

########################
# SPR/RAC & 팀 컬러 계산
########################
def calculate_points_and_team_colors(sessions):
    rider_dict = {}
    max_workers = min(20, len(sessions)) if sessions else 1
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futmap = {executor.submit(fetch_session_classification, s["id"]): s for s in sessions}
        for fut in as_completed(futmap):
            session = futmap[fut]
            try:
                data = fut.result()
            except Exception as e:
                print(f"Error fetching session classification: {e}")
                continue
            if not data or "classification" not in data:
                continue
            session_type = session.get("type", "").upper()
            if session_type not in ["SPR", "RAC"]:
                continue
            for rider_info in data["classification"]:
                rid = rider_info["rider"]["id"]
                if rid not in rider_dict:
                    rider_dict[rid] = {"SPR": 0, "RAC": 0, "team_color": ""}
                pts = rider_info.get("points", 0)
                if session_type == "SPR":
                    rider_dict[rid]["SPR"] += pts
                else:
                    rider_dict[rid]["RAC"] += pts
                color = extract_rider_color(rider_info)
                rider_dict[rid]["team_color"] = color
    return rider_dict

#######################
# 스탠딩 계산 (한 시즌/카테고리)
#######################
def get_full_standings(season_id, category_id):
    raw = fetch_standings_api(season_id, category_id)
    standings = raw.get("classification", [])
    if not standings:
        return []
    leader_points = standings[0].get("points", 0) if standings[0].get("points") else 0

    events = fetch_events(season_id)
    all_sess = []
    if events:
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(fetch_sessions, e["id"], category_id): e for e in events}
            for f in as_completed(futs):
                try:
                    r = f.result()
                    if isinstance(r, list):
                        all_sess.extend(r)
                except Exception as e:
                    print(f"Error fetching sessions: {e}")
                    continue

    rider_map = {}
    if all_sess:
        rider_map = calculate_points_and_team_colors(all_sess)

    results = []
    for rd in standings:
        rider = rd["rider"]
        full_name = (rider.get("full_name") or (rider.get("name", "") + " " + rider.get("surname", ""))).strip()
        pos = rd.get("position", "")
        pts = rd.get("points", 0)
        rid = rider.get("id", 0)
        def_gap = ""
        if pos != 1:
            gap = leader_points - pts
            def_gap = f"-{gap}" if gap > 0 else "0"

        team_data = rd.get("team") or {}
        fallback_color = team_data.get("color", "")
        spr_val = rider_map.get(rid, {}).get("SPR", 0)
        rac_val = rider_map.get(rid, {}).get("RAC", 0)
        final_color = rider_map.get(rid, {}).get("team_color", fallback_color) or "#ddd"

        rnum = rider.get("number")
        if not rnum or str(rnum).strip().lower() == "none":
            rnum = ""

        country = rider.get("country", {})
        iso = country.get("iso", "")
        if iso:
            flag_url = f"https://flagicons.lipis.dev/flags/4x3/{iso.lower()}.svg"
        else:
            flag_url = "https://flagicons.lipis.dev/flags/4x3/xx.svg"

        team_name = team_data.get("name", "")
        bike_name = rd.get("constructor", {}).get("name", "N/A")

        results.append({
            "P": pos,
            "Rider": full_name,
            "#": rnum,
            "Points": pts,
            "Def.": def_gap,
            "RAC": rac_val,
            "SPR": spr_val,
            "Country": flag_url,
            "Team": team_name,
            "Bike": bike_name,
            "TeamColor": final_color
        })
    return results

#######################
# 캐시 갱신 함수 (Precompute)
#######################
def precompute_standings():
    global standings_cache
    standings_cache = {}

    print("Fetching seasons...")
    all_seasons = fetch_seasons()
    print(f"Fetched {len(all_seasons)} seasons.")

    valid_seasons = [s for s in all_seasons if "year" in s and isinstance(s["year"], int)]
    valid_seasons.sort(key=lambda x: x["year"], reverse=True)

    max_year_with_data = None
    for s in valid_seasons:
        season_id = s["id"]
        year_val = s["year"]
        print(f"Check if season {year_val} has any data...")
        try:
            events = fetch_events(season_id)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                print(f"  => 404 Not Found for year={year_val}, skip.")
                events = []
            else:
                print(f"  => HTTP Error for year {year_val}: {e}")
                raise
        if isinstance(events, list) and len(events) > 0:
            max_year_with_data = year_val
            print(f"  => Found events for year {year_val}. Using this as most recent data year.")
            break
        else:
            print(f"  => No events for year {year_val}, skip.")
    if not max_year_with_data:
        print("No season has any events. Aborting cache build.")
        return

    LAST_DATA_YEAR = max_year_with_data

    seasons_to_build = [s for s in valid_seasons if s["year"] <= max_year_with_data]
    seasons_to_build.sort(key=lambda x: x["year"], reverse=True)
    print(f"Will build cache for {len(seasons_to_build)} seasons: from {max_year_with_data} downwards.")

    for s in seasons_to_build:
        season_id = s["id"]
        year = s["year"]
        print(f"Now building for season_id={season_id}, year={year}...")
        cats = fetch_categories(season_id)
        print(f"  => Found {len(cats)} categories.")
        for c in cats:
            cat_id = c["id"]
            cat_name = c.get("name", "")
            print(f"    -> get_full_standings({season_id}, {cat_id}) {cat_name}")
            data = get_full_standings(season_id, cat_id)
            key_str = f"{season_id}__{cat_id}"
            standings_cache[key_str] = data

    # 파일에 캐시 저장
    with open("standings_cache.json", "w", encoding="utf-8") as f:
        json.dump(standings_cache, f, indent=2)
    print("캐시를 standings_cache.json에 저장했습니다.")

if __name__ == "__main__":
    precompute_standings()