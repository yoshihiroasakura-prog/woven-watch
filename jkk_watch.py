#!/usr/bin/env python3
"""
JKK東京 コーシャハイム方南町ガーデンコート あき家監視スクリプト

物件専用の直リンクを開き、あき家の有無だけを判定する。

実際のページ挙動（2026-09 時点で確認）:
  空室ゼロのときは検索結果一覧ではなく、検索条件画面に
  「ご希望の住宅、またはご希望の条件の空室はございませんでした。」
  というメッセージ付きで戻される。
  この画面には区名・沿線名が大量に並ぶため、本文全体の差分監視は
  ノイズにしかならない。よって判定結果の変化だけを通知条件とする。

判定は3値:
  none    空室なし（定常状態）
  some    空室あり  → 通知
  unknown 判別できない → エラー終了（勝手に「あり」にしない）
"""

from __future__ import annotations

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

TARGET_URL = (
    "https://jhomes.to-kousya.or.jp/search/jkknet/service/akiyaJyokenDirect"
    "?sen_flg=1"
    "&jutaku_name=30B330FC30B730E330CF30A430E030DB30A630CA30F330C130E730A630AC30FC30C730F330B330FC30C8"
)

STATE_FILE = Path("state-jkk/honancho.json")
ALERT_FILE = Path("ALERT_JKK.md")

# 「空室なし」を示す文言（実ページで確認済み）
NO_VACANCY_HINTS = [
    "空室はございませんでした",
    "ございませんでした",
]

# 「空室あり」を示す語（結果一覧に物件名が出る）
VACANCY_MARKER = "コーシャハイム"

# 検索システムのページに到達したことを示す語
PAGE_MARKERS = ["先着順募集", "条件から検索", "エリアで検索", "住宅名"]

# 中継ページを示す語
REDIRECT_HINTS = ["自動で次の画面", "しばらくたっても"]

# 通知本文に載せる意味のある行だけを拾うための除外パターン
NOISE_PATTERNS = [
    re.compile(r"^[ぁ-んァ-ヶ一-龥Ａ-ＺA-Za-z\s]{0,4}(線|区|市|町|村)$"),
    re.compile(r"^(ＪＲ|JR|東武|京成|西武|小田急|京王|東急|東京メトロ|都営|京急|相鉄|多摩|埼玉|北総|新交通|ゆりかもめ)"),
    re.compile(r"^[^\s]+(区|市|町|村)(\s+[^\s]+(区|市|町|村))+$"),
]

MAX_ATTEMPTS = 6
LOG_PREVIEW_LINES = 40
JST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"[ \t\u3000]+", " ", raw).strip()
        if not line:
            continue
        line = re.sub(r"\d{4}\s*[年/-]\s*\d{1,2}\s*[月/-]\s*\d{1,2}\s*日?", "<日付>", line)
        line = re.sub(r"\d{1,2}\s*[:時]\s*\d{2}\s*分?", "<時刻>", line)
        lines.append(line)
    return "\n".join(lines)


def signal_lines(text: str) -> list[str]:
    """区名・沿線名の羅列を除いた、意味のある行だけを返す。"""
    out = []
    for line in text.splitlines():
        if any(p.match(line) for p in NOISE_PATTERNS):
            continue
        out.append(line)
    return out


def is_interstitial(text: str) -> bool:
    return any(h in text for h in REDIRECT_HINTS)


def on_search_system(text: str) -> bool:
    return any(m in text for m in PAGE_MARKERS)


def judge(text: str) -> str:
    """none / some / unknown を返す。"""
    if any(h in text for h in NO_VACANCY_HINTS):
        return "none"
    if VACANCY_MARKER in text:
        return "some"
    if on_search_system(text):
        return "unknown"
    return "unknown"


# ---------------------------------------------------------------------------

def body_text(page) -> str:
    return page.evaluate("() => document.body ? document.body.innerText : ''") or ""


def try_advance(page) -> str:
    try:
        return page.evaluate(
            """() => {
                const f = document.forwardForm || document.forms[0];
                if (!f) return 'フォームなし';
                try {
                    let nextURL = null;
                    if (f.url && f.url.value) {
                        nextURL = f.url.value;
                        f.action = nextURL;
                    }
                    f.target = '_self';
                    f.submit();
                    return 'forwardForm を同一ウィンドウへ送信';
                } catch (e) {
                    return '送信失敗: ' + e.message;
                }
            }"""
        )
    except Exception as exc:  # noqa: BLE001
        return f"遷移中のため評価できず ({type(exc).__name__})"


def usable(raw: str) -> bool:
    return on_search_system(raw) and not is_interstitial(raw)


def fetch_text(page) -> tuple[str, bool]:
    page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        page.wait_for_timeout(2500)

        raw = body_text(page)
        if usable(raw):
            print(f"[ok] 検索システムに到達 (試行 {attempt})", flush=True)
            return normalize(raw), True

        action = try_advance(page)
        print(f"[wait] 中継ページ ({attempt}/{MAX_ATTEMPTS}) → {action}", flush=True)

        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:  # noqa: BLE001
            pass

        for other in page.context.pages:
            if other is page:
                continue
            try:
                other_raw = other.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                ) or ""
                if usable(other_raw):
                    print("[ok] 別ウィンドウ側で検索システムを検出", flush=True)
                    return normalize(other_raw), True
            except Exception:  # noqa: BLE001
                pass

    raw = body_text(page)
    if usable(raw):
        return normalize(raw), True

    print("---- 到達できなかったページの冒頭 ----", flush=True)
    for ln in normalize(raw).splitlines()[:20]:
        print("  " + ln, flush=True)
    print("---- ここまで ----", flush=True)
    return normalize(raw), False


# ---------------------------------------------------------------------------

def build_alert(verdict: str, prev_verdict: str | None, lines: list[str]) -> str:
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    out = [f"## {BUKKEN_NAME}", "", f"検知時刻: {now}", ""]

    if verdict == "some":
        out.append("# あき家が出ました")
        out.append("")
        out.append("**先着順です。申込みが入り次第、受付は終了します。**")
        out.append("")
        out.append(f"[JKKねっとであき家検索を開く]({TARGET_URL})")
    else:
        label = {"none": "空室なし", "unknown": "判別不能"}[verdict]
        out.append(f"判定が「{label}」に変化しました。")
        out.append("")
        out.append(f"[あき家検索を開く]({TARGET_URL})")

    out.append("")
    out.append(f"- 前回: {prev_verdict or '（記録なし）'}")
    out.append(f"- 今回: {verdict}")
    out.append("")
    out.append("**ページから読み取った内容**")
    out.append("")
    out.append("```")
    for ln in lines[:25]:
        out.append(ln)
    out.append("```")
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
            text, ok = fetch_text(page)
        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] 取得失敗: {type(exc).__name__}: {exc}", flush=True)
            browser.close()
            return 1
        browser.close()

    if not ok:
        print("[ERROR] 検索システムに到達できませんでした。判定は行いません。", flush=True)
        return 1

    lines = signal_lines(text)

    print(f"---- 読み取った内容（ノイズ除去後 先頭 {LOG_PREVIEW_LINES} 行）----", flush=True)
    for ln in lines[:LOG_PREVIEW_LINES]:
        print("  " + ln, flush=True)
    print("---- ここまで ----", flush=True)

    verdict = judge(text)
    print(f"[判定] {verdict}", flush=True)

    prev_verdict = None
    if STATE_FILE.exists():
        try:
            prev_verdict = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("verdict")
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 前回状態を読めませんでした: {exc}", flush=True)

    STATE_FILE.write_text(
        json.dumps(
            {
                "url": TARGET_URL,
                "fetched_at": datetime.now(JST).isoformat(),
                "verdict": verdict,
                "lines": lines[:40],
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )

    if verdict == "unknown":
        print("[ERROR] 空室の有無を判別できませんでした。文言が変わった可能性があります。",
              flush=True)
        return 1

    if prev_verdict is None:
        print("[alert] 初回のため通知しません", flush=True)
        return 0

    if verdict != prev_verdict:
        ALERT_FILE.write_text(build_alert(verdict, prev_verdict, lines), encoding="utf-8")
        print(f"[alert] 判定変化 {prev_verdict} → {verdict}", flush=True)
    else:
        print("[alert] なし（変化なし）", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
