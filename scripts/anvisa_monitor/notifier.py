"""
Sends email alert via Resend when ANVISA monitor completes.
Always sends a run summary — even if nothing changed, so you know it ran.
"""

import logging
import os
from datetime import datetime

import resend

logger = logging.getLogger(__name__)


def build_html_email(
    summary: str,
    relevant: list[dict],
    total_scraped: int,
    ingredient_changes: int,
    run_id: str,
    dry_run: bool,
) -> str:
    """Build HTML email body."""

    dry_run_banner = (
        '<div style="background:#fff3cd;border:1px solid #ffc107;padding:12px;'
        'border-radius:4px;margin-bottom:20px;">'
        '<strong>⚠️ DRY RUN</strong> — No database changes were made.'
        '</div>'
        if dry_run else ""
    )

    urgency_colors = {"high": "#dc3545", "medium": "#fd7e14", "low": "#6c757d"}

    publications_html = ""
    if relevant:
        for pub in sorted(
            relevant,
            key=lambda p: p.get("classification", {}).get("urgency", "low"),
            reverse=True,
        ):
            c = pub.get("classification", {})
            urgency = c.get("urgency", "low")
            color = urgency_colors.get(urgency, "#6c757d")

            added = c.get("ingredients_added", [])
            removed = c.get("ingredients_removed", [])
            modified = c.get("ingredients_modified", [])

            ingredients_html = ""
            if added:
                names = ", ".join(i.get("name_en") or i.get("name_pt", "?") for i in added[:5])
                ingredients_html += f'<p style="color:#155724">✅ Added: {names}</p>'
            if removed:
                names = ", ".join(i.get("name_en") or i.get("name_pt", "?") for i in removed[:5])
                ingredients_html += f'<p style="color:#721c24">❌ Removed: {names}</p>'
            if modified:
                names = ", ".join(i.get("name_en") or i.get("name_pt", "?") for i in modified[:5])
                ingredients_html += f'<p style="color:#856404">⚠️ Modified: {names}</p>'

            url = pub.get("url", "")
            link_html = f'<a href="{url}" style="color:#0066cc">View source →</a>' if url else ""

            publications_html += f"""
<div style="border-left:4px solid {color};padding:12px 16px;margin:12px 0;
            background:#f8f9fa;border-radius:0 4px 4px 0;">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <strong style="font-size:14px">{pub.get('title', 'N/A')[:120]}</strong>
    <span style="background:{color};color:white;padding:2px 8px;border-radius:12px;
                 font-size:11px;font-weight:bold">{urgency.upper()}</span>
  </div>
  <p style="color:#555;font-size:13px;margin:6px 0">
    {c.get('summary_en') or 'No summary available.'}
  </p>
  {ingredients_html}
  <p style="font-size:12px;color:#888;margin:4px 0">
    {c.get('publication_number', '')} · {pub.get('pub_date', 'Date unknown')} · {link_html}
  </p>
</div>"""
    else:
        publications_html = (
            '<p style="color:#6c757d;font-style:italic">'
            'No relevant publications found this month.</p>'
        )

    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
             max-width:700px;margin:0 auto;padding:20px;color:#333">

  <div style="border-bottom:3px solid #009b3a;padding-bottom:16px;margin-bottom:24px">
    <h1 style="margin:0;font-size:22px;color:#009b3a">
      🇧🇷 ANVISA Regulatory Monitor
    </h1>
    <p style="margin:4px 0 0;color:#666;font-size:13px">
      Monthly update · Run ID: {run_id} · {datetime.now().strftime('%B %d, %Y')}
    </p>
  </div>

  {dry_run_banner}

  <div style="background:#e8f5e9;border-radius:6px;padding:16px;margin-bottom:24px">
    <h2 style="margin:0 0 8px;font-size:16px">Summary</h2>
    <p style="margin:0;font-size:14px;line-height:1.6">{summary}</p>
  </div>

  <table style="width:100%;border-collapse:collapse;margin-bottom:24px;font-size:13px">
    <tr style="background:#f1f3f4">
      <td style="padding:8px 12px"><strong>Total publications scraped</strong></td>
      <td style="padding:8px 12px">{total_scraped}</td>
    </tr>
    <tr>
      <td style="padding:8px 12px"><strong>Relevant to supplement ingredients</strong></td>
      <td style="padding:8px 12px">{len(relevant)}</td>
    </tr>
    <tr style="background:#f1f3f4">
      <td style="padding:8px 12px"><strong>Ingredient change records written</strong></td>
      <td style="padding:8px 12px">{ingredient_changes}</td>
    </tr>
  </table>

  <h2 style="font-size:16px;margin-bottom:8px">Relevant Publications</h2>
  {publications_html}

  <hr style="border:none;border-top:1px solid #eee;margin:32px 0">
  <p style="font-size:11px;color:#999;text-align:center">
    ANVISA Monitor · GitHub Actions · Sources: gov.br/anvisa + DOU (in.gov.br)
  </p>
</body>
</html>"""


def send_alert(
    summary: str,
    relevant: list[dict],
    total_scraped: int,
    ingredient_changes: int,
    run_id: str,
    dry_run: bool = False,
) -> bool:
    """
    Send run summary email via Resend.
    Returns True if sent successfully.
    """
    api_key = os.environ.get("RESEND_API_KEY")
    alert_email = os.environ.get("ALERT_EMAIL")

    if not api_key or not alert_email:
        logger.warning("Resend API key or alert email not configured — skipping email")
        return False

    resend.api_key = api_key

    high_count = sum(
        1 for p in relevant
        if p.get("classification", {}).get("urgency") == "high"
    )

    subject_prefix = "🚨 ACTION REQUIRED" if high_count > 0 else "📋 Monthly Update"
    dry_suffix = " [DRY RUN]" if dry_run else ""
    subject = (
        f"{subject_prefix}: ANVISA Monitor — "
        f"{len(relevant)} relevant publication{'s' if len(relevant) != 1 else ''} "
        f"found{dry_suffix}"
    )

    html = build_html_email(
        summary=summary,
        relevant=relevant,
        total_scraped=total_scraped,
        ingredient_changes=ingredient_changes,
        run_id=run_id,
        dry_run=dry_run,
    )

    try:
        result = resend.Emails.send({
            "from": "ANVISA Monitor <noreply@regcheck360.com>",
            "to": [alert_email],
            "subject": subject,
            "html": html,
        })
        logger.info(f"Email sent: {result}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return False
