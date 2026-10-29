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


SUBJECT_STATUS = {"no": "Not Started", "unknown": "Unclear"}


def notification_subject(verdict: str, has_insight: bool = False) -> str:
    """Started leads with the status so it survives mobile truncation; otherwise Aero India | ... format."""
    insight = " | New Insight" if has_insight else ""
    if verdict.lower() == "yes":
        return f"🔔 Registration OPEN | Aero India{insight}"
    status = SUBJECT_STATUS.get(verdict.lower(), SUBJECT_STATUS["unknown"])
    return f"Aero India{insight} | Registration: {status}"


def _split_hook(answer: str) -> tuple[str, str]:
    """First paragraph is the teaser; the remainder follows it."""
    parts = re.split(r"\n\s*\n", (answer or "").strip(), maxsplit=1)
    return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")


def _first_sentence(text: str) -> str:
    text = " ".join(text.replace("**", "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return m.group(1) if m else text


def _vote_block(up_url, down_url, comment_url):
    def cell(label, url, bg):
        return (f'<td width="50%" style="padding:0 4px;"><a href="{escape(url, quote=True)}" '
                f'style="display:block;background-color:{bg};color:#ffffff;text-decoration:none;'
                'font-weight:700;font-size:16px;text-align:center;padding:14px 8px;border-radius:8px;">'
                f"{label}</a></td>")
    comment = (f'<div style="margin-top:10px;font-size:13px;"><a href="{escape(comment_url, quote=True)}" '
               'style="color:#0b5cad;">Leave a comment</a></div>') if comment_url else ""
    return (
        '<div style="margin-top:22px;padding-top:16px;border-top:1px solid #d5e0ee;text-align:center;">'
        f'<div style="font-size:16px;font-weight:700;color:{NAVY};margin-bottom:10px;">'
        "Was this insight useful?</div>"
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'{cell("&#128077; Useful", up_url, "#1b873f")}{cell("&#128078; Not for me", down_url, "#6b7785")}'
        f"</tr></table>{comment}</div>"
    )


def render_notification(run_time, verdict, content_changed, website_name, website_url,
                        insight=None, feedback_url=None, vote_up_url=None, vote_down_url=None,
                        unsubscribe_url=None):
    label, color = STATUS_STYLES.get(verdict.lower(), STATUS_STYLES["unknown"])
    link = escape(website_url, quote=True)
    body = (
        '<div style="font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#6b7785;">'
        "Aero India Tracker</div>"
        f'<div style="margin:8px 0 6px 0;"><span style="display:inline-block;background-color:{color};'
        'color:#ffffff;font-size:16px;font-weight:700;padding:8px 16px;border-radius:20px;">'
        f"Registration: {escape(label)}</span></div>"
        f'<div style="font-size:13px;color:#6b7785;">{escape(run_time)} · page changed: '
        f'{escape(content_changed)} · <a href="{link}" style="color:#0b5cad;">View registration page</a></div>'
    )
    text = (
        f"Aero India visitor registration update ({run_time})\n\n"
        f"General Visitor Registration: {verdict}\n"
        f"Website changed since last run: {content_changed}\n"
        f"Page: {website_url}\n"
    )
    preheader = f"General visitor registration: {label}"
    if insight:
        hook, rest = _split_hook(insight["answer"])
        bridge = ("Registration is open. Bonus read:" if verdict.lower() == "yes"
                  else "Nothing to act on today. Here's something worth a minute:")
        body += (
            f'<p style="margin:22px 0 0 0;color:#4b5563;">{escape(bridge)}</p>'
            '<div style="margin-top:12px;border-left:4px solid #0b5cad;background-color:#f3f7fc;'
            'padding:18px 20px;border-radius:0 6px 6px 0;">'
            '<div style="font-size:12px;letter-spacing:1.5px;text-transform:uppercase;color:#0b5cad;'
            'font-weight:700;">&#9992; Today\'s spotlight &middot; Bonus</div>'
            f'<div style="font-size:20px;line-height:26px;font-weight:700;color:{NAVY};margin:6px 0 12px 0;">'
            f'{escape(insight["topic"])}</div>'
            f'<div style="font-size:16px;line-height:24px;">{article_to_html(hook)}</div>'
            f"{article_to_html(rest)}"
            f"{_vote_block(vote_up_url, vote_down_url, feedback_url) if vote_up_url and vote_down_url else ''}</div>"
        )
        text += (f"\n{bridge}\n\n--- Today's spotlight (bonus) ---\n{insight['topic']}\n\n"
                 f"{hook}\n\n{rest}\n")
        if vote_up_url and vote_down_url:
            text += (f"\nWas this insight useful?\n👍 Useful: {vote_up_url}\n"
                     f"👎 Not for me: {vote_down_url}\n")
        preheader = _first_sentence(hook) or f"Today's spotlight: {insight['topic']}"
    footer = f"Source: {escape(website_name)}<br>Sent by your Aero India website checker."
    if feedback_url:
        footer += f'<br><a href="{escape(feedback_url, quote=True)}" style="color:#0b5cad;">Send feedback</a>'
        text += f"\nSend feedback: {feedback_url}\n"
    if unsubscribe_url:
        footer += f'<br><a href="{escape(unsubscribe_url, quote=True)}" style="color:#0b5cad;">Unsubscribe</a>'
        text += f"\nUnsubscribe: {unsubscribe_url}\n"
    text += f"\nSource: {website_name} ({website_url})\n"
    html = _wrap(preheader, "", body, footer)
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

    body += '<h3 style="color:%s;margin:26px 0 10px 0;">Insight votes</h3>' % NAVY
    text += "\nInsight votes (thumbs up / down)\n"
    if not d.get("votes"):
        body += '<p style="color:#6b7785;margin:0 0 12px 0;">No votes yet.</p>'
        text += "No votes yet.\n"
    for v in d.get("votes", []):
        body += (f'<div style="margin:0 0 6px 0;">{escape(v["topic"])}: '
                 f'&#128077; <strong>{v["up"]}</strong> &middot; &#128078; <strong>{v["down"]}</strong></div>')
        text += f"- {v['topic']}: up {v['up']}, down {v['down']}\n"

    html = _wrap(subject, _header("Daily insights digest", date_label), body,
                 "Sent to the owner only. Contains recipient addresses and feedback.")
    return subject, text, html


def render_page(title: str, body_html: str) -> str:
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title>"
        "<style>*{box-sizing:border-box;}"
        f"body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;"
        f"padding:32px 16px;font-family:{FONT};color:#1f2937;"
        "background:linear-gradient(160deg,rgba(3,10,24,.88) 0%,rgba(8,24,48,.60) 45%,rgba(3,10,24,.92) 100%),"
        'url("background-jet.jpg") center/cover no-repeat fixed;}'
        ".card{width:100%;max-width:560px;background:rgba(255,255,255,.97);"
        "-webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);"
        "border:1px solid rgba(255,255,255,.6);border-radius:18px;"
        "box-shadow:0 24px 60px rgba(2,8,23,.45);padding:32px 36px;}"
        f"h2{{margin-top:0;color:{NAVY};}}"
        "@media (max-width:400px){.card{padding:22px 16px;}}"
        "</style></head><body>"
        f'<div class="card"><h2>{escape(title)}</h2>{body_html}</div></body></html>'
    )


def render_subscribe_form(site_key: str, message: str = "", limit: int = 50) -> str:
    note = f'<p class="note">{escape(message)}</p>' if message else ""
    css = (
        "*{box-sizing:border-box;}"
        "body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;"
        "padding:32px 16px;font-family:" + FONT + ";color:#1f2937;"
        "background:linear-gradient(160deg,rgba(3,10,24,.88) 0%,rgba(8,24,48,.60) 45%,rgba(3,10,24,.92) 100%),"
        'url("background-jet.jpg") center/cover no-repeat fixed;}'
        ".card{width:100%;max-width:520px;background:rgba(255,255,255,.97);"
        "-webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);"
        "border:1px solid rgba(255,255,255,.6);border-radius:18px;"
        "box-shadow:0 24px 60px rgba(2,8,23,.45);padding:36px 40px;}"
        ".header{text-align:center;}"
        "h1{margin:0;color:" + NAVY + ";font-size:28px;letter-spacing:-.02em;}"
        ".subtitle{margin:8px 0 16px;color:#4b5b6e;font-size:15px;}"
        ".badge{display:inline-block;margin:0 0 22px;padding:7px 16px;border-radius:999px;"
        "background:#e7f0fc;border:1px solid #c3d9f2;color:#0b5cad;font-size:13px;font-weight:600;}"
        ".info-section{background:#f3f7fc;border-left:4px solid #0b5cad;border-radius:0 10px 10px 0;"
        "padding:16px 20px;margin:0 0 22px;font-size:14px;line-height:21px;text-align:left;}"
        ".info-title{font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;"
        "color:" + NAVY + ";margin-bottom:10px;}"
        ".info-section ul{margin:0;padding-left:18px;}"
        ".info-section li{margin:0 0 12px;}"
        ".info-section li:last-child{margin-bottom:0;}"
        ".note{color:#b42318;margin:0 0 14px;}"
        ".field-label{display:block;font-weight:700;font-size:14px;margin:0 0 6px;color:#1f2937;}"
        ".email{width:100%;padding:12px 14px;font-size:15px;border:1px solid #c9d4e0;border-radius:8px;"
        "margin-bottom:16px;background:#fff;}"
        ".email:focus{outline:2px solid #0b5cad;outline-offset:1px;border-color:#0b5cad;}"
        ".turnstile-wrap{display:flex;justify-content:center;margin:0 0 16px;}"
        ".cta{width:100%;padding:14px 18px;font-size:16px;font-weight:600;color:#fff;border:0;border-radius:8px;"
        "background:linear-gradient(90deg,#0b5cad,#1b6fd1);cursor:pointer;}"
        ".cta:hover{background:#0a4f96;}"
        "@media (max-width:400px){.card{padding:22px 16px;}h1{font-size:23px;}}"
    )
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Subscribe &middot; Aero India 2027 Tracker</title>"
        f"<style>{css}</style></head><body>"
        '<div class="card">'
        '<div class="header">'
        "<h1>Aero India 2027 Tracker</h1>"
        '<p class="subtitle">Get notified as visitor registration opens.</p>'
        '<p class="badge">&#10024; Includes weekly aircraft deep-dives &amp; stories</p>'
        "</div>"
        '<div class="info-section">'
        '<div class="info-title">&#9992; Service Details</div>'
        "<ul>"
        "<li><strong>Aviation Snippets:</strong> Every Wednesday, Friday, and Sunday, you will receive "
        "a short, engaging read exploring the history and engineering breakthroughs of an amazing "
        "flying machine.</li>"
        "<li><strong>Tracker Alerts:</strong> Immediate high-priority notification updates sent the "
        "moment ticket and visitor registrations go live.</li>"
        "<li><strong>Passion Project:</strong> Formed directly out of my personal struggle to navigate "
        "public ticket timelines and a deep love for aerospace development.</li>"
        "<li><strong>100% Free &amp; Clean:</strong> No hidden costs, promotional banners, or "
        "third-party marketing ads.</li>"
        f"<li><strong>Capped Space:</strong> Restricted strictly to {int(limit)} curated subscriber "
        "slots to keep cloud server bills sustainable.</li>"
        "<li><strong>Temporary:</strong> The automated tracker terminates completely exactly one week "
        "after Aero India 2027 wraps up.</li>"
        "</ul></div>" + note +
        '<form method="POST" action="">'
        '<label class="field-label" for="email">Email Address</label>'
        '<input class="email" id="email" type="email" name="email" required maxlength="254" '
        'placeholder="you@example.com">'
        '<div style="position:absolute;left:-9999px;" aria-hidden="true">'
        '<input type="text" name="website" tabindex="-1" autocomplete="off"></div>'
        '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>'
        '<div class="turnstile-wrap">'
        f'<div class="cf-turnstile" data-sitekey="{escape(site_key)}"></div></div>'
        '<button class="cta" type="submit">Subscribe to Updates &amp; Stories</button></form>'
        "</div></body></html>"
    )


def render_unsubscribe_confirm(action_url: str) -> str:
    body = (
        "<p>Stop receiving Aero India tracker emails at this address?</p>"
        f'<form method="POST" action="{escape(action_url, quote=True)}">'
        '<button type="submit" style="background:#b42318;color:#fff;border:0;border-radius:6px;'
        'padding:10px 18px;font-size:15px;">Unsubscribe</button></form>'
    )
    return render_page("Unsubscribe", body)
