import requests
import json
from datetime import datetime
import time

# ==========================================
# 1. API 인증 정보
# ==========================================
APP_ID = "haGtc0rlR2YV5yTXYG7d1FA-64zS-56_RnnRUTWaZe8"
SECRET = "UCjU03blwlxFKOmRyTvnBtdF5szL5zgqf3rUZ78HZ94"

# ==========================================
# 2. 수집할 Hero SKU 목록 (파일 상단에 위치)
# ==========================================
TARGET_SKUS = {
    # 블러 세럼으로 의심되는 2300은 제외하고, 찾아내신 과거 코드들을 깔끔하게 정리
    "레티날스킨부스터세럼": [
        "2294", "2265", "2226", "2203", "2154", "2149", "2124", "2104",
        "1971", "1962", "1959", "1938", "1927", "1921", "1916", "1826", "1484"
    ],
    "포쎄라리얼비피다핑크블러크림": [
        "2298", "2292", "2290", "2261", "2254", "2241", "2231", "2228", "2208", "2206", "2202"
    ],
    "히알루론산스킨부스터퍼스트세럼": [
        "2227", "2151", "2126", "2118", "2054", "2048"
    ]
}

def get_access_token():
    token_url = "https://api.cre.ma/oauth/token"
    payload = {
        "grant_type": "client_credentials",
        "client_id": APP_ID,
        "client_secret": SECRET
    }
    try:
        response = requests.post(token_url, data=payload)
        response.raise_for_status()
        return response.json().get("access_token")
    except requests.exceptions.RequestException as e:
        print(f"❌ 토큰 발급 실패: {e}")
        return None

def fetch_crema_reviews(access_token):
    review_url = "https://api.cre.ma/v1/reviews" 
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    
    all_reviews = []
    
    # TARGET_SKUS에 등록된 모든 상품 코드들을 평탄화
    all_target_codes = []
    for code_list in TARGET_SKUS.values():
        all_target_codes.extend(code_list)

    print(f"🔍 총 {len(all_target_codes)}개 상품 코드로 개별 집중 수집을 시작합니다...")

    # 각 상품 코드별로 페이지를 끝까지 순회
    for p_code in all_target_codes:
        print(f"📥 상품코드 [{p_code}] 리뷰 스캔 중...")
        
        for page in range(1, 101):
            params = {
                "limit": 100,
                "page": page,
                "product_code": p_code  # 해당 상품 번호 리뷰만 집중 조회
            }
            
            try:
                response = requests.get(review_url, headers=headers, params=params)
                response.raise_for_status() 
                data = response.json()
                
                reviews_list = data if isinstance(data, list) else []
                if not reviews_list:
                    break
                    
                all_reviews.extend(reviews_list)
                
                if len(reviews_list) < 100:
                    break
                    
                time.sleep(0.2)  # API 요청 간격 조절
                
            except requests.exceptions.RequestException as e:
                print(f"❌ API 호출 실패 (상품코드 {p_code}, {page}페이지): {e}")
                break
                
    return all_reviews

# --- 작성자 마스킹 함수 ---
def mask_author(name):
    if not name:
        return "고객"
    name = str(name).strip()
    if len(name) <= 1:
        return name + "*"
    # 첫 글자 + 공백 + 나머지 글자수만큼 '*' 처리 (예: '금순봉' -> '금 **')
    return name[0] + " " + "*" * (len(name) - 1)


# --- 리뷰 정제 함수 ---
def process_reviews(api_response):
    formatted_reviews = []
    
    for item in api_response:
        p_code = str(item.get('product_code', ''))
        matched_product = None
        
        for hero_name, code_list in TARGET_SKUS.items():
            if p_code in code_list:
                matched_product = hero_name
                break
                
        if matched_product:
            review_data = {
                "channel": "own",
                "product": matched_product,
                "rating": int(item.get('score', 5)),
                "date": item.get('created_at', '')[:10],
                "author": mask_author(item.get('user_name', '고객')),
                "text": item.get('message', '')
            }
            formatted_reviews.append(review_data)
            
    return formatted_reviews

def save_to_json(data, filename='reviews_data.json'):
    # 작성일 기준 최신순 내림차순 정렬
    data.sort(key=lambda x: x.get('date', ''), reverse=True)
    
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\n🎉 성공! 총 {len(data)}건의 타겟 리뷰가 '{filename}'에 저장되었습니다.")
    print(f"업데이트 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

if __name__ == "__main__":
    print("=== 크리마 리뷰 자동 업데이트 시작 ===")
    token = get_access_token()
    
    if token:
        raw_data = fetch_crema_reviews(token)
        if raw_data:
            final_data = process_reviews(raw_data)
            save_to_json(final_data)