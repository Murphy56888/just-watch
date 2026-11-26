# 把明文 feed JSON 加密成 app 能读的 .enc 文件（RSA-OAEP-256 包裹 AES-256-GCM 密钥）。
# 用法: python3 tools/seal.py <明文.json> <feed/xxx.enc>
# 明文格式: {"generated": "2026-11-26T07:35+08:00", "chips": ["..."], "items": [ {...}, ... ]}
import sys, json, os, base64
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CATS = {'news', 'invest', 'talk', 'video', 'learn', 'comedy'}
REQ = ['id', 'c', 'd', 't', 's', 'src', 'u']


def check(doc):
    assert isinstance(doc.get('generated'), str), 'generated 缺失'
    assert isinstance(doc.get('items'), list) and doc['items'], 'items 为空'
    ids = set()
    for it in doc['items']:
        for k in REQ:
            assert it.get(k), f'条目缺少 {k}: {it}'
        assert it['c'] in CATS, f'未知板块 {it["c"]}'
        assert it['id'] not in ids, f'id 重复 {it["id"]}'
        ids.add(it['id'])
        assert len(it['d']) in (2, 5), f'日期格式应为 MM-DD: {it["d"]}'
        assert it['u'].startswith('https://'), f'链接须为 https: {it["u"]}'
        if it['c'] == 'learn' and it.get('v'):
            assert '.mp4' in it['v'], f'视频帖需要 mp4 直链: {it["id"]}'
        if it['c'] in ('video', 'comedy'):
            assert it.get('v', '').startswith('https://') and '.mp4' in it['v'], f'剧集缺少 mp4 直链: {it["id"]}'
        if it['c'] in ('video', 'talk', 'learn', 'comedy'):
            assert it.get('img'), f'缺少封面: {it["id"]}'


def main(src, dst):
    doc = json.load(open(src, encoding='utf-8'))
    check(doc)
    here = os.path.dirname(os.path.abspath(__file__))
    pub = serialization.load_pem_public_key(open(os.path.join(here, 'public.pem'), 'rb').read())
    key = AESGCM.generate_key(256)
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, json.dumps(doc, ensure_ascii=False).encode('utf-8'), None)
    wk = pub.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    e = lambda b: base64.b64encode(b).decode()
    json.dump({'v': 1, 'k': e(wk), 'iv': e(iv), 'ct': e(ct)}, open(dst, 'w'))
    print('sealed', dst, len(doc['items']), 'items')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
