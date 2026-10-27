"""提醒推送。通过环境变量（GitHub Secrets）启用渠道，未配置的渠道自动跳过。

| 渠道           | 环境变量 |
| -------------- | -------- |
| GitHub Issue   | GITHUB_TOKEN + GITHUB_REPOSITORY（Actions 中自动提供） |
| Server酱(微信) | SERVERCHAN_SENDKEY |
| PushPlus(微信) | PUSHPLUS_TOKEN |
| 企业微信机器人 | WECOM_WEBHOOK |
| 钉钉机器人     | DINGTALK_WEBHOOK（可选 DINGTALK_SECRET 加签） |
| 飞书机器人     | FEISHU_WEBHOOK |
| Telegram       | TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID |
| Bark(iOS)      | BARK_URL（如 https://api.day.app/你的key） |
| 邮件           | SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, MAIL_TO（逗号分隔，默认发给 SMTP_USER 自己） |

配置后可在 Actions 中手动运行“测试提醒”，或本地执行 `python -m tracker.notify --test`，向已配置的渠道发一条测试消息。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import smtplib
import sys
import time
import urllib.parse
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

import requests

from .signals import LABELS

log = logging.getLogger(__name__)
TIMEOUT = 20


def build_message(alerts: list[dict], data_date: str, site_url: str | None,
                  changes: list[dict] | None = None) -> tuple[str, str]:
    buys = [a for a in alerts if a["signal"] == "BUY"]
    sells = [a for a in alerts if a["signal"] == "SELL"]
    parts = [f"新买入 {len(buys)} / 新卖出 {len(sells)}"] if alerts else []
    if changes:
        parts.append("成分股调整")
    title = f"红利指数提醒 {data_date}：{'；'.join(parts)}"
    lines = [f"## {title}", ""]
    for group, emoji in ((buys, "🔴"), (sells, "🟢")):  # 红买绿卖，与网页一致
        for a in group:
            y = f"{a['yield']:.2%}" if a.get("yield") is not None else "-"
            price = f"{a['price']:.2f}" if a.get("price") is not None else "-"
            lines.append(
                f"- {emoji} **{LABELS[a['signal']]}** {a['name']}（{a['code']}）"
                f" 价格 {price}，股息率 {y}，评分 {a.get('score', '-')}"
            )
            for r in a["reasons"]:
                lines.append(f"  - {r}")
    if changes:
        lines += ["", "### 成分股调整"]
        for c in changes:
            fmt = lambda xs: "、".join(f"{x['name']}（{x['code']}）" for x in xs) or "无"
            lines.append(f"- {c['index_name']}：调入 {fmt(c['added'])}；调出 {fmt(c['removed'])}")
    lines.append("")
    if site_url:
        lines.append(f"[查看图表]({site_url})")
    lines.append("> 规则信号仅供参考，不构成投资建议。")
    return title, "\n".join(lines)


def _post(url: str, **kwargs) -> None:
    r = requests.post(url, timeout=TIMEOUT, **kwargs)
    r.raise_for_status()


def send_github_issue(title: str, body: str, mention: bool) -> None:
    token, repo = os.getenv("GITHUB_TOKEN"), os.getenv("GITHUB_REPOSITORY")
    if not (token and repo):
        return
    owner = repo.split("/")[0]
    if mention:
        body += f"\n\ncc @{owner}"
    url = f"https://api.github.com/repos/{repo}/issues"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    try:
        _post(url, headers=headers, json={"title": title, "body": body, "labels": ["signal"]})
    except requests.HTTPError:  # 标签不可用时不带标签重试
        _post(url, headers=headers, json={"title": title, "body": body})


def send_serverchan(title: str, body: str) -> None:
    key = os.getenv("SERVERCHAN_SENDKEY")
    if key:
        _post(f"https://sctapi.ftqq.com/{key}.send", data={"title": title, "desp": body})


def send_pushplus(title: str, body: str) -> None:
    token = os.getenv("PUSHPLUS_TOKEN")
    if token:
        _post("https://www.pushplus.plus/send", json={"token": token, "title": title, "content": body, "template": "markdown"})


def send_wecom(title: str, body: str) -> None:
    url = os.getenv("WECOM_WEBHOOK")
    if url:
        _post(url, json={"msgtype": "markdown", "markdown": {"content": body}})


def send_dingtalk(title: str, body: str) -> None:
    url = os.getenv("DINGTALK_WEBHOOK")
    if not url:
        return
    secret = os.getenv("DINGTALK_SECRET")
    if secret:
        ts = str(round(time.time() * 1000))
        sign = hmac.new(secret.encode(), f"{ts}\n{secret}".encode(), hashlib.sha256).digest()
        url += f"&timestamp={ts}&sign={urllib.parse.quote_plus(base64.b64encode(sign))}"
    _post(url, json={"msgtype": "markdown", "markdown": {"title": title, "text": body}})


def send_feishu(title: str, body: str) -> None:
    url = os.getenv("FEISHU_WEBHOOK")
    if url:
        _post(url, json={"msg_type": "text", "content": {"text": body}})


def send_telegram(title: str, body: str) -> None:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        _post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat, "text": body})


def send_bark(title: str, body: str) -> None:
    url = os.getenv("BARK_URL")
    if url:
        _post(url.rstrip("/"), json={"title": title, "body": body, "group": "红利指数"})


def plain_text(body: str) -> str:
    """把 Markdown 消息转成适合邮件阅读的纯文本。"""
    body = re.sub(r"\[([^\]]+)\]\((\S+?)\)", r"\1：\2", body)  # [查看图表](url) → 查看图表：url
    body = re.sub(r"^#+\s*", "", body, flags=re.M)
    body = re.sub(r"^>\s*", "", body, flags=re.M)
    return body.replace("**", "")


def send_email(title: str, body: str) -> None:
    host = os.getenv("SMTP_HOST")
    user, password = os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASSWORD", "")
    to = os.getenv("MAIL_TO") or user
    if not (host and to):
        return
    port = int(os.getenv("SMTP_PORT") or "465")
    msg = MIMEText(plain_text(body), "plain", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = formataddr((str(Header("红利指数跟踪", "utf-8")), user))
    msg["To"] = to
    smtp = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT) if port == 465 else smtplib.SMTP(host, port, timeout=TIMEOUT)
    with smtp:
        if port != 465:
            smtp.starttls()
        if user:
            smtp.login(user, password)
        smtp.sendmail(user, [x.strip() for x in to.split(",") if x.strip()], msg.as_string())


CHANNELS = [send_serverchan, send_pushplus, send_wecom, send_dingtalk, send_feishu, send_telegram, send_bark, send_email]

# 各渠道需要的环境变量（用于测试时报告哪些渠道已配置）
CHANNEL_ENV = {
    send_serverchan: ("Server酱", ["SERVERCHAN_SENDKEY"]),
    send_pushplus: ("PushPlus", ["PUSHPLUS_TOKEN"]),
    send_wecom: ("企业微信", ["WECOM_WEBHOOK"]),
    send_dingtalk: ("钉钉", ["DINGTALK_WEBHOOK"]),
    send_feishu: ("飞书", ["FEISHU_WEBHOOK"]),
    send_telegram: ("Telegram", ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]),
    send_bark: ("Bark", ["BARK_URL"]),
    send_email: ("邮件", ["SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"]),
}


def dispatch(alerts: list[dict], data_date: str, cfg: dict, site_url: str | None,
             changes: list[dict] | None = None) -> None:
    if not alerts and not changes:
        log.info("无新信号、无成分股调整，不发送提醒")
        return
    title, body = build_message(alerts, data_date, site_url, changes)
    log.info("发送提醒：%s\n%s", title, body)
    senders = list(CHANNELS)
    if cfg.get("github_issue", True):
        senders.insert(0, lambda t, b: send_github_issue(t, b, cfg.get("github_issue_mention", True)))
    for send in senders:
        try:
            send(title, body)
        except Exception as e:  # 单个渠道失败不影响其他渠道
            log.error("提醒发送失败 %s: %s", getattr(send, "__name__", send), e)


def send_test() -> int:
    """向所有已配置的推送渠道（不含 GitHub Issue）发送一条测试消息，返回失败数。"""
    sample = [{"code": "000000", "name": "测试股票", "signal": "BUY", "price": 10.0, "yield": 0.06, "score": 80,
               "reasons": ["这是一条测试提醒：收到即说明推送配置正确，无需处理"]}]
    title, body = build_message(sample, "测试", os.getenv("SITE_URL"))
    title = "【测试】" + title
    failures, sent = 0, 0
    for send, (name, env) in CHANNEL_ENV.items():
        missing = [k for k in env if not os.getenv(k)]
        if missing:
            print(f"- {name}：未配置（缺少 {', '.join(missing)}）")
            continue
        try:
            send(title, body)
            sent += 1
            print(f"- {name}：✅ 已发送")
        except Exception as e:
            failures += 1
            print(f"- {name}：❌ 发送失败：{e}")
    if not sent and not failures:
        print("没有已配置的推送渠道（GitHub 站内通知无需配置，有新信号时自动创建 Issue）")
    return failures


if __name__ == "__main__":
    if "--test" not in sys.argv:
        sys.exit("用法：python -m tracker.notify --test")
    sys.exit(1 if send_test() else 0)
