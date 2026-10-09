#!/usr/bin/env python3
"""Taiwan TWSE + TPEx top-300 gainers, KD(26,3,3) screen.

Uses TWSE daily official MI_INDEX and TPEx daily official OTC quotations for
ranking, and TWSE STOCK_DAY / TPEx STOCK_DAY historical monthly endpoints.
Outputs results.json for a static GitHub Pages app.
"""
import datetime as dt
import json
import logging
import os
import re
import time
from collections import defaultdict
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'results.json'
S = requests.Session()
S.headers.update({'User-Agent':'Mozilla/5.0 (compatible; TaiwanStockScreener/1.0)', 'Accept':'application/json,text/plain,*/*'})
LOG = logging.getLogger('screen')


def get(url, params=None, attempts=3):
    for i in range(attempts):
        try:
            r = S.get(url, params=params, timeout=28)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            if i == attempts - 1:
                raise
            LOG.warning('Retry %s: %s', url, e)
            time.sleep(2 ** i)


def num(v):
    if isinstance(v, (int,float)):
        return float(v)
    s = str(v or '').replace(',', '').replace('+', '').replace('%','').strip()
    if not s or s in {'--','---','除權息','N/A'}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(v):
    s = str(v).strip()
    parts = re.findall(r'\d+', s)
    if len(parts) < 3:
        return None
    y,m,d = map(int,parts[:3])
    if y < 1911:
        y += 1911
    try:
        return dt.date(y,m,d)
    except ValueError:
        return None


def roc_date(day):
    return f'{day.year-1911}/{day.month:02d}/{day.day:02d}'


def listed_quotes(day):
    j = get('https://www.twse.com.tw/exchangeReport/MI_INDEX', {'response':'json','date':day.strftime('%Y%m%d'),'type':'ALLBUT0999'})
    tables = j.get('tables', [])
        table = next(
        (t for t in tables if '證券代號' in t.get('fields', [])), None)
    if table:
        fields = table.get('fields', [])
        rows = table.get('data', [])
    else:
        fields = j.get('fields9', [])
        rows = j.get('data9', [])
    if not rows:
        raise RuntimeError('TWSE MI_INDEX data9 unavailable (market holiday or API change)')
    LOG.info('TWSE columns: %s', fields)
    result = []
    for row in rows:
        if len(row) < 11:
            continue
        code = re.sub(r'<[^>]+>', '',str(row[0])).strip()
        if not re.fullmatch(r'\d{4}',code):
            continue
        close, delta, volume = num(row[8]),num(row[10]),num(row[2])
        if close is None or delta is None or volume is None or close-delta <= 0:
            continue
        result.append({'code':code,'name':re.sub(r'<[^>]+>','',str(row[1])).strip(),'market':'TWSE','gain_pct':round(delta/(close-delta)*100,4),'close':close,'volume':volume})
    return result


def otc_quotes(day):
    # Official TPEx daily quotation API. Date is ROC calendar.
    j = get('https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes', {'date':roc_date(day),'id':'','response':'json'})
    tables = j.get('tables') or []
    table = next((t for t in tables if t.get('data') and len(t.get('data')[0]) >= 10),None)
    if table is None:
        raise RuntimeError('TPEx dailyQuotes rows unavailable (market holiday or API change)')
    fields = table.get('fields',[])
    LOG.info('TPEx columns: %s', fields)
    result=[]
    for row in table['data']:
        code = re.sub(r'<[^>]+>','',str(row[0])).strip()
        if not re.fullmatch(r'\d{4}',code):
            continue
        # Typical fields: code,name,close,change,open,high,low,avg,volume,value,trades,...
        close, delta, volume = num(row[2]),num(row[3]),num(row[8])
        if close is None or delta is None or volume is None or close-delta <= 0:
            continue
        result.append({'code':code,'name':str(row[1]).strip(),'market':'TPEx','gain_pct':round(delta/(close-delta)*100,4),'close':close,'volume':volume})
    return result


def month_iter(end, months=42):
    y,m=end.year,end.month
    for _ in range(months):
        yield dt.date(y,m,1)
        m-=1
        if m==0:
            m=12;y-=1


def history_twse(code,end):
    out={}
    for day in month_iter(end):
        try:
            j=get('https://www.twse.com.tw/exchangeReport/STOCK_DAY',{'response':'json','date':day.strftime('%Y%m%d'),'stockNo':code})
            for row in j.get('data',[]):
                date=parse_date(row[0])
                if date and date<=end:
                    high,low,close,vol=num(row[4]),num(row[5]),num(row[6]),num(row[1])
                    if None not in (high,low,close,vol):
                        out[date]=(high,low,close,vol)
        except Exception as e:
            LOG.warning('TWSE history %s %s: %s',code,day,e)
        time.sleep(.13)
    return out


def history_tpex(code,end):
    out={}
    for day in month_iter(end):
        try:
            j=get('https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock',{'code':code,'date':roc_date(day),'response':'json'})
            tables=j.get('tables') or []
            for table in tables:
                for row in table.get('data',[]):
                    if len(row)<7:
                        continue
                    date=parse_date(row[0])
                    if date and date<=end:
                        # TPEx tradingStock: date, volume, value, open, high, low, close, change, trades
                        vol,high,low,close=num(row[1]),num(row[4]),num(row[5]),num(row[6])
                        if None not in (high,low,close,vol):
                            out[date]=(high,low,close,vol)
        except Exception as e:
            LOG.warning('TPEx history %s %s: %s',code,day,e)
        time.sleep(.13)
    return out


def aggregate(items,mode):
    if mode=='daily':
        return [v for _,v in items]
    periods={}
    for day,(high,low,close,vol) in items:
        key=(day.isocalendar().year,day.isocalendar().week) if mode=='weekly' else (day.year,day.month)
        if key not in periods:
            periods[key]=[high,low,close,vol]
        else:
            p=periods[key]
            p[0]=max(p[0],high);p[1]=min(p[1],low);p[2]=close;p[3]+=vol
    return list(periods.values())


def kd(bars,period=26):
    if len(bars)<period:
        return None
    k=d=50.0
    for i in range(period-1,len(bars)):
        segment=bars[i-period+1:i+1]
        hi=max(x[0] for x in segment)
        lo=min(x[1] for x in segment)
        rsv=50.0 if hi==lo else (bars[i][2]-lo)/(hi-lo)*100
        k=(2*k+rsv)/3
        d=(2*d+k)/3
    return {'k':round(k,3),'d':round(d,3)}


def evaluate(q,end):
    history=history_twse(q['code'],end) if q['market']=='TWSE' else history_tpex(q['code'],end)
    items=sorted(history.items())
    if len(items)<2:
        return None,'insufficient daily data'
    # If latest historical day doesn't match quote day, never silently use stale data.
    if items[-1][0]!=end:
        return None,'history not up to date'
    prev_volume=items[-2][1][3]
    if prev_volume<=0:
        return None,'previous volume zero'
    ratio=items[-1][1][3]/prev_volume
    if ratio<3.2:
        return None,'volume ratio below 3.2'
    kds={mode:kd(aggregate(items,mode)) for mode in ('daily','weekly','monthly')}
    if any(v is None for v in kds.values()):
        return None,'insufficient KD periods'
    if not all(v['k']>v['d'] for v in kds.values()):
        return None,'K not greater than D'
    return {**q,'volume_ratio':round(ratio,3),'kd':kds},None


def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    # On weekends or holidays, step backward to find most recent common trading day.
    today=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
    latest=None
    for offset in range(0,12):
        day=today-dt.timedelta(days=offset)
        if day.weekday()>=5:
            continue
        try:
            listed=listed_quotes(day)
            otc=otc_quotes(day)
            if listed and otc:
                latest=(day,listed,otc)
                break
        except Exception as e:
            LOG.warning('Quote day %s failed: %s',day,e)
    if not latest:
        raise RuntimeError('Unable to fetch a common TWSE/TPEx trading day. Existing results preserved.')
    day,listed,otc=latest
    ranked=sorted(listed+otc,key=lambda x:x['gain_pct'],reverse=True)[:300]
    LOG.info('Screening %s candidates on %s',len(ranked),day)
    matches=[]; errors=[];rejected=defaultdict(int)
    for idx,q in enumerate(ranked,1):
        try:
            match,reason=evaluate(q,day)
            if match:
                matches.append(match)
            else:
                rejected[reason]+=1
        except Exception as e:
            LOG.warning('%s %s failed: %s',q['code'],q['name'],e)
            errors.append({'code':q['code'],'error':str(e)[:180]})
        if idx%10==0:
            LOG.info('Progress %s/300, matches=%s, errors=%s',idx,len(matches),len(errors))
    payload={'date':day.isoformat(),'generated_at':dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
             'universe':'TWSE + TPEx','rank':'daily percentage gain, top 300',
             'rules':{'kd_period':26,'k_smoothing':3,'d_smoothing':3,'kd':'K > D on daily, weekly, monthly','volume_ratio_min':3.2},
             'candidate_count':len(ranked),'evaluated_count':len(ranked)-len(errors),'match_count':len(matches),
             'error_count':len(errors),'rejected_counts':dict(rejected),'results':matches,'errors':errors}
    # Avoid replacing previous output with a partial/broken API result.
    if len(errors)>len(ranked)//4:
        raise RuntimeError(f'Too many data errors: {len(errors)} / {len(ranked)}; previous results preserved')
    tmp=OUT.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    tmp.replace(OUT)
    LOG.info('Wrote %s, %s matches',OUT,len(matches))

if __name__=='__main__':
    main()
