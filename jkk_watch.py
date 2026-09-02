#!/usr/bin/env python3
"""
JKK東京 コーシャハイム方南町ガーデンコート あき家監視スクリプト

物件専用の直リンク（akiyaJyokenDirect）を開き、
あき家検索結果ページの内容を前回実行時と比較する。

重要な設計方針:
  結果ページに到達したと確認できない限り、あき家の有無を判定しない。
  取得失敗を「あき家あり」と誤って扱わないため。
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

TARGET_URL = (
    "https://jhomes.to-kousya.or.jp/search/jkknet/service/akiyaJyokenDirect"
    "?sen_flg=1"
    "&jutaku_name=30B330FC30B730E330CF30A430E030DB30A630CA30F330C130E730A630AC30FC30C730F330B330FC30C8"
)

STATE_FILE = Path("state-jkk/honancho.json")
ALERT_FILE = Path("ALERT_JKK.md")

# 「あき家なし」を示す語
NO_VACANCY_HINTS = [
    "該当する住宅はありません",
    "該当する住戸はありません",
    "該当がありません",
    "見つかりませんでした",
    "ありませんでした",
]

# 「0件」は「10件」に部分一致するため正規表現で判定
NO_VACANCY_PATTERNS = [re.compile(r"(?<!\d)0\s*件")]

# 中継ページを示す語
REDIRECT_HINTS = ["自動で次の画面", "しばらくたっても"]

# 結果ページに到達したことを示す語。どれも無ければ取得失敗とみなす。
RESULT_MARKERS = ["コーシャハイム", "検索結果", "あき家", "空家", "住宅名", "間取"]

MIN_TEXT_LEN = 40
MAX_DIFF_LINES = 40
LOG_PREVIEW_LINES = 60
MAX_ATTEMPTS = 6

JST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------------------
# テキスト処理
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


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def line_diff(old: str, new: str) -> list[str]:
    old_lines = set(old.splitlines())
    return [ln for ln in new.splitlines() if ln not in old_lines]


def looks_no_vacancy(text: str) -> bool:
    if any(h in text for h in NO_VACANCY_HINTS):
        return True
    return any(p.search(text) for p in NO_VACANCY_PATTERNS)


def is_interstitial(text: str) -> bool:
    return any(h in text for h in REDIRECT_HINTS)


def reached_result(text: str) -> bool:
    return any(m in text for m in RESULT_MARKERS)


# ---------------------------------------------------------------------------
# 取得
# ---------------------------------------------------------------------------

def body_text(page) -> str:
    return page.evaluate("() => document.body ? document.body.innerText : ''") or ""


def try_advance(page) -> str:
    """中継ページの forwardForm を同一ウィンドウ宛に送信して先へ進む。

    JKKの中継ページは window.open した別ウィンドウ宛にPOSTする作りなので、
    target を _self に付け替えてから送信する。
    """
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
                    return 'forwardForm を同一ウィンドウへ送信: ' + (nextURL || f.action);
                } catch (e) {
                    return '送信失敗: ' + e.message;
                }
            }"""
        )
    except Exception as exc:  # noqa: BLE001
        return f"遷移中のため評価できず ({type(exc).__name__})"


def dump_diagnostics(page) -> None:
    """到達できなかったときに構造を吐き出す。"""
    print("---- 診断情報 ----", flush=True)
    print(f"  URL: {page.url}", flush=True)
    try:
        info = page.evaluate(
            """() => {
                const forms = Array.from(document.forms).map(f => ({
                    name: f.name, action: f.action, method: f.method,
                    fields: Array.from(f.elements).map(e => e.name).filter(Boolean)
                }));
                const links = Array.from(document.querySelectorAll('a'))
                    .slice(0, 10)
                    .map(a => ({ text: (a.innerText || '').trim().slice(0, 30),
                                 href: a.getAttribute('href'),
                                 onclick: a.getAttribute('onclick') }));
                const scripts = Array.from(document.querySelectorAll('script'))
                    .map(s => (s.innerText || '').trim().slice(0, 300))
                    .filter(Boolean).slice(0, 3);
                return { forms, links, scripts };
            }"""
        )
        print("  forms: " + json.dumps(info["forms"], ensure_ascii=False), flush=True)
        print("  links: " + json.dumps(info["links"], ensure_ascii=False), flush=True)
        for i, s in enumerate(info["scripts"]):
            print(f"  script[{i}]: {s}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  診断取得失敗: {exc}", flush=True)
    print("---- 診断ここまで ----", flush=True)


def fetch_text(page) -> tuple[str, bool]:
    """(正規化済み本文, 結果ページに到達したか) を返す。"""
    page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        page.wait_for_timeout(2500)
        raw = body_text(page)

        if reached_result(raw) and not is_interstitial(raw):
            print(f"[ok] 結果ページに到達 (試行 {attempt})", flush=True)
            return normalize(raw), True

        action = try_advance(page)
        print(f"[wait] 中継ページ ({attempt}/{MAX_ATTEMPTS}) → {action}", flush=True)

        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:  # noqa: BLE001
            pass

        # 別ウィンドウが開いてしまった場合はそちらを確認する
        for other in page.context.pages:
            if other is page:
                continue
            try:
                other_raw = other.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                ) or ""
                if reached_result(other_raw) and not is_interstitial(other_raw):
                    print("[ok] 別ウィンドウ側で結果ページを検出", flush=True)
                    return normalize(other_raw), True
            except Exception:  # noqa: BLE001
                pass

    raw = body_text(page)
    if reached_result(raw) and not is_interstitial(raw):
        return normalize(raw), True

    dump_diagnostics(page)
    return normalize(raw), False


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
            text, ok = fetch_text(page)
        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] 取得失敗: {type(exc).__name__}: {exc}", flush=True)
            browser.close()
            return 1
        browser.close()

    print(f"---- 取得した本文（先頭 {LOG_PREVIEW_LINES} 行）----", flush=True)
    for ln in text.splitlines()[:LOG_PREVIEW_LINES]:
        print("  " + ln, flush=True)
    print("---- ここまで ----", flush=True)

    # 結果ページに到達できていないなら、あき家の有無は判定しない
    if not ok:
        print("[ERROR] 結果ページに到達できませんでした。判定は行いません。", flush=True)
        return 1

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
                f"あき家判定が変化: {'あり' if prev_vacancy else 'なし'}"
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
