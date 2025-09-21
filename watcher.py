#!/usr/bin/env python3

import os, sys, json, ssl, smtplib, datetime, html
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path
from collections import defaultdict
import requests

GITHUB_API = "https://api.github.com"
SEARCH_ISSUES = f"{GITHUB_API}/search/issues"

def env(name, default=None, required=False):
    v = os.getenv(name, default)
    if required and (v is None or v == ""):
        print(f"[ERR] Missing env: {name}", file=sys.stderr); sys.exit(1)
    return v

GH_TOKEN     = env("GH_TOKEN", required=True)

SMTP_HOST    = env("SMTP_HOST")
SMTP_PORT    = int(env("SMTP_PORT", "587")) if os.getenv("SMTP_PORT") else None
SMTP_USER    = env("SMTP_USER")
SMTP_PASS    = env("SMTP_PASS")
MAIL_TO      = env("MAIL_TO")
MAIL_FROM    = env("MAIL_FROM")

TG_BOT_TOKEN = env("TG_BOT_TOKEN")
TG_CHAT_ID   = env("TG_CHAT_ID")

FILTER_LINKED_PR     = env("FILTER_LINKED_PR", "1")
MAX_TIMELINE_CHECKS  = int(env("MAX_TIMELINE_CHECKS", "60"))
TIMELINE_MAX_PAGES   = int(env("TIMELINE_MAX_PAGES", "2"))
HTTP_TIMEOUT         = int(env("HTTP_TIMEOUT", "45"))

def read_json(path, default=None):
    p = Path(path)
    if not p.exists(): return default
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)

cfgA = read_json("config.json", default={"queries": [], "interval_minutes": 30, "max_results": 100})
cfgB = read_json("targets.json", default={"repos": [], "labels": [], "exclude_labels": [], "interval_minutes": 30, "max_results": 100})

INTERVAL_MIN = int(os.getenv("INTERVAL_MIN") or cfgA.get("interval_minutes") or cfgB.get("interval_minutes") or 30)
MAX_RESULTS  = int(os.getenv("MAX_RESULTS")  or cfgA.get("max_results")      or cfgB.get("max_results")      or 100)

def build_queries_from_targets(cfg):
    repos = cfg.get("repos", [])
    labels = cfg.get("labels", [])
    exclude_labels = cfg.get("exclude_labels", [])
    queries = []
    if not repos or not labels: return queries
    label_or = " OR ".join([f'label:"{lb}"' if " " in lb else f"label:{lb}" for lb in labels])
    excl = " ".join([f'-label:"{lb}"' if " " in lb else f"-label:{lb}" for lb in exclude_labels]) if exclude_labels else ""
    for r in repos:
        name = f"{r}-{'_'.join(lb.replace(' ','_') for lb in labels)}"
        q = f'repo:{r} is:issue is:open ({label_or}) {excl}'.strip()
        queries.append({"name": name, "q": q})
    return queries

queriesA = cfgA.get("queries", [])
queriesB = build_queries_from_targets(cfgB)
ALL_QUERIES = queriesA + queriesB
if not ALL_QUERIES:
    print("[ERR] Require config.json/targets.json", file=sys.stderr)
    sys.exit(1)

def iso_utc(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

def plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"

def gh_search(q, since_iso):
    query = f"{q} updated:>={since_iso}"
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {GH_TOKEN}"}
    per_page = min(MAX_RESULTS, 100)
    max_pages = int(os.getenv("PAGINATE_PAGES", "3"))

    items, page = [], 1
    while page <= max_pages and len(items) < MAX_RESULTS:
        params = {"q": query, "sort": "updated", "order": "desc", "per_page": per_page, "page": page}
        r = requests.get(SEARCH_ISSUES, params=params, headers=headers, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        chunk = r.json().get("items", [])
        if not chunk:
            break
        items.extend(chunk)
        if len(chunk) < per_page:
            break
        page += 1
    return items[:MAX_RESULTS]

def issue_has_open_linked_pr(repo_full_name: str, issue_number: int) -> bool:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GH_TOKEN}",
    }
    page = 1
    per_page = 100
    while page <= TIMELINE_MAX_PAGES:
        url = f"{GITHUB_API}/repos/{repo_full_name}/issues/{issue_number}/timeline"
        params = {"per_page": per_page, "page": page}
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT)
            if resp.status_code >= 400:
                return False
            data = resp.json()
        except Exception:
            return False

        for ev in data:
            if ev.get("event") != "cross-referenced":
                continue
            source = ev.get("source") or {}
            src_issue = source.get("issue") or {}
            if "pull_request" in src_issue:
                if (src_issue.get("state") or "").lower() == "open":
                    return True

        if len(data) < per_page:
            break
        page += 1

    return False

def label_span(label):
    name = html.escape(label.get("name", ""))
    color = label.get("color") or "dddddd"
    try:
        r = int(color[0:2], 16); g = int(color[2:4], 16); b = int(color[4:6], 16)
        luminance = 0.2126*r + 0.7152*g + 0.0722*b
    except Exception:
        luminance = 200
    text_color = "#000000" if luminance > 160 else "#ffffff"
    return (f'<span style="display:inline-block; padding:2px 6px; margin:2px 4px 2px 0; '
            f'border-radius:12px; background-color: #{color}; color:{text_color}; '
            f'font-size:12px; line-height:18px; font-family:ui-sans-serif,system-ui,Arial">{name}</span>')

def html_table_for_repo(repo, items):
    rows = []
    for it in items:
        num    = it.get("number")
        title  = html.escape(it.get("title", ""))
        url    = it.get("html_url")
        updated = it.get("updated_at","")
        created = it.get("created_at","")
        labels_html = "".join([label_span(lb) for lb in it.get("labels", [])])

        state = (it.get("state") or "").lower()
        assignees = it.get("assignees") or []
        assignee_names = ", ".join([a.get("login","") for a in assignees]) if assignees else "--"

        title_html = f'<a href="{url}" style="text-decoration:none; color:#0969da;"><strong>{title}</strong></a>'

        rows.append(f"""
          <tr>
            <td style="padding:8px; border:1px solid #ddd; white-space:nowrap;">
              <a href="{url}" style="text-decoration:none;">#{num}</a>
            </td>
            <td style="padding:8px; border:1px solid #ddd;">{title_html}</td>
            <td style="padding:8px; border:1px solid #ddd;">{labels_html}</td>
            <td style="padding:8px; border:1px solid #ddd; white-space:nowrap;">{assignee_names}</td>
            <td style="padding:8px; border:1px solid #ddd; text-transform:capitalize; white-space:nowrap;">{state or "—"}</td>
            <td style="padding:8px; border:1px solid #ddd; white-space:nowrap;">{updated}</td>
            <td style="padding:8px; border:1px solid #ddd; white-space:nowrap;">{created}</td>
          </tr>
        """)

    repo_link  = f"https://github.com/{repo}"
    header = (f'<h3 style="margin:20px 0 8px; font-family:ui-sans-serif,system-ui,Arial;">'
              f'<a href="{repo_link}" style="text-decoration:none; color:#24292f;">{html.escape(repo)}</a></h3>')
    table = f"""
    <table role="grid" style="border-collapse:collapse; width:100%; max-width:100%; font-family:ui-sans-serif,system-ui,Arial; font-size:14px;">
      <thead>
        <tr style="background:#f6f8fa;">
          <th style="padding:8px; border:1px solid #ddd; text-align:left;">Issue</th>
          <th style="padding:8px; border:1px solid #ddd; text-align:left;">Title</th>
          <th style="padding:8px; border:1px solid #ddd; text-align:left;">Labels</th>
          <th style="padding:8px; border:1px solid #ddd; text-align:left;">Assignees</th>
          <th style="padding:8px; border:1px solid #ddd; text-align:left;">State</th>
          <th style="padding:8px; border:1px solid #ddd; text-align:left;">Updated</th>
          <th style="padding:8px; border:1px solid #ddd; text-align:left;">Created</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>
    """
    return header + table

def build_html_report_by_query(grouped_by_query_repo, since_iso, per_query_counts):
    if not grouped_by_query_repo:
        return (f'<div style="font-family:ui-sans-serif,system-ui,Arial; font-size:14px;">'
                f'No update since: {html.escape(since_iso)} </div>')

    parts = [f'<div style="margin:0 0 16px; color:#57606a; font-family:ui-sans-serif,system-ui,Arial;">Time window: since {html.escape(since_iso)}</div>']
    for qname in sorted(grouped_by_query_repo.keys()):
        count = per_query_counts.get(qname, 0)
        parts.append(f'<h2 style="margin:12px 0 8px; font-family:ui-sans-serif,system-ui,Arial;">Query: {html.escape(qname)} ({plural(count, "result")})</h2>')
        repo_map = grouped_by_query_repo[qname]
        for repo in sorted(repo_map.keys()):
            parts.append(html_table_for_repo(repo, repo_map[repo]))

    return '<div style="font-family:ui-sans-serif,system-ui,Arial; font-size:14px;">' + "".join(parts) + '<div style="margin-top:16px; color:#57606a;">-- Powered by Zealot</div></div>'

def build_text_fallback_by_query(grouped_by_query_repo, since_iso, per_query_counts):
    if not grouped_by_query_repo:
        return f"No update since: {since_iso}\n"
    lines = [f"Github Issue Update since: {since_iso}"]
    for qname in sorted(grouped_by_query_repo.keys()):
        lines.append(f"\n### Query: {qname} ({plural(per_query_counts.get(qname,0), 'result')})")
        repo_map = grouped_by_query_repo[qname]
        for repo in sorted(repo_map.keys()):
            lines.append(f"\n## {repo}")
            for it in repo_map[repo]:
                labels = ", ".join([lb.get("name","") for lb in it.get("labels", [])])
                assignees = ", ".join([a.get("login","") for a in (it.get("assignees") or [])]) or "--"
                state = (it.get("state") or "--")
                lines.append(f"- #{it.get('number')} {it.get('title')} [{it.get('html_url')}]")
                lines.append(f"  labels: {labels} | assignees: {assignees} | state: {state}")
                lines.append(f"  updated: {it.get('updated_at','')}  created: {it.get('created_at','')}")
    lines.append("\n-- Powered by Zealot")
    return "\n".join(lines)

def send_email_html(subject, html_body, text_fallback):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and MAIL_TO and MAIL_FROM and SMTP_PORT):
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO
    msg["Date"] = formatdate(localtime=True)

    msg.attach(MIMEText(text_fallback, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.ehlo()
        s.starttls(context=ctx)
        s.ehlo()
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(MAIL_FROM, [MAIL_TO], msg.as_string())
    return True

def send_tg(text_content):
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        return False
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text_content, "disable_web_page_preview": True}
    r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
    try:
        r.raise_for_status()
        return True
    except Exception:
        print("[WARN] Telegram Sent Failed", r.text, file=sys.stderr)
        return False

def main():
    now = datetime.datetime.now(datetime.UTC)
    since = now - datetime.timedelta(minutes=INTERVAL_MIN)
    since_iso = iso_utc(since)

    grouped_by_query_repo = defaultdict(lambda: defaultdict(list))
    per_query_counts = {}

    timeline_checks = 0

    for q in ALL_QUERIES:
        qname = q.get("name") or q.get("q")[:40]
        try:
            items = gh_search(q["q"], since_iso)
        except Exception as e:
            print(f"[ERR] Query failed: {qname}: {e}", file=sys.stderr)
            continue

        seen_urls_in_this_query = set()
        kept = 0

        for it in items:
            upd = it.get("updated_at")
            if upd:
                try:
                    if datetime.datetime.strptime(upd, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC) < since:
                        continue
                except Exception:
                    pass

            state = (it.get("state") or "").lower()
            if state != "open":
                continue
            assignees = it.get("assignees") or []
            if assignees:
                continue
            if FILTER_LINKED_PR == "1" and timeline_checks < MAX_TIMELINE_CHECKS:
                repo_full = "/".join(it["repository_url"].split("/")[-2:])
                num = it.get("number")
                try:
                    if issue_has_open_linked_pr(repo_full, num):
                        timeline_checks += 1
                        continue
                finally:
                    timeline_checks += 1

            url = it.get("html_url")
            if not url or url in seen_urls_in_this_query:
                continue
            seen_urls_in_this_query.add(url)

            repo = "/".join(it["repository_url"].split("/")[-2:])
            grouped_by_query_repo[qname][repo].append(it)
            kept += 1

        per_query_counts[qname] = kept

    total = sum(per_query_counts.values())

    html_report = build_html_report_by_query(grouped_by_query_repo, since_iso, per_query_counts)
    text_report = build_text_fallback_by_query(grouped_by_query_repo, since_iso, per_query_counts)
    Path("notify.html").write_text(html_report, encoding="utf-8")
    Path("notify.txt").write_text(text_report, encoding="utf-8")

    gh_out = os.getenv("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"HAS_RESULTS={'true' if total>0 else 'false'}\n")
            f.write(f"TOTAL={total}\n")

    if total == 0:
        print(f"[OK] No unassigned open issues (without open linked PRs) since {since_iso}. No email/telegram sent.")
        return

    subject = f"[Zealot] {plural(total, 'unassigned open issue')} across {plural(len([k for k in per_query_counts if per_query_counts[k]>0]), 'query')}"
    mail_ok = send_email_html(subject, html_report, text_report)
    tg_ok   = send_tg(text_report)
    print(f"[OK] Sent email: {mail_ok}, telegram: {tg_ok}")

if __name__ == "__main__":
    main()
