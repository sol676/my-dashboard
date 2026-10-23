"""
네이버 브랜드스토어 / 스마트스토어 리뷰 수집기
 
방식: Playwright로 실제 브라우저를 띄워 상품 페이지의 리뷰 탭을 연 뒤,
      페이지가 내부적으로 호출하는 리뷰 API(reviews/query-pages) 요청을 가로채서
      같은 요청을 page 번호만 바꿔 반복 호출합니다.
      (merchantNo 같은 내부 ID를 직접 찾을 필요가 없고, 쿠키/헤더도 브라우저 것을 그대로 씀)
 
설치:
    pip install playwright
    playwright install chromium
 
실행 예:
    python naver_review_scraper.py                         # 스토어 전체 상품
    python naver_review_scraper.py --products 1234567890   # 특정 상품만
    python naver_review_scraper.py --max-pages 10 --sort latest
"""
import argparse, asyncio, json, os, random, re
from playwright.async_api import async_playwright
 
STORE_URL = "https://brand.naver.com/meditherapy"   # 스마트스토어면 https://smartstore.naver.com/<스토어명>
PRODUCT_IDS = [
    "13779177250",
    "13592208924",
    "12963768877",
]
 
# 대시보드 Hero SKU 이름 ↔ 상품명 키워드
# 왼쪽 이름은 hero_data.json / 자사몰·올리브영 데이터에 쓰인 이름과 글자 하나까지 같아야 함
# (순서 중요: '히알루론산스킨부스터…'가 '부스터'에 걸리지 않도록 레티날을 마지막에 둠)
HERO_MAP = [
    ("포쎄라리얼비피다핑크블러크림",   ["포쎄라", "핑크"]),
    ("히알루론산스킨부스터퍼스트세럼", ["히알루론산", "퍼스트"]),
    ("레티날스킨부스터세럼",          ["레티날", "부스터"]),
]
DASHBOARD_JSON = "reviews_data.json"   # 대시보드가 자동으로 읽는 파일명
 
REVIEW_API = "reviews/query-pages"
SORT_MAP = {"ranking": "REVIEW_RANKING", "latest": "REVIEW_CREATE_DATE_DESC",
            "high": "REVIEW_SCORE_DESC", "low": "REVIEW_SCORE_ASC"}
 
 
async def sleep(a=1.5, b=3.5):
    await asyncio.sleep(random.uniform(a, b))
 
 
async def collect_product_ids(page):
    """스토어 전체상품 목록에서 상품번호 수집"""
    ids, cp = [], 1
    while True:
        await page.goto(f"{STORE_URL}/category/ALL?cp={cp}", wait_until="domcontentloaded")
        await sleep()
        for _ in range(6):                      # 지연 로딩 대비 스크롤
            await page.mouse.wheel(0, 3000)
            await asyncio.sleep(0.5)
        hrefs = await page.eval_on_selector_all("a[href*='/products/']", "els => els.map(e => e.href)")
        found = [m.group(1) for h in hrefs if (m := re.search(r"/products/(\d+)", h))]
        new = [i for i in dict.fromkeys(found) if i not in ids]
        if not new:
            break
        ids += new
        print(f"  목록 {cp}페이지: +{len(new)}개 (누적 {len(ids)})")
        cp += 1
    return ids
 
 
async def fetch_reviews(page, product_id, max_pages, sort):
    captured, review_urls = {}, []
 
    def on_request(req):
        u = req.url
        if "review" in u.lower():
            review_urls.append(f"{req.method} {u[:110]}")
        if "body" in captured or req.method != "POST":
            return
        if REVIEW_API in u or ("review" in u.lower() and req.post_data and '"page"' in req.post_data):
            captured.update(url=u, body=req.post_data, headers=req.headers)
 
    page.on("request", on_request)
    title = ""
    for attempt in (1, 2):                      # 못 잡으면 새로고침해서 한 번 더 시도
        if attempt == 1:
            await page.goto(f"{STORE_URL}/products/{product_id}", wait_until="domcontentloaded")
        else:
            print(f"  [{product_id}] 리뷰 요청 재시도 (새로고침)")
            await sleep(5, 8)
            await page.reload(wait_until="domcontentloaded")
        await sleep()
        title = (await page.title()).split(":")[0].strip()
 
        # 리뷰 탭 클릭: 정확한 탭 → '리뷰' 글자 순서로 시도
        for sel in ("a[href='#REVIEW']", "a[href*='#REVIEW']", "a:has-text('리뷰')"):
            try:
                await page.locator(sel).first.click(timeout=3000)
                break
            except Exception:
                continue
 
        # 최대 20초 동안 조금씩 스크롤하며 리뷰 요청 대기
        for i in range(40):
            if "body" in captured:
                break
            if i % 4 == 0:
                await page.mouse.wheel(0, 1500)
            await asyncio.sleep(0.5)
        if "body" in captured:
            break
    page.remove_listener("request", on_request)
 
    if "body" not in captured:
        shot = f"fail_{product_id}.png"
        try:
            await page.screenshot(path=shot, full_page=False)
        except Exception:
            shot = "(스크린샷 실패)"
        print(f"  [{product_id}] 리뷰 요청을 못 잡음")
        print(f"     열린 주소: {page.url}")
        print(f"     페이지 제목: {title}")
        print(f"     화면 캡처: {shot}")
        print(f"     감지된 리뷰 관련 요청: {review_urls[-5:] or '없음'}")
        return []
 
    body = json.loads(captured["body"])
    if sort:
        body["reviewSearchSortType"] = SORT_MAP[sort]
    headers = {k: v for k, v in captured["headers"].items()
               if k.lower() in ("content-type", "accept", "x-client-version")}
 
    rows, pg = [], 1
    while pg <= max_pages:
        body["page"] = pg
        res = await page.evaluate(
            """async ({url, body, headers}) => {
                const r = await fetch(url, {method: 'POST', headers, body: JSON.stringify(body),
                                            credentials: 'include'});
                return {status: r.status, text: await r.text()};
            }""",
            {"url": captured["url"], "body": body, "headers": headers},
        )
        if res["status"] == 429:
            print("  429 (요청 과다) → 60초 대기 후 재시도")
            await asyncio.sleep(60)
            continue
        if res["status"] != 200:
            print(f"  HTTP {res['status']} → 중단")
            break
 
        data = json.loads(res["text"])
        contents = data.get("contents", [])
        if not contents:
            break
        for r in contents:
            rows.append({
                "product_id": product_id,
                "product_name": title,
                "review_id": r.get("id"),
                "score": r.get("reviewScore"),
                "date": r.get("createDate"),
                "writer": r.get("writerMemberId") or r.get("maskedWriterId"),
                "option": r.get("productOptionContent"),
                "content": (r.get("reviewContent") or "").replace("\n", " "),
                "photo_count": len(r.get("reviewAttaches") or []),
                "repurchase": r.get("repurchase"),
                "one_month_review": r.get("reviewType"),
            })
        total_pages = data.get("totalPages", pg)
        print(f"  [{title[:20]}] {pg}/{total_pages}페이지, 누적 {len(rows)}개")
        if pg >= total_pages:
            break
        pg += 1
        await sleep(1.0, 2.5)
    return rows
 
 
async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--products", nargs="*", help="상품번호 지정 (없으면 스토어 전체)")
    ap.add_argument("--max-pages", type=int, default=50, help="상품당 최대 리뷰 페이지 수")
    ap.add_argument("--sort", choices=SORT_MAP.keys(), default="latest")
    ap.add_argument("--dashboard-dir", default=".", help="대시보드 HTML이 있는 폴더 (reviews_data.json 저장 위치)")
    ap.add_argument("--window", choices=["min", "offscreen", "show"], default="min",
                    help="min: 최소화(기본) / offscreen: 화면 밖에 띄움 / show: 창 보이기")
    ap.add_argument("--headless", action="store_true", help="창 없이 실행 (차단 확률 ↑)")
    args = ap.parse_args()
 
    async with async_playwright() as p:
        # 창이 가려져 있어도 페이지가 멈추지 않도록 백그라운드 절전 기능 끄기
        launch_args = [
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
        ]
        if args.window == "offscreen":
            launch_args.append("--window-position=-3000,-3000")
        browser = await p.chromium.launch(headless=args.headless, args=launch_args)
        ctx = await browser.new_context(locale="ko-KR", viewport={"width": 1400, "height": 900})
        page = await ctx.new_page()
 
        if args.window == "min" and not args.headless:
            try:
                cdp = await ctx.new_cdp_session(page)
                win = await cdp.send("Browser.getWindowForTarget")
                await cdp.send("Browser.setWindowBounds",
                               {"windowId": win["windowId"], "bounds": {"windowState": "minimized"}})
            except Exception as e:
                print(f"창 최소화 실패 (그냥 진행): {e}")
 
        ids = args.products or PRODUCT_IDS or await collect_product_ids(page)
        print(f"상품 {len(ids)}개 수집 시작")
 
        all_rows = []
        for pid in ids:
            all_rows += await fetch_reviews(page, pid, args.max_pages, args.sort)
            await sleep(3, 6)
        await browser.close()
 
    save_for_dashboard(all_rows, args.dashboard_dir)
 
 
def match_hero(name):
    for hero, keys in HERO_MAP:
        if any(k in (name or "") for k in keys):
            return hero
    return None
 
 
def save_for_dashboard(rows, dashboard_dir):
    """수집 결과를 대시보드 형식으로 변환해 reviews_data.json에만 저장"""
    dash = []
    for r in rows:
        hero = match_hero(r["product_name"])
        if not hero:
            continue                                   # Hero SKU가 아닌 상품은 제외
        author = r["writer"] or "네이버고객"
        dash.append({
            "channel": "naver",
            "product": hero,
            "rating": int(r["score"] or 5),
            "date": str(r["date"] or "")[:10].replace(".", "-"),
            "author": author[:3] + "***" if len(author) > 3 else "네이버고객",
            "text": r["content"] or "",
        })
    skipped = len(rows) - len(dash)
 
    # reviews_data.json: 기존 파일(올리브영·자사몰 리뷰 포함)과 합치고 중복 제거
    json_path = os.path.join(dashboard_dir, DASHBOARD_JSON)
    existing = []
    if os.path.exists(json_path):
        with open(json_path, encoding="utf-8") as f:
            existing = json.load(f)
    key = lambda d: f"{d.get('date')}_{d.get('author')}_{(d.get('text') or '')[:15]}"
    # 기존 데이터의 상품명도 HERO_MAP 이름으로 통일 (예전 이름으로 저장된 네이버 리뷰 정리)
    for d in existing:
        d["product"] = match_hero(d.get("product")) or d.get("product")
    seen, merged = set(), []
    for d in dash + existing:
        if key(d) not in seen:
            seen.add(key(d))
            merged.append(d)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=1)
 
    print(f"완료: Hero SKU 리뷰 {len(dash)}개 (그 외 상품 {skipped}개 제외)")
    print(f"  → 대시보드 데이터: {json_path} (기존 포함 총 {len(merged)}개)")
 
 
if __name__ == "__main__":
    asyncio.run(main())
 