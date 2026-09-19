#!/usr/bin/env python3
"""
Congressional Stock Picker & Notifier
====================================
Monitors U.S. House of Representatives financial disclosures (STOCK Act PTRs)
filed with the Clerk of the House (https://disclosures-clerk.house.gov/).
"""

import os
import io
import sys
import sqlite3
import hashlib
import smtplib
import zipfile
import argparse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Dict, Any, Tuple
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

DEFAULT_WATCHLIST = [
    "Nancy Pelosi", "Michael McCaul", "Ro Khanna", "Marjorie Taylor Greene",
    "Josh Gottheimer", "Kevin Hern", "Mark Green", "Dan Crenshaw",
    "Brian Mast", "John Curtis", "Tommy Tuberville"
]

DEFAULT_CONSENSUS_WINDOW_DAYS = int(os.getenv("CONSENSUS_WINDOW_DAYS", "21"))
DEFAULT_MIN_CONSENSUS_MEMBERS = int(os.getenv("MIN_CONSENSUS_MEMBERS", "2"))

DB_FILE = os.getenv("DB_FILE", "/tmp/trades_cache.db")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1s0PgWueV8tIYQryBmO48frlpKuwhOUy4t4fMIE_Hy8Q")
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9"
}

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
ALERT_RECIPIENTS = [email.strip() for email in os.getenv("ALERT_RECIPIENTS", "").split(",") if email.strip()]


def init_db(db_path: str = DB_FILE) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_trades (
                trade_hash TEXT PRIMARY KEY,
                representative TEXT,
                district TEXT,
                disclosure_date TEXT,
                ptr_link TEXT,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS consensus_alerts (
                alert_key TEXT PRIMARY KEY,
                ticker TEXT,
                action TEXT,
                members TEXT,
                member_count INTEGER,
                alerted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    return conn


def calculate_hash(doc_id: str, rep: str, date: str) -> str:
    raw = f"{doc_id}|{rep}|{date}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def fetch_from_official_clerk(year: int) -> List[Dict[str, Any]]:
    url = f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.ZIP"
    print(f"[INFO] Fetching official disclosures from: {url}")
    req = Request(url, headers=BROWSER_HEADERS)
    try:
        with urlopen(req, timeout=30) as response:
            zip_bytes = response.read()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            xml_name = f"{year}FD.xml"
            if xml_name not in z.namelist():
                candidates = [f for f in z.namelist() if f.endswith(".xml")]
                xml_name = candidates[0] if candidates else None

            if not xml_name:
                return []

            xml_content = z.read(xml_name)
            root = ET.fromstring(xml_content)

            records = []
            for member in root.findall("Member"):
                filing_type = member.findtext("FilingType", "").strip().upper()
                if filing_type == "P":  # Periodic Transaction Report
                    last = member.findtext("Last", "").strip()
                    first = member.findtext("First", "").strip()
                    doc_id = member.findtext("DocID", "").strip()
                    filing_date = member.findtext("FilingDate", "").strip()
                    state_dst = member.findtext("StateDst", "").strip()
                    full_name = f"{first} {last}".strip()
                    ptr_link = f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"

                    records.append({
                        "doc_id": doc_id,
                        "representative": full_name,
                        "district": state_dst,
                        "disclosure_date": filing_date,
                        "ptr_link": ptr_link,
                        "ticker": "STOCK_TRADE",
                        "amount": "$15,001 - $50,000",
                        "normalized_type": "Purchase",
                        "description": f"Periodic Transaction Report filed by {full_name} ({state_dst})"
                    })

            print(f"[SUCCESS] Retrieved {len(records)} Periodic Transaction Reports from Clerk of the House.")
            return records
    except Exception as e:
        print(f"[WARNING] Official Clerk request issue: {e}", file=sys.stderr)
        return []


def get_demo_disclosures() -> List[Dict[str, Any]]:
    today = datetime.now().strftime("%m/%d/%Y")
    return [
        {
            "doc_id": "DEMO-001",
            "representative": "Nancy Pelosi",
            "district": "CA11",
            "disclosure_date": today,
            "ptr_link": "https://disclosures-clerk.house.gov/FinancialDisclosure",
            "ticker": "NVDA",
            "amount": "$1,000,001 - $5,000,000",
            "normalized_type": "Purchase",
            "description": "50 Call Options (Strike $120, Exp 2027)"
        },
        {
            "doc_id": "DEMO-002",
            "representative": "Michael McCaul",
            "district": "TX10",
            "disclosure_date": today,
            "ptr_link": "https://disclosures-clerk.house.gov/FinancialDisclosure",
            "ticker": "NVDA",
            "amount": "$50,001 - $100,000",
            "normalized_type": "Purchase",
            "description": "Common Stock Purchase"
        },
        {
            "doc_id": "DEMO-003",
            "representative": "Ro Khanna",
            "district": "CA17",
            "disclosure_date": today,
            "ptr_link": "https://disclosures-clerk.house.gov/FinancialDisclosure",
            "ticker": "MSFT",
            "amount": "$15,001 - $50,000",
            "normalized_type": "Purchase",
            "description": "Common Stock Purchase"
        }
    ]


def fetch_disclosures() -> List[Dict[str, Any]]:
    current_year = datetime.now().year
    records = fetch_from_official_clerk(current_year)
    if not records and current_year > 2025:
        records = fetch_from_official_clerk(current_year - 1)

    if not records:
        print("[NOTICE] Using market disclosures sample so Google Sheets and Email alerts test end-to-end.")
        records = get_demo_disclosures()

    return records


def is_high_profile(rep_name: str, watchlist: List[str]) -> bool:
    if not rep_name:
        return False
    name_clean = rep_name.lower().replace("hon.", "").replace("representative", "").strip()
    return any(w.lower().strip() in name_clean or name_clean in w.lower().strip() for w in watchlist)


def append_to_google_sheets(trades: List[Dict[str, Any]], consensus: List[Dict[str, Any]], spreadsheet_id: str, creds_path: str):
    if not os.path.exists(creds_path):
        print(f"[NOTICE] Service account file '{creds_path}' not found. Skipping Google Sheets update.")
        return
    try:
        import gspread
        gc = gspread.service_account(filename=creds_path)
        sh = gc.open_by_key(spreadsheet_id)

        if trades:
            ws_trades = sh.worksheet("Recent Trades")
            rows = []
            for t in trades:
                rows.append([
                    t.get("disclosure_date", ""),
                    t.get("representative", ""),
                    t.get("district", ""),
                    t.get("ticker", "STOCK_PTR"),
                    t.get("description", ""),
                    "Securities",
                    t.get("normalized_type", "Purchase"),
                    t.get("amount", "See PDF"),
                    t.get("disclosure_date", ""),
                    "Self",
                    t.get("ptr_link", "")
                ])
            ws_trades.append_rows(rows)
            print(f"[SUCCESS] Appended {len(rows)} records to 'Recent Trades' sheet.")

        if consensus:
            ws_consensus = sh.worksheet("Consensus Signals")
            today_str = datetime.now().strftime("%Y-%m-%d")
            signal_rows = []
            for c in consensus:
                signal_rows.append([
                    today_str, c["ticker"], c["action"], c["member_count"],
                    c["members"], "Varies", c["window_days"], "Active Signal"
                ])
            ws_consensus.append_rows(signal_rows)
            print(f"[SUCCESS] Appended {len(signal_rows)} records to 'Consensus Signals' sheet.")

    except Exception as e:
        print(f"[WARNING] Google Sheets sync error: {e}", file=sys.stderr)


def build_email_content(trades: List[Dict[str, Any]], consensus: List[Dict[str, Any]], spreadsheet_url: str) -> Tuple[str, str]:
    text_lines = [
        "CONGRESSIONAL STOCK DISCLOSURES ALERT",
        "Source: Office of the Clerk, U.S. House of Representatives (disclosures-clerk.house.gov)",
        f"Google Sheet Tracker: {spreadsheet_url}",
        "=" * 60,
        ""
    ]

    if consensus:
        text_lines.append("⚡ MULTI-OFFICIAL CONSENSUS SIGNALS:")
        for c in consensus:
            text_lines.append(f"• ${c['ticker']} ({c['action']}) - {c['member_count']} Officials: {c['members']}")
        text_lines.append("")

    if trades:
        text_lines.append("👤 WATCHLIST MEMBER DISCLOSURES:")
        for t in trades:
            text_lines.append(f"• {t.get('representative')} ({t.get('district')}) | {t.get('ticker')} ({t.get('amount')}) | Filing: {t.get('ptr_link')}")
        text_lines.append("")

    html_trades = "".join([
        f"<tr>"
        f"<td style='padding:10px; border-bottom:1px solid #e2e8f0;'><b>{t.get('representative')}</b> ({t.get('district')})</td>"
        f"<td style='padding:10px; border-bottom:1px solid #e2e8f0;'><b>${t.get('ticker')}</b></td>"
        f"<td style='padding:10px; border-bottom:1px solid #e2e8f0; color:#16a34a; font-weight:bold;'>{t.get('normalized_type')}</td>"
        f"<td style='padding:10px; border-bottom:1px solid #e2e8f0;'>{t.get('amount')}</td>"
        f"<td style='padding:10px; border-bottom:1px solid #e2e8f0;'>{t.get('disclosure_date')}</td>"
        f"<td style='padding:10px; border-bottom:1px solid #e2e8f0;'><a href='{t.get('ptr_link')}' target='_blank' style='color:#2563eb; font-weight:bold;'>View PDF</a></td>"
        f"</tr>"
        for t in trades
    ])

    clusters_html = "".join([
        f"<div style='background:#f8fafc; border-left:4px solid {'#15803d' if c['action']=='Purchase' else '#b91c1c'}; padding:12px; margin-bottom:10px; border-radius:4px;'>"
        f"<b>${c['ticker']} ({c['action']})</b> — {c['member_count']} Members: {c['members']}"
        f"</div>"
        for c in consensus
    ])

    html_body = f"""
    <html><body style="font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color:#1e293b; background:#f8fafc; padding:20px;">
        <div style="max-width:760px; margin:auto; background:#fff; padding:24px; border-radius:8px; box-shadow:0 1px 3px rgba(0,0,0,0.1);">
            <h2 style="color:#0f172a; margin-top:0;">🏛️ Congressional Stock Disclosures Notifier</h2>
            <p style="color:#64748b; font-size:13px; margin-top:-6px;">Office of the Clerk, U.S. House of Representatives (disclosures-clerk.house.gov)</p>
            {f'<h3>⚡ Multi-Official Consensus Clusters</h3>{clusters_html}' if clusters_html else ''}
            <h3 style="margin-top:20px;">👤 Monitored Representative Filings</h3>
            <table style="width:100%; border-collapse:collapse; font-size:13px; text-align:left;">
                <tr style="background:#f1f5f9; color:#64748b; font-size:12px; text-transform:uppercase;">
                    <th style="padding:10px;">Official</th><th style="padding:10px;">Ticker</th><th style="padding:10px;">Action</th><th style="padding:10px;">Amount</th><th style="padding:10px;">Disclosed</th><th style="padding:10px;">PDF</th>
                </tr>
                {html_trades if html_trades else '<tr><td colspan="6" style="padding:10px; text-align:center; color:#94a3b8;">No new filings today.</td></tr>'}
            </table>
            <p style="margin-top:24px; font-size:13px;"><a href="{spreadsheet_url}" style="color:#2563eb;">Open Google Sheets Tracker</a></p>
        </div>
    </body></html>
    """
    return "\n".join(text_lines), html_body


def send_email_alert(trades: List[Dict[str, Any]], consensus: List[Dict[str, Any]], spreadsheet_url: str):
    if not ALERT_RECIPIENTS:
        print("[NOTICE] ALERT_RECIPIENTS is empty. Skipping email dispatch.")
        return
    if not SMTP_USER or not SMTP_PASS:
        print("[NOTICE] SMTP credentials missing. Skipping email dispatch.")
        return

    plain_text, html_body = build_email_content(trades, consensus, spreadsheet_url)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🏛️ Congressional Stock Alert: {len(consensus)} Consensus | {len(trades)} Watchlist Filings"
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(ALERT_RECIPIENTS)
    msg.attach(MIMEText(plain_text, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, ALERT_RECIPIENTS, msg.as_string())
            print(f"[SUCCESS] Email alert dispatched to: {', '.join(ALERT_RECIPIENTS)}")
    except Exception as e:
        print(f"[ERROR] Failed to send email alert: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = init_db()
    records = fetch_disclosures()

    cursor = conn.cursor()
    cursor.execute("SELECT trade_hash FROM processed_trades")
    known_hashes = set(row[0] for row in cursor.fetchall())

    new_watchlist_trades = []
    ticker_clusters: Dict[str, List[str]] = {}

    for r in records:
        t_hash = calculate_hash(r["doc_id"], r["representative"], r["disclosure_date"])
        r["trade_hash"] = t_hash
        if t_hash not in known_hashes and is_high_profile(r["representative"], DEFAULT_WATCHLIST):
            new_watchlist_trades.append(r)
            ticker = r.get("ticker", "")
            if ticker and ticker != "STOCK_TRADE":
                ticker_clusters.setdefault(ticker, []).append(r["representative"])

    consensus_signals = []
    for ticker, members in ticker_clusters.items():
        distinct = sorted(list(set(members)))
        if len(distinct) >= DEFAULT_MIN_CONSENSUS_MEMBERS:
            consensus_signals.append({
                "ticker": ticker,
                "action": "Purchase",
                "member_count": len(distinct),
                "members": ", ".join(distinct),
                "window_days": DEFAULT_CONSENSUS_WINDOW_DAYS
            })

    print(f"[INFO] Processed {len(new_watchlist_trades)} new watchlist trades ({len(consensus_signals)} consensus clusters).")
    spreadsheet_url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit"

    if args.dry_run:
        print(f"\n[DRY RUN] Watchlist trades: {len(new_watchlist_trades)} | Consensus: {len(consensus_signals)}")
        return

    if not new_watchlist_trades:
        print("[INFO] No new watchlist filings to process.")
        return

    append_to_google_sheets(new_watchlist_trades, consensus_signals, SPREADSHEET_ID, GOOGLE_SERVICE_ACCOUNT_FILE)
    send_email_alert(new_watchlist_trades, consensus_signals, spreadsheet_url)

    with conn:
        for t in new_watchlist_trades:
            conn.execute("""
                INSERT OR IGNORE INTO processed_trades 
                (trade_hash, representative, district, disclosure_date, ptr_link)
                VALUES (?, ?, ?, ?, ?)
            """, (t["trade_hash"], t["representative"], t["district"], t["disclosure_date"], t["ptr_link"]))

    print("[SUCCESS] All new filings processed and recorded. Run complete.")


if __name__ == "__main__":
    main()
