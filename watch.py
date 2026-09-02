#!/usr/bin/env python3
"""
Woven City 予約情報 監視スクリプト

公式ページをヘッドレスブラウザでレンダリングし、
  (a) 注目キーワードの出現・消滅
  (b) 本文テキスト全体の変化
を前回実行時の状態と比較する。

変化があれば ALERT.md を書き出して exit 0 で終了する。
GitHub Actions 側は ALERT.md の有無で通知の要否を判断する。
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
        # このページに現れたら重要度が高い語
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

# ページ読み込み後、JS描画を待つ時間（ミリ秒）
RENDER_WAIT_MS = 4000
# 取得した本文がこの文字数未満なら「取得失敗」とみなす
MIN_TEXT_LEN = 300
# 通知に載せる差分の最大行数
MAX_DIFF_LINES = 25

JST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------------------
# テキスト正規化
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    """比較用にテキストを正規化する。

    全角半角の揺れ、連続空白、空行を潰す。
    日付や数字は意味を持つので残す。
    """
    text = unicodedata.normalize("NFKC", text)
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"[ \t\u3000]+", " ", raw).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 取得
# ---------------------------------------------------------------------------

def fetch_text(page, url: str) -> str:
    page.goto(url, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(RENDER_WAIT_MS)
    body = page.evaluate("() => document.body ? document.body.innerText : ''")
    return normalize(body or "")


# ---------------------------------------------------------------------------
# 差分
# ---------------------------------------------------------------------------

def line_diff(old: str, new: str) -> list[str]:
    """追加された行だけを抜き出す（削除は件数のみ報告）。"""
    old_lines = set(old.splitlines())
    added = [ln for ln in new.splitlines() if ln not in old_lines]
    return added


def check_target(page, target: dict) -> dict:
    """1ページ分を確認し、結果 dict を返す。"""
    result = {
        "id": target["id"],
        "label": target["label"],
        "url": target["url"],
        "ok": False,
        "changed": False,
        "reasons": [],
        "added_lines": [],
        "removed_count": 0,
        "keywords_now": [],
        "keywords_added": [],
        "keywords_removed": [],
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

    result["ok"] = True

    kw_now = sorted({kw for kw in target["keywords"] if kw in text})
    result["keywords_now"] = kw_now

    state_path = STATE_DIR / f"{target['id']}.json"
    if state_path.exists():
        prev = json.loads(state_path.read_text(encoding="utf-8"))
        prev_text = prev.get("text", "")
        prev_kw = set(prev.get("keywords", []))

        kw_added = sorted(set(kw_now) - prev_kw)
        kw_removed = sorted(prev_kw - set(kw_now))
        result["keywords_added"] = kw_added
        result["keywords_removed"] = kw_removed

        if kw_added:
            result["changed"] = True
            result["reasons"].append(f"キーワード出現: {', '.join(kw_added)}")
        if kw_removed:
            result["changed"] = True
            result["reasons"].append(f"キーワード消滅: {', '.join(kw_removed)}")

        if text_hash(prev_text) != text_hash(text):
            added = line_diff(prev_text, text)
            removed = line_diff(text, prev_text)
            result["added_lines"] = added
            result["removed_count"] = len(removed)
            if added or removed:
                result["changed"] = True
                result["reasons"].append(
                    f"本文変化: 追加{len(added)}行 / 削除{len(removed)}行"
                )
    else:
        # 初回。基準を作るだけで通知はしない。
        result["reasons"].append("初回取得（基準を保存）")

    state_path.write_text(
        json.dumps(
            {
                "url": target["url"],
                "fetched_at": datetime.now(JST).isoformat(),
                "hash": text_hash(text),
                "keywords": kw_now,
                "text": text,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )

    return result


# ---------------------------------------------------------------------------
# 通知本文
# ---------------------------------------------------------------------------

def build_alert(results: list[dict]) -> str:
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    out = [f"検知時刻: {now}", ""]

    for r in results:
        if not (r["changed"] or r["error"]):
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

        if r["added_lines"]:
            out.append("**追加された本文**")
            out.append("")
            out.append("```")
            for ln in r["added_lines"][:MAX_DIFF_LINES]:
                out.append(ln)
            if len(r["added_lines"]) > MAX_DIFF_LINES:
                out.append(f"... 他 {len(r['added_lines']) - MAX_DIFF_LINES} 行")
            out.append("```")
            out.append("")

    out.append("---")
    out.append("")
    out.append("このIssueは自動生成です。確認したらCloseしてください。")
    return "\n".join(out)


# ---------------------------------------------------------------------------

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
            status = "ERROR" if r["error"] else ("CHANGED" if r["changed"] else "same")
            print(f"[{status}] {target['id']} :: {'; '.join(r['reasons']) or '-'}",
                  flush=True)
            results.append(r)
        browser.close()

    notify = [r for r in results if r["changed"] or r["error"]]
    if notify:
        ALERT_FILE.write_text(build_alert(results), encoding="utf-8")
        print(f"[alert] {len(notify)}件の通知対象", flush=True)
    else:
        print("[alert] なし", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
