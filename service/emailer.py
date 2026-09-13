"""邮箱推送：把每日候选论文渲染成 HTML 邮件发给指定收件人。

走 SMTP_SSL（QQ 邮箱 smtp.qq.com:465 或 Gmail smtp.gmail.com:465）；密钥从 .env 读：
    SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS（QQ 授权码 或 Gmail 应用专用密码）/ SMTP_TO
"""
import html
import os
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def _render_html(cands: list[dict]) -> str:
    rows = []
    for i, c in enumerate(cands, 1):
        title = html.escape(c.get("title") or "")
        year = c.get("year") or "?"
        venue = html.escape(c.get("venue") or "未知")
        cites = c.get("citations") or 0
        score = c.get("score") or 0
        url = html.escape(c.get("url") or "")
        abstract = html.escape((c.get("abstract") or "")[:300])
        reason = html.escape(c.get("llm_reason") or "")
        rows.append(f"""
<tr>
  <td style="padding:12px 0;border-bottom:1px solid #e2e8f0">
    <div style="font-size:14px;font-weight:600;color:#1e293b;line-height:1.4">{i}. {title}</div>
    <div style="font-size:12px;color:#64748b;margin:4px 0">{year} · {venue} · 被引 {cites} · 综合分 {score:.3f}</div>
    {f'<div style="font-size:12px;color:#2563eb;margin:2px 0">{reason}</div>' if reason else ''}
    <div style="font-size:12px;color:#475569;margin:4px 0;line-height:1.5">{abstract}{'…' if abstract else ''}</div>
    {f'<a href="{url}" style="font-size:12px;color:#2563eb">查看原文 →</a>' if url else ''}
  </td>
</tr>""")
    return f"""<!doctype html><html><body style="font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;background:#f8fafc;padding:24px;margin:0">
<div style="max-width:680px;margin:0 auto;background:#ffffff;border:1px solid #e2e8f0;border-radius:12px;padding:24px">
  <h2 style="margin:0 0 4px;color:#1e293b;font-size:18px">每日论文检索 · {date.today().isoformat()}</h2>
  <p style="color:#64748b;font-size:13px;margin:0 0 16px">共 {len(cands)} 篇候选，按质量评分排序。看完可在网页端逐条「推入论文库」。</p>
  <table style="border-collapse:collapse;width:100%">{''.join(rows)}</table>
</div></body></html>"""


def send_daily(cands: list[dict], to: str | None = None) -> dict:
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or "465")
    user = os.environ.get("SMTP_USER")
    pwd = os.environ.get("SMTP_PASS")
    to = to or os.environ.get("SMTP_TO")
    if not (user and pwd and to):
        raise RuntimeError("邮箱未配置：请在 .env 填 SMTP_USER / SMTP_PASS（QQ 授权码 或 Gmail 应用专用密码）/ SMTP_TO")
    if not cands:
        raise RuntimeError("没有要发送的候选论文")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"每日论文检索 {date.today().isoformat()}（{len(cands)} 篇候选）"
    msg["From"] = user
    msg["To"] = to
    msg.attach(MIMEText(_render_html(cands), "html", "utf-8"))

    with smtplib.SMTP_SSL(host, port, timeout=30) as s:
        s.login(user, pwd)
        s.sendmail(user, [to], msg.as_string())
    return {"to": to, "count": len(cands)}
