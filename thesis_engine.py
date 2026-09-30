"""Explainable trade-thesis engine."""
import time

def analyze_position(p):
    import engine
    symbol=str(p.get('symbol') or '').upper(); side='LONG' if p.get('side')=='Buy' else 'SHORT'
    entry=float(p.get('avgPrice') or 0); margin=abs(float(p.get('positionIM') or 0)); upnl=float(p.get('unrealisedPnl') or 0)
    pnl_pct=(upnl/max(margin,1e-9))*100 if margin else 0.0
    f5=engine.frame_features(engine.klines(symbol,'5',80)); f15=engine.frame_features(engine.klines(symbol,'15',80)); f60=engine.frame_features(engine.klines(symbol,'60',80))
    biases={'5M':engine.bias(f5),'15M':engine.bias(f15),'1H':engine.bias(f60)}; z5,z15=f5.iloc[-1],f15.iloc[-1]
    opposite='SHORT' if side=='LONG' else 'LONG'; positive=[]; negative=[]; score=50
    def add(cond,pts,good,bad):
        nonlocal score
        if cond:
            score+=pts
            (positive if pts>0 else negative).append(good if pts>0 else bad)
        elif pts>0: negative.append(bad)
    aligned=sum(b==side for b in biases.values()); opposed=sum(b==opposite for b in biases.values())
    add(biases['1H']==side,20,'1H тренд подтверждает направление','1H тренд больше не подтверждает')
    add(biases['15M']==side,15,'15M структура подтверждает направление','15M структура развернулась против')
    add(biases['5M']==side,8,'5M импульс поддерживает направление','5M импульс против позиции')
    ema_ok=((float(z15.close)>float(z15.ema20) and float(z15.ema20)>float(z15.ema50)) if side=='LONG' else (float(z15.close)<float(z15.ema20) and float(z15.ema20)<float(z15.ema50)))
    add(ema_ok,12,'EMA20/EMA50 подтверждают тренд','EMA20/EMA50 больше не подтверждают')
    ret5=(float(f5.close.iloc[-1])/float(f5.close.iloc[-4])-1)*100 if len(f5)>=4 else 0.0
    ret15=(float(f15.close.iloc[-1])/float(f15.close.iloc[-2])-1)*100 if len(f15)>=2 else 0.0
    momentum_ok=(ret5>0 and ret15>0) if side=='LONG' else (ret5<0 and ret15<0)
    add(momentum_ok,10,'5M+15M momentum направлен в нашу сторону','5M+15M momentum ухудшается')
    rsi5=float(z5.rsi); rsi15=float(z15.rsi); rsi_ok=(42<=rsi5<=72 and 42<=rsi15<=72) if side=='LONG' else (28<=rsi5<=58 and 28<=rsi15<=58)
    add(rsi_ok,5,'RSI не показывает явного истощения','RSI указывает на перегрев/истощение')
    vol_ok=(float(z15.volume)>float(z15.vol_ma20)) if float(z15.vol_ma20 or 0)>0 else False
    add(vol_ok,5,'объём подтверждает движение','объём не подтверждает движение')
    news=engine.news_snapshot(symbol=symbol); news_against=False; news_support=False
    for item in (news.get('top') or []):
        if item.get('relevance_level')=='high' and item.get('market_confirmed'):
            sent=item.get('sentiment')
            if (side=='LONG' and sent=='bearish') or (side=='SHORT' and sent=='bullish'): news_against=True
            if (side=='LONG' and sent=='bullish') or (side=='SHORT' and sent=='bearish'): news_support=True
    if news_support: score+=8; positive.append('подтверждённая новость поддерживает направление')
    if news_against: score-=20; negative.append('подтверждённая новость против позиции')
    score=max(0,min(100,score))
    if score>=82 and aligned>=2 and not news_against: decision='STRONG_HOLD'
    elif score>=62 and opposed==0: decision='HOLD / REASSESS'
    elif score<=34 or (opposed>=2 and not momentum_ok): decision='EXIT'
    else: decision='REDUCE_RISK / REASSESS'
    return {'thesis_health':int(score),'thesis_decision':decision,'side':side,'symbol':symbol,'entry_price':entry,'pnl_on_margin_pct':round(pnl_pct,3),'aligned_timeframes':aligned,'opposed_timeframes':opposed,'bias_5m':biases['5M'],'bias_15m':biases['15M'],'bias_1h':biases['1H'],'momentum_5m_pct':round(ret5,4),'momentum_15m_pct':round(ret15,4),'positive_factors':positive[:8],'negative_factors':negative[:8],'news_against':news_against,'news_support':news_support,'news_impact':news.get('impact'),'thesis_updated_at':int(time.time()*1000)}
