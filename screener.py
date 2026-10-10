#!/usr/bin/env python3
"""Taiwan TWSE+TPEx top 300 gainers, volume 3.2x, D/W/M KD(26,3,3).

Historical data is unadjusted. Do not overwrite results when APIs fail.
"""
import datetime as dt
import json
import logging
import re
import time
from collections import defaultdict
from pathlib import Path
import requests

OUT = Path(__file__).with_name('results.json')
LOG = logging.getLogger('screener')
S = requests.Session()
S.headers.update({'User-Agent': 'Mozilla/5.0 Chrome/125.0 Safari/537.36',
                  'Accept': 'application/json, */*', 'Referer': 'https://www.tpex.org.tw/'})


def fetch(url, params):
    r = S.get(url, params=params, timeout=20)
    r.raise_for_status()
    try:
        j = r.json()
    except ValueError as e:
        raise RuntimeError(f'Non-JSON API response: {r.status_code} {r.text[:90]!r}') from e
    if not isinstance(j, dict):
        raise RuntimeError('Unexpected API JSON response')
    return j


def number(value):
    if isinstance(value, (int, float)):
        return float(value)
    s = re.sub('<[^>]*>', '', str(value or '')).replace(',', '').replace('+', '').replace('%', '').strip()
    try:
        return float(s)
    except ValueError:
        return None


def day_parse(value):
    x = re.findall(r'\d+', str(value))
    if len(x) < 3:
        return None
    y, m, d = map(int, x[:3])
    if y < 1911:
        y += 1911
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


def tables_of(j):
    tables = j.get('tables') or []
    if isinstance(tables, list):
        return [t for t in tables if isinstance(t, dict) and isinstance(t.get('data'), list)]
    return []


def listed(day):
    j = fetch('https://www.twse.com.tw/exchangeReport/MI_INDEX',
              {'response': 'json', 'date': day.strftime('%Y%m%d'), 'type': 'ALLBUT0999'})
    t = next((t for t in tables_of(j) if '證券代號' in (t.get('fields') or [])), None)
    rows = t['data'] if t else j.get('data9', [])
    if not rows:
        raise RuntimeError('TWSE daily quotes unavailable')
    out = []
    for row in rows:
        if len(row) < 11:
            continue
        code = re.sub('<[^>]*>', '', str(row[0])).strip()
        close, change, volume = number(row[8]), number(row[10]), number(row[2])
        if not re.fullmatch(r'\d{4}', code) or None in (close, change, volume) or close-change <= 0:
            continue
        out.append(dict(code=code, name=re.sub('<[^>]*>', '', str(row[1])).strip(),
                        market='TWSE', close=close, volume=volume,
                        gain_pct=round(100*change/(close-change), 4)))
    if not out:
        raise RuntimeError('No usable TWSE daily quotes')
    return out


def otc(day):
    j = fetch('https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes',
              {'date': f'{day.year-1911}/{day.month:02d}/{day.day:02d}',
               'id': '', 'response': 'json'})
    t = next((t for t in tables_of(j) if t['data'] and len(t['data'][0]) >= 10), None)
    if not t:
        raise RuntimeError('TPEx daily quotes unavailable')
    out = []
    for row in t['data']:
        code = re.sub('<[^>]*>', '', str(row[0])).strip()
        close, change, volume = number(row[2]), number(row[3]), number(row[8])
        if not re.fullmatch(r'\d{4}', code) or None in (close, change, volume) or close-change <= 0:
            continue
        out.append(dict(code=code, name=str(row[1]).strip(), market='TPEx',
                        close=close, volume=volume, gain_pct=round(100*change/(close-change), 4)))
    if not out:
        raise RuntimeError('No usable TPEx daily quotes')
    return out


def months(end, count=34):
    y, m = end.year, end.month
    for _ in range(count):
        yield dt.date(y, m, 1)
        m -= 1
        if m == 0:
            m, y = 12, y-1


def history(code, market, end):
    out = {}
    failures = 0
    for month in months(end):
        try:
            if market == 'TWSE':
                j = fetch('https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY',
                          {'response': 'json', 'date': month.strftime('%Y%m%d'), 'stockNo': code})
                if j.get('stat') != 'OK':
                    raise RuntimeError(f'TWSE status {j.get("stat")}')
                rows = j.get('data') or []
                for row in rows:
                    if len(row) < 7:
                        continue
                    day = day_parse(row[0])
                    high, low, close, volume = (number(row[i]) for i in (4, 5, 6, 1))
                    if day and day <= end and None not in (high, low, close, volume):
                        out[day] = (high, low, close, volume)
            else:
                # Official TPEx monthly historical OHLCV endpoint.
                # Try Gregorian and ROC year dates for API compatibility.
                rows = []
                issues = []
                for date_text in (month.strftime('%Y/%m/%d'),
                                  f'{month.year-1911}/{month.month:02d}/{month.day:02d}'):
                    try:
                        j = fetch('https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock',
                                  {'code': code, 'date': date_text, 'id': '', 'response': 'json'})
                        rows = [r for t in tables_of(j) for r in t['data']]
                        if not rows and isinstance(j.get('data'), list):
                            rows = j['data']
                        if rows:
                            break
                        issues.append(f'{date_text}: {str(j.get("stat", j.get("message", "empty")))[:60]}')
                    except (requests.RequestException, RuntimeError, ValueError) as e:
                        issues.append(f'{date_text}: {str(e)[:60]}')
                if not rows:
                    # Fallback to the legacy official TPEx monthly historical endpoint.
                    legacy = S.get('https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_info/st43_result.php',
                                   params={'d': f'{month.year-1911}/{month.month:02d}', 'stkno': code}, timeout=20)
                    legacy.raise_for_status()
                    old = legacy.json()
                    rows = old.get('aaData') or old.get('data') or []
                    if not rows:
                        raise RuntimeError(f'TPEx monthly history empty: {"; ".join(issues)[:170]}')
                for row in rows:
                    if len(row) < 7:
                        continue
                    day = day_parse(row[0])
                    volume, high, low, close = (number(row[i]) for i in (1, 4, 5, 6))
                    if day and day <= end and None not in (high, low, close, volume):
                        out[day] = (high, low, close, volume)
            failures = 0
        except (requests.RequestException, RuntimeError) as e:
            failures += 1
            LOG.warning('%s history %s %s: %s', market, code, month, e)
            if failures >= 3:
                raise RuntimeError(f'{market} history unavailable for {code}: 3 consecutive errors') from e
        time.sleep(0.15)
    return sorted(out.items())


def bars_for(items, frequency):
    if frequency == 'daily':
        return [v for _, v in items]
    periods = {}
    for day, (high, low, close, volume) in items:
        key = (day.isocalendar().year, day.isocalendar().week) if frequency == 'weekly' else (day.year, day.month)
        if key not in periods:
            periods[key] = [high, low, close, volume]
        else:
            bar = periods[key]
            bar[0], bar[1], bar[2], bar[3] = max(bar[0], high), min(bar[1], low), close, bar[3]+volume
    return list(periods.values())


def kd(bars, period=26):
    if len(bars) < period:
        return None
    k = d = 50.0
    for i in range(period-1, len(bars)):
        chunk = bars[i-period+1:i+1]
        high, low = max(b[0] for b in chunk), min(b[1] for b in chunk)
        rsv = 50.0 if high == low else (bars[i][2]-low)/(high-low)*100
        k = (2*k+rsv)/3
        d = (2*d+k)/3
    return {'k': round(k, 3), 'd': round(d, 3)}


def evaluate(q, end):
    items = history(q['code'], q['market'], end)
    if len(items) < 2 or items[-1][0] != end:
        return None, 'history missing latest day'
    prior = items[-2][1][3]
    if prior <= 0:
        return None, 'previous volume zero'
    ratio = items[-1][1][3]/prior
    if ratio < 3.2:
        return None, 'volume ratio below 3.2'
    values = {freq: kd(bars_for(items, freq)) for freq in ('daily', 'weekly', 'monthly')}
    if any(v is None for v in values.values()):
        return None, 'insufficient 26-period KD history'
    if not all(v['k'] > v['d'] for v in values.values()):
        return None, 'K <= D in one or more periods'
    return {**q, 'volume_ratio': round(ratio, 3), 'kd': values}, None


def quotes_on(day):
    return listed(day), otc(day)


def latest_quotes(before, start=0):
    for offset in range(start, start+14):
        day = before-dt.timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        try:
            a, b = quotes_on(day)
            return day, a, b
        except (requests.RequestException, RuntimeError, ValueError) as e:
            LOG.warning('Quote day %s failed: %s', day, e)
    raise RuntimeError('No usable TWSE + TPEx quotes in date window; old results preserved')


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
    day, a, b = latest_quotes(today)
    prev_day, prev_a, prev_b = latest_quotes(day, start=1)
    ranked = sorted(a+b, key=lambda x: x['gain_pct'], reverse=True)[:300]
    previous = {(q['market'], q['code']): q['volume'] for q in prev_a+prev_b}
    candidates = [q for q in ranked if previous.get((q['market'], q['code']), 0) > 0
                  and q['volume']/previous[(q['market'], q['code'])] >= 3.2]
    LOG.info('Top %d gainers; %d pass volume >=3.2x; quote date %s vs %s',
             len(ranked), len(candidates), day, prev_day)
    matches, errors, rejected = [], [], defaultdict(int)
    for index, q in enumerate(candidates, 1):
        try:
            match, reason = evaluate(q, day)
            if match:
                matches.append(match)
            else:
                rejected[reason] += 1
        except (requests.RequestException, RuntimeError, ValueError) as e:
            LOG.warning('History failed %s %s: %s', q['code'], q['name'], e)
            errors.append({'code': q['code'], 'error': str(e)[:180]})
        LOG.info('Progress %d/%d matches=%d errors=%d', index, len(candidates), len(matches), len(errors))
        # Do not silently publish partial results when upstream data is broken.
        if len(errors) >= 3:
            raise RuntimeError('3 historical API failures; previous results preserved')
    if errors:
        raise RuntimeError(f'{len(errors)} history errors; previous results preserved')
    payload = {
        'date': day.isoformat(),
        'generated_at': dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
        'universe': 'TWSE + TPEx', 'rank': 'daily percentage gain, top 300',
        'rules': {'kd_period': 26, 'k_smoothing': 3, 'd_smoothing': 3,
                  'kd': 'K > D on daily, weekly, monthly', 'volume_ratio_min': 3.2},
        'candidate_count': len(ranked), 'volume_prefilter_pass_count': len(candidates),
        'evaluated_count': len(candidates), 'match_count': len(matches),
        'error_count': 0, 'rejected_counts': dict(rejected), 'results': matches, 'errors': []}
    tmp = OUT.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(OUT)
    LOG.info('Wrote %s with %d matches', OUT, len(matches))


if __name__ == '__main__':
    main()
