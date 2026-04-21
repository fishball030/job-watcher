import json
import os
import re
import hashlib
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

SITES_FILE = "sites.json"
SEEN_FILE = "seen_jobs.json"

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()
TG_ERROR_CHAT_ID = os.getenv("TG_ERROR_CHAT_ID", "").strip()
FIRST_RUN_NOTIFY = os.getenv("FIRST_RUN_NOTIFY", "false").lower() == "true"
MAX_NOTIFY_ITEMS = int(os.getenv("MAX_NOTIFY_ITEMS", "10"))
USE_PLAYWRIGHT = os.getenv("USE_PLAYWRIGHT", "true").lower() == "true"


def now_str() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def load_sites() -> list[dict]:
    with open(SITES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list) or not data:
        raise RuntimeError("sites.json must be a non-empty list")
    return data


def load_seen_by_site(site_ids: list[str]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {site_id: set() for site_id in site_ids}
    if not os.path.exists(SEEN_FILE):
        return result
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return result

    # Backward compatible with old format: list of IDs (single site)
    if isinstance(data, list):
        if site_ids:
            result[site_ids[0]] = set(str(x) for x in data)
        return result

    if isinstance(data, dict):
        for site_id in site_ids:
            values = data.get(site_id, [])
            if isinstance(values, list):
                result[site_id] = set(str(v) for v in values)
    return result


def save_seen_by_site(seen_by_site: dict[str, set[str]]) -> None:
    output = {site_id: sorted(list(values)) for site_id, values in seen_by_site.items()}
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


def tg_send_message(text: str, chat_id: str | None = None) -> None:
    if not TG_BOT_TOKEN:
        raise RuntimeError("Missing TG_BOT_TOKEN")
    target_chat_id = (chat_id or TG_CHAT_ID).strip()
    if not target_chat_id:
        raise RuntimeError("Missing TG_CHAT_ID")

    api = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        api,
        data={"chat_id": target_chat_id, "text": text, "disable_web_page_preview": True},
        timeout=30,
    )
    resp.raise_for_status()


def same_domain(url: str, allowed_domain: str | None) -> bool:
    if not allowed_domain:
        return True
    host = (urlparse(url).hostname or "").lower()
    allowed = allowed_domain.lower()
    return host == allowed or host.endswith(f".{allowed}")


def collect_jobs_from_anchors(site: dict, anchors: list[tuple[str, str]]) -> list[dict]:
    include_keywords = [k.lower() for k in site.get("include_keywords", [])]
    exclude_keywords = [k.lower() for k in site.get("exclude_keywords", [])]
    include_match = (site.get("include_match") or "all").lower()
    domain = site.get("domain")
    jobs: dict[str, dict] = {}
    base_url = site["url"]

    for href, title in anchors:
        href = normalize_text(href)
        title = normalize_text(title)
        if not title:
            continue

        full_url = urljoin(base_url, href) if href else base_url
        if href and not same_domain(full_url, domain):
            continue

        text_blob = f"{title} {full_url}".lower()
        if include_keywords:
            if include_match == "any":
                if not any(k in text_blob for k in include_keywords):
                    continue
            else:
                if not all(k in text_blob for k in include_keywords):
                    continue
        if exclude_keywords and any(k in text_blob for k in exclude_keywords):
            continue

        job_id = full_url if href else f"{site['id']}:{hashlib.sha1(title.encode('utf-8')).hexdigest()[:16]}"
        jobs[job_id] = {"id": job_id, "title": title, "url": full_url, "site_name": site["name"]}

    return list(jobs.values())


def parse_jobs_with_requests(site: dict) -> list[dict]:
    url = site["url"]
    headers = {
        "User-Agent": "Mozilla/5.0 (JobWatcherBot/2.0; +https://github.com/)",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    resp = requests.get(url, headers=headers, timeout=45)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    anchors = []
    for a in soup.select("a[href]"):
        title = a.get_text(" ", strip=True) or a.get("title", "") or a.get("aria-label", "")
        container = a.parent.get_text(" ", strip=True) if a.parent else ""
        merged_title = normalize_text(f"{title} {container}")
        anchors.append((a.get("href", ""), merged_title))
    return collect_jobs_from_anchors(site, anchors)


def parse_jobs_with_playwright(site: dict) -> list[dict]:
    url = site["url"]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            # Allow JS-heavy job boards to render list content.
            page.wait_for_timeout(6000)
            raw_items = page.eval_on_selector_all(
                "a, [role='link'], button",
                """els => els.map(e => {
                    const txt = (e.innerText || e.textContent || e.getAttribute('aria-label') || '').trim();
                    const href = e.getAttribute('href') || e.getAttribute('data-href') || '';
                    const parentTxt = (e.parentElement && (e.parentElement.innerText || e.parentElement.textContent) || '').trim();
                    const onclick = e.getAttribute('onclick') || '';
                    return { txt, href, parentTxt, onclick };
                })""",
            )
        except PlaywrightTimeoutError:
            raw_items = []
        finally:
            browser.close()
    anchors: list[tuple[str, str]] = []
    for item in raw_items:
        href = normalize_text(item.get("href", ""))
        title = normalize_text(f"{item.get('txt', '')} {item.get('parentTxt', '')}")
        onclick = item.get("onclick", "")
        if not href and "http" in onclick:
            m = re.search(r"https?://[^'\\\"]+", onclick)
            if m:
                href = m.group(0)
        if title:
            anchors.append((href, title))
    return collect_jobs_from_anchors(site, anchors)


def parse_jobs_for_site(site: dict) -> list[dict]:
    if USE_PLAYWRIGHT:
        try:
            jobs = parse_jobs_with_playwright(site)
            if jobs:
                return jobs
        except Exception:
            # Fall back to plain HTTP parsing if browser rendering fails.
            pass
    return parse_jobs_with_requests(site)


def build_new_jobs_message(all_new_jobs: list[dict]) -> str:
    total = len(all_new_jobs)
    lines = [f"Job Watcher 發現 {total} 個新職位", f"檢查時間: {now_str()}", ""]

    for idx, job in enumerate(all_new_jobs[:MAX_NOTIFY_ITEMS], start=1):
        lines.append(f"{idx}. [{job['site_name']}] {job['title']}")
        lines.append(job["url"])
        lines.append("")

    remaining = total - MAX_NOTIFY_ITEMS
    if remaining > 0:
        lines.append(f"...另外仲有 {remaining} 個新職位未顯示")
    return "\n".join(lines).strip()


def build_baseline_message(summary: list[tuple[str, int]]) -> str:
    lines = ["Job Watcher 已完成首次 baseline", f"時間: {now_str()}", ""]
    for site_name, count in summary:
        lines.append(f"- {site_name}: 記錄 {count} 個")
    lines.append("")
    lines.append("之後有新職位先會通知。")
    return "\n".join(lines)


def build_error_message(err: Exception) -> str:
    return f"Job Watcher 執行失敗\n時間: {now_str()}\n錯誤: {type(err).__name__}: {err}"


def main() -> None:
    sites = load_sites()
    site_ids = [s["id"] for s in sites]
    seen_by_site = load_seen_by_site(site_ids)

    fetched_by_site: dict[str, list[dict]] = {}
    current_ids_by_site: dict[str, set[str]] = {}
    all_new_jobs: list[dict] = []

    for site in sites:
        site_id = site["id"]
        jobs = parse_jobs_for_site(site)
        fetched_by_site[site_id] = jobs
        current_ids = {j["id"] for j in jobs}
        current_ids_by_site[site_id] = current_ids

        previous = seen_by_site.get(site_id, set())
        new_jobs = [j for j in jobs if j["id"] not in previous]
        all_new_jobs.extend(new_jobs)

    is_first_run = all(len(v) == 0 for v in seen_by_site.values())
    if is_first_run:
        for site_id, ids in current_ids_by_site.items():
            seen_by_site[site_id] = ids
        save_seen_by_site(seen_by_site)
        if FIRST_RUN_NOTIFY:
            summary = [(site["name"], len(current_ids_by_site[site["id"]])) for site in sites]
            tg_send_message(build_baseline_message(summary))
        return

    if all_new_jobs:
        tg_send_message(build_new_jobs_message(all_new_jobs))

    for site_id, ids in current_ids_by_site.items():
        seen_by_site[site_id] = ids
    save_seen_by_site(seen_by_site)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            target = TG_ERROR_CHAT_ID or TG_CHAT_ID
            if TG_BOT_TOKEN and target:
                tg_send_message(build_error_message(e), chat_id=target)
        finally:
            raise
