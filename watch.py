#!/usr/bin/env python3
"""
Woven City 予約情報 監視スクリプト

公式ページをレンダリングし、前回実行時と比較して
「予約に関係のある文章が増えたとき」だけ通知する。

■ なぜこの方式か（2026-09 の誤検知3件を受けた設計）

  当初は本文の追加行をそのまま通知していたが、クッキー同意バナーが
  読み込みタイミングで出たり消えたりして誤検知が続いた。
  英語版・日本語版・カリフォルニア州向けパネルの3種類が確認され、
  文言を1つずつ除外していく方式は追いつかないと判断した。

  一方、キーワードの新規出現だけで判定する方式も弱い。
  「当日現地予約」「10月」は既にページに載っているため、
  告知が出ても新規出現にならず取り逃がす。

  そこで、追加行のうち「話題語」を含む行だけを通知対象とする。
  バナー類は予約・受付・訪問といった語を一切含まないため確実に落ち、
  本物の告知はこれらの語を必ず含むため確実に拾える。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

# 追加行がこれらの語を1つでも含めば「意味のある変化」とみなす。
# バナー・同意パネル・フッター類はこれらを含まない。
TOPIC_WORDS = [
    "予約", "受付", "ツアー", "訪問", "ビジター", "入場", "定員",
    "先着", "申込", "見学", "来場", "募集", "開始", "整理券",
    "One Day", "Weavers",
]

TARGETS = [
    {
        "id": "visitors",
        "label": "Visitors（本命：予約方式の案内が出るページ）",
        "url": "https://www.woven-city.global/jpn/people/weavers/visitors/",
        "keywords": [
            "当日現地予約", "当日予約", "現地予約", "整理券",
            "予約方法", "受付開始", "先着", "定員", "満席", "受付を終了",
        ],
    },
    {
        "id": "top",
        "label": "公式トップ（News欄）",
        "url": "https://www.woven-city.global/jpn/",
        "keywords": [
            "One Day Weavers", "ビジター", "当日現地予約",
            "受付を開始", "10月", "静岡県在住",
        ],
    },
    {
        "id": "wbyt",
        "label": "Woven by Toyota ニュースリリース",
        "url": "https://woven.toyota/jp/our-latest/",
        "keywords": [
            "One Day Weavers", "ビジター", "訪問", "当日現地予約", "静岡県",
        ],
    },
]

STATE_DIR = Path("state")
ALERT_FILE = Path("ALERT.md")

RENDER_WAIT_MS = 8000
MIN_TEXT_LEN = 300
MAX_DIFF_LINES = 25
TRUNCATION_RATIO = 0.85

JST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"[ \t\u3000]+", " ", raw).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def is_topical(line: str) -> bool:
    return any(w in line for w in TOPIC_WORDS)


def added_lines(old: str, new: str) -> list[str]:
    old_set = set(old.splitlines())
    return [ln for ln in new.splitlines() if ln not in old_set]


# ---------------------------------------------------------------------------

def fetch_text(page, url: str) -> str:
    page.goto(url, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(RENDER_WAIT_MS)
    body = page.evaluate("() => document.body ? document.body.innerText : ''")
    return normalize(body or "")


def check_target(page, target: dict) -> dict:
    result = {
        "id": target["id"],
        "label": target["label"],
        "url": target["url"],
        "notify": False,
        "reasons": [],
        "added": [],
        "keywords_added": [],
        "error": None,
    }

    try:
        text = fetch_text(page, target["url"])
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    if len(text) < MIN_TEXT_LEN:
        result["error"] = (
            f"本文が{len(text)}文字しか取得できませんでした"
            "（レンダリング失敗またはページ構造の変更の可能性）"
        )
        return result

    kw_now = sorted({kw for kw in target["keywords"] if kw in text})
    state_path = STATE_DIR / f"{target['id']}.json"

    if not state_path.exists():
        result["reasons"].append("初回取得（基準を保存）")
        save_state(state_path, target["url"], text, kw_now)
        return result

    prev = json.loads(state_path.read_text(encoding="utf-8"))
    prev_text = prev.get("text", "")
    prev_kw = set(prev.get("keywords", []))

    now_n = len(text.splitlines())
    prev_n = len(prev_text.splitlines())
    if prev_n > 0 and now_n < prev_n * TRUNCATION_RATIO:
        result["reasons"].append(
            f"取得が不完全と判断（{prev_n}行 → {now_n}行）。保存も通知もしません。"
        )
        return result

    # 追加行のうち、話題語を含むものだけを対象にする
    all_added = added_lines(prev_text, text)
    topical = [ln for ln in all_added if is_topical(ln)]
    ignored = len(all_added) - len(topical)

    kw_added = sorted(set(kw_now) - prev_kw)
    result["keywords_added"] = kw_added

    if kw_added:
        result["notify"] = True
        result["reasons"].append(f"キーワード出現: {', '.join(kw_added)}")

    if topical:
        result["notify"] = True
        result["added"] = topical
        result["reasons"].append(f"予約関連の記述が{len(topical)}行追加")

    if ignored:
        result["reasons"].append(f"（無関係な変化{ignored}行は無視）")

    if text_hash(prev_text) != text_hash(text):
        save_state(state_path, target["url"], text, kw_now)

    return result


def save_state(path: Path, url: str, text: str, keywords: list[str]) -> None:
    path.write_text(
        json.dumps(
            {
                "url": url,
                "fetched_at": datetime.now(JST).isoformat(),
                "hash": text_hash(text),
                "lines": len(text.splitlines()),
                "keywords": keywords,
                "text": text,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------

def build_alert(results: list[dict]) -> str:
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    out = [f"検知時刻: {now}", ""]

    for r in results:
        if not (r["notify"] or r["error"]):
            continue

        out.append(f"## {r['label']}")
        out.append("")
        out.append(r["url"])
        out.append("")

        if r["error"]:
            out.append(f"**取得エラー** — {r['error']}")
            out.append("")
            continue

        for reason in r["reasons"]:
            out.append(f"- {reason}")
        out.append("")

        if r["keywords_added"]:
            out.append("**新たに出現した語**")
            out.append("")
            for kw in r["keywords_added"]:
                out.append(f"- `{kw}`")
            out.append("")

        if r["added"]:
            out.append("**追加された記述**")
            out.append("")
            out.append("```")
            for ln in r["added"][:MAX_DIFF_LINES]:
                out.append(ln)
            if len(r["added"]) > MAX_DIFF_LINES:
                out.append(f"... 他 {len(r['added']) - MAX_DIFF_LINES} 行")
            out.append("```")
            out.append("")

    out.append("---")
    out.append("")
    out.append("このIssueは自動生成です。確認したらCloseしてください。")
    return "\n".join(out)


def main() -> int:
    STATE_DIR.mkdir(exist_ok=True)
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            viewport={"width": 1280, "height": 1600},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        for target in TARGETS:
            print(f"[check] {target['id']} {target['url']}", flush=True)
            r = check_target(page, target)
            if r["error"]:
                status = "ERROR"
            elif r["notify"]:
                status = "NOTIFY"
            else:
                status = "same"
            print(f"[{status}] {target['id']} :: {'; '.join(r['reasons']) or '-'}",
                  flush=True)
            results.append(r)
        browser.close()

    notify = [r for r in results if r["notify"] or r["error"]]
    if notify:
        ALERT_FILE.write_text(build_alert(results), encoding="utf-8")
        print(f"[alert] {len(notify)}件の通知対象", flush=True)
    else:
        print("[alert] なし", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
