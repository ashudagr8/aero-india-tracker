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
                        insight=None, feedback_url=None):
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
    if feedback_url:
        footer += f'<br><a href="{escape(feedback_url, quote=True)}" style="color:#0b5cad;">Send feedback</a>'
        text += f"\nSend feedback: {feedback_url}\n"
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


def _fmt(dt):
    return dt.strftime("%d/%m/%y %I:%M %p") if dt else "never"


def _feedback_block(entries, with_email=False):
    out = ""
    for e in entries:
        who = f"{escape(e['email'])} &middot; " if with_email else ""
        out += (
            '<div style="border-left:3px solid #c9d6e6;padding:4px 12px;margin:0 0 12px 0;">'
            f'<div style="font-size:12px;color:#6b7785;">{who}{escape(_fmt(e["at"]))} &middot; '
            f'{escape(e["topic"])}</div>'
            f'<div style="white-space:pre-wrap;">{escape(e["text"])}</div></div>'
        )
    return out


def render_digest(d):
    """Daily owner report. Returns (subject, text, html)."""
    date_label = d["now"].strftime("%d/%m/%y")
    people, new_fb, threshold = d["people"], d["new_feedback"], d["threshold"]
    subject = (f"Aero India | Daily Insights Digest | {date_label} | "
               f"{len(people)} recipients, {len(new_fb)} new feedback")
    if d["low_stock"]:
        subject += " | LOW STOCK"

    body = (
        f'<p style="margin:0 0 16px 0;">Approved insights: <strong>{d["approved"]}</strong> &middot; '
        f'pending review: <strong>{d["pending"]}</strong> &middot; '
        f'topics not yet drafted: <strong>{d["undrafted"]}</strong></p>'
    )
    text = (f"Daily insights digest ({date_label})\n\nApproved: {d['approved']}, "
            f"pending review: {d['pending']}, topics not yet drafted: {d['undrafted']}\n")
    if d["low_stock"]:
        low = [p for p in people if p["remaining"] <= threshold]
        body += (
            '<div style="background-color:#fdecea;border-left:4px solid #c0392b;padding:12px 16px;'
            f'margin:0 0 18px 0;"><strong>Low stock:</strong> {len(low)} recipient(s) have '
            f'{threshold} or fewer insights left. Add new topics soon.</div>')
        text += f"\nLOW STOCK: {len(low)} recipient(s) have {threshold} or fewer insights left.\n"

    cell = 'style="padding:6px 8px;border-bottom:1px solid #e5e9f0;text-align:left;"'
    rows = ""
    text += "\nRecipients (received / today / remaining / last sent / feedback total / today)\n"
    for p in people:
        flag = ' style="background-color:#fdecea;"' if p["remaining"] <= threshold else ""
        rows += (
            f"<tr{flag}><td {cell}>{escape(p['email'])}</td><td {cell}>{p['received']}</td>"
            f"<td {cell}>{p['received_today']}</td><td {cell}><strong>{p['remaining']}</strong></td>"
            f"<td {cell}>{escape(_fmt(p['last_sent']))}</td>"
            f"<td {cell}>{p['feedback_total']} ({p['feedback_today']} today)</td></tr>")
        text += (f"- {p['email']}: {p['received']} / {p['received_today']} / {p['remaining']} / "
                 f"{_fmt(p['last_sent'])} / {p['feedback_total']} / {p['feedback_today']}\n")
    heads = "".join(f"<th {cell}>{h}</th>" for h in
                    ("Recipient", "Received", "Today", "Remaining", "Last sent (IST)", "Feedback"))
    body += (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
             f'style="font-size:13px;border-collapse:collapse;"><tr>{heads}</tr>{rows}</table>')

    body += '<h3 style="color:%s;margin:26px 0 10px 0;">New feedback today</h3>' % NAVY
    text += "\nNew feedback today\n"
    if new_fb:
        body += _feedback_block(new_fb, with_email=True)
        for e in new_fb:
            text += f"- {e['email']} [{_fmt(e['at'])}] {e['topic']}\n  {e['text']}\n"
    else:
        body += '<p style="color:#6b7785;">None.</p>'
        text += "None.\n"

    body += '<h3 style="color:%s;margin:26px 0 10px 0;">Overall feedback by recipient</h3>' % NAVY
    text += "\nOverall feedback by recipient (latest entries)\n"
    for p in people:
        body += (f'<div style="font-weight:600;margin:0 0 6px 0;">{escape(p["email"])} '
                 f'<span style="font-weight:400;color:#6b7785;">({p["feedback_total"]} total)</span></div>')
        text += f"{p['email']} ({p['feedback_total']} total)\n"
        if not p["recent_feedback"]:
            body += '<p style="color:#6b7785;margin:0 0 12px 0;">No feedback yet.</p>'
            continue
        shown = [{**e, "text": e["text"][:300] + ("..." if len(e["text"]) > 300 else "")}
                 for e in p["recent_feedback"]]
        body += _feedback_block(shown)
        for e in shown:
            text += f"  [{_fmt(e['at'])}] {e['topic']}: {e['text']}\n"
        if p["feedback_total"] > len(shown):
            more = f"Showing latest {len(shown)} of {p['feedback_total']}."
            body += f'<p style="font-size:12px;color:#6b7785;margin:0 0 12px 0;">{more}</p>'
            text += f"  {more}\n"

    html = _wrap(subject, _header("Daily insights digest", date_label), body,
                 "Sent to the owner only. Contains recipient addresses and feedback.")
    return subject, text, html


def render_page(title: str, body_html: str) -> str:
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title></head>"
        f'<body style="margin:0;background:#eef2f7;font-family:{FONT};color:#1f2937;">'
        '<div style="max-width:600px;margin:30px auto;background:#fff;border-radius:8px;padding:28px 30px;">'
        f'<h2 style="margin-top:0;color:{NAVY};">{escape(title)}</h2>{body_html}</div></body></html>'
    )
