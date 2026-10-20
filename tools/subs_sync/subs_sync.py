#!/usr/bin/env python3
"""땅콩집 플래너 청약 공고 동기화.

공공데이터포털의 청약홈 분양정보 API에서 서울 APT 공고를 가져와 조건에 맞게 걸러 내고,
크롬으로 땅콩집 플래너 앱을 열어 "공고 가져오기" 칸에 넣어 줍니다.

사용법은 README.md 를 보세요.
  python subs_sync.py login          # 크롬을 열어 claude.ai 에 한 번 로그인
  python subs_sync.py fetch          # 공고만 가져와서 JSON 파일로 저장 (앱에는 넣지 않음)
  python subs_sync.py run            # 가져와서 앱에 넣기
  python subs_sync.py import FILE    # 저장해 둔 JSON 파일을 앱에 넣기
"""
import argparse
import datetime as dt
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

API_BASE = "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1"
DEFAULT_ARTIFACT = "https://claude.ai/artifact/PEcENbrXxFuvdH9mK96fTK"
PRIORITY = ["영등포", "마포", "강서", "양천", "동작"]
HERE = Path(__file__).resolve().parent
DEFAULT_PROFILE = HERE / ".chrome-profile"
OUT_DIR = HERE / "out"


# ---------------------------------------------------------------- API ----
def api_get(endpoint, key, params, page, per_page=200):
    q = {"page": page, "perPage": per_page, "serviceKey": key}
    q.update(params)
    r = requests.get(f"{API_BASE}/{endpoint}", params=q, timeout=30)
    if r.status_code != 200:
        raise SystemExit(f"API 오류 {r.status_code}: {r.text[:300]}\n인증키(일반 인증키 Decoding)와 활용신청 승인 여부를 확인하세요.")
    return r.json()


def fetch_all(endpoint, key, params):
    rows, page = [], 1
    while True:
        data = api_get(endpoint, key, params, page)
        got = data.get("data") or []
        rows.extend(got)
        total = data.get("matchCount", data.get("totalCount", len(rows)))
        if not got or len(rows) >= total:
            return rows
        page += 1


def in_regions(d, regions):
    if not regions:
        return True
    addr = (d.get("HSSPLY_ADRES") or "").strip()
    region = (d.get("SUBSCRPT_AREA_CODE_NM") or "").strip()
    return any(region.startswith(r) or addr.startswith(r) for r in regions)


def fetch_models(key, details):
    """공고마다 주택형(면적·세대수·분양가)을 따로 조회한다."""
    models = {}
    for d in details:
        hm = str(d.get("HOUSE_MANAGE_NO") or "")
        if not hm:
            continue
        pb = str(d.get("PBLANC_NO") or hm)
        rows = []
        for params in ({"cond[HOUSE_MANAGE_NO::EQ]": hm, "cond[PBLANC_NO::EQ]": pb}, {"cond[HOUSE_MANAGE_NO::EQ]": hm}):
            try:
                rows = fetch_all("getAPTLttotPblancMdl", key, params)
            except SystemExit as e:
                print(f"  주택형 조회 오류({hm}): {e}")
                rows = []
            if rows:
                break
        print(f"  {d.get('HOUSE_NM', hm)}: 주택형 {len(rows)}건")
        models[hm] = rows
    return models


def fetch_notices(key, today, horizon_days, regions):
    details = fetch_all("getAPTLttotPblancDetail", key, {"cond[RCEPT_ENDDE::GTE]": today.isoformat()})
    limit = (today + dt.timedelta(days=horizon_days)).isoformat()
    details = [d for d in details if (not d.get("RCEPT_BGNDE") or d["RCEPT_BGNDE"] <= limit) and in_regions(d, regions)]
    return details, fetch_models(key, details)


# ------------------------------------------------------------ transform ----
def to_int(v):
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def parse_area(house_ty):
    m = re.match(r"\s*0*(\d+(?:\.\d+)?)", str(house_ty or ""))
    return float(m.group(1)) if m else None


def won(man):
    """만원 단위 숫자를 '7.2억'처럼."""
    if not man:
        return ""
    return f"{man / 10000:.1f}억".replace(".0억", "억")


def build_item(d, models, area_min, area_max, regions):
    name = (d.get("HOUSE_NM") or "").strip()
    addr = (d.get("HSSPLY_ADRES") or "").strip()
    region = (d.get("SUBSCRPT_AREA_CODE_NM") or "").strip()
    if regions and not any(region.startswith(r) or addr.startswith(r) for r in regions):
        return None
    types, nwwds, nwbb, frst = [], 0, 0, 0
    for m in models:
        area = parse_area(m.get("HOUSE_TY"))
        nwwds += to_int(m.get("NWWDS_HSHLDCO"))
        nwbb += to_int(m.get("NWBB_HSHLDCO"))
        frst += to_int(m.get("LFE_FRST_HSHLDCO"))
        types.append({
            "area": area,
            "units": to_int(m.get("SUPLY_HSHLDCO")),
            "nwwds": to_int(m.get("NWWDS_HSHLDCO")),
            "price": to_int(m.get("LTTOT_TOP_AMOUNT")),
        })
    fits_area = [t for t in types if t["area"] is not None and area_min <= t["area"] <= area_max]
    priority = any(p in addr for p in PRIORITY)
    area_fit = "" if not types else ("yes" if fits_area else "no")
    region_fit = "yes" if priority else "no"
    nw_ok = any(t["nwwds"] > 0 for t in fits_area)
    good = priority and bool(fits_area) and nw_ok
    fit = "good" if good else "check"
    bits = ["관심 지역" if priority else "관심 지역 아님",
            "희망 평형 있음" if area_fit == "yes" else ("희망 평형 없음" if area_fit == "no" else "평형 정보 없음")]
    if good:
        bits.append("신혼특공 물량 있음")
    reason = " · ".join(bits) + " (소득·자격은 공고문 확인)"
    groups = {}
    for t in types:
        if t["area"] is None:
            continue
        g = groups.setdefault(round(t["area"]), {"a": round(t["area"]), "u": 0, "n": 0, "b": 0, "pmin": 0, "pmax": 0})
        g["u"] += t["units"]; g["n"] += t["nwwds"]
        if t["price"]:
            g["pmin"] = t["price"] if not g["pmin"] else min(g["pmin"], t["price"])
            g["pmax"] = max(g["pmax"], t["price"])
    for m in models:
        a = parse_area(m.get("HOUSE_TY"))
        if a is not None and round(a) in groups:
            groups[round(a)]["b"] += to_int(m.get("NWBB_HSHLDCO"))
    type_groups = [groups[k] for k in sorted(groups)]
    notes = []
    if "토지임대" in name:
        notes.append("토지임대부: 건물만 분양받고 토지는 임대료를 내는 방식이에요. 매달 토지 임대료가 따로 붙어요")
    if str(d.get("SPECLT_RDN_EARTH_AT", "")).upper() == "Y":
        notes.append("투기과열지구: 전매제한·재당첨 제한·대출 규제가 강해요 (공고문 확인)")
    if str(d.get("PUBLIC_HOUSE_SPCLW_APPLC_AT", "")).upper() == "Y":
        notes.append("공공택지 공급: 소득·자산 기준이 공고마다 따로 있어요")
    mv = str(d.get("MVN_PREARNGE_YM") or "")
    move_in = f"{mv[:4]}-{mv[4:6]}" if re.fullmatch(r"\d{6}", mv) else ""
    supply = " · ".join(x for x in [
        f"총 {to_int(d.get('TOT_SUPLY_HSHLDCO'))}세대" if d.get("TOT_SUPLY_HSHLDCO") else "",
        f"신혼 {nwwds}" if nwwds else "", f"신생아 {nwbb}" if nwbb else "", f"생애최초 {frst}" if frst else ""] if x)
    shown = types
    areas = " / ".join(
        f"{t['area']:g}㎡ {t['units']}세대" + (f" {won(t['price'])}" if t["price"] else "")
        for t in shown[:10] if t["area"] is not None)
    prices = [t["price"] for t in shown if t["price"]]
    price = (won(min(prices)) if min(prices) == max(prices) else f"{won(min(prices))}~{won(max(prices))}") if prices else ""
    hm, pb = str(d.get("HOUSE_MANAGE_NO", "")), str(d.get("PBLANC_NO", ""))
    no = re.sub(r"[^A-Za-z0-9_-]", "", hm if hm == pb or not pb else f"{hm}_{pb}")
    link = d.get("PBLANC_URL") or ""
    return {
        "no": no, "name": name, "location": addr or region, "supply": supply, "areas": areas, "price": price,
        "applyStart": d.get("RCEPT_BGNDE") or "", "applyEnd": d.get("RCEPT_ENDDE") or "",
        "winnerDate": d.get("PRZWNER_PRESNATN_DE") or "",
        "link": link if link.startswith("https://") else "",
        "fit": fit, "fitReason": reason, "areaFit": area_fit, "regionFit": region_fit, "source": "청약홈 API",
        "types": type_groups, "moveIn": move_in, "notes": notes,
    }


def build_items(details, models, area_min, area_max, regions):
    items = []
    for d in details:
        it = build_item(d, models.get(str(d.get("HOUSE_MANAGE_NO")), []), area_min, area_max, regions)
        if it and it["name"]:
            items.append(it)
    def rank(x):
        return -((x["fit"] == "good") * 4 + (x["regionFit"] == "yes") * 2 + (x["areaFit"] == "yes"))
    items.sort(key=lambda x: (rank(x), x["applyEnd"] or "9999"))
    return items


# ------------------------------------------------------------- browser ----
def find_chrome():
    cands = {
        "Darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
        "Windows": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"],
    }.get(platform.system(), ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"])
    for c in cands:
        if os.path.exists(c):
            return c
    return None


def cmd_login(args):
    chrome = args.chrome or find_chrome()
    if not chrome:
        raise SystemExit("크롬 위치를 못 찾았어요. --chrome 경로를 알려 주세요.")
    Path(args.profile).mkdir(parents=True, exist_ok=True)
    subprocess.Popen([chrome, f"--user-data-dir={args.profile}", "https://claude.ai"])
    print("크롬이 열렸어요. claude.ai 에 평소 쓰는 계정으로 로그인한 뒤,\n"
          f"앱 주소({args.url})가 열리는지 확인하고 크롬 창을 모두 닫으세요.")


def wait_for_app_frame(page, timeout):
    end = time.time() + timeout
    while time.time() < end:
        for fr in page.frames:
            try:
                if fr.query_selector("#subsImportBox"):
                    return fr
            except Exception:
                pass
        time.sleep(1)
    hint = "로그인이 풀린 것 같아요. `login` 을 다시 실행해 주세요." if "login" in page.url else "앱 화면을 찾지 못했어요."
    raise SystemExit(f"{hint} (현재 주소: {page.url})")


def import_to_app(url, items, profile, headless=False, chrome_path=None, init_script=None, timeout=60):
    from playwright.sync_api import sync_playwright
    payload = json.dumps(items, ensure_ascii=False)
    with sync_playwright() as p:
        kw = dict(user_data_dir=str(profile), headless=headless, viewport={"width": 430, "height": 900},
                  args=["--disable-blink-features=AutomationControlled"], ignore_default_args=["--enable-automation"])
        if chrome_path:
            kw["executable_path"] = chrome_path
        else:
            kw["channel"] = "chrome"
        ctx = p.chromium.launch_persistent_context(**kw)
        try:
            if init_script:
                ctx.add_init_script(init_script)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded")
            fr = wait_for_app_frame(page, timeout)
            fr.evaluate("document.querySelector('[data-tab-go=\"subs\"]').click()")
            fr.evaluate("document.querySelector('#subsImportBox').open = true; document.querySelector('#subsImportMsg').textContent = ''")
            fr.fill("#subsImport", payload)
            fr.click("#subsImportBtn")
            fr.wait_for_function("document.querySelector('#subsImportMsg').textContent.length > 0", timeout=timeout * 1000)
            return fr.inner_text("#subsImportMsg")
        finally:
            ctx.close()


PLIST = Path.home() / "Library" / "LaunchAgents" / "com.ttangkong.subs.plist"


def cmd_schedule(args):
    """맥에서 매일 정해진 시각에 `run` 을 자동 실행하도록 등록한다."""
    if platform.system() != "Darwin":
        raise SystemExit("자동 실행 등록은 맥에서만 지원해요.")
    if not args.key:
        raise SystemExit("인증키가 없어요. 같은 터미널에서 export ODCLOUD_KEY=\"키\" 를 먼저 하세요.")
    here = Path(__file__).resolve().parent
    log = here / "out" / "schedule.log"
    log.parent.mkdir(exist_ok=True)
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.ttangkong.subs</string>
<key>ProgramArguments</key><array><string>{sys.executable}</string><string>{here / "subs_sync.py"}</string><string>run</string></array>
<key>WorkingDirectory</key><string>{here}</string>
<key>EnvironmentVariables</key><dict><key>ODCLOUD_KEY</key><string>{args.key}</string></dict>
<key>StartCalendarInterval</key><dict><key>Hour</key><integer>{args.hour}</integer><key>Minute</key><integer>{args.minute}</integer></dict>
<key>StandardOutPath</key><string>{log}</string>
<key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
"""
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(plist, encoding="utf-8")
    PLIST.chmod(0o600)
    os.system(f'launchctl unload "{PLIST}" 2>/dev/null; launchctl load "{PLIST}"')
    print(f"등록했어요. 매일 {args.hour:02d}:{args.minute:02d}에 자동 실행돼요(노트북이 켜져 있고 로그인된 상태일 때).")
    print(f"결과 기록: {log}")
    print("해제하려면: python3 subs_sync.py unschedule")


def cmd_unschedule(args):
    if PLIST.exists():
        os.system(f'launchctl unload "{PLIST}" 2>/dev/null')
        PLIST.unlink()
    print("자동 실행을 해제했어요.")


# ---------------------------------------------------------------- main ----
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["login", "fetch", "run", "import", "schedule", "unschedule"])
    ap.add_argument("file", nargs="?", help="import 할 JSON 파일")
    ap.add_argument("--key", default=os.environ.get("ODCLOUD_KEY"), help="공공데이터포털 일반 인증키(Decoding). 환경변수 ODCLOUD_KEY 도 가능")
    ap.add_argument("--url", default=os.environ.get("SUBS_ARTIFACT_URL", DEFAULT_ARTIFACT), help="땅콩집 플래너 주소")
    ap.add_argument("--area-min", type=float, default=40)
    ap.add_argument("--area-max", type=float, default=60)
    ap.add_argument("--regions", default="서울,경기", help="쉼표로 구분 (기본 서울,경기)")
    ap.add_argument("--horizon", type=int, default=60, help="앞으로 며칠 안에 접수가 시작되는 공고까지 볼지")
    ap.add_argument("--profile", default=str(DEFAULT_PROFILE), help="로그인 정보를 저장할 크롬 프로필 폴더")
    ap.add_argument("--chrome", default=os.environ.get("CHROME_PATH"), help="크롬 실행 파일 경로(비워 두면 설치된 크롬 사용)")
    ap.add_argument("--headless", action="store_true", help="창 없이 실행(로그인이 풀릴 수 있어요)")
    ap.add_argument("--hour", type=int, default=8, help="schedule: 자동 실행 시(0~23)")
    ap.add_argument("--minute", type=int, default=30, help="schedule: 자동 실행 분")
    ap.add_argument("--raw", action="store_true", help="API 응답의 첫 항목 필드를 출력(필드 이름이 안 맞을 때 확인용)")
    args = ap.parse_args()

    if args.command == "login":
        return cmd_login(args)
    if args.command == "schedule":
        return cmd_schedule(args)
    if args.command == "unschedule":
        return cmd_unschedule(args)
    if args.command == "import":
        if not args.file:
            raise SystemExit("import 할 JSON 파일을 알려 주세요.")
        items = json.loads(Path(args.file).read_text(encoding="utf-8"))
    else:
        if not args.key:
            raise SystemExit("인증키가 없어요. --key 또는 환경변수 ODCLOUD_KEY 를 설정하세요.")
        today = dt.date.today()
        regions = [r.strip() for r in args.regions.split(",") if r.strip()]
        details, models = fetch_notices(args.key, today, args.horizon, regions)
        if args.raw and details:
            print("상세 필드:", sorted(details[0].keys()))
            first = next((rows for rows in models.values() if rows), [])
            if first:
                print("주택형 필드:", sorted(first[0].keys()))
                print("주택형 예시:", json.dumps(first[0], ensure_ascii=False)[:900])
            else:
                print("주택형을 한 건도 못 읽었어요.")
        items = build_items(details, models, args.area_min, args.area_max, regions)
        OUT_DIR.mkdir(exist_ok=True)
        out = OUT_DIR / f"subs_{today.isoformat()}.json"
        out.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        if items and not any(models.values()):
            print("주택형(평형·분양가)을 못 읽었어요. `python3 subs_sync.py fetch --raw` 결과를 알려 주세요.")
        good = sum(1 for i in items if i["fit"] == "good")
        print(f"공고 {len(details)}건을 읽고 {len(items)}건을 추렸어요 (적합 가능성 {good}건). 저장: {out}")
        if args.command == "fetch":
            return
    if not items:
        print("넣을 공고가 없어요.")
        return
    msg = import_to_app(args.url, items, args.profile, headless=args.headless, chrome_path=args.chrome)
    print("앱 결과:", msg)


if __name__ == "__main__":
    sys.exit(main())
