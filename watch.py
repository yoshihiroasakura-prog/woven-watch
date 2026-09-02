#!/usr/bin/env python3
"""
Woven City 予約情報 監視スクリプト

公式ページをヘッドレスブラウザでレンダリングし、
前回実行時の状態と比較する。

設計方針（2026-09 の誤検知を受けて改訂）:
  このサイトはJavaScript描画のため、実行のたびに取得できる行数が揺れる。
  特にクッキー同意バナーが出たり消えたりして誤検知の原因になったため、
  比較前に除去する。
  また「行が減っただけ」の変化は取得漏れの疑いが濃く、告知ではない。
  9月下旬の告知は必ず文章が増える形で来るので、
    ・追加行があるとき
    ・注目キーワードが出現/消滅したとき
  のみ通知し、削除だけの変化は無視する。
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

TARGETS = [
    {
        "id": "visitors",
        "label": "Visitors（本命：予約方式の案内が出るページ）",
        "url": "https://www.woven-city.global/jpn/people/weavers/visitors/",
        "keywords": [
            "当日現地予約",
            "当日予約",
            "10月1日",
            "10月",
            "予約受付",
            "受付開始",
            "先着",
            "定員",
            "満席",
            "受付を終了",
        ],
    },
    {
        "id": "top",
        "label": "公式トップ（News欄）",
        "url": "https://www.woven-city.global/jpn/",
        "keywords": [
            "One Day Weavers",
            "ビジター",
            "受付",
            "予約",
            "10月",
            "静岡県在住",
        ],
    },
    {
        "id": "wbyt",
        "label": "Woven by Toyota ニュースリリース",
        "url": "https://woven.toyota/jp/our-latest/",
        "keywords": [
            "One Day Weavers",
            "ビジター",
            "訪問",
            "予約",
            "静岡県",
        ],
    },
]

STATE_DIR = Path("state")
ALERT_FILE = Path("ALERT.md")

# JS描画待ち。4秒では足りず取得が揺れたため延長。
RENDER_WAIT_MS = 8000
MIN_TEXT_LEN = 300
MAX_DIFF_LINES = 25

# 前回より行数がこの割合を下回ったら「取得が不完全」とみなし保存しない
TRUNCATION_RATIO = 0.85

# クッキー同意バナーなど、読み込みタイミングで出たり消えたりする要素。
# 告知とは無関係なので比較対象から外す。
NOISE_PATTERNS = [
    re.compile(r"website cookies", re.I),
    re.compile(r"Cookies are used to give you", re.I),
    re.compile(r"^Show details$", re.I),
    re.compile(r"^Accept all$", re.I),
    re.compile(r"^Customize$", re.I),
    re.compile(r"^Reject all$", re.I),
    re.compile(r"^クッキー", re.I),
    re.compile(r"すべて(受け入れる|拒否)"),
]

JST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"[ \t\u3000]+", " ", raw).strip()
        if not line:
            continue
        if any(p.search(line) for p in NOISE_PATTERNS):
            continue
        lines.append(line)
    return "\n".join(lines)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def added_lines(old: str, new: str) -> list[str]:
    old_set = set(old.splitlines())
    return [ln for ln in new.splitlines() if ln not in old_set]


def removed_lines(old: str, new: str) -> list[str]:
    new_set = set(new.splitlines())
    return [ln for ln in old.splitlines() if ln not in new_set]


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
        "keywords_removed": [],
        "error": None,
        "skipped_save": False,
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

    now_lines = len(text.splitlines())
    prev_lines = len(prev_text.splitlines())

    # 取得が明らかに不完全な回は、比較も保存もしない
    if prev_lines > 0 and now_lines < prev_lines * TRUNCATION_RATIO:
        result["skipped_save"] = True
        result["reasons"].append(
            f"取得が不完全と判断（{prev_lines}行 → {now_lines}行）。保存も通知もしません。"
        )
        return result

    add = added_lines(prev_text, text)
    rem = removed_lines(prev_text, text)

    kw_added = sorted(set(kw_now) - prev_kw)
    kw_removed = sorted(prev_kw - set(kw_now))
    result["keywords_added"] = kw_added
    result["keywords_removed"] = kw_removed

    if kw_added:
        result["notify"] = True
        result["reasons"].append(f"キーワード出現: {', '.join(kw_added)}")
    if kw_removed:
        result["notify"] = True
        result["reasons"].append(f"キーワード消滅: {', '.join(kw_removed)}")

    if add:
        result["notify"] = True
        result["added"] = add
        result["reasons"].append(f"本文に{len(add)}行追加")
    elif rem:
        # 削除だけの変化は取得揺れの疑いが濃いので通知しない
        result["reasons"].append(f"削除のみ{len(rem)}行（通知対象外）")

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
            out.append("**追加された本文**")
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
            elif r["skipped_save"]:
                status = "SKIP"
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
