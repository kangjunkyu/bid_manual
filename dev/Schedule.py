import requests
import smtplib
import sqlite3
import sys
import os
import json

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta

# from dotenv import load_dotenv
# load_dotenv()

NAVER_ID = os.getenv("NAVER_ID")
NAVER_PW = os.getenv("NAVER_PW")
API_KEY = os.getenv("API_KEY")

def load_config():
    with open('dev/config.json', 'r', encoding='utf-8') as f:
        return json.load(f)
    
config = load_config()

KEYWORDS = config['KEYWORDS']
NEGATIVE_KEYWORDS = config['NEGATIVE_KEYWORDS']
EXCLUDE_CONTRACTS = config['EXCLUDE_CONTRACTS']

ENDPOINTS = {
    "물품": "/getBidPblancListInfoThngPPSSrch",
    "용역": "/getBidPblancListInfoServcPPSSrch",
    "공사": "/getBidPblancListInfoCnstwkPPSSrch",
    "외자": "/getBidPblancListInfoFrgcptPPSSrch",
    "기타": "/getBidPblancListInfoEtcPPSSrch",
}

BASE_URL = "http://apis.data.go.kr/1230000/ad/BidPublicInfoService"
BIZINFO_URL = (
    "http://apis.data.go.kr/1721000/msitannouncementinfo/businessAnnouncMentList"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
}

    
def get_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    
    return session

def init_db():
    conn = sqlite3.connect("sent_list.db")
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS sent_bids (
            bid_id TEXT PRIMARY KEY,
            found_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            is_sent INTEGER DEFAULT 0
        )
    """
    )
    conn.commit()
    conn.close()


def is_already_stored(bid_id):
    conn = sqlite3.connect("sent_list.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM sent_bids WHERE bid_id = ?", (bid_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None


def save_bid_to_db(bid_id):
    conn = sqlite3.connect("sent_list.db")
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO sent_bids (bid_id, is_sent) VALUES (?, 0)", (bid_id,)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()


def mask_as_sent():
    conn = sqlite3.connect("sent_list.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE sent_bids SET is_sent = 1 WHERE is_sent = 0")
    conn.commit()
    conn.close()


def get_combined_data():
    all_items = []
    session = get_session()
    now = datetime.now()
    yesterday = now - timedelta(days=1)
    n = now.strftime("%Y%m%d%H%M")
    y = yesterday.strftime("%Y%m%d%H%M")

    for category, path in ENDPOINTS.items():
        page_no = 1
        while True:
            params = {
                "serviceKey": API_KEY,
                "numOfRows": "999",
                "inqryDiv": "1",
                "pageNo": str(page_no),
                "inqryBgnDt": y,
                "inqryEndDt": n,
                "type": "json",
            }

            try:
                response = session.get(BASE_URL + path, params=params, timeout=30)
                if response.status_code == 200:
                    data = response.json()
                    body = data.get("response", {}).get("body", {})
                    items = body.get("items", [])
                    total_count = int(body.get("totalCount", 0))  #

                    if not items:
                        break

                    if isinstance(items, dict):
                        items = [items]

                    for i in items:
                        i["category"] = category
                        all_items.append(i)

                    if (
                        len([x for x in all_items if x["category"] == category])
                        >= total_count
                    ):
                        break

                    page_no += 1

                    if page_no > 10:
                        break
                else:
                    print(f"API 응답 에러 ({response.status_code})")
                    break
            except Exception as e:
                print(f"{category} 에러 발생: {e}")
                break

    return all_items


def get_bizinfo_data():
    all_items = []
    params = {
        "serviceKey": API_KEY,
        "pageNo": "1",
        "numOfRows": "999",
        "returnType": "json",
    }
    session = get_session()

    try:
        response = session.get(BIZINFO_URL, params=params, timeout=30)
        if response.status_code == 200:
            data = response.json()
            resp_list = data.get("response", [])

            if len(resp_list) >= 2:
                body = resp_list[1].get("body", {})
                items_wrapper = body.get("items", [])

                for wrapper in items_wrapper:
                    i = wrapper.get("item", {})
                    if not i:
                        continue

                    all_items.append(
                        {
                            "category": "과기부",
                            "bidNtceNm": i.get("subject"),
                            "ntceInsttNm": i.get("deptName"),
                            "bidNtceDtlUrl": i.get("viewUrl"),
                            "bidClseDt": i.get("pressDt"),
                            "bidNtceNo": (
                                i.get("viewUrl").split("nttSeqNo=")[-1]
                                if "nttSeqNo=" in i.get("viewUrl", "")
                                else "MSIT"
                            ),
                            "bidNtceOrd": "00",
                        }
                    )
    except Exception as e:
        print(f"기업마당 에러 발생: {e}")
    return all_items

def send_naver_email(content_html):
    global NAVER_ID, NAVER_PW

    msg = MIMEMultipart()
    msg["Subject"] = (
        f"[{datetime.now().strftime('%Y-%m-%d')}] 나라장터 & 정부사업 신규 공고 알림"
    )

    msg["From"] = f"{NAVER_ID}@naver.com"

    to_list = ["rootforyou@mcloudoc.com"]
    cc_list = ["hgchoi@mcloudoc.com", "mjhwang@mcloudoc.com", "wnsrb933@mcloudoc.com", "kaspi0402@mcloudoc.com"]

    # TEST
    # to_list = ["wnsrb933@mcloudoc.com"]
    # cc_list = ["wnsrb933@naver.com"]

    msg["To"] = ", ".join(to_list)
    msg["Cc"] = ", ".join(cc_list)

    all_recipients = to_list + cc_list

    msg.attach(MIMEText(content_html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.naver.com", 465) as server:
            server.login(NAVER_ID, NAVER_PW)
            server.send_message(msg, from_addr=msg["From"], to_addrs=all_recipients)
        print(f"메일 발송 성공! (수신: {len(to_list)}명, 참조: {len(cc_list)}명)")
    except Exception as e:
        print(f"메일 발송 실패: {e}")


def main():
    init_db()
    is_mail_time = len(sys.argv) > 1 and sys.argv[1] == "send"

    nara_raw = get_combined_data()
    biz_raw = get_bizinfo_data()

    categorized_results = {kw: {"nara": [], "gov": []} for kw in KEYWORDS}
    categorized_results["기타 지원사업"] = {"nara": [], "gov": []}

    for item in nara_raw:
        bid_nm = item.get("bidNtceNm", "")
        bid_id = f"{item.get('bidNtceNo')}-{item.get('bidNtceOrd', '00')}"

        if any(nk in bid_nm for nk in NEGATIVE_KEYWORDS):
            continue

        matched_kw = None
        for kw in KEYWORDS:
            if kw in bid_nm:
                matched_kw = kw
                break

        if matched_kw:
            if not is_already_stored(bid_id):
                save_bid_to_db(bid_id)

            if is_not_yet_sent(bid_id):
                categorized_results[matched_kw]["nara"].append(
                    {
                        "category": item.get("category", "입찰"),
                        "title": bid_nm,
                        "org": item.get("ntceInsttNm"),
                        "url": item.get("bidNtceDtlUrl"),
                        "end": item.get("bidClseDt"),
                    }
                )

    for item in biz_raw:
        bid_nm = item.get("bidNtceNm") or item.get("pblancNm", "제목 없음")
        bid_no = item.get("bidNtceNo") or item.get("pblancId")
        bid_id = f"{bid_no}-00"

        if any(nk in bid_nm for nk in NEGATIVE_KEYWORDS):
            continue

        matched_kw = "기타 지원사업"
        for kw in KEYWORDS:
            if kw in bid_nm:
                matched_kw = kw
                break

        if not is_already_stored(bid_id):
            save_bid_to_db(bid_id)

        if is_not_yet_sent(bid_id):
            categorized_results[matched_kw]["gov"].append(
                {
                    "category": "지원사업",
                    "title": bid_nm,
                    "org": item.get("ntceInsttNm") or item.get("excutInsttNm"),
                    "url": item.get("bidNtceDtlUrl")
                    or (
                        "https://www.bizinfo.go.kr/saw/saw0101V.do?pblancId="
                        + item.get("pblancId", "")
                    ),
                    "end": item.get("bidClseDt") or item.get("reqstEndDt"),
                }
            )

    has_content = any(src["nara"] or src["gov"] for src in categorized_results.values())

    if is_mail_time:
        if has_content:
            print(f"[{datetime.now()}] 메일을 발송합니다.")
            send_email(categorized_results)
            mask_as_sent()
        else:
            print("발송할 신규 공고가 없습니다.")
    else:
        print(f"[{datetime.now()}] 데이터 수집 완료 (DB 저장)")


def is_not_yet_sent(bid_id):
    conn = sqlite3.connect("sent_list.db")
    cur = conn.cursor()
    cur.execute("SELECT is_sent FROM sent_bids WHERE bid_id = ?", (bid_id,))
    row = cur.fetchone()
    conn.close()
    return row and row[0] == 0


def send_email(categorized_results):
    today_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    MINT_COLOR = "#29C1C1"
    DEEP_BLUE_COLOR = "#004BA0"

    html = f"""
    <div style="width: 100%; background-color: #f4f4f4; padding: 20px 0; font-family: 'Malgun Gothic', sans-serif;">
        <table align="center" border="0" cellpadding="0" cellspacing="0" width="620" style="background-color: #ffffff; border: 1px solid #dddddd;">
            <tr>
                <td style="padding: 35px 20px; background-color: {MINT_COLOR}; text-align: center;">
                    <h1 style="margin: 0; color: #ffffff; font-size: 24px; letter-spacing: -1px;">📅 통합 키워드 공고 리포트</h1>
                    <p style="margin: 10px 0 0 0; color: #ffffff; font-size: 13px; opacity: 0.8;">{today_str} 기준 업데이트</p>
                </td>
            </tr>
            <tr><td style="padding: 10px 20px;">
    """

    for kw, sources in categorized_results.items():
        if not sources["nara"] and not sources["gov"]:
            continue

        if kw == "기타 지원사업":
            header_bg = "#f0f4fa"
            header_text = DEEP_BLUE_COLOR
            header_border = DEEP_BLUE_COLOR
        else:
            header_bg = "#f0fafa"
            header_text = MINT_COLOR
            header_border = MINT_COLOR

        html += f"""
            <div style="margin-top: 35px; padding: 10px 15px; background-color: {header_bg}; color: {header_text}; border-left: 5px solid {header_border}; font-weight: bold; font-size: 19px;">
                # {kw}
            </div>
        """

        for b in sources["nara"]:
            html += f"""
            <table width="100%" style="border: 1px solid #d1eded; border-left: 5px solid {MINT_COLOR}; margin-top: 12px; background-color: #F0FAFA;">
                <tr><td style="padding: 18px;">
                    <div style="font-size: 11px; color: #209a9a; font-weight: bold; text-transform: uppercase;">[NARA] {b['org']}</div>
                    <div style="font-size: 16px; color: #333333; font-weight: bold; margin: 6px 0; line-height: 1.4;">{b['title']}</div>
                    <div style="font-size: 13px; color: #e64a19; margin-top: 8px;">마감일: <strong>{b['end']}</strong> 
                        <span style="float:right;"><a href="{b['url']}" style="background-color: {MINT_COLOR}; color: #ffffff; padding: 5px 12px; text-decoration: none; font-size: 12px; border-radius: 3px; font-weight:bold;">상세보기</a></span>
                    </div>
                </td></tr>
            </table>
            """

        for b in sources["gov"]:
            html += f"""
            <table width="100%" style="border: 1px solid #cfdcf0; border-left: 5px solid {DEEP_BLUE_COLOR}; margin-top: 12px; background-color: #F0F4FA;">
                <tr><td style="padding: 18px;">
                    <div style="font-size: 11px; color: {DEEP_BLUE_COLOR}; font-weight: bold; text-transform: uppercase;">[GOV] {b['org']}</div>
                    <div style="font-size: 16px; color: #333333; font-weight: bold; margin: 6px 0; line-height: 1.4;">{b['title']}</div>
                    <div style="font-size: 13px; color: {DEEP_BLUE_COLOR}; margin-top: 8px;">기한: <strong>{b['end']}</strong> 
                        <span style="float:right;"><a href="{b['url']}" style="background-color: {DEEP_BLUE_COLOR}; color: #ffffff; padding: 5px 12px; text-decoration: none; font-size: 12px; border-radius: 3px; font-weight:bold;">상세보기</a></span>
                    </div>
                </td></tr>
            </table>
            """

    html += """
                </td>
            </tr>
            <tr><td style="padding: 30px 20px; text-align: center; font-size: 12px; color: #999; line-height: 1.6;">
                본 리포트는 설정된 키워드에 따라 시스템에서 자동 생성되었습니다.<br/>
                © 엠클라우독 영업팀
            </td></tr>
        </table>
    </div>
    """
    send_naver_email(html)


if __name__ == "__main__":
    main()
