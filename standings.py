import os
import re
import json
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Thread
import redis

app = Flask(__name__)
CORS(app)

# 전역 캐시 (문자열 키 사용: "seasonId__catId")
standings_cache = {}

#############################
# Redis 캐시 연결 설정
#############################
# Render 환경 변수에서 Redis 연결 정보 불러오기
redis_host = os.environ.get("REDIS_HOST", "localhost")
redis_port = int(os.environ.get("REDIS_PORT", 6379))
redis_password = os.environ.get("REDIS_PASSWORD", None)

# Redis 클라이언트 생성 (decode_responses=True로 문자열 반환)
r = redis.Redis(host=redis_host, port=redis_port, password=redis_password, decode_responses=True)
CACHE_KEY = "standings_cache"

def save_cache_to_redis(cache_data):
    """cache_data를 JSON 문자열로 변환하여 Redis에 저장 (ex=3600: 1시간 후 만료)"""
    try:
        r.set(CACHE_KEY, json.dumps(cache_data), ex=3600)
        print("Redis에 캐시 저장 완료.")
    except Exception as e:
        print("Redis에 캐시 저장 실패:", e)

def load_cache_from_redis():
    """Redis에서 캐시 데이터를 불러옴. 없으면 빈 dict 반환"""
    try:
        data = r.get(CACHE_KEY)
        if data:
            print("Redis에서 캐시 불러옴.")
            return json.loads(data)
    except Exception as e:
        print("Redis에서 캐시 불러오기 실패:", e)
    return {}

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

        # 플래그 이미지를 담는 키는 "Country"로 함.
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
    global standings_cache, LAST_DATA_YEAR
    standings_cache = {}

    print("Fetching seasons...")
    all_seasons = fetch_seasons()
    print(f"Fetched {len(all_seasons)} seasons.")

    # 'year' 키가 있는 시즌만 추출 및 내림차순 정렬
    valid_seasons = [s for s in all_seasons if "year" in s and isinstance(s["year"], int)]
    valid_seasons.sort(key=lambda x: x["year"], reverse=True)

    # 가장 최근 데이터가 있는 연도 판별 (실제 이벤트가 있는 시즌)
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

    # 전체 데이터를 가져오려면 하한 조건을 제거합니다.
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

    # 파일 캐시도 업데이트 (옵션)
    with open("standings_cache.json", "w", encoding="utf-8") as f:
        json.dump(standings_cache, f)
    # Redis에도 캐시 저장
    save_cache_to_redis(standings_cache)
    print("Done building cache, saved to standings_cache.json and Redis.")

#######################
# 캐시 로드 혹은 생성
#######################
def load_cache():
    global standings_cache
    # 먼저 Redis 캐시 시도
    loaded_cache = load_cache_from_redis()
    if loaded_cache:
        standings_cache = loaded_cache
        print("Loaded cache from Redis.")
    elif os.path.exists("standings_cache.json"):
        try:
            with open("standings_cache.json", "r", encoding="utf-8") as f:
                standings_cache = json.load(f)
            print("Loaded cache from standings_cache.json.")
            # 파일에서 로드한 캐시를 Redis에 저장
            save_cache_to_redis(standings_cache)
        except Exception as e:
            print(f"Failed to load from standings_cache.json: {e}, re-building cache...")
            precompute_standings()
    else:
        print("No cache found. Building from scratch...")
        precompute_standings()

#######################
# Flask 라우트
#######################
@app.route("/")
def serve_index():
    return """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>MotoGP Standings</title>
  <style>
    body { font-family: sans-serif; margin:20px; }
    select { padding:4px; }
    table { border-collapse: collapse; margin-top:10px; width:100%; max-width:900px; }
    table, th, td { border: 1px solid #888; }
    th, td { padding:6px; text-align:left; }
    .rider-num { padding:2px 6px; border-radius:4px; display:inline-block; }
    .flag { vertical-align: middle; margin-right:5px; }
    #loading { margin-top:10px; color:#666; display:none; }
  </style>
</head>
<body>
  <h2>MotoGP Standings</h2>
  <div>
    <label>Season:</label>
    <select id="seasonSelect"></select>
    <label style="margin-left:20px;">Category:</label>
    <select id="categorySelect"></select>
  </div>
  <div id="loading">Loading...</div>
  <div id="tableContainer"></div>

<script>
(async function(){
  const seasonSel = document.getElementById("seasonSelect");
  const catSel = document.getElementById("categorySelect");
  const tableContainer = document.getElementById("tableContainer");
  const loadingDiv = document.getElementById("loading");

  function showLoading(){ loadingDiv.style.display = "block"; }
  function hideLoading(){ loadingDiv.style.display = "none"; }

  // 헬퍼: 배경색 기반 텍스트 색 결정 (YIQ 공식)
  function getBestTextColor(bgColor) {
    if(!bgColor.startsWith("#")) return "#fff";
    let hex = bgColor.replace("#", "");
    if(hex.length === 3){
      hex = hex[0]+hex[0]+hex[1]+hex[1]+hex[2]+hex[2];
    }
    const r = parseInt(hex.substring(0,2), 16);
    const g = parseInt(hex.substring(2,4), 16);
    const b = parseInt(hex.substring(4,6), 16);
    const brightness = (0.299 * r) + (0.587 * g) + (0.114 * b);
    return (brightness > 150) ? "#000" : "#fff";
  }

  // "RAC, SPR" 표시 조건: 연도>=2023 && 카테고리 이름에 "motogp" 포함
  function shouldShowSPR(year, catName){
    if (!year || !catName) return false;
    return (parseInt(year, 10) >= 2023 && catName.toLowerCase().includes("motogp"));
  }

  async function fetchJson(url){
    showLoading();
    try {
      const res = await fetch(url);
      if(!res.ok) throw new Error("HTTP "+res.status);
      return await res.json();
    } catch(e){
      console.error(e);
      return [];
    } finally {
      hideLoading();
    }
  }

  async function loadSeasons(){
    return await fetchJson("/api/seasons");
  }

  async function loadCategories(seasonId){
    return await fetchJson("/api/categories?seasonUuid=" + encodeURIComponent(seasonId));
  }

  async function loadStandings(seasonId, categoryId){
    return await fetchJson(`/api/standings?seasonUuid=${encodeURIComponent(seasonId)}&categoryUuid=${encodeURIComponent(categoryId)}`);
  }

  // 초기 로드: 시즌 목록 불러오기
  let seasons = await loadSeasons();
  seasonSel.innerHTML = "";
  if(seasons.length === 0){
    tableContainer.innerHTML = "<p>No seasons available.</p>";
    return;
  }
  seasons.forEach(s => {
    const opt = document.createElement("option");
    opt.value = s.id;
    opt.textContent = s.year;
    seasonSel.appendChild(opt);
  });

  // 시즌 변경 시 카테고리 및 스탠딩 자동 업데이트
  async function updateCategoriesAndTable(){
    const sid = seasonSel.value;
    if(!sid) return;
    let cats = await loadCategories(sid);
    catSel.innerHTML = "";
    cats.forEach(c => {
      const opt = document.createElement("option");
      opt.value = c.id;
      opt.textContent = c.name;
      catSel.appendChild(opt);
    });
    await updateTable();
  }

  async function updateTable(){
    const sid = seasonSel.value;
    const cid = catSel.value;
    if(!sid || !cid) return;
    let data = await loadStandings(sid, cid);
    // 시즌 연도 및 카테고리 이름 확인
    const seasonObj = seasons.find(x => x.id === sid);
    const yearVal = seasonObj ? seasonObj.year : 0;
    const catObj = catSel.options[catSel.selectedIndex];
    const catName = catObj ? catObj.textContent : "";
    renderTable(data, yearVal, catName);
  }

  seasonSel.addEventListener("change", updateCategoriesAndTable);
  catSel.addEventListener("change", updateTable);

  // 페이지 로드 시 자동 업데이트
  await updateCategoriesAndTable();

  function renderTable(data, yearVal, catName){
    if(!Array.isArray(data) || data.length === 0){
      tableContainer.innerHTML = "<p>No data.</p>";
      return;
    }
    const showSpr = shouldShowSPR(yearVal, catName);
    let columns = ["P", "Rider", "#", "Points", "Def.", "Country", "Team", "Bike"];
    if(showSpr){
      columns = ["P", "Rider", "#", "Points", "Def.", "RAC", "SPR", "Country", "Team", "Bike"];
    }
    let html = "<table><thead><tr>";
    columns.forEach(col => { html += `<th>${col}</th>`; });
    html += "</tr></thead><tbody>";
    data.forEach(row => {
      html += "<tr>";
      columns.forEach(col => {
        if(col === "Country"){
          const flag = row[col] || "";
          html += `<td>${flag ? `<img class="flag" src="${flag}" width="24"/>` : ""}</td>`;
        }
        else if(col === "#"){
          const num = row["#"] || "";
          const bg = row["TeamColor"] || "#ddd";
          const txt = getBestTextColor(bg);
          html += `<td><span class="rider-num" style="background-color:${bg}; color:${txt};">${num}</span></td>`;
        }
        else {
          const val = row[col] || "";
          html += `<td>${val}</td>`;
        }
      });
      html += "</tr>";
    });
    html += "</tbody></table>";
    tableContainer.innerHTML = html;
  }
})();
</script>
</body>
</html>
    """

@app.route("/api/refresh")
def api_refresh():
    precompute_standings()
    return jsonify({"status": "ok", "message": "Cache Refreshed"})

@app.route("/api/seasons")
def api_seasons():
    season_ids = set()
    for key in standings_cache.keys():
        sid, _ = key.split("__")
        season_ids.add(sid)
    all_seasons = fetch_seasons()
    filtered = [s for s in all_seasons if s.get("id") in season_ids]
    filtered.sort(key=lambda x: x.get("year", 0), reverse=True)
    return jsonify(filtered)

@app.route("/api/categories")
def api_categories():
    season_id = request.args.get("seasonUuid", "")
    if not season_id:
        return jsonify([])
    cats = fetch_categories(season_id)
    return jsonify(cats)

@app.route("/api/standings")
def api_standings():
    global standings_cache
    season_id = request.args.get("seasonUuid", "")
    category_id = request.args.get("categoryUuid", "")
    if not season_id or not category_id:
        return jsonify([])
    key_str = f"{season_id}__{category_id}"
    data = standings_cache.get(key_str, [])
    # 만약 캐시 데이터가 오래된 경우 "CountryFlag" 키를 "Country"로 변경 (안되어 있다면)
    if data and "CountryFlag" in data[0]:
        for row in data:
            row["Country"] = row.pop("CountryFlag")
    return jsonify(data)

if __name__ == "__main__":
    # Render에서는 PORT 환경 변수가 지정됨 (기본 8080)
    port = int(os.environ.get("PORT", 8080))
    # 별도 스레드에서 캐시를 로드 (Redis 및 파일 캐시 모두 확인)
    Thread(target=load_cache).start()
    app.run(host="0.0.0.0", port=port, debug=True)
