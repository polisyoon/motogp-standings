import streamlit as st
import requests
import pandas as pd
import pycountry
from concurrent.futures import ThreadPoolExecutor, as_completed

st.set_page_config(page_title="MotoGP Standings", layout="wide")
st.title("MotoGP Standings")

# 캐시 데코레이터 사용 (캐시를 유지하면서도 최신 데이터 반영)
@st.cache_data(ttl=3600)  # 캐시 유효 기간 설정 (1시간)
def fetch_seasons():
    url = "https://api.motogp.pulselive.com/motogp/v1/results/seasons"
    response = requests.get(url)
    if response.status_code == 200:
        data = response.json()
        return data if isinstance(data, list) else []
    else:
        st.error(f"Seasons API 요청 실패: {response.status_code}")
        return []

@st.cache_data(ttl=3600)
def fetch_categories(season_id):
    url = f"https://api.motogp.pulselive.com/motogp/v1/results/categories?seasonUuid={season_id}"
    response = requests.get(url)
    if response.status_code == 200:
        data = response.json()
        return data if isinstance(data, list) else []
    else:
        st.error(f"Categories API 요청 실패: {response.status_code}")
        return []

@st.cache_data(ttl=3600)
def fetch_events(season_id):
    url = f"https://api.motogp.pulselive.com/motogp/v1/results/events?seasonUuid={season_id}&isFinished=true"
    response = requests.get(url)
    if response.status_code == 200:
        data = response.json()
        return data if isinstance(data, list) else []
    else:
        st.error(f"Events API 요청 실패: {response.status_code}")
        return []

@st.cache_data(ttl=3600)
def fetch_standings(season_id, category_id):
    url = f"https://api.motogp.pulselive.com/motogp/v1/results/standings?seasonUuid={season_id}&categoryUuid={category_id}"
    response = requests.get(url)
    if response.status_code == 200:
        data = response.json()
        return data if 'classification' in data else None
    else:
        st.error(f"Standings API 요청 실패: {response.status_code}")
        return None

@st.cache_data(ttl=3600)
def fetch_session_classification(session_id):
    """세션별 분류 데이터 가져오기"""
    classification_url = f"https://api.motogp.pulselive.com/motogp/v1/results/session/{session_id}/classification?test=false"
    response = requests.get(classification_url)
    if response.status_code == 200:
        data = response.json()
        return data if 'classification' in data else {}
    else:
        st.error(f"Session Classification API 요청 실패(Session ID: {session_id}): {response.status_code}")
        return {}

def fetch_sessions(event_id, category_id):
    """해당 이벤트/카테고리의 SPR 및 RAC 세션 정보만 가져옴"""
    url = f"https://api.motogp.pulselive.com/motogp/v1/results/sessions?eventUuid={event_id}&categoryUuid={category_id}"
    response = requests.get(url)
    if response.status_code == 200:
        data = response.json()
        # SPR과 RAC 세션만 필터링
        return [s for s in data if s.get('type', '').upper() in ['SPR', 'RAC']]
    else:
        st.error(f"Sessions API 요청 실패: {response.status_code}")
        return []

def get_country_flag_url(country_iso, country_name):
    if country_iso:
        return f"https://flagicons.lipis.dev/flags/4x3/{country_iso.lower()}.svg"
    try:
        country = pycountry.countries.search_fuzzy(country_name)[0]
        return f"https://flagicons.lipis.dev/flags/4x3/{country.alpha_2.lower()}.svg"
    except:
        return "https://flagicons.lipis.dev/flags/4x3/xx.svg"

def get_rider_name(rider_data):
    return rider_data.get('full_name') or f"{rider_data.get('name', '')} {rider_data.get('surname', '')}".strip()

def calculate_points_and_team_colors(sessions):
    """
    SPR/RAC 포인트와 team_color를 함께 가져와 rider별로 저장한다.
    반환값: rider_dict = {
        rider_id: {
            'SPR': int,
            'RAC': int,
            'team_color': str,
        },
        ...
    }
    """
    rider_dict = {}
    max_workers = min(20, len(sessions))  # 최대 스레드 수 조정

    progress_bar = st.progress(0)
    total_sessions = len(sessions)
    processed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_session = {executor.submit(fetch_session_classification, s['id']): s for s in sessions}
        for future in as_completed(future_to_session):
            session = future_to_session[future]
            data = future.result()
            if not data:
                processed += 1
                progress_bar.progress(min(processed / total_sessions, 1.0))
                continue

            session_type = session.get('type', '').upper()
            # SPR, RAC만 점수 계산
            if session_type not in ['SPR', 'RAC']:
                processed += 1
                progress_bar.progress(min(processed / total_sessions, 1.0))
                continue

            for rider_info in data.get('classification', []):
                rider_id = rider_info['rider']['id']
                if rider_id not in rider_dict:
                    rider_dict[rider_id] = {'SPR': 0, 'RAC': 0, 'team_color': ''}

                # SPR/RAC 포인트
                points = rider_info.get('points', 0)
                if session_type == 'SPR':
                    rider_dict[rider_id]['SPR'] += points
                elif session_type == 'RAC':
                    rider_dict[rider_id]['RAC'] += points

                # 팀 컬러 정보 가져오기
                color_in_api = rider_info.get('team_color') or rider_info.get('team', {}).get('color') or ''
                if color_in_api:
                    rider_dict[rider_id]['team_color'] = color_in_api

            processed += 1
            progress_bar.progress(min(processed / total_sessions, 1.0))

    progress_bar.empty()
    return rider_dict

def process_standings(season_id, category_id, year, category_name):
    """Standings 데이터를 표로 표시 (SPR/RAC Points + Team Color)"""
    standings_data = fetch_standings(season_id, category_id)
    if not standings_data:
        st.warning("데이터를 가져오지 못했습니다.")
        return
    standings = standings_data.get('classification', [])
    if not standings:
        st.info("데이터가 없습니다.")
        return

    leader_points = standings[0].get('points', 0)

    # 2023년 이상 + MotoGP만 SPR/RAC
    show_spr_rac = (year >= 2023) and ("MotoGP" in category_name)

    # 이벤트/세션 가져오기
    events = fetch_events(season_id)
    if not events:
        st.warning("데이터를 가져오지 못했습니다.")
        return

    all_sessions = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(fetch_sessions, e['id'], category_id): e for e in events}
        for fut in as_completed(futures):
            sessions = fut.result()
            all_sessions.extend(sessions)

    # SPR/RAC 포인트 + 팀 컬러 계산
    rider_map = {}
    if show_spr_rac:
        rider_map = calculate_points_and_team_colors(all_sessions)

    # 디버깅: rider_map 확인
    # st.write("Rider Map:", rider_map)  # 필요 시 활성화

    # 테이블 구성
    table_data = []
    for rider_data in standings:
        rider = rider_data['rider']
        full_name = get_rider_name(rider)
        position = rider_data.get('position', 'N/A')
        total_points = rider_data.get('points', 0)
        rider_id = rider.get('id', 0)

        # Def. Gap 계산: 1위는 공란, 그 외는 마이너스 부호 추가
        if position == 1:
            def_gap = ""
        else:
            gap = leader_points - total_points
            def_gap = f"-{gap}" if gap > 0 else "0"

        # (1) team 정보 안전 처리 (team이 None이면 {})
        team_data = rider_data.get('team') or {}
        fallback_color = team_data.get('color', '')

        # (2) rider_map에서 SPR/RAC 포인트 및 team_color 가져오기
        spr_points = rider_map.get(rider_id, {}).get('SPR', 0) if show_spr_rac else "-"
        rac_points = rider_map.get(rider_id, {}).get('RAC', 0) if show_spr_rac else "-"
        team_color = rider_map.get(rider_id, {}).get('team_color', fallback_color)

        # (3) rider number - None일 경우 공란으로 처리
        rider_number = rider.get('number')
        if rider_number is None or str(rider_number).strip().lower() == 'none':
            rider_number = ""
        else:
            rider_number = str(rider_number)

        # (4) 팀 컬러 설정
        final_color = team_color

        # 국기
        country_info = rider.get('country', {})
        country_name = country_info.get('name', '')
        country_iso = country_info.get('iso', '')
        flag_url = get_country_flag_url(country_iso, country_name)

        team_name = team_data.get('name', '')
        constructor_name = rider_data.get('constructor', {}).get('name', 'N/A')

        # rider # 컬러 배경 (final_color가 비어 있으면 공란으로 처리)
        rider_num_html = (
            f"<div style='background-color:{final_color}; "
            f"padding:5px; text-align:center; color:white; "
            f"border-radius:4px;'>{rider_number}</div>"
            if final_color else f"{rider_number}"
        )

        # 데이터가 없으면 모든 컬럼을 공란으로 표시
        if rider_number == "" and full_name == "" and team_name == "" and constructor_name == "":
            row = {
                'P': "",
                'Rider': "",
                '#': "",
                'Points': "",
                'Def.': "",
                'RAC': "",
                'SPR': "",
                'Country': "",
                'Team': "",
                'Bike': ""
            }
        else:
            row = {
                'P': position,
                'Rider': full_name,
                '#': rider_num_html,
                'Points': total_points,
                'Def.': def_gap,  # 수정된 Def. Gap
                'RAC': rac_points,
                'SPR': spr_points,
                'Country': f"<img src='{flag_url}' width='30'>",
                'Team': team_name,
                'Bike': constructor_name
            }
        table_data.append(row)

    # DataFrame 생성
    df = pd.DataFrame(table_data)

    # 컬럼 순서 지정
    columns_order = [
        'P', 'Rider', '#', 'Points', 
        'Def.', 'RAC', 'SPR',
        'Country', 'Team', 'Bike'
    ]
    if not show_spr_rac:
        columns_order.remove('RAC')
        columns_order.remove('SPR')

    existing_cols = [c for c in columns_order if c in df.columns]
    df = df.reindex(columns=existing_cols)

    # 모든 None 값을 공란으로 대체
    df = df.replace({None: ""})

    # 테이블 HTML로 변환 및 렌더링
    st.markdown(df.to_html(escape=False, index=False), unsafe_allow_html=True)

# --------------------------------------------------------------------------------
# 메인 로직
# --------------------------------------------------------------------------------

seasons = fetch_seasons()
if not seasons:
    st.stop()

# 2024년 이하만 필터 (예시)
seasons = [s for s in seasons if s.get('year', 0) <= 2024]
seasons = sorted(seasons, key=lambda x: x.get('year', 0), reverse=True)

# 시즌 선택에서 ID 숨기기
# Display list: 연도만 표시
display_seasons = [str(s['year']) for s in seasons if 'id' in s and 'year' in s]
# Data list: 실제 데이터
data_seasons = [s for s in seasons if 'id' in s and 'year' in s]

if not display_seasons:
    st.warning("표시할 시즌 정보가 없습니다.")
    st.stop()

selected_season_display = st.selectbox("Season", display_seasons, index=0)
selected_season_data = data_seasons[display_seasons.index(selected_season_display)]
selected_season_id = selected_season_data['id']
selected_season_year = selected_season_data['year']

categories = fetch_categories(selected_season_id)
if not categories:
    st.warning("해당 시즌에 카테고리가 없습니다.")
    st.stop()

# 기본 MotoGP 카테고리 인덱스 설정
motoGP_index = 0
for i, cat in enumerate(categories):
    if cat.get('name') in ["MotoGP", "500cc"]:
        motoGP_index = i
        break

# 카테고리 선택에서 ID 숨기기
# Display list: 카테고리 이름만 표시
display_categories = [c['name'] for c in categories if 'id' in c and 'name' in c]
# Data list: 실제 데이터
data_categories = [c for c in categories if 'id' in c and 'name' in c]

if not display_categories:
    st.warning("표시할 카테고리가 없습니다.")
    st.stop()

selected_category_display = st.selectbox("Category", display_categories, index=motoGP_index)
selected_category_data = data_categories[display_categories.index(selected_category_display)]
selected_category_id = selected_category_data['id']
selected_category_name = selected_category_data['name']

process_standings(selected_season_id, selected_category_id, selected_season_year, selected_category_name)
