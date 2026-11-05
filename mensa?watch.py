#!/usr/bin/env python3
"""
JAPAN MENSA 入会テスト 空き枠監視スクリプト

https://www.mensa.jp/exam/ を監視し、
  (a) 満員だった枠が申込可能になった（キャンセル発生）
  (b) 新しい日程が追加された
のときだけ通知する。

申込可能だった枠が満員になった場合は通知しない（指定による）。

■ ページの構造（2026-11 に確認）
  通常のHTML。JavaScript描画ではないので requests だけで取得できる。
  各枠は次の形で並ぶ:
      日時 ： 2026/11/15(日)　9:00~10:00
      場所 ： 東京都港区
      このテストには、全国の方がお申込みいただけます。
  状態は画像とリンクで表される:
      満員     → entry_quota.jpg
      申込可能 → /exam/index/notice/id/805/ へのリンク

  HTMLのタグ構造に依存すると改修で壊れるため、
  タグを落としたテキスト上で正規表現により枠を切り出す方式をとる。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

URL = "https://www.mensa.jp/exam/"

# 監視対象の都道府県。場所欄の先頭と照合する。
# 静岡＝在住者が申込可、東京・神奈川・埼玉＝全国から申込可。
TARGET_PREFS = ["静岡県", "東京都", "神奈川県", "埼玉県"]

STATE_FILE = Path("state-mensa/exam.json")
ALERT_FILE = Path("ALERT_MENSA.md")

TIMEOUT = 30
JST = timezone(timedelta(hours=9))

# 状態マーカー
FULL_IMG = "entry_quota.jpg"
OPEN_LINK = re.compile(r"/exam/index/notice/id/(\d+)")

SLOT_RE = re.compile(
    r"日時\s*[：:]\s*(\d{4}/\d{1,2}/\d{1,2})\s*\(([日月火水木金土])\)\s*"
    r"(\d{1,2}:\d{2})\s*[~〜]\s*(\d{1,2}:\d{2})"
)
PLACE_RE = re.compile(r"場所\s*[：:]\s*([^\n]+)")


# ---------------------------------------------------------------------------

def fetch_html() -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ja,en;q=0.8",
    }
    r = requests.get(URL, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def to_markers(html: str) -> str:
    """タグを落としつつ、状態を表す印だけをテキストに残す。

    状態は <img src="...entry_quota.jpg"> や
    <a href=".../notice/id/805/"> のようにタグの内側にあるため、
    タグを消す処理の中で印に置き換える（先に置換するとタグごと消える）。
    """
    # script / style を落とす
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)

    def tag_sub(m: "re.Match[str]") -> str:
        tag = m.group(0)
        om = OPEN_LINK.search(tag)
        if om:
            return f" [[OPEN:{om.group(1)}]] "
        if FULL_IMG in tag:
            return " [[FULL]] "
        return " "

    text = re.sub(r"(?s)<[^>]+>", tag_sub, html)

    for a, b in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                 ("&gt;", ">"), ("&quot;", '"'), ("&#039;", "'")]:
        text = text.replace(a, b)

    text = text.replace("\u3000", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return text


def parse_slots(text: str) -> dict[str, dict]:
    """枠を切り出して {キー: 情報} を返す。"""
    slots: dict[str, dict] = {}
    matches = list(SLOT_RE.finditer(text))

    for i, m in enumerate(matches):
        date, wday, start, end = m.groups()
        # この枠の記述範囲＝次の「日時 ：」の直前まで
        begin = m.start()
        stop = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[begin:stop]

        pm = PLACE_RE.search(block)
        place = pm.group(1).strip() if pm else "（場所不明）"
        # 場所欄に印が紛れ込んだ場合に備えて除去
        place = re.sub(r"\[\[(FULL|OPEN:\d+)\]\]", "", place).strip()

        om = re.search(r"\[\[OPEN:(\d+)\]\]", block)
        if om:
            status = "open"
            notice_id = om.group(1)
        elif "[[FULL]]" in block:
            status = "full"
            notice_id = None
        else:
            status = "unknown"
            notice_id = None

        # 対象者の記述（「全国の方が〜」など）
        tm = re.search(r"(このテストには、[^\n\[]+)", block)
        target = tm.group(1).strip() if tm else ""

        key = f"{date} {start}-{end} {place}"
        slots[key] = {
            "date": date,
            "wday": wday,
            "start": start,
            "end": end,
            "place": place,
            "status": status,
            "notice_id": notice_id,
            "target": target,
        }

    return slots


def pref_of(place: str) -> str:
    for p in TARGET_PREFS:
        if place.startswith(p):
            return p
    return ""


def filter_targets(slots: dict[str, dict]) -> dict[str, dict]:
    return {k: v for k, v in slots.items() if pref_of(v["place"])}


# ---------------------------------------------------------------------------

def notice_url(notice_id: str | None) -> str:
    if notice_id:
        return f"https://www.mensa.jp/exam/index/notice/id/{notice_id}/"
    return URL


def build_alert(opened: list[tuple[str, dict]],
                added: list[tuple[str, dict]]) -> str:
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    out = ["# JAPAN MENSA 入会テスト", "", f"検知時刻: {now}", ""]

    if opened:
        out.append("## 空きが出ました")
        out.append("")
        out.append("**申込締切は開催日の10日前です。先着のため早めに。**")
        out.append("")
        for key, s in opened:
            out.append(f"### {s['place']}　{s['date']}({s['wday']}) "
                       f"{s['start']}~{s['end']}")
            out.append("")
            if s["target"]:
                out.append(s["target"])
                out.append("")
            out.append(f"[この枠に申し込む]({notice_url(s['notice_id'])})")
            out.append("")

    if added:
        out.append("## 新しい日程が追加されました")
        out.append("")
        for key, s in added:
            state = {"open": "申込可能", "full": "満員", "unknown": "状態不明"}[s["status"]]
            out.append(f"- **{s['place']}**　{s['date']}({s['wday']}) "
                       f"{s['start']}~{s['end']}　— {state}")
            if s["status"] == "open":
                out.append(f"  - [申し込む]({notice_url(s['notice_id'])})")
        out.append("")

    out.append("---")
    out.append("")
    out.append(f"[入会テスト日程一覧]({URL})")
    out.append("")
    out.append("このIssueは自動生成です。確認したらCloseしてください。")
    return "\n".join(out)


def main() -> int:
    STATE_FILE.parent.mkdir(exist_ok=True)

    try:
        html = fetch_html()
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] 取得失敗: {type(exc).__name__}: {exc}", flush=True)
        return 1

    text = to_markers(html)
    all_slots = parse_slots(text)

    if not all_slots:
        print("[ERROR] 枠を1件も読み取れませんでした。ページ構成が変わった可能性があります。",
              flush=True)
        return 1

    slots = filter_targets(all_slots)
    print(f"[取得] 全{len(all_slots)}枠 / 対象{len(slots)}枠", flush=True)

    for key, s in sorted(slots.items()):
        mark = {"open": "空きあり", "full": "満員  ", "unknown": "不明  "}[s["status"]]
        print(f"  {mark} {key}", flush=True)

    prev: dict[str, dict] = {}
    if STATE_FILE.exists():
        try:
            prev = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("slots", {})
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 前回状態を読めませんでした: {exc}", flush=True)

    opened: list[tuple[str, dict]] = []
    added: list[tuple[str, dict]] = []

    first_run = not prev

    for key, s in slots.items():
        if key not in prev:
            added.append((key, s))
            continue
        before = prev[key].get("status")
        # 満員 → 申込可能 のときだけ通知。逆方向は通知しない。
        if before == "full" and s["status"] == "open":
            opened.append((key, s))

    STATE_FILE.write_text(
        json.dumps(
            {
                "url": URL,
                "fetched_at": datetime.now(JST).isoformat(),
                "slots": slots,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )

    if first_run:
        print("[alert] 初回のため通知しません", flush=True)
        return 0

    if opened:
        print(f"[alert] 空き発生 {len(opened)}件", flush=True)
    if added:
        print(f"[alert] 新日程 {len(added)}件", flush=True)

    if opened or added:
        ALERT_FILE.write_text(build_alert(opened, added), encoding="utf-8")
    else:
        print("[alert] なし（変化なし）", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
