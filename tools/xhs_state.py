# 小红书分批抓取的状态管理。状态加密存在 state/xhs.enc（AES-256-GCM，密钥由环境变量 STATE_KEY 提供，base64）。
# 用法（都在仓库根目录运行）：
#   STATE_KEY=... python3 tools/xhs_state.py plan               # 输出本次要做的小批任务（JSON）
#   STATE_KEY=... python3 tools/xhs_state.py apply results.json # 合并本次抓取结果，重建 feed/video.enc、comedy.enc、learn.enc
#   STATE_KEY=... python3 tools/xhs_state.py ext ext.json       # 写入/更新站外集（YouTube / B站），然后重建 feed
#   STATE_KEY=... python3 tools/xhs_state.py show               # 打印状态摘要
import sys, os, json, re, base64, time, random, subprocess, datetime
from urllib.parse import unquote, quote
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SP = os.path.join(ROOT, 'state', 'xhs.enc')
TZ = datetime.timezone(datetime.timedelta(hours=8))
MAX_REQ = 6          # 每次运行最多的小红书请求数（主页 + 详情 + 搜索）
PROFILES_PER_RUN = 2
LEARN_KW = ['Claude 技巧', 'Claude Code 使用', 'Claude 使用体验', 'ChatGPT 使用技巧', 'GPT 提示词 技巧', 'GPT 使用体验', 'Muse 使用技巧', 'Muse AI 体验']


def now(): return datetime.datetime.now(TZ)
def today(): return now().strftime('%Y-%m-%d')
def key(): return base64.b64decode(os.environ['STATE_KEY'])


def load():
    if not os.path.exists(SP):
        return {'bloggers': [], 'items': {}, 'pending': {}, 'rejected': [], 'learn': {}, 'learn_kw': {}}
    e = json.load(open(SP))
    pt = AESGCM(key()).decrypt(base64.b64decode(e['iv']), base64.b64decode(e['ct']), None)
    return json.loads(pt)


def save(S):
    os.makedirs(os.path.dirname(SP), exist_ok=True)
    iv = os.urandom(12)
    ct = AESGCM(key()).encrypt(iv, json.dumps(S, ensure_ascii=False).encode(), None)
    json.dump({'iv': base64.b64encode(iv).decode(), 'ct': base64.b64encode(ct).decode()}, open(SP, 'w'))


def note_time(nid):
    try: return datetime.datetime.fromtimestamp(int(nid[:8], 16), TZ)
    except Exception: return None


def v_exp(v):
    m = re.search(r'[?&]t=([0-9a-f]{8})', v or '')
    return int(m.group(1), 16) if m else 0


def xhs_url(nid, tok, src):
    return f'https://www.xiaohongshu.com/explore/{nid}?xsec_token={quote(tok, safe="")}&xsec_source={src}'


# ---------------- plan ----------------
def dkey(it):
    a = it['src'].split(' · ')[0]
    return f"se:{a}|{it['se']}" if it.get('se') and it.get('ep') is not None else (it.get('_nid') and 'xhs:' + it['_nid'] or it['id'])


def plan():
    S = load(); t = today(); ts = time.time(); hour = now().hour
    DIS = set(S.get('disliked', []))
    jobs = {'max_requests': MAX_REQ, 'profiles': [], 'learn_keyword': None, 'details': [], 'pending_to_review': []}
    budget = MAX_REQ
    # 1) 博主主页：今天还没查过的，最久没查的优先
    def is_due(b):
        if b.get('skip') or b.get('last_check') == t: return False
        if not b.get('last_check'): return True
        days = (now().date() - datetime.date.fromisoformat(b['last_check'])).days
        active = b.get('last_new') and (now().date() - datetime.date.fromisoformat(b['last_new'])).days <= 14
        return days >= (1 if active else 3)
    due = [b for b in S['bloggers'] if is_due(b)]
    due.sort(key=lambda b: b.get('last_check') or '')
    for b in due[:PROFILES_PER_RUN]:
        jobs['profiles'].append({k: b[k] for k in ('uid', 'name', 'kind')})
    budget -= len(jobs['profiles'])
    # 2) AI学习 关键词：每次 1 个，下午 3 点前做完
    done_today = sum(1 for k in LEARN_KW if S['learn_kw'].get(k) == t)
    if hour < 15 and budget > 2 and done_today < 4:
        left = sorted([k for k in LEARN_KW if S['learn_kw'].get(k) != t], key=lambda k: S['learn_kw'].get(k, ''))
        if left:
            jobs['learn_keyword'] = left[0]; budget -= 1
    # 3) 待处理的新笔记（要判断是不是正片），列出来给 agent 先按标题筛
    pend = sorted(S['pending'].values(), key=lambda p: p['nid'], reverse=True)
    jobs['pending_to_review'] = [{'nid': p['nid'], 'title': p.get('title', ''), 'type': p.get('type'), 'blogger': p.get('name'), 'kind': p['kind'],
                                  'date': (note_time(p['nid']) or now()).strftime('%m-%d')} for p in pend]
    # 4) 详情：先新笔记，再快过期（36 小时内）的链接
    det = []
    for p in pend:
        if len(det) >= budget: break
        det.append({'nid': p['nid'], 'tok': p['tok'], 'src': p.get('xsrc', 'pc_user'), 'why': 'new', 'title': p.get('title', '')})
    exp = sorted([it for it in S['items'].values() if it.get('_tok') and it.get('v') and v_exp(it['v']) < ts + 36 * 3600 and dkey(it) not in DIS],
                 key=lambda it: v_exp(it['v']))
    for it in exp:
        if len(det) >= budget: break
        det.append({'nid': it['_nid'], 'tok': it['_tok'], 'src': it.get('_xsrc', 'pc_user'), 'why': 'refresh', 'title': it.get('t', '')})
    jobs['details'] = det
    W = "await new Promise(r=>setTimeout(r,5000+Math.random()*10000));"
    P = "const P=h=>{const m=h.match(/window\\.__INITIAL_STATE__=(\\{.*?\\})<\\/script>/s);if(!m)return null;return JSON.parse(m[1].replace(/:undefined/g,':null').replace(/new Map\\(\\[.*?\\]\\)/g,'{}'))};"
    for j in jobs['profiles']:
        j['code'] = ("await (async()=>{" + W + P + f"const s=P(await (await fetch('/user/profile/{j['uid']}')).text());"
                     "if(!s)return 'STOP: 拿不到页面数据';return JSON.stringify({uid:'" + j['uid'] + "',notes:(s.user.notes||[]).flat().map(x=>{const c=x.noteCard||x;"
                     "return {noteId:c.noteId||x.id,tok:c.xsecToken||x.xsecToken,title:c.displayTitle||'',type:c.type,sticky:!!(c.interactInfo&&c.interactInfo.sticky)}})})})()")
    for j in jobs['details']:
        base = ("await (async()=>{" + W + P + f"const s=P(await (await fetch('/explore/{j['nid']}?xsec_token='+encodeURIComponent('{j['tok']}')+'&xsec_source={j['src']}')).text());"
                "if(!s)return 'STOP: 拿不到页面数据';const n=Object.values(s.note.noteDetailMap)[0].note;let v='';const st=n.video&&n.video.media&&n.video.media.stream;"
                "if(st)for(const k in st)for(const q of (st[k]||[]))if(!v&&q.masterUrl&&q.masterUrl.includes('.mp4?'))v=q.masterUrl.replace(/^http:/,'https:');"
                "const imgs=(n.imageList||[]).slice(0,9).map(i=>(i.urlDefault||'').replace(/^http:/,'https:'));")
        if j['why'] == 'refresh':
            j['code'] = base + f"return JSON.stringify({{nid:'{j['nid']}',ok:!!v,v,img:imgs[0]||'',liked:n.interactInfo.likedCount,collected:n.interactInfo.collectedCount}})}})()"
        else:
            j['code'] = base + (f"return JSON.stringify({{nid:'{j['nid']}',ok:true,title:n.title,desc:(n.desc||'').slice(0,1500),type:n.type,tags:(n.tagList||[]).map(t=>t.name),"
                                "liked:n.interactInfo.likedCount,collected:n.interactInfo.collectedCount,duration:n.video&&n.video.capa&&n.video.capa.duration,v,img:imgs[0]||'',imgs})})()")
    if jobs['learn_keyword']:
        jobs['learn_url'] = 'https://www.xiaohongshu.com/search_result?keyword=' + quote(jobs['learn_keyword'], safe='') + '&source=web_search_result_notes'
        jobs['learn_code'] = ("await new Promise(r=>setTimeout(r,4000));const f=window.__INITIAL_STATE__&&window.__INITIAL_STATE__.search.feeds;const a=f&&(f._rawValue||f._value||f)||[];"
                              "JSON.stringify({kw:" + json.dumps(jobs['learn_keyword'], ensure_ascii=False) + ",feeds:a.filter(x=>x.noteCard).map(x=>({id:x.id,tok:x.xsecToken,title:x.noteCard.displayTitle||'',"
                              "liked:x.noteCard.interactInfo.likedCount,collected:x.noteCard.interactInfo.collectedCount,author:(x.noteCard.user||{}).nickname||''}))})")
    jobs['login_code'] = ("await new Promise(r=>setTimeout(r,3000));const u=window.__INITIAL_STATE__&&window.__INITIAL_STATE__.user.userInfo;const v=u&&(u._rawValue||u._value||u);"
                          "[v&&v.nickname,document.body.innerText.slice(0,300).replace(/\\s+/g,' ')]")
    jobs['nothing'] = not (jobs['profiles'] or jobs['learn_keyword'] or jobs['details'])
    jobs['stats'] = {'items': len(S['items']), 'pending': len(S['pending']), 'expiring_36h': len(exp),
                     'bloggers_left_today': len(due)}
    print(json.dumps(jobs, ensure_ascii=False, indent=1))


# ---------------- apply ----------------
def apply(path):
    S = load(); R = json.load(open(path, encoding='utf-8')); t = today()
    byuid = {b['uid']: b for b in S['bloggers']}
    known = set(S['items']) | set(S['pending']) | set(S['rejected'])
    for pr in R.get('profiles', []):
        b = byuid.get(pr['uid'])
        if not b: continue
        b['last_check'] = t
        has = any(it.get('_uid') == b['uid'] or it['src'].split(' · ')[0] == b['name'] for it in S['items'].values())
        for n in pr.get('notes', []):
            nid = n['noteId']
            if nid in known or n.get('sticky') or n.get('type') != 'video':
                continue
            nt = note_time(nid)
            if has and nt and (now() - nt).days > 30:   # 已经收录过的博主，30 天前的旧笔记不再逐条打开
                continue
            S['pending'][nid] = {'nid': nid, 'tok': n['tok'], 'title': n.get('title', ''), 'type': n.get('type'), 'uid': b['uid'],
                                 'name': b['name'], 'kind': b['kind'], 'xsrc': 'pc_user'}
            known.add(nid)
    for lk in R.get('learn', []):
        S['learn_kw'][lk['kw']] = t
        for f in lk.get('feeds', []):
            nid = f['id']; heat = num(f.get('liked')) + num(f.get('collected'))
            nt = note_time(nid)
            if not nt or (now() - nt).days > 7: continue
            c = S['learn'].setdefault(nid, {'nid': nid})
            c.update({'tok': f['tok'], 'title': f.get('title', ''), 'heat': heat, 'author': f.get('author', '')})
        # 只把当前热度前 12 名里还没取过详情的放进待处理
        top = sorted(S['learn'].values(), key=lambda c: -c.get('heat', 0))[:12]
        for c in top:
            if c['nid'] not in known:
                S['pending'][c['nid']] = {'nid': c['nid'], 'tok': c['tok'], 'title': c['title'], 'type': 'learn', 'name': c['author'],
                                          'kind': 'learn', 'xsrc': 'pc_search'}
                known.add(c['nid'])
    for nid in R.get('reject', []):
        S['pending'].pop(nid, None)
        if nid not in S['rejected']: S['rejected'].append(nid)
    for d in R.get('details', []):
        nid = d['nid']
        if not d.get('ok'):
            continue
        if nid in S['items']:                       # 刷新
            it = S['items'][nid]
            for k_src, k_dst in (('v', 'v'), ('img', 'img'), ('liked', 'l'), ('collected', 'k')):
                if d.get(k_src): it[k_dst] = d[k_src]
            if d.get('imgs'): it['imgs'] = d['imgs']
            continue
        p = S['pending'].pop(nid, None)
        if not p: continue
        if not d.get('keep'):
            S['rejected'].append(nid); continue
        nt = note_time(nid) or now()
        it = {'id': d.get('id') or nid, 'c': d['c'], 'd': nt.strftime('%m-%d'), 'img': d.get('img', ''), 'h': '', 't': d.get('t') or d.get('title', ''),
              's': d.get('s', ''), 'src': d['src'], 'u': xhs_url(nid, p['tok'], p.get('xsrc', 'pc_user')), 'l': d.get('liked', ''), 'k': d.get('collected', ''),
              '_nid': nid, '_tok': p['tok'], '_xsrc': p.get('xsrc', 'pc_user'), '_uid': p.get('uid', '')}
        for k in ('v', 'se', 'ep', 'el', 'body', 'imgs'):
            if d.get(k) not in (None, '', []): it[k] = d[k]
        if dkey(it) in set(S.get('disliked', [])):
            S['rejected'].append(nid); continue
        if d['c'] == 'learn':
            it['_heat'] = num(d.get('liked')) + num(d.get('collected'))
        S['items'][nid] = it
        for b in S['bloggers']:
            if b['uid'] == p.get('uid'): b['last_new'] = t
    S['rejected'] = S['rejected'][-3000:]
    save(S); build(S)


def dislike(path):
    # 文本里每行形如 "- 艺一《困兽之战》  [se:艺一|困兽之战]"，取方括号里的 key
    S = load(); keys = re.findall(r'\[((?:se:|xhs:)[^\]]+)\]', open(path, encoding='utf-8').read())
    S.setdefault('disliked', [])
    for k in keys:
        if k not in S['disliked']: S['disliked'].append(k)
    print('disliked', S['disliked']); save(S); build(S)


def ext(path):
    S = load()
    for it in json.load(open(path, encoding='utf-8')):
        it = dict(it); it['yt'] = 1; it['c'] = 'video'
        S['items'][it['id']] = it
    for rid in []: pass
    save(S); build(S)


def num(s):
    s = str(s or '0').replace('+', '')
    try:
        return int(float(s[:-1]) * 10000) if s.endswith('万') else int(float(s))
    except Exception:
        return 0


def clean(it):
    return {k: v for k, v in it.items() if not k.startswith('_')}


def build(S):
    gen = now().strftime('%Y-%m-%dT%H:%M+08:00'); ts = now()
    DIS = set(S.get('disliked', []))
    items = [it for it in S['items'].values() if dkey(it) not in DIS]
    for it in items:
        nt = note_time(it.get('_nid', '')) if it.get('_nid') else None
        if nt is None:
            try: nt = datetime.datetime.strptime(f"{ts.year}-{it['d']}", '%Y-%m-%d').replace(tzinfo=TZ)
            except Exception: nt = ts
            if nt > ts + datetime.timedelta(days=2): nt = nt.replace(year=ts.year - 1)
        it['_dt'] = nt.timestamp()
        it.pop('n', None)
        if (ts - nt).days < 2: it['n'] = 1
    bl = {b['name']: b for b in S['bloggers']}
    author = lambda it: it['src'].split(' · ')[0]
    # ---- video ----
    vids = [it for it in items if it['c'] == 'video']
    out, chips = [], []
    series = {}
    singles = {}
    for it in vids:
        if it.get('se') and it.get('ep') is not None:
            series.setdefault((author(it), it['se']), []).append(it)
        else:
            singles.setdefault(author(it), []).append(it)
    for (a, se), L in series.items():
        b = bl.get(a, {})
        L.sort(key=lambda x: (x['ep'], x['_dt']))
        if b.get('limit'):                      # 单元剧（美妖姐姐）只要最近 N 集，按时间重新编号
            L = sorted(L, key=lambda x: x['_dt'])[-b['limit']:]
            for i, x in enumerate(L): x['ep'] = i + 1
        for i, x in enumerate(L):
            x.pop('bk', None)
            if i < len(L) - 6: x['bk'] = 1
        out += L
        last = max(L, key=lambda x: x['_dt'])
        chips.append(f"{a}《{se}》{int(last['d'][:2])}/{int(last['d'][3:])}")
    for a, L in singles.items():
        L = sorted(L, key=lambda x: -x['_dt'])[:6]
        out += L
        chips.append(f"{a}《{L[0]['t'][:10]}》{int(L[0]['d'][:2])}/{int(L[0]['d'][3:])}")
    write('video', gen, chips, out)
    # ---- comedy ----
    com = {}
    for it in items:
        if it['c'] == 'comedy': com.setdefault(author(it), []).append(it)
    cout = []
    for a, L in com.items(): cout += sorted(L, key=lambda x: -x['_dt'])[:6]
    write('comedy', gen, ['71脱口秀', '付航脱口秀', '三个火呛手', '林简七', '脱口秀小荟', '清一色脱口秀'], cout)
    # ---- learn：最近 7 天，热度前 10 ----
    L = [it for it in items if it['c'] == 'learn' and (ts.timestamp() - it['_dt']) < 7.5 * 86400]
    L.sort(key=lambda x: -(x.get('_heat') or num(x.get('l')) + num(x.get('k'))))
    write('learn', gen, ['GPT', 'Claude', 'Muse', '近 7 天 · 按点赞+收藏排序'], L[:10])
    # 太老的 learn 条目从状态里删掉
    for nid in [k for k, it in S['items'].items() if it['c'] == 'learn' and (ts.timestamp() - it['_dt']) > 8 * 86400]:
        S['items'].pop(nid)
    for it in S['items'].values(): it.pop('_dt', None)
    save(S)


def write(name, gen, chips, items):
    if not items:
        print('skip', name, '(empty)'); return
    doc = {'generated': gen, 'chips': chips, 'items': [clean(x) for x in items]}
    tmp = f'/tmp/{name}.json'
    json.dump(doc, open(tmp, 'w'), ensure_ascii=False)
    subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'seal.py'), tmp, os.path.join(ROOT, 'feed', f'{name}.enc')], check=True)
    os.makedirs(os.path.join(ROOT, 'feed', 'hist'), exist_ok=True)
    import shutil
    shutil.copy(os.path.join(ROOT, 'feed', f'{name}.enc'), os.path.join(ROOT, 'feed', 'hist', f'{name}-{today()}.enc'))


def series():
    S = load(); out = {}
    for it in S['items'].values():
        if it['c'] == 'video' and it.get('se') and it.get('ep') is not None:
            k = it['src'].split(' · ')[0] + '《' + it['se'] + '》'
            o = out.setdefault(k, {'max_ep': 0, 'eps': 0, 'external': []})
            o['eps'] += 1; o['max_ep'] = max(o['max_ep'], it['ep'])
            if it.get('yt'): o['external'].append(it['id'])
    print(json.dumps(out, ensure_ascii=False, indent=1))


def show():
    S = load()
    from collections import Counter
    print('items', len(S['items']), Counter(it['c'] for it in S['items'].values()))
    print('pending', len(S['pending']), [p['title'][:15] for p in S['pending'].values()][:20])
    print('bloggers', [(b['name'], b.get('last_check')) for b in S['bloggers']])
    exp = sorted(v_exp(it.get('v')) for it in S['items'].values() if it.get('_tok') and it.get('v'))
    if exp: print('earliest link expiry', datetime.datetime.fromtimestamp(exp[0], TZ))


if __name__ == '__main__':
    cmd = sys.argv[1]
    {'plan': plan, 'show': show, 'series': series}.get(cmd, lambda: None)() if cmd in ('plan', 'show', 'series') else {'apply': apply, 'ext': ext, 'dislike': dislike}[cmd](sys.argv[2])
