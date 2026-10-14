"""GTFS（沖縄バス）を、ブラウザで読みやすい小さなデータ（data/bus-data.js）に整理する。

使い方（プロジェクトのフォルダで）:
    python tools/build_data.py "沖縄バス（事業者）の情報" data/bus-data.js

出力は `window.BUS_DATA = {...};` 形式の JavaScript です。
<script> で読み込めるので、GitHub Pages でもローカルで HTML を直接開いても動きます。

出力の中身:
  meta      データの期間・作成日・クレジット用の情報
  stops     [stop_id, 名前, 緯度, 経度]
  routes    [系統番号, 系統名]
  days      {"YYYYMMDD": [その日に走る service の番号, ...]}  （calendar + calendar_dates を展開済み）
  services  service_id の一覧（days の番号が指す先）
  trips     [route番号, service番号, [停留所番号...], [発時刻の差分(分)...], [着時刻-発時刻(分)...]?, 乗車不可の位置?, 降車不可の位置?]
            時刻は 1 本目が 0 時からの分、2 本目以降は前の停留所との差分（データを小さくするため）。
  fares     [route番号(-1はどの系統でも), 乗る停留所番号, 降りる停留所番号, 運賃(円)]  ※データにある分だけ
"""
import csv
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path


def read(gtfs: Path, name: str):
    path = gtfs / name
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [{k.strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


def to_min(hms: str) -> int:
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 60 + m + (1 if s >= 30 else 0)


def ymd(s: str) -> dt.date:
    return dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def main(gtfs_dir: str, out_path: str):
    gtfs = Path(gtfs_dir)
    out = Path(out_path)

    # --- バス停 ---
    stops_raw = [s for s in read(gtfs, "stops.txt") if s.get("location_type", "") in ("", "0")]
    stop_idx = {}
    stops = []
    for s in stops_raw:
        try:
            lat, lon = float(s["stop_lat"]), float(s["stop_lon"])
        except ValueError:
            continue
        stop_idx[s["stop_id"]] = len(stops)
        stops.append([s["stop_id"], s["stop_name"], round(lat, 6), round(lon, 6)])

    # --- 系統 ---
    route_idx = {}
    routes = []
    for r in read(gtfs, "routes.txt"):
        route_idx[r["route_id"]] = len(routes)
        routes.append([r.get("route_short_name", ""), r.get("route_long_name", "")])

    # --- 運行日（calendar + calendar_dates を日付ごとに展開） ---
    feed = (read(gtfs, "feed_info.txt") or [{}])[0]
    calendar = read(gtfs, "calendar.txt")
    cdates = read(gtfs, "calendar_dates.txt")
    service_ids = sorted({c["service_id"] for c in calendar} | {c["service_id"] for c in cdates})
    svc_idx = {s: i for i, s in enumerate(service_ids)}

    if feed.get("feed_start_date") and feed.get("feed_end_date"):
        start, end = ymd(feed["feed_start_date"]), ymd(feed["feed_end_date"])
    else:
        all_dates = [ymd(c["start_date"]) for c in calendar] + [ymd(c["end_date"]) for c in calendar]
        all_dates += [ymd(c["date"]) for c in cdates]
        start, end = min(all_dates), max(all_dates)

    wd_keys = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    active = defaultdict(set)
    d = start
    while d <= end:
        key = d.strftime("%Y%m%d")
        for c in calendar:
            if c[wd_keys[d.weekday()]] == "1" and c["start_date"] <= key <= c["end_date"]:
                active[key].add(svc_idx[c["service_id"]])
        d += dt.timedelta(days=1)
    for c in cdates:
        key = c["date"]
        if not (start.strftime("%Y%m%d") <= key <= end.strftime("%Y%m%d")):
            continue
        if c["exception_type"] == "1":
            active[key].add(svc_idx[c["service_id"]])
        elif c["exception_type"] == "2":
            active[key].discard(svc_idx[c["service_id"]])
    days = {k: sorted(v) for k, v in sorted(active.items()) if v}

    # --- 便ごとの停車順と時刻 ---
    by_trip = defaultdict(list)
    for st in read(gtfs, "stop_times.txt"):
        by_trip[st["trip_id"]].append(st)

    trips = []
    skipped = 0
    used_services = set()
    for t in read(gtfs, "trips.txt"):
        rows = by_trip.get(t["trip_id"])
        if not rows or t["route_id"] not in route_idx or t["service_id"] not in svc_idx:
            skipped += 1
            continue
        rows.sort(key=lambda r: int(r["stop_sequence"]))
        rows = [r for r in rows if r["stop_id"] in stop_idx]
        if len(rows) < 2:
            skipped += 1
            continue
        seq, deltas, dwell, no_pick, no_drop = [], [], [], [], []
        prev = None
        for i, r in enumerate(rows):
            dep = to_min(r["departure_time"] or r["arrival_time"])
            arr = to_min(r["arrival_time"] or r["departure_time"])
            seq.append(stop_idx[r["stop_id"]])
            deltas.append(dep if prev is None else dep - prev)
            dwell.append(dep - arr)
            prev = dep
            if r.get("pickup_type") == "1":
                no_pick.append(i)
            if r.get("drop_off_type") == "1":
                no_drop.append(i)
        rec = [route_idx[t["route_id"]], svc_idx[t["service_id"]], seq, deltas,
               dwell if any(dwell) else [], no_pick, no_drop]
        while rec and rec[-1] == []:
            rec.pop()
        trips.append(rec)
        used_services.add(t["service_id"])

    # 時刻順に並べておくと、ブラウザ側で並べ替えがほぼ不要になる
    trips.sort(key=lambda r: (r[3][0], r[0]))

    # --- 運賃（fare_rules の origin_id/destination_id はバス停の zone_id を指す） ---
    price = {f["fare_id"]: int(float(f["price"])) for f in read(gtfs, "fare_attributes.txt") if f.get("price")}
    zone_idx = {}
    for s in stops_raw:
        if s.get("zone_id") and s["stop_id"] in stop_idx:
            zone_idx.setdefault(s["zone_id"], []).append(stop_idx[s["stop_id"]])
    fares = []
    for fr in read(gtfs, "fare_rules.txt"):
        if fr["fare_id"] not in price or not fr.get("origin_id") or not fr.get("destination_id"):
            continue
        r = route_idx.get(fr.get("route_id", ""), -1)  # -1 = どの系統でも
        for o in zone_idx.get(fr["origin_id"], []):
            for d in zone_idx.get(fr["destination_id"], []):
                fares.append([r, o, d, price[fr["fare_id"]]])

    agency = (read(gtfs, "agency.txt") or [{}])[0]
    data = {
        "meta": {
            "built": dt.date.today().strftime("%Y%m%d"),
            "feedStart": start.strftime("%Y%m%d"),
            "feedEnd": end.strftime("%Y%m%d"),
            "publisher": feed.get("feed_publisher_name", ""),
            "agency": agency.get("agency_name", ""),
        },
        "stops": stops,
        "routes": routes,
        "services": service_ids,
        "days": days,
        "trips": trips,
        "fares": fares,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    out.write_text("window.BUS_DATA=" + body + ";\n", encoding="utf-8")

    print(f"バス停 {len(stops)} / 系統 {len(routes)} / 便 {len(trips)}（除外 {skipped}）")
    fare_routes = {routes[r][0] for r, *_ in fares if r >= 0}
    print(f"運賃 {len(fares)} 件（系統: {', '.join(sorted(fare_routes)) or 'なし'}）")
    print(f"期間 {data['meta']['feedStart']}〜{data['meta']['feedEnd']}（運行日 {len(days)} 日）")
    unused = sorted(set(service_ids) - used_services)
    if unused:
        print("便のない service_id:", ", ".join(unused))
    print(f"出力 {out}（{out.stat().st_size / 1024:.0f} KB）")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("使い方: python tools/build_data.py <GTFSフォルダ> <出力先.js>")
    main(sys.argv[1], sys.argv[2])
