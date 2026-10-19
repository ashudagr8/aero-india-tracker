"""Email-safe HTML and plain-text templates. All dynamic text is HTML-escaped."""
import re
from html import escape

NAVY = "#0b2545"
FONT = "Segoe UI, Helvetica, Arial, sans-serif"

STATUS_STYLES = {
    "yes": ("Registration Started", "#1b873f"),
    "no": ("Not Started Yet", "#c77700"),
    "unknown": ("Status Unclear", "#5f6b7a"),
}


def _inline(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escape(text))


def article_to_html(text: str) -> str:
    """Convert plain text with blank-line paragraphs, '-'/'*' bullets and **bold** to HTML."""
    blocks = []
    for block in re.split(r"\n\s*\n", (text or "").strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        if all(re.match(r"^[-*•]\s+", ln) for ln in lines):
            items = "".join(
                f'<li style="margin:0 0 6px 0;">{_inline(re.sub(r"^[-*•]\s+", "", ln))}</li>'
                for ln in lines
            )
            blocks.append(f'<ul style="margin:0 0 14px 0;padding-left:20px;">{items}</ul>')
        else:
            blocks.append(f'<p style="margin:0 0 14px 0;">{_inline(" ".join(lines))}</p>')
    return "".join(blocks)


def _header(title: str, subtitle: str) -> str:
    inner = (
        '<div style="padding:34px 30px 30px 30px;">'
        f'<div style="font-size:12px;letter-spacing:2px;text-transform:uppercase;color:#9fc3ee;">Aero India Tracker</div>'
        f'<div style="font-size:26px;line-height:32px;font-weight:700;color:#ffffff;margin-top:8px;">{escape(title)}</div>'
        f'<div style="font-size:13px;color:#d6e4f5;margin-top:8px;">{escape(subtitle)}</div>'
        "</div>"
    )
    return f'<tr><td bgcolor="{NAVY}" style="background-color:{NAVY};">{inner}</td></tr>'


def _button(label: str, url: str, color: str = "#0b5cad") -> str:
    return (
        f'<a href="{escape(url, quote=True)}" style="display:inline-block;background-color:{color};'
        "color:#ffffff;text-decoration:none;font-weight:600;font-size:15px;padding:12px 22px;"
        f'border-radius:6px;margin:0 8px 8px 0;">{escape(label)}</a>'
    )


def _wrap(preheader: str, header_html: str, body_html: str, footer: str) -> str:
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        f'<body style="margin:0;padding:0;background-color:#eef2f7;font-family:{FONT};">'
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{escape(preheader)}</div>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#eef2f7">'
        '<tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;max-width:600px;background-color:#ffffff;border-radius:8px;overflow:hidden;">'
        f"{header_html}"
        f'<tr><td style="padding:28px 30px;color:#1f2937;font-size:15px;line-height:23px;">{body_html}</td></tr>'
        f'<tr><td style="padding:18px 30px;background-color:#f6f8fb;color:#6b7785;font-size:12px;line-height:18px;">{footer}</td></tr>'
        "</table></td></tr></table></body></html>"
    )


def render_notification(run_time, verdict, content_changed, website_name, website_url,
                        insight=None):
    label, color = STATUS_STYLES.get(verdict.lower(), STATUS_STYLES["unknown"])
    body = (
        '<div style="font-size:13px;color:#6b7785;margin-bottom:8px;">General visitor registration</div>'
        f'<div style="display:inline-block;background-color:{color};color:#ffffff;font-size:18px;'
        f'font-weight:700;padding:10px 20px;border-radius:24px;">{escape(label)}</div>'
        f'<p style="margin:18px 0 22px 0;color:#4b5563;">Website changed since last run: '
        f"<strong>{escape(content_changed)}</strong></p>"
        f"{_button('View registration page', website_url)}"
    )
    text = (
        f"Aero India visitor registration update ({run_time})\n\n"
        f"General Visitor Registration: {verdict}\n"
        f"Website changed since last run: {content_changed}\n"
        f"Page: {website_url}\n"
    )
    if insight:
        body += (
            '<div style="margin-top:30px;border-left:4px solid #0b5cad;background-color:#f3f7fc;'
            'padding:18px 20px;border-radius:0 6px 6px 0;">'
            '<div style="font-size:12px;letter-spacing:1.5px;text-transform:uppercase;color:#0b5cad;'
            'font-weight:700;">Insight of the day</div>'
            f'<div style="font-size:18px;font-weight:700;color:{NAVY};margin:6px 0 12px 0;">'
            f'{escape(insight["topic"])}</div>'
            f'{article_to_html(insight["answer"])}</div>'
        )
        text += f"\n--- Insight of the day ---\n{insight['topic']}\n\n{insight['answer']}\n"
    footer = f"Source: {escape(website_name)}<br>Sent by your Aero India website checker."
    text += f"\nSource: {website_name} ({website_url})\n"
    html = _wrap(f"General visitor registration: {label}", _header(label, run_time), body, footer)
    return text, html


def render_review(article, approve_url, feedback_url, discard_url):
    revision = article["revision"]
    can_revise = bool(feedback_url)
    body = (
        '<p style="margin:0 0 16px 0;color:#4b5563;">A new insight is waiting for your approval. '
        "Only approved insights are included in notification emails.</p>"
        f'<div style="font-size:18px;font-weight:700;color:{NAVY};margin:0 0 12px 0;">'
        f'{escape(article["topic"])}</div>'
        f'<div style="border-left:4px solid #0b5cad;background-color:#f3f7fc;padding:16px 18px;margin-bottom:22px;">'
        f'{article_to_html(article["answer"])}</div>'
    )
    if article.get("feedback"):
        last = article["feedback"][-1]["text"]
        body += (
            '<p style="margin:0 0 18px 0;font-size:13px;color:#6b7785;">Revised after your feedback: '
            f"<em>{escape(last)}</em></p>"
        )
    body += _button("Approve", approve_url, "#1b873f")
    if can_revise:
        body += _button("Request changes", feedback_url, "#c77700")
    body += _button("Discard", discard_url, "#8a94a0")
    if not can_revise:
        body += '<p style="font-size:12px;color:#6b7785;">Revision limit reached: approve or discard.</p>'
    text = (
        f"Insight review (v{revision})\n\nTopic: {article['topic']}\n\n{article['answer']}\n\n"
        f"Approve: {approve_url}\n"
        + (f"Request changes: {feedback_url}\n" if can_revise else "")
        + f"Discard: {discard_url}\n"
    )
    title = "Insight ready for review" if revision == 1 else f"Revised insight (v{revision})"
    html = _wrap(title, _header(title, f"Version {revision}"), body,
                 "You are receiving this because insight review is enabled.")
    return title, text, html


def render_page(title: str, body_html: str) -> str:
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title></head>"
        f'<body style="margin:0;background:#eef2f7;font-family:{FONT};color:#1f2937;">'
        '<div style="max-width:600px;margin:30px auto;background:#fff;border-radius:8px;padding:28px 30px;">'
        f'<h2 style="margin-top:0;color:{NAVY};">{escape(title)}</h2>{body_html}</div></body></html>'
    )
