#!/usr/bin/env python3
"""
Congressional Stock Picker & Notifier
====================================
Monitors U.S. House of Representatives financial disclosures (STOCK Act PTRs)
directly from the Clerk of the House (https://disclosures-clerk.house.gov/).
"""

import os
import io
import sys
import json
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

# Configuration & Settings
DEFAULT_WATCHLIST = [
    "Nancy Pelosi", "Michael McCaul", "Ro Khanna", "Marjorie Taylor Greene",
    "Josh Gottheimer", "Kevin Hern", "Mark Green", "Dan Crenshaw",
    "Brian Mast", "John Curtis", "Tommy Tuberville"
]

DEFAULT_CONSENSUS_WINDOW_DAYS = int(os.getenv("CONSENSUS_WINDOW_DAYS", "21"))
DEFAULT_MIN_CONSENSUS_MEMBERS = int(os.getenv("MIN_CONSENSUS_MEMBERS", "2"))

DB_FILE = os.getenv("DB_FILE", "trades_cache.db")
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
                ticker TEXT,
                transaction_type TEXT,
                amount TEXT,
                transaction_date TEXT,
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


def calculate_trade_hash(trade: Dict[str, Any]) -> str:
    rep = (trade.get("representative") or "").strip().lower()
    ticker = (trade.get("ticker") or "").strip().upper()
    t_date = (trade.get("transaction_date") or "").strip()
    d_date = (trade.get("disclosure_date") or "").strip()
    amount = (trade.get("amount") or "").strip()
    tx_type = (trade.get("type") or "").strip().lower()
    ptr = (trade.get("ptr_link") or "").strip()
    raw_key = f"{rep}|{ticker}|{t_date}|{d_date}|{amount}|{tx_type}|{ptr}"
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def fetch_from_official_clerk(year: int) -> List[Dict[str, Any]]:
    url = f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.ZIP"
    print(f"[INFO] Fetching official disclosures archive from: {url}")
    req = Request(url, headers=BROWSER_HEADERS)
    try:
        with urlopen(req, timeout=30) as response:
            zip_data = response.read()

        with zipfile.ZipFile(io.BytesIO(zip_data)) as z:
            xml_name = f"{year}FD.xml"
            if xml_name not in z.namelist():
                candidates = [f for f in z.namelist() if f.endswith(".xml")]
                xml_name = candidates[0] if candidates else None

            if not xml_name:
                print(f"[WARNING] No XML index found in {year}FD.ZIP", file=sys.stderr)
                return []

            xml_bytes = z.read(xml_name)
            root = ET.fromstring(xml_bytes)

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
                        "representative": full_name,
                        "district": state_dst,
                        "ticker": "STOCK_TRADE",
                        "asset_description": f"Periodic Transaction Report filed by {full_name} ({state_dst})",
                        "asset_type": "Securities",
                        "type": "Stock Transaction",
                        "amount": "Disclosed in PDF",
                        "transaction_date": filing_date,
                        "disclosure_date": filing_date,
                        "ptr_link": ptr_link,
                        "doc_id": doc_id
                    })

            print(f"[SUCCESS] Retrieved {len(records)} Periodic Transaction Reports directly from Clerk of the House.")
            return records

    except Exception as e:
        print(f"[WARNING] Could not fetch year {year} ({e}).", file=sys.stderr)
        return []


def fetch_disclosures() -> List[Dict[str, Any]]:
    current_year = datetime.now().year
    records = fetch_from_official_clerk(current_year)
    if not records and current_year > 2025:
        print(f"[INFO] Trying previous year ({current_year - 1})...")
        records = fetch_from_official_clerk(current_year - 1)
    return records


def parse_date(date_str: str) -> datetime:
    if not date_str or date_str in ["--", "N/A"]:
        return datetime.min
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            pass
    return datetime.min


def is_high_profile(rep_name: str, watchlist: List[str]) -> bool:
    if not rep_name:
        return False
    name_clean = rep_name.lower().replace("hon.", "").replace("representative", "").strip()
    return any(w.lower().strip() in name_clean or name_clean in w.lower().strip() for w in watchlist)


def process_signals(
    trades: List[Dict[str, Any]],
    conn: sqlite3.Connection,
    watchlist: List[str]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    cursor = conn.cursor()
    cursor.execute("SELECT trade_hash FROM processed_trades")
    known_hashes = set(row[0] for row in cursor.fetchall())

    new_trades = []
    unseen_high_profile_trades = []

    trades.sort(key=lambda x: parse_date(x.get("disclosure_date", "")), reverse=True)

    for item in trades:
        t_hash = calculate_trade_hash(item)
        if t_hash not in known_hashes:
            item["trade_hash"] = t_hash
            item["normalized_type"] = item.get("type", "Stock Transaction")
            new_trades.append(item)

            rep = item.get("representative", "")
            if is_high_profile(rep, watchlist):
                unseen_high_profile_trades.append(item)

    print(f"[INFO] Found {len(new_trades)} total filings ({len(unseen_high_profile_trades)} matching watchlist).")
    return unseen_high_profile_trades, []


def append_to_google_sheets(
    high_profile_trades: List[Dict[str, Any]],
    consensus_signals: List[Dict[str, Any]],
    spreadsheet_id: str = SPREADSHEET_ID,
    creds_path: str = GOOGLE_SERVICE_ACCOUNT_FILE
):
    if not os.path.exists(creds_path):
        print(f"[NOTICE] Service account file '{creds_path}' not found. Skipping Google Sheets update.")
        return

    try:
        import gspread
        gc = gspread.service_account(filename=creds_path)
        sh = gc.open_by_key(spreadsheet_id)

        if high_profile_trades:
            ws_trades = sh.worksheet("Recent Trades")
            rows = []
            for t in high_profile_trades:
                rows.append([
                    t.get("disclosure_date", ""),
                    t.get("representative", ""),
                    t.get("district", ""),
                    t.get("ticker", ""),
                    t.get("asset_description", ""),
                    t.get("asset_type", "Stock"),
                    t.get("normalized_type", ""),
                    t.get("amount", ""),
                    t.get("transaction_date", ""),
                    t.get("owner", "Self"),
                    t.get("ptr_link", "")
                ])
            ws_trades.append_rows(rows)
            print(f"[SUCCESS] Appended {len(rows)} records to 'Recent Trades' sheet.")

    except Exception as e:
        print(f"[WARNING] Google Sheets sync error: {e}", file=sys.stderr)


def build_email_content(
    trades: List[Dict[str, Any]],
    consensus: List[Dict[str, Any]],
    spreadsheet_url: str
) -> Tuple[str, str]:
    text_lines = [
        "CONGRESSIONAL STOCK DISCLOSURES ALERT",
        "Source: Office of the Clerk, U.S. House of Representatives (disclosures-clerk.house.gov)",
        f"Google Sheet Tracker: {spreadsheet_url}",
        "=" * 60,
        ""
    ]
    for t in trades:
        rep = t.get("representative", "Unknown")
        d_date = t.get("disclosure_date", "N/A")
        ptr = t.get("ptr_link", "")
        text_lines.append(f"• {rep} ({t.get('district')}) | Disclosed: {d_date} | PDF: {ptr}")

    html_trades = "".join([
        f"<tr>"
        f"<td style='padding:10px; border-bottom:1px solid #e2e8f0;'><b>{t.get('representative')}</b> ({t.get('district')})</td>"
        f"<td style='padding:10px; border-bottom:1px solid #e2e8f0;'>{t.get('disclosure_date')}</td>"
        f"<td style='padding:10px; border-bottom:1px solid #e2e8f0;'>{t.get('asset_description')}</td>"
        f"<td style='padding:10px; border-bottom:1px solid #e2e8f0;'><a href='{t.get('ptr_link', '#')}' target='_blank' style='color:#2563eb; font-weight:bold;'>View Official PDF</a></td>"
        f"</tr>"
        for t in trades
    ])

    html_body = f"""
    <html><body style="font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color:#1e293b; background:#f8fafc; padding:20px;">
        <div style="max-width:760px; margin:auto; background:#fff; padding:24px; border-radius:8px; box-shadow:0 1px 3px rgba(0,0,0,0.1);">
            <h2 style="color:#0f172a; margin-top:0;">🏛️ Congressional Stock Disclosures Notifier</h2>
            <p style="color:#64748b; font-size:13px; margin-top:-6px;">Office of the Clerk, U.S. House of Representatives (disclosures-clerk.house.gov)</p>
            <h3 style="margin-top:20px;">👤 Monitored Representative Disclosures</h3>
            <table style="width:100%; border-collapse:collapse; font-size:13px; text-align:left;">
                <tr style="background:#f1f5f9; color:#64748b; font-size:12px; text-transform:uppercase;">
                    <th style="padding:10px;">Official</th><th style="padding:10px;">Filing Date</th><th style="padding:10px;">Description</th><th style="padding:10px;">Filing Link</th>
                </tr>
                {html_trades if html_trades else '<tr><td colspan="4" style="padding:10px; text-align:center; color:#94a3b8;">No new watchlist trades in this run.</td></tr>'}
            </table>
            <p style="margin-top:24px; font-size:13px;"><a href="{spreadsheet_url}" style="color:#2563eb;">Open Google Sheets Tracker</a></p>
        </div>
    </body></html>
    """
    return "\n".join(text_lines), html_body


def send_email_alert(
    trades: List[Dict[str, Any]],
    consensus: List[Dict[str, Any]],
    spreadsheet_url: str = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit",
    recipients: List[str] = ALERT_RECIPIENTS
):
    if not recipients:
        print("[NOTICE] ALERT_RECIPIENTS is empty. Skipping email dispatch.")
        return
    if not SMTP_USER or not SMTP_PASS:
        print("[NOTICE] SMTP credentials missing. Skipping email dispatch.")
        return

    plain_text, html_body = build_email_content(trades, consensus, spreadsheet_url)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🏛️ Congressional Stock Alert: {len(trades)} Watchlist Filings"
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(plain_text, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, recipients, msg.as_string())
            print(f"[SUCCESS] Email alert dispatched to: {', '.join(recipients)}")
    except Exception as e:
        print(f"[ERROR] Failed to send email alert: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = init_db()
    records = fetch_disclosures()
    if not records:
        print("[INFO] No records retrieved from Clerk of the House. Exiting.")
        return

    high_profile_trades, consensus_signals = process_signals(records, conn, DEFAULT_WATCHLIST)
    spreadsheet_url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit"

    if args.dry_run:
        print(f"\n[DRY RUN] Watchlist filings: {len(high_profile_trades)}")
        for t in high_profile_trades[:5]:
            print(f"  - {t['representative']} ({t['district']}): {t['disclosure_date']} -> {t['ptr_link']}")
        return

    if not high_profile_trades and not consensus_signals:
        print("[INFO] No new actionable signals detected in this run.")
        return

    append_to_google_sheets(high_profile_trades, consensus_signals, SPREADSHEET_ID)
    send_email_alert(high_profile_trades, consensus_signals, spreadsheet_url)

    with conn:
        for t in high_profile_trades:
            conn.execute("""
                INSERT OR IGNORE INTO processed_trades 
                (trade_hash, representative, ticker, transaction_type, amount, transaction_date, disclosure_date, ptr_link)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                t["trade_hash"], t.get("representative"), t.get("ticker"),
                t.get("normalized_type"), t.get("amount"), t.get("transaction_date"),
                t.get("disclosure_date"), t.get("ptr_link")
            ))

    print("[SUCCESS] All new signals processed and cached. Run complete.")


if __name__ == "__main__":
    main()
