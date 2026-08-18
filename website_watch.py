#!/usr/bin/env python3
"""Fetch one web page, store a snapshot, and write an HTML change report."""

from __future__ import annotations

import argparse
import difflib
import email.utils
import html
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path


class VisibleTextParser(HTMLParser):
    SKIP_TAGS = {"script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        if tag in {"p", "div", "section", "article", "li", "tr", "br", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if tag in {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        lines = []
        for line in "".join(self.parts).splitlines():
            normalized = re.sub(r"\s+", " ", line).strip()
            if normalized:
                lines.append(normalized)
        return "\n".join(lines) + "\n"


class TableParser(HTMLParser):
    SKIP_TAGS = {"script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._in_table = False
        self._in_row = False
        self._in_cell = False
        self._current_cell: list[str] = []
        self._current_row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "table":
            self._in_table = True
        elif self._in_table and tag == "tr":
            self._in_row = True
            self._current_row = []
        elif self._in_row and tag in {"td", "th"}:
            self._in_cell = True
            self._current_cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in {"td", "th"} and self._in_cell:
            cell = re.sub(r"\s+", " ", "".join(self._current_cell)).strip()
            self._current_row.append(cell)
            self._current_cell = []
            self._in_cell = False
        elif tag == "tr" and self._in_row:
            if any(cell for cell in self._current_row):
                self.rows.append(self._current_row)
            self._current_row = []
            self._in_row = False
        elif tag == "table":
            self._in_table = False

    def handle_data(self, data: str) -> None:
        if self._in_cell and not self._skip_depth:
            self._current_cell.append(data)


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if not config.get("url"):
        raise ValueError("config must include a non-empty 'url'")
    return config


def fetch_page(url: str, user_agent: str, verify_ssl: bool = True) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "*/*",
            "Accept-Language": "de",
            "Referer": "https://turniere.ringerdb.de/zukunft",
            "User-Agent": user_agent,
        },
    )
    context = None if verify_ssl else ssl._create_unverified_context()
    with urllib.request.urlopen(request, timeout=30, context=context) as response:
        return response.read()


def extract_text(page_bytes: bytes) -> str:
    page = page_bytes.decode("utf-8", errors="replace")
    parser = VisibleTextParser()
    parser.feed(page)
    return parser.text()


def extract_tables(page_bytes: bytes) -> str:
    page = page_bytes.decode("utf-8", errors="replace")
    parser = TableParser()
    parser.feed(page)
    lines = []
    for row in parser.rows:
        cells = [cell.replace("\\", "\\\\").replace("\t", " ").replace("\n", " ") for cell in row]
        lines.append("\t".join(cells))
    return "\n".join(lines) + ("\n" if lines else "")


def get_nested_value(item: dict, path: str) -> object:
    value: object = item
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part, "")
        else:
            return ""
    return "" if value is None else value


def format_api_value(value: object) -> str:
    text = str(value)
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})T00:00:00", text)
    if match:
        return f"{match.group(3)}.{match.group(2)}.{match.group(1)}"
    return text


def get_payload_data(page_bytes: bytes) -> object:
    payload = json.loads(page_bytes.decode("utf-8", errors="replace"))
    return payload.get("data", payload) if isinstance(payload, dict) else payload


def summarize_altersklassen(items: object) -> tuple[str, str]:
    if not isinstance(items, list):
        return "", ""

    classes = []
    styles = []
    for item in items:
        if not isinstance(item, dict):
            continue
        class_name = item.get("bezeichnungKurz") or item.get("bezeichnung") or ""
        style = item.get("stilart") or ""
        if class_name:
            classes.append(str(class_name).strip())
        if style:
            styles.append(str(style).strip())

    unique_classes = sorted(dict.fromkeys(classes))
    unique_styles = sorted(dict.fromkeys(styles))
    return ", ".join(unique_classes), ", ".join(unique_styles)


def enrich_tournament(item: dict, config: dict, user_agent: str) -> dict:
    enriched = dict(item)
    tournament_id = get_nested_value(item, "id.value")
    if not tournament_id:
        return enriched

    detail_template = config.get("detail_api_url", "")
    if detail_template:
        detail_url = detail_template.format(id=tournament_id)
        try:
            detail_data = get_payload_data(fetch_page(detail_url, user_agent, config.get("verify_ssl", True)))
            enriched["altersklassen"], enriched["stilarten"] = summarize_altersklassen(detail_data)
        except Exception as exc:
            enriched["altersklassen"] = f"Fehler beim Laden: {exc}"
            enriched["stilarten"] = ""

    registration_template = config.get("registration_url_template", "")
    if registration_template:
        enriched["anmeldelink"] = registration_template.format(id=tournament_id)

    return enriched


def extract_api_rows(page_bytes: bytes, config: dict, user_agent: str) -> str:
    columns = config.get("columns", [])
    payload = json.loads(page_bytes.decode("utf-8", errors="replace"))
    items = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ValueError("API response must be a list or an object with a 'data' list")

    lines = ["\t".join(columns)]
    for item in items:
        if not isinstance(item, dict):
            continue
        item = enrich_tournament(item, config, user_agent)
        cells = []
        for column in columns:
            value = format_api_value(get_nested_value(item, column))
            cells.append(value.replace("\\", "\\\\").replace("\t", " ").replace("\n", " "))
        lines.append("\t".join(cells))
    return "\n".join(lines) + "\n"


def apply_ignores(text: str, patterns: list[str]) -> str:
    result = text
    for pattern in patterns:
        result = re.sub(pattern, "", result, flags=re.MULTILINE)
    return "\n".join(line for line in result.splitlines() if line.strip()) + "\n"


def latest_snapshot(snapshot_dir: Path, extension: str) -> Path | None:
    snapshots = sorted(snapshot_dir.glob(f"*.{extension}"))
    return snapshots[-1] if snapshots else None


def prune_old_files(directory: Path, pattern: str, keep: int) -> list[Path]:
    if keep < 1:
        raise ValueError("retention_count must be at least 1")
    files = sorted(path for path in directory.glob(pattern) if path.is_file())
    removed = files[:-keep]
    for path in removed:
        path.unlink()
    return removed


def write_report(previous: str, current: str, report_path: Path, title: str, url: str) -> tuple[int, int]:
    diff = list(difflib.ndiff(previous.splitlines(), current.splitlines()))
    added = sum(1 for line in diff if line.startswith("+ "))
    removed = sum(1 for line in diff if line.startswith("- "))

    rows = []
    for line in diff:
        marker, content = line[:2], html.escape(line[2:])
        if marker == "+ ":
            rows.append(f'<div class="line added"><span>+</span>{content}</div>')
        elif marker == "- ":
            rows.append(f'<div class="line removed"><span>-</span>{content}</div>')
        elif marker == "  ":
            rows.append(f'<div class="line same"><span></span>{content}</div>')

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report = f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} - Website Watch</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 32px; color: #1f2933; }}
    header {{ margin-bottom: 24px; }}
    a {{ color: #005a9c; }}
    .summary {{ display: flex; gap: 12px; margin: 16px 0; }}
    .pill {{ border: 1px solid #d8dde5; border-radius: 6px; padding: 8px 10px; }}
    .diff {{ border: 1px solid #d8dde5; border-radius: 6px; overflow: hidden; }}
    .line {{ display: grid; grid-template-columns: 32px 1fr; gap: 8px; padding: 4px 10px; white-space: pre-wrap; }}
    .line span {{ color: #697386; user-select: none; }}
    .added {{ background: #e8f7ee; }}
    .removed {{ background: #fdeaea; text-decoration: line-through; }}
    .same {{ background: #ffffff; }}
  </style>
</head>
<body>
  <header>
    <h1>{html.escape(title)}</h1>
    <p><a href="{html.escape(url)}">{html.escape(url)}</a></p>
    <p>Geprueft am {html.escape(now)}</p>
    <div class="summary">
      <div class="pill">Neu: {added}</div>
      <div class="pill">Entfernt: {removed}</div>
    </div>
  </header>
  <main class="diff">
    {''.join(rows)}
  </main>
</body>
</html>
"""
    report_path.write_text(report, encoding="utf-8")
    return added, removed


def split_table_rows(snapshot: str) -> list[list[str]]:
    return [line.split("\t") for line in snapshot.splitlines() if line.strip()]


def format_table_rows(rows: list[list[str]], status: str) -> str:
    if not rows:
        return '<p class="empty">Keine Eintraege.</p>'
    column_count = max(len(row) for row in rows)
    header = rows[0] if status == "headered" else []
    data_rows = rows[1:] if status == "headered" else rows
    body = []
    for row in data_rows:
        cells = row + [""] * (column_count - len(row))
        body.append(
            f'<tr class="{status}">'
            + "".join(f"<td>{format_cell(cell)}</td>" for cell in cells)
            + "</tr>"
        )
    head = ""
    if header:
        head = "<thead><tr>" + "".join(f"<th>{html.escape(cell)}</th>" for cell in header) + "</tr></thead>"
    return f"<table>{head}<tbody>{''.join(body)}</tbody></table>"


def format_cell(cell: str) -> str:
    escaped = html.escape(cell)
    if cell.startswith(("http://", "https://")):
        return f'<a href="{escaped}">{escaped}</a>'
    return escaped


def write_table_report(previous: str, current: str, report_path: Path, title: str, url: str) -> tuple[int, int]:
    previous_lines = previous.splitlines()
    current_lines = current.splitlines()
    header = current_lines[0] if current_lines else ""
    previous_data_lines = previous_lines[1:] if previous_lines and previous_lines[0] == header else previous_lines
    current_data_lines = current_lines[1:] if current_lines and current_lines[0] == header else current_lines
    previous_set = set(previous_data_lines)
    current_set = set(current_data_lines)
    added_lines = [line for line in current_data_lines if line not in previous_set]
    removed_lines = [line for line in previous_data_lines if line not in current_set]

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report = f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} - Tabellen-Aenderungen</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 32px; color: #1f2933; }}
    header {{ margin-bottom: 24px; }}
    a {{ color: #005a9c; }}
    .summary {{ display: flex; flex-wrap: wrap; gap: 12px; margin: 16px 0; }}
    .pill {{ border: 1px solid #d8dde5; border-radius: 6px; padding: 8px 10px; }}
    section {{ margin-top: 28px; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
    th, td {{ border: 1px solid #d8dde5; padding: 8px 10px; vertical-align: top; text-align: left; }}
    th {{ background: #f4f6f8; font-weight: 700; }}
    tr.added td {{ background: #e8f7ee; }}
    tr.removed td {{ background: #fdeaea; text-decoration: line-through; }}
    .empty {{ color: #697386; }}
  </style>
</head>
<body>
  <header>
    <h1>{html.escape(title)}</h1>
    <p><a href="{html.escape(url)}">{html.escape(url)}</a></p>
    <p>Geprueft am {html.escape(now)}</p>
    <div class="summary">
      <div class="pill">Neue Tabellenzeilen: {len(added_lines)}</div>
      <div class="pill">Entfernte Tabellenzeilen: {len(removed_lines)}</div>
    </div>
  </header>
  <section>
    <h2>Neue Zeilen</h2>
    {format_table_rows(split_table_rows(chr(10).join([header] + added_lines)), "headered").replace('class="headered"', 'class="added"') if added_lines and header else format_table_rows(split_table_rows(chr(10).join(added_lines)), "added")}
  </section>
  <section>
    <h2>Entfernte Zeilen</h2>
    {format_table_rows(split_table_rows(chr(10).join([header] + removed_lines)), "headered").replace('class="headered"', 'class="removed"') if removed_lines and header else format_table_rows(split_table_rows(chr(10).join(removed_lines)), "removed")}
  </section>
</body>
</html>
"""
    report_path.write_text(report, encoding="utf-8")
    return len(added_lines), len(removed_lines)


def get_env_or_value(config: dict, key: str, default: str = "") -> str:
    env_key = config.get(f"{key}_env")
    if env_key:
        return os.environ.get(env_key, default)
    return config.get(key, default)


def send_email_report(config: dict, report_path: Path, added: int, removed: int) -> None:
    email_config = config.get("email", {})
    if not email_config.get("enabled", False):
        return
    if email_config.get("send_only_on_changes", True) and not (added or removed):
        return

    smtp_host = get_env_or_value(email_config, "smtp_host")
    smtp_port = int(get_env_or_value(email_config, "smtp_port", "587"))
    smtp_user = get_env_or_value(email_config, "smtp_user")
    smtp_password = get_env_or_value(email_config, "smtp_password")
    sender = get_env_or_value(email_config, "from", smtp_user)
    recipients_env = email_config.get("to_env")
    recipients = os.environ.get(recipients_env, "") if recipients_env else email_config.get("to", [])
    if isinstance(recipients, str):
        recipients = [part.strip() for part in recipients.split(",")]
    recipients = [recipient for recipient in recipients if recipient]

    if not smtp_host or not sender or not recipients:
        raise ValueError("email config must include smtp_host, from and to")

    subject = email_config.get("subject", "RingerDB Watcher: Aenderungen gefunden")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Date"] = email.utils.formatdate(localtime=True)
    message["Subject"] = f"{subject} (+{added} -{removed})"
    html_report = report_path.read_text(encoding="utf-8")
    message.set_content(
        f"RingerDB Watcher hat Aenderungen gefunden: +{added} -{removed}\n\n"
        f"Der HTML-Report ist als Anhang enthalten.\n"
    )
    message.add_alternative(html_report, subtype="html")
    message.add_attachment(
        html_report.encode("utf-8"),
        maintype="text",
        subtype="html",
        filename=report_path.name,
    )

    smtp_ssl = email_config.get("ssl", False)
    if smtp_ssl:
        smtp = smtplib.SMTP_SSL(
            smtp_host,
            smtp_port,
            timeout=30,
            context=ssl.create_default_context(),
        )
    else:
        smtp = smtplib.SMTP(smtp_host, smtp_port, timeout=30)

    with smtp:
        if email_config.get("starttls", True) and not smtp_ssl:
            smtp.starttls(context=ssl.create_default_context())
        if smtp_user or smtp_password:
            smtp.login(smtp_user, smtp_password)
        smtp.send_message(message)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json", help="Path to JSON config")
    parser.add_argument("--fail-on-changes", action="store_true", help="Exit with code 1 when changes are found")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    base_dir = config_path.parent
    output_dir = base_dir / config.get("output_dir", "runs")
    snapshot_dir = output_dir / "snapshots"
    report_dir = output_dir / "reports"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    name = config.get("name", "website")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    user_agent = config.get("user_agent", "WebsiteWatch/1.0")
    compare_mode = config.get("compare", "text")
    page_bytes = fetch_page(config["url"], user_agent, config.get("verify_ssl", True))
    if compare_mode == "api":
        text = extract_api_rows(page_bytes, config, user_agent)
        snapshot_extension = "tsv"
    elif compare_mode == "tables":
        text = extract_tables(page_bytes)
        snapshot_extension = "tsv"
    else:
        text = extract_text(page_bytes)
        snapshot_extension = "txt"
    text = apply_ignores(text, config.get("ignore_patterns", []))

    current_snapshot = snapshot_dir / f"{timestamp}-{name}.{snapshot_extension}"
    previous_snapshot = latest_snapshot(snapshot_dir, snapshot_extension)
    current_snapshot.write_text(text, encoding="utf-8")
    retention_count = int(config.get("retention_count", 2))

    if previous_snapshot is None:
        prune_old_files(snapshot_dir, f"*.{snapshot_extension}", retention_count)
        print(f"First snapshot saved: {current_snapshot}")
        return 0

    previous = previous_snapshot.read_text(encoding="utf-8")
    report_path = report_dir / f"{timestamp}-{name}.html"
    if compare_mode in {"api", "tables"}:
        added, removed = write_table_report(previous, text, report_path, name, config.get("page_url", config["url"]))
    else:
        added, removed = write_report(previous, text, report_path, name, config.get("page_url", config["url"]))
    print(f"Snapshot saved: {current_snapshot}")
    print(f"Report written: {report_path}")
    print(f"Changes: +{added} -{removed}")
    removed_snapshots = prune_old_files(snapshot_dir, f"*.{snapshot_extension}", retention_count)
    removed_reports = prune_old_files(report_dir, "*.html", retention_count)
    if removed_snapshots or removed_reports:
        print(
            f"Retention cleanup: {len(removed_snapshots)} snapshot(s), "
            f"{len(removed_reports)} report(s) removed"
        )
    send_email_report(config, report_path, added, removed)
    return 1 if args.fail_on_changes and (added or removed) else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"website_watch failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
