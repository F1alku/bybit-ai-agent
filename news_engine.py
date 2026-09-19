"""Lightweight crypto/macro news engine.

Transparent RSS/Atom rules only. News is a risk filter, not a signal generator.
A headline is considered market-confirmed only when its directional sentiment
matches a recent BTC move; neutral headlines never create a directional block.
"""
import os, re, time, hashlib
from datetime import datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET
import httpx

NEWS_TIMEOUT = float(os.getenv('NEWS_TIMEOUT', '8'))
NEWS_CACHE_TTL = float(os.getenv('NEWS_CACHE_TTL', '90'))
NEWS_MAX_ITEMS = int(os.getenv('NEWS_MAX_ITEMS', '40'))
NEWS_HIGH_MINUTES = int(os.getenv('NEWS_HIGH_MINUTES', '30'))
NEWS_SCALP_BLOCK_MINUTES = int(os.getenv('NEWS_SCALP_BLOCK_MINUTES', '15'))
NEWS_FEEDS = [
    ('CoinDesk', 'https://www.coindesk.com/arc/outboundfeeds/rss/', 'crypto'),
    ('Cointelegraph', 'https://cointelegraph.com/rss', 'crypto'),
    ('Federal Reserve', 'https://www.federalreserve.gov/feeds/press_all.xml', 'macro'),
]
_extra = os.getenv('NEWS_FEEDS', '').strip()
if _extra:
    for raw in _extra.split(','):
        parts = raw.split('|', 2)
        if len(parts) == 3:
            NEWS_FEEDS.append(tuple(parts))

_cache = {'ts': 0.0, 'items': [], 'sources': {}}
_client = httpx.Client(timeout=NEWS_TIMEOUT, headers={'User-Agent': 'BybitAI-Agent/5.10.1 NewsEngine'})

HIGH_KEYWORDS = {
    'fed': 5, 'federal reserve': 5, 'interest rate': 5, 'rate decision': 5,
    'cpi': 5, 'inflation': 4, 'pce': 5, 'payroll': 4, 'nonfarm': 4,
    'sec': 4, 'cftc': 4, 'etf': 4, 'hack': 5, 'hacked': 5, 'exploit': 5,
    'bankruptcy': 5, 'bankrupt': 5, 'ban crypto': 5, 'sanction': 4,
    'war': 4, 'tariff': 4, 'default': 5, 'liquidation': 4,
}
MARKET_KEYWORDS = {'bitcoin','btc','ethereum','eth','crypto','cryptocurrency','digital asset','stablecoin','defi','exchange','binance','coinbase','bybit','solana','xrp'}
BULLISH = {'approval','approved','inflow','inflows','adoption','bullish','surge','rally','cuts','cut rates','dovish','easing','launches'}
BEARISH = {'outflow','outflows','hack','hacked','exploit','ban','banned','bearish','selloff','sell-off','raises rates','hike','hawkish','lawsuit','liquidation'}
ASSET_ALIASES = {
    'BTC': {'btc','bitcoin'}, 'ETH': {'eth','ethereum'}, 'SOL': {'sol','solana'},
    'XRP': {'xrp','ripple'}, 'SUI': {'sui'}, 'DOGE': {'doge','dogecoin'},
    'ADA': {'ada','cardano'}, 'AVAX': {'avax','avalanche'}, 'LINK': {'link','chainlink'},
    'BNB': {'bnb','binance coin'},
}


def _clean(text):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', text or '')).strip()


def _parse_time(value):
    if not value: return time.time()
    try: return parsedate_to_datetime(value).timestamp()
    except Exception:
        try: return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
        except Exception: return time.time()


def _tag(root, names):
    for el in root.iter():
        tag = el.tag.split('}')[-1].lower()
        if tag in names: return _clean(el.text or '')
    return ''


def _parse_feed(xml, source, kind):
    root = ET.fromstring(xml); items=[]
    for node in root.iter():
        tag=node.tag.split('}')[-1].lower()
        if tag not in ('item','entry'): continue
        title=_tag(node, {'title'}); desc=_tag(node, {'description','summary','content'}); pub=_tag(node, {'pubdate','published','updated','date'}); link=''
        for child in node.iter():
            if child.tag.split('}')[-1].lower()=='link':
                link=child.attrib.get('href','') or (child.text or '')
                if link: break
        if not title: continue
        items.append({'id':hashlib.sha1((source+'|'+title+'|'+link).encode()).hexdigest(),'source':source,'kind':kind,'title':title,'summary':desc[:500],'url':link,'published_at':_parse_time(pub),'text':f'{title} {desc}'})
    return items


def _fetch_bundle(force=False):
    now=time.time()
    if not force and now-_cache['ts']<NEWS_CACHE_TTL:
        return list(_cache['items']), dict(_cache['sources'])
    out=[]; statuses={}
    for source,url,kind in NEWS_FEEDS:
        try:
            r=_client.get(url); r.raise_for_status(); parsed=_parse_feed(r.text,source,kind); out.extend(parsed)
            statuses[source]={'ok':True,'items':len(parsed)}
        except Exception as exc:
            statuses[source]={'ok':False,'error':str(exc)[:160]}
    dedup={x['id']:x for x in out}; items=sorted(dedup.values(),key=lambda x:x['published_at'],reverse=True)
    _cache.update({'ts':now,'items':items[:NEWS_MAX_ITEMS],'sources':statuses})
    return list(_cache['items']), dict(statuses)


def fetch_news(force=False):
    return _fetch_bundle(force)[0]


def _token_hit(text, token):
    return bool(re.search(r'(?<![a-z0-9])'+re.escape(token)+r'(?![a-z0-9])', text, re.I))


def _relevance(item, symbol=None):
    text=item['text'].lower(); crypto_hits=sum(1 for k in MARKET_KEYWORDS if _token_hit(text,k) or k in text)
    impact=sum(v for k,v in HIGH_KEYWORDS.items() if _token_hit(text,k) or k in text)
    asset='market'
    if symbol:
        base=symbol.replace('USDT','').upper()
        aliases=ASSET_ALIASES.get(base,{base.lower()})
        if any(_token_hit(text,a) for a in aliases): asset='asset'; impact += 3
    if item['kind']=='macro' and any(_token_hit(text,k) or k in text for k in ('fed','inflation','cpi','pce','payroll','interest rate')): impact += 2
    if crypto_hits==0 and item['kind']=='macro' and impact<5: return 0,'low',asset
    level='high' if impact>=8 else ('medium' if impact>=4 else 'low')
    return min(10,impact),level,asset


def _sentiment(item):
    t=item['text'].lower(); bull=sum(1 for k in BULLISH if _token_hit(t,k) or k in t); bear=sum(1 for k in BEARISH if _token_hit(t,k) or k in t)
    return 'bullish' if bull>bear else ('bearish' if bear>bull else 'neutral')


def _btc_reaction():
    try:
        import engine
        df=engine.klines('BTCUSDT','15',8)
        if len(df)<3: return {'pct_15m':0.0,'pct_30m':0.0,'strength':'none','direction':'neutral'}
        c=df['close'].astype(float); p15=(float(c.iloc[-1])/float(c.iloc[-2])-1)*100; p30=(float(c.iloc[-1])/float(c.iloc[-3])-1)*100
        strength='strong' if abs(p30)>=1.0 or abs(p15)>=0.6 else ('moderate' if abs(p30)>=0.35 or abs(p15)>=0.2 else 'none')
        direction='bullish' if (p30>0 or (p30==0 and p15>0)) else ('bearish' if (p30<0 or (p30==0 and p15<0)) else 'neutral')
        return {'pct_15m':round(p15,4),'pct_30m':round(p30,4),'strength':strength,'direction':direction}
    except Exception:
        return {'pct_15m':0.0,'pct_30m':0.0,'strength':'unknown','direction':'neutral'}


def _market_confirmed(sentiment,reaction,age,level):
    if sentiment not in ('bullish','bearish'): return False
    if reaction.get('strength') not in ('moderate','strong') or age>45 or level not in ('medium','high'): return False
    direction = reaction.get('direction')
    if direction not in ('bullish','bearish'):
        p30=float(reaction.get('pct_30m') or 0); p15=float(reaction.get('pct_15m') or 0)
        direction='bullish' if (p30>0 or (p30==0 and p15>0)) else ('bearish' if (p30<0 or (p30==0 and p15<0)) else 'neutral')
    return sentiment==direction


def _evaluate(items,reaction,symbol=None):
    now=time.time(); relevant=[]
    for item in items:
        age=max(0,(now-item['published_at'])/60); rel,level,asset=_relevance(item,symbol)
        if rel<=0 or age>24*60: continue
        sentiment=_sentiment(item); confirmed=_market_confirmed(sentiment,reaction,age,level)
        effective='high' if level=='high' and confirmed else ('medium' if level in ('medium','high') else 'low')
        relevant.append({**{k:item[k] for k in ('id','source','title','summary','url','published_at')},'age_min':round(age,1),'relevance':rel,'relevance_level':effective,'asset_relevance':asset,'sentiment':sentiment,'confidence':round(min(0.99,0.55+(0.15 if item['kind']=='macro' else 0.05)+(0.12 if confirmed else 0)),2),'market_confirmed':confirmed})
    relevant.sort(key=lambda x:(x['relevance_level']=='high',x['relevance'], -x['age_min']),reverse=True)
    return relevant


def snapshot(symbol=None,force=False):
    items,sources=_fetch_bundle(force); reaction=_btc_reaction(); relevant=_evaluate(items,reaction,symbol)
    high=[x for x in relevant if x['relevance_level']=='high']; medium=[x for x in relevant if x['relevance_level']=='medium']
    source_ok=any(v.get('ok') for v in sources.values()); status='ok' if source_ok else 'degraded'
    return {'status':status,'updated_at':int(time.time()*1000),'sources':sources,'btc_reaction':reaction,'impact':'high' if high else ('medium' if medium else 'low'),'items':relevant[:12],'top':relevant[:5]}


def snapshot_for_symbol(news,symbol):
    reaction=(news or {}).get('btc_reaction') or _btc_reaction(); relevant=_evaluate(_cache.get('items',[]),reaction,symbol); sources=(news or {}).get('sources',_cache.get('sources',{}));
    return {'status':(news or {}).get('status','degraded' if sources and not any(v.get('ok') for v in sources.values()) else 'ok'),'sources':sources,'impact':'high' if any(x['relevance_level']=='high' for x in relevant) else ('medium' if relevant else 'low'),'items':relevant[:12],'top':relevant[:5],'btc_reaction':reaction}


def apply_to_signal(signal,news,strategy='normal'):
    snap=snapshot_for_symbol(news,signal.get('symbol',''))
    signal.update({'news_impact':snap['impact'],'news_items':snap['items'][:3],'news_btc_reaction':snap['btc_reaction'],'news_checked':True,'news_status':snap['status'],'news_sources':snap['sources']})
    if signal.get('decision') not in ('OPEN LONG','OPEN SHORT'): return signal
    top=snap['top'][0] if snap['top'] else None
    if not top: return signal
    signal.update({'news_top_event':top['title'],'news_confidence':top['confidence'],'news_sentiment':top['sentiment'],'news_market_confirmed':top['market_confirmed']})
    if top['relevance_level']=='high' and top['market_confirmed']:
        minutes=top['age_min']; block=(strategy=='scalp' and minutes<=NEWS_SCALP_BLOCK_MINUTES) or (strategy=='normal' and minutes<=NEWS_HIGH_MINUTES)
        if block:
            signal['decision']='WAIT'; signal['entry_timing']='WAIT'; signal['signal_quality']='WAIT'; signal.setdefault('blockers',[]).append('NEWS: подтверждённое сильное рыночное событие'); signal.setdefault('reasons',[]).append('NEWS FILTER BLOCK')
    return signal
