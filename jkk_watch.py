#!/usr/bin/env python3
"""
JKK東京 コーシャハイム方南町ガーデンコート あき家監視スクリプト

物件専用の直リンク（akiyaJyokenDirect）を開き、
あき家検索結果ページの内容を前回実行時と比較する。

変化があれば ALERT_JKK.md を書き出す。
GitHub Actions 側は ALERT_JKK.md の有無で通知の要否を判断する。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

BUKKEN_NAME = "コーシャハイム方南町ガーデンコート"

# 公式サイトの物件ページに掲載されている「最新の空室状況を確認する」の直リンク。
# jutaku_name は住宅名カナのUTF-16BE16進表記。
TARGET_URL = (
    "https://jhomes.to-kousya.or.jp/search/jkknet/service/akiyaJyokenDirect"
    "?sen_flg=1"
    "&jutaku_name=30B330FC30B730E330CF30A430E030DB30A630CA30F330C130E730A630AC30FC30C730F330B330FC30C8"
)

STATE_FILE = Path("state-jkk/honancho.json")
ALERT_FILE = Path("ALERT_JKK.md")

# 「あき家なし」を示すと思われる語。どれか1つでも含まれれば空室なしと判定する。
# 初回実行のログを見て実際の文言に合わせて調整すること。
NO_VACANCY_HINTS = [
    "該当する住宅はありません",
    "該当する住戸はありません",
    "該当がありません",
    "見つかりませんでした",
    "ありませんでした",
]

# 「0件」は「10件」「20件」に部分一致してしまうため正規表現で判定する
NO_VACANCY_PATTERNS = [
    re.compile(r"(?<!\d)0\s*件"),
]

# リダイレクト中継ページを示す語
REDIRECT_HINTS = ["自動で次の画面", "しばらくたっても"]

MIN_TEXT_LEN = 40
MAX_DIFF_LINES = 40
LOG_PREVIEW_LINES = 60

JST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------------------
# テキスト処理
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    """比較用の正規化。

    日付・時刻の表示は実行のたびに変わりうるので伏せ字にする。
    行そのものは消さない（部屋情報の行を失わないため）。
    """
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"[ \t\u3000]+", " ", raw).strip()
        if not line:
            continue
        # 2026年9月2日 / 2026/09/02 / 2026-09-02
        line = re.sub(r"\d{4}\s*[年/-]\s*\d{1,2}\s*[月/-]\s*\d{1,2}\s*日?", "<日付>", line)
        # 14:05 / 14時05分
        line = re.sub(r"\d{1,2}\s*[:時]\s*\d{2}\s*分?", "<時刻>", line)
        lines.append(line)
    return "\n".join(lines)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def line_diff(old: str, new: str) -> list[str]:
    old_lines = set(old.splitlines())
    return [ln for ln in new.splitlines() if ln not in old_lines]


def looks_no_vacancy(text: str) -> bool:
    if any(h in text for h in NO_VACANCY_HINTS):
        return True
    return any(p.search(text) for p in NO_VACANCY_PATTERNS)


# ---------------------------------------------------------------------------
# 取得
# ---------------------------------------------------------------------------

def fetch_text(page) -> str:
    """中継ページのリダイレクトを追ってから本文を取る。"""
    page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)

    for attempt in range(4):
        page.wait_for_timeout(3000)
        body = page.evaluate("() => document.body ? document.body.innerText : ''") or ""

        if not any(h in body for h in REDIRECT_HINTS) and len(body.strip()) >= MIN_TEXT_LEN:
            return normalize(body)

        print(f"[wait] 中継ページ待機中 ({attempt + 1}/4)", flush=True)

        # 自動遷移しない場合に備えてリンクを踏む
        try:
            link = page.locator("a").first
            if link.count() > 0:
                link.click(timeout=5000)
                page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:  # noqa: BLE001
            pass

    body = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
    return normalize(body)


# ---------------------------------------------------------------------------

def build_alert(reasons: list[str], added: list[str], vacancy: bool, text: str) -> str:
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    out = [f"## {BUKKEN_NAME}", "", f"検知時刻: {now}", ""]

    if vacancy:
        out.append("**あき家あり と判定されました。**")
        out.append("")
        out.append("先着順です。すぐにJKKねっとで確認・申込みしてください。")
    else:
        out.append("ページ内容が変化しました（あき家なしと判定）。")
    out.append("")
    out.append(f"[あき家検索を開く]({TARGET_URL})")
    out.append("")

    for r in reasons:
        out.append(f"- {r}")
    out.append("")

    if added:
        out.append("**追加された行**")
        out.append("")
        out.append("```")
        for ln in added[:MAX_DIFF_LINES]:
            out.append(ln)
        if len(added) > MAX_DIFF_LINES:
            out.append(f"... 他 {len(added) - MAX_DIFF_LINES} 行")
        out.append("```")
        out.append("")

    out.append("<details><summary>取得した全文</summary>")
    out.append("")
    out.append("```")
    out.append(text[:4000])
    out.append("```")
    out.append("")
    out.append("</details>")
    return "\n".join(out)


def main() -> int:
    STATE_FILE.parent.mkdir(exist_ok=True)

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
        try:
            text = fetch_text(page)
        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] 取得失敗: {type(exc).__name__}: {exc}", flush=True)
            browser.close()
            return 1
        finally:
            pass
        browser.close()

    # 初回チューニング用に取得内容をログへ出す
    print("---- 取得した本文（先頭 %d 行）----" % LOG_PREVIEW_LINES, flush=True)
    for ln in text.splitlines()[:LOG_PREVIEW_LINES]:
        print("  " + ln, flush=True)
    print("---- ここまで ----", flush=True)

    if len(text) < MIN_TEXT_LEN:
        print(f"[ERROR] 本文が{len(text)}文字しか取得できませんでした", flush=True)
        return 1

    vacancy = not looks_no_vacancy(text)
    print(f"[判定] あき家: {'あり' if vacancy else 'なし'}", flush=True)

    reasons: list[str] = []
    added: list[str] = []
    changed = False

    if STATE_FILE.exists():
        prev = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        prev_text = prev.get("text", "")
        prev_vacancy = prev.get("vacancy")

        if prev_vacancy is not None and prev_vacancy != vacancy:
            changed = True
            reasons.append(
                f"あき家判定が変化: {'なし' if prev_vacancy is False else 'あり'}"
                f" → {'あり' if vacancy else 'なし'}"
            )

        if text_hash(prev_text) != text_hash(text):
            added = line_diff(prev_text, text)
            removed = line_diff(text, prev_text)
            changed = True
            reasons.append(f"本文変化: 追加{len(added)}行 / 削除{len(removed)}行")
    else:
        reasons.append("初回取得（基準を保存）")

    STATE_FILE.write_text(
        json.dumps(
            {
                "url": TARGET_URL,
                "fetched_at": datetime.now(JST).isoformat(),
                "hash": text_hash(text),
                "vacancy": vacancy,
                "text": text,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )

    if changed:
        ALERT_FILE.write_text(build_alert(reasons, added, vacancy, text), encoding="utf-8")
        print("[alert] 通知対象あり", flush=True)
    else:
        print("[alert] なし", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
