import json
import os
import time
from playwright.sync_api import sync_playwright
 
# 대시보드가 읽는 JSON 파일 경로: 이 스크립트와 같은 폴더에 고정
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_FILE_PATH = os.path.join(BASE_DIR, "reviews_data.json")
 
# 상품당 최대로 불러올 페이지 수 (한 페이지 약 10~20건)
MAX_PAGES = 30
# 리뷰 응답이 안 오면 페이지를 새로 열어 다시 시도하는 횟수
MAX_ATTEMPTS = 2
 
SKU_LINKS = [
    {
        "product": "레티날스킨부스터세럼",
        "url": "https://www.oliveyoung.co.kr/store/goods/getGoodsDetail.do?goodsNo=A000000231062",
    },
    {
        "product": "히알루론산스킨부스터퍼스트세럼",
        "url": "https://www.oliveyoung.co.kr/store/goods/getGoodsDetail.do?goodsNo=A000000261626",
    },
]
 
 
def load_reviews_data():
    if os.path.exists(JSON_FILE_PATH):
        with open(JSON_FILE_PATH, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return []
    return []
 
 
def save_reviews_data(reviews):
    with open(JSON_FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(reviews, f, ensure_ascii=False, indent=2)
 
 
def to_review(item, product):
    """API 응답의 리뷰 1건을 대시보드 형식으로 변환"""
    profile = item.get("profileDto") or {}
    text = (item.get("content") or "").replace("\r\n", "\n").strip()
    return {
        "channel": "oliveyoung",
        "product": product,
        "rating": int(item.get("reviewScore") or 0),
        "date": (item.get("createdDateTime") or "")[:10].replace(".", "-"),
        "author": profile.get("memberNickname") or "올리브영고객",
        "text": text,
        "review_id": item.get("reviewId"),
    }
 
 
def scrape_oliveyoung():
    all_data = load_reviews_data()
    # 자사몰/스토어 데이터는 보존하고, 올리브영 데이터만 매번 새로 수집
    result = [r for r in all_data if isinstance(r, dict) and r.get("channel") != "oliveyoung"]
    # 수집 실패(0건) 시 되살릴 수 있도록 기존 올리브영 리뷰를 상품별로 보관
    old_oy = {}
    for r in all_data:
        if isinstance(r, dict) and r.get("channel") == "oliveyoung":
            old_oy.setdefault(r.get("product"), []).append(r)
    total_new = 0
 
    with sync_playwright() as p:
        # headless=True(창 없는 모드)는 올리브영이 차단하므로 창은 띄우되 화면 밖 + 최소화
        browser = p.chromium.launch(
            headless=False,
            args=[
                "--window-position=-32000,-32000",            # 화면 밖에서 시작
                "--disable-backgrounding-occluded-windows",  # 최소화돼도 로딩 계속
                "--disable-renderer-backgrounding",
                "--disable-background-timer-throttling",
            ],
        )
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            cdp = page.context.new_cdp_session(page)
            win_id = cdp.send("Browser.getWindowForTarget")["windowId"]
            cdp.send("Browser.setWindowBounds", {"windowId": win_id, "bounds": {"windowState": "minimized"}})
        except Exception as e:
            print(" -> 창 최소화 실패(수집에는 지장 없음):", e)
 
        st = {"product": None, "reviews": {}, "has_next": False, "next": {}, "req": None, "resp_count": 0}
 
        def on_response(resp):
            if "/review/api/v2/reviews/cursor" not in resp.url or st["product"] is None:
                return
            try:
                data = (resp.json() or {}).get("data") or {}
            except Exception:
                return
            st["resp_count"] += 1
            for item in data.get("goodsReviewList") or []:
                rid = item.get("reviewId")
                if rid and rid not in st["reviews"]:
                    st["reviews"][rid] = to_review(item, st["product"])
            st["has_next"] = bool(data.get("hasNext"))
            st["next"] = {
                "cursorid": data.get("nextCursorId"),
                "cursorscore": data.get("nextCursorScore"),
                "cursorcount": data.get("nextCursorCount"),
            }
            req = resp.request
            st["req"] = {"url": req.url, "method": req.method, "body": req.post_data,
                         "headers": {k: v for k, v in req.headers.items()
                                     if k.lower() in ("content-type", "accept") or k.lower().startswith("x-")}}
 
        page.on("response", on_response)
 
        def wait_for_response(start_count, timeout):
            """리뷰 API 응답이 새로 도착할 때까지 최대 timeout초 대기"""
            end = time.time() + timeout
            while time.time() < end:
                if st["resp_count"] > start_count:
                    page.wait_for_timeout(1000)  # 응답 처리 여유
                    return True
                page.wait_for_timeout(300)
            return False
 
        def load_first_page(url):
            """페이지 열기 → 리뷰 탭 → 최신순까지. 첫 페이지 리뷰 수를 반환"""
            st.update(reviews={}, has_next=False, next={}, req=None)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
 
            # 1. 리뷰 탭 클릭 → 리뷰 API 호출 시작
            c0 = st["resp_count"]
            for sel in ["#reviewInfo", "button:has-text('리뷰')", "a:has-text('리뷰')"]:
                try:
                    loc = page.locator(sel).first
                    if loc.is_visible(timeout=1500):
                        loc.click()
                        print(" -> 리뷰 탭 클릭 성공")
                        break
                except Exception:
                    pass
            if not wait_for_response(c0, 15):
                print(" -> 리뷰 응답 대기 시간 초과 (15초)")
 
            # 2. 최신순 정렬. 기본 정렬 첫 페이지는 클릭 '전에' 비우고, 응답이 안 오면 되살림
            backup = dict(st["reviews"])
            backup_next = (st["has_next"], dict(st["next"]))
            st["reviews"].clear()
            try:
                c1 = st["resp_count"]
                page.get_by_text("최신순").first.click(timeout=4000)
                print(" -> '최신순' 정렬 클릭")
                wait_for_response(c1, 10)
            except Exception:
                print(" -> '최신순' 클릭 실패, 기본 정렬로 진행")
            if not st["reviews"]:
                st["reviews"].update(backup)
                st["has_next"], st["next"] = backup_next
 
            print(f" -> 첫 페이지: {len(st['reviews'])}건 | 다음 페이지 있음: {st['has_next']}")
            return len(st["reviews"])
 
        def load_more_pages():
            pages = 1
            # 3. 스크롤로 다음 페이지 불러오기
            stall = 0
            while st["has_next"] and pages < MAX_PAGES and stall < 4:
                before = len(st["reviews"])
                for _ in range(3):
                    page.mouse.wheel(0, 1500)
                    page.wait_for_timeout(800)
                page.wait_for_timeout(1500)
                if len(st["reviews"]) > before:
                    pages += 1
                    stall = 0
                    print(f" -> [스크롤] 누적 {len(st['reviews'])}건")
                else:
                    stall += 1
 
            # 4. 스크롤로 안 늘어나면 API를 직접 다음 커서로 호출
            if not (st["has_next"] and st["req"] and st["req"]["body"]):
                return
            print(" -> 스크롤 로딩이 멈춰서 API 직접 호출로 전환")
            try:
                template = json.loads(st["req"]["body"])
            except Exception:
                template = None
            if not isinstance(template, dict):
                print(" -> 요청 형식을 읽지 못함:", str(st["req"]["body"])[:300])
                return
            while st["has_next"] and pages < MAX_PAGES:
                body = dict(template)
                matched = False
                for k in body:
                    for nk, nv in st["next"].items():
                        if k.lower().endswith(nk) and nv is not None:
                            body[k] = nv
                            matched = True
                if not matched:
                    print(" -> 커서 필드를 못 찾음. 요청 본문:", json.dumps(template, ensure_ascii=False)[:300])
                    return
                before = len(st["reviews"])
                page.evaluate(
                    """async ([url, method, headers, body]) => {
                        await fetch(url, {method, headers, body, credentials: 'include'});
                    }""",
                    [st["req"]["url"], st["req"]["method"], st["req"]["headers"], json.dumps(body)],
                )
                page.wait_for_timeout(2000)
                if len(st["reviews"]) == before:
                    print(" -> 더 이상 새 리뷰가 없음")
                    return
                pages += 1
                print(f" -> [API] 누적 {len(st['reviews'])}건")
 
        for sku in SKU_LINKS:
            print("\n==========================================")
            print(f"[{sku['product']}] 올리브영 리뷰 수집 시작...")
            print("==========================================")
            st["product"] = sku["product"]
            got = []
            try:
                for attempt in range(1, MAX_ATTEMPTS + 1):
                    if attempt > 1:
                        print(f" -> 리뷰가 안 들어와서 페이지를 새로 열어 재시도 ({attempt}/{MAX_ATTEMPTS})")
                    if load_first_page(sku["url"]) > 0:
                        break
                if st["reviews"]:
                    load_more_pages()
                got = list(st["reviews"].values())
            except Exception as e:
                print(f" -> 수집 오류: {e}")
                got = list(st["reviews"].values())
 
            if not got:
                kept = old_oy.get(sku["product"], [])
                print(f" -> ⚠️ 0건 수집됨. 기존 데이터 {len(kept)}건을 그대로 유지합니다.")
                result.extend(kept)
                continue
 
            result.extend(got)
            total_new += len(got)
            print(f" -> [{sku['product']}]: 총 {len(got)}건 수집 완료!")
            for r in got[:3]:
                print(f"    예) [{r['author']}] {r['date']} ★{r['rating']}: {r['text'][:30]}")
 
        st["product"] = None
        browser.close()
 
    save_reviews_data(result)
    print(f"\n🎉 올리브영 리뷰 총 {total_new}건 저장 완료!")
    print("저장 위치:", JSON_FILE_PATH)
 
 
if __name__ == "__main__":
    scrape_oliveyoung()
 