"""
Polymarket 套利监控机器人（纯只读，不需要钱包/私钥）

做了什么：
1. 从 Polymarket 公共 API 抓取 24 小时交易量最高的 N 个活跃事件（默认 20 个）。
2. 用订单簿里的「最低卖价」（也就是你现在真正能买到的价格）检查两类互斥组合：
   - 二元市场：买 YES + 买 NO 的价格之和 < 阈值
   - 多选一事件（比如「谁会赢得选举」）：把每个选项的 YES 都买一份，价格之和 < 阈值
   因为这些选项最终有且只有一个会赢（赔付 1 美元），总成本低于 1 就是套利空间。
3. 发现机会就推送到 Gmail 邮箱 / 飞书 / 钉钉 / Telegram（在 GitHub Secrets 里配置哪个就推哪个）。

只用 Python 自带的库，不需要安装任何依赖。
"""

import json
import os
import smtplib
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
USER_AGENT = "Mozilla/5.0 (polymarket-arb-monitor; read-only)"

TOP_N = int(os.getenv("TOP_N") or 20)
THRESHOLD = float(os.getenv("THRESHOLD") or 0.98)

FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "").strip()
DINGTALK_WEBHOOK = os.getenv("DINGTALK_WEBHOOK", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GMAIL_USER = os.getenv("GMAIL_USER", "").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
EMAIL_TO = os.getenv("EMAIL_TO", "").strip() or GMAIL_USER
TEST_ALERT = os.getenv("TEST_ALERT", "").strip().lower() == "true"


def http(method, url, body=None, retries=3):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode()
                return json.loads(text) if text else None
        except Exception as e:
            if attempt == retries - 1:
                raise
            print(f"  请求失败，{2 ** attempt} 秒后重试：{e}")
            time.sleep(2 ** attempt)


def load_list(value):
    if isinstance(value, list):
        return value
    try:
        return json.loads(value or "[]")
    except (TypeError, ValueError):
        return []


def is_tradable(m):
    return (
        m.get("active")
        and not m.get("closed")
        and m.get("enableOrderBook")
        and m.get("acceptingOrders")
        and len(load_list(m.get("clobTokenIds"))) == 2
    )


def fetch_top_events():
    url = (
        f"{GAMMA_API}/events?active=true&closed=false&archived=false"
        f"&order=volume24hr&ascending=false&limit={TOP_N}"
    )
    return http("GET", url) or []


def fetch_best_asks(token_ids):
    """返回 {token_id: (最低卖价, 该价位可买数量)}"""
    best = {}
    ids = list(dict.fromkeys(token_ids))
    for i in range(0, len(ids), 200):
        chunk = ids[i : i + 200]
        books = http("POST", f"{CLOB_API}/books", [{"token_id": t} for t in chunk]) or []
        for book in books:
            asks = book.get("asks") or []
            if not asks:
                continue
            top = min(asks, key=lambda a: float(a["price"]))
            best[book.get("asset_id")] = (float(top["price"]), float(top["size"]))
    return best


def check_set(legs, best):
    """legs: [(选项名, token_id)]。全部都有卖单时返回 (总价, 每条腿详情, 可成交份数)。"""
    details = []
    for name, token in legs:
        if token not in best:
            return None
        price, size = best[token]
        details.append((name, price, size))
    total = sum(p for _, p, _ in details)
    capacity = min(s for _, _, s in details)
    return total, details, capacity


def scan():
    events = fetch_top_events()
    print(f"抓取到 {len(events)} 个热门活跃事件（按 24h 交易量排序）")

    all_tokens = []
    for e in events:
        for m in e.get("markets") or []:
            if is_tradable(m):
                all_tokens += load_list(m["clobTokenIds"])
    best = fetch_best_asks(all_tokens)
    print(f"读取了 {len(best)} 个订单簿的最低卖价")

    results = []
    for e in events:
        title = e.get("title", "")
        url = f"https://polymarket.com/event/{e.get('slug', '')}"
        markets = [m for m in (e.get("markets") or []) if is_tradable(m)]

        # 1) 每个二元市场：YES + NO
        for m in markets:
            outcomes = load_list(m.get("outcomes")) or ["Yes", "No"]
            tokens = load_list(m["clobTokenIds"])
            r = check_set(list(zip(outcomes, tokens)), best)
            if r:
                name = m.get("groupItemTitle") or m.get("question") or title
                label = title if len(markets) == 1 else f"{title} → {name}"
                results.append({
                    "type": "二元市场 YES+NO",
                    "title": label,
                    "url": url,
                    "total": r[0],
                    "legs": r[1],
                    "capacity": r[2],
                    "warning": "",
                    "fees": bool(m.get("feesEnabled")),
                })

        # 2) 多选一事件（negRisk = Polymarket 标记的互斥事件）：所有选项的 YES 之和
        # 只有当事件的每个选项都能买到时，"全买一遍"才稳赚（比如「其他」选项不能交易时就不算）
        all_options_tradable = all(
            m.get("closed") or is_tradable(m) for m in (e.get("markets") or [])
        )
        is_exclusive = e.get("negRisk") or e.get("enableNegRisk")
        if is_exclusive and len(markets) >= 2 and not all_options_tradable:
            print(f"  跳过「{title}」的多选一检查：有选项暂时无法交易，全买也不能保证赔付")
        elif is_exclusive and len(markets) >= 2:
            legs = [
                (m.get("groupItemTitle") or m.get("question"), load_list(m["clobTokenIds"])[0])
                for m in markets
            ]
            r = check_set(legs, best)
            if r:
                warning = ""
                if e.get("negRiskAugmented"):
                    warning = "该事件以后可能新增选项（含「其他」选项），请人工确认规则后再操作"
                results.append({
                    "type": f"多选一事件（{len(legs)} 个选项全买 YES）",
                    "title": title,
                    "url": url,
                    "total": r[0],
                    "legs": r[1],
                    "capacity": r[2],
                    "warning": warning,
                    "fees": any(m.get("feesEnabled") for m in markets),
                })

    results.sort(key=lambda x: x["total"])
    return results


def format_opportunity(o):
    profit = 1 - o["total"]
    lines = [
        f"【{o['type']}】{o['title']}",
        f"买入总成本：{o['total']:.4f} 美元 → 结算必得 1 美元",
        f"理论利润：{profit:.4f} 美元/份（{profit / o['total'] * 100:.2f}%）",
        f"最优价位可成交约 {o['capacity']:.0f} 份",
    ]
    legs = sorted(o["legs"], key=lambda x: -x[1])
    for name, price, size in legs[:8]:
        lines.append(f"  · {name}：{price:.3f}（可买 {size:.0f} 份）")
    if len(legs) > 8:
        lines.append(f"  · ……其余 {len(legs) - 8} 个选项价格更低")
    if o["fees"]:
        lines.append("⚠️ 该市场收取交易手续费，实际利润会更低")
    if o["warning"]:
        lines.append(f"⚠️ {o['warning']}")
    lines.append(o["url"])
    return "\n".join(lines)


def gmail_diagnosis():
    """只检查格式，不打印账号密码本身。"""
    user, pwd = GMAIL_USER, GMAIL_APP_PASSWORD
    domain = user.rsplit("@", 1)[-1] if "@" in user else "（没有 @）"
    return "\n".join([
        "  诊断（不含账号密码内容）：",
        f"  · GMAIL_USER：{len(user)} 个字符，邮箱后缀是 {domain}"
        + ("，含有中文/全角字符" if not user.isascii() else "")
        + ("，中间有空格" if " " in user else ""),
        f"  · GMAIL_APP_PASSWORD：去掉空格后 {len(pwd)} 位"
        + ("（正确应为 16 位）" if len(pwd) != 16 else "")
        + ("，全是小写字母 ✓" if pwd.isascii() and pwd.isalpha() and pwd.islower() else "，不全是小写字母（应用专用密码只含 16 个小写字母）"),
    ])


def send_email(text):
    subject = text.splitlines()[0][:80]
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = EMAIL_TO
    recipients = [a.strip() for a in EMAIL_TO.split(",") if a.strip()]
    if not (GMAIL_USER + GMAIL_APP_PASSWORD).isascii():
        raise RuntimeError("账号或密码里有中文/全角字符。\n" + gmail_diagnosis())
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.ehlo()
        server.user, server.password = GMAIL_USER, GMAIL_APP_PASSWORD
        try:
            server.auth("PLAIN", server.auth_plain)
        except smtplib.SMTPAuthenticationError as e:
            raise RuntimeError(
                "Gmail 拒绝了账号或密码（535）。请检查 GMAIL_USER 是否是完整的 Gmail 地址，"
                "GMAIL_APP_PASSWORD 是否是 16 位「应用专用密码」（不是 Gmail 登录密码）。\n"
                + gmail_diagnosis()
            ) from e
        server.sendmail(GMAIL_USER, recipients, msg.as_string())


def send_alert(text):
    sent = False
    configured = bool(
        (GMAIL_USER and GMAIL_APP_PASSWORD)
        or FEISHU_WEBHOOK
        or DINGTALK_WEBHOOK
        or (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    )
    if GMAIL_USER and GMAIL_APP_PASSWORD:
        try:
            send_email(text)
            print(f"邮件已发送到：{EMAIL_TO}")
            sent = True
        except Exception as e:
            print(f"Gmail 发送失败：{e}")
    chunks = [text[i : i + 3500] for i in range(0, len(text), 3500)] or [text]
    for chunk in chunks:
        if FEISHU_WEBHOOK:
            try:
                r = http("POST", FEISHU_WEBHOOK, {"msg_type": "text", "content": {"text": chunk}})
                print(f"飞书返回：{r}")
                sent = True
            except Exception as e:
                print(f"飞书发送失败：{e}")
        if DINGTALK_WEBHOOK:
            try:
                r = http("POST", DINGTALK_WEBHOOK, {"msgtype": "text", "text": {"content": chunk}})
                print(f"钉钉返回：{r}")
                sent = True
            except Exception as e:
                print(f"钉钉发送失败：{e}")
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            try:
                r = http(
                    "POST",
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    {"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "disable_web_page_preview": True},
                )
                print(f"Telegram 返回：ok={r.get('ok') if r else r}")
                sent = True
            except Exception as e:
                print(f"Telegram 发送失败：{e}")
    if not configured:
        print("⚠️ 没有配置任何提醒通道（GMAIL_USER+GMAIL_APP_PASSWORD / FEISHU_WEBHOOK / DINGTALK_WEBHOOK / TELEGRAM_BOT_TOKEN+TELEGRAM_CHAT_ID）")
    elif not sent:
        print("⚠️ 所有提醒通道都发送失败，请看上面的报错")
    return sent


def main():
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    print(f"=== Polymarket 套利监控 {now}（北京时间）阈值 < {THRESHOLD} ===")

    if TEST_ALERT:
        ok = send_alert(f"✅ Polymarket 套利监控 测试消息\n时间：{now}\n提醒通道配置成功！")
        if not ok:
            sys.exit(1)

    results = scan()

    print("\n离套利最近的 5 个组合（总价越低越好）：")
    for o in results[:5]:
        print(f"  {o['total']:.4f}  {o['type']}  {o['title']}")

    opportunities = [o for o in results if o["total"] < THRESHOLD]
    if not opportunities:
        print(f"\n本次没有发现总价 < {THRESHOLD} 的套利机会。")
        return

    print(f"\n🚨 发现 {len(opportunities)} 个套利机会！")
    header = f"🚨 Polymarket 套利提醒（{now} 北京时间）\n发现 {len(opportunities)} 个总价 < {THRESHOLD} 的组合：\n"
    body = "\n\n".join(format_opportunity(o) for o in opportunities[:10])
    footer = "\n\n（纯只读监控，价格瞬息万变，下单前请到网页确认）"
    text = header + "\n" + body + footer
    print(text)
    send_alert(text)


if __name__ == "__main__":
    main()
