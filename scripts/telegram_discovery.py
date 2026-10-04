"""Public Telegram discovery radar. Standard library only; monitor is read-only.

Entity extraction and candidate scoring are transparent heuristics, not investment
recommendations. Publication dates come exclusively from Telegram time elements.
"""
import hashlib
import json
import os
import re
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from telegram_collect import KST, USER_AGENT, add_error, merge_existing, parse_page, parse_timestamp, post_key

D1 = (
    'SK_Research_Asset', 'sk_smallcap', 'eqmirae', 'opendisco', 'hanasmallcap',
    'SKSCyclical', 'HI_GS', 'hana_us_stock', 'siglab', 'corevalue',
    'growthresearch', 'growth_semi', 'knowledge_to_wealth', 'sksresearch', 'HanaResearch',
)
D2 = ('quantum_algo', 'STANDARDCAPITAL', 'FastStockNewsUSA', 'FastStockNews')
CHANNELS = D1 + D2
MAX_PAGES = 40
CHANNEL_SECONDS = 100
SCHEMA_VERSION = 1

# A seed vocabulary broadens recall; new names are also discovered from titles,
# tickers, hashtags, business-change context, products and named projects.
ALIASES = {
    'Anthropic': ('company', ('Anthropic', '앤트로픽')),
    'OpenAI': ('company', ('OpenAI', '오픈AI', '오픈에이아이')),
    'Google': ('company', ('Google', 'Alphabet', '알파벳', '구글')),
    'Tesla': ('company', ('Tesla', '테슬라')),
    'Oracle': ('company', ('Oracle', '오라클')),
    'NVIDIA': ('company', ('NVIDIA', '엔비디아')),
    'Micron': ('company', ('Micron', '마이크론')),
    'SpaceX': ('company', ('SpaceX', '스페이스X', '스페이스엑스')),
    '삼성전자': ('company', ('삼성전자', 'Samsung Electronics')),
    'SK하이닉스': ('company', ('SK하이닉스', 'SK hynix', '하이닉스')),
    '현대차': ('company', ('현대자동차', '현대차')),
    'LS ELECTRIC': ('company', ('LS ELECTRIC', 'LS일렉트릭')),
    'HD현대일렉트릭': ('company', ('HD현대일렉트릭',)),
    '효성중공업': ('company', ('효성중공업',)),
    'PSK': ('company', ('PSK', '피에스케이')),
    '전력망': ('industry', ('전력망', 'power grid')),
    '변압기': ('product', ('변압기', 'transformer')),
    '원자력': ('industry', ('원자력', 'nuclear')),
    '로봇': ('industry', ('로봇', 'robotics')),
    '조선': ('industry', ('조선업', '조선소', 'shipbuilding')),
    '방산': ('industry', ('방산', '방위산업')),
    '해운': ('industry', ('해운', 'shipping')),
    '제약': ('industry', ('제약', 'pharmaceutical')),
    '배터리': ('industry', ('배터리', 'battery')),
    '광통신': ('technology', ('광통신', 'optical interconnect')),
    '액체냉각': ('technology', ('액체냉각', '수냉', 'liquid cooling')),
    '데이터센터': ('industry', ('데이터센터', 'data center', 'datacenter')),
    'HBM': ('product', ('HBM',)), 'DRAM': ('product', ('DRAM', '디램')),
    'NAND': ('product', ('NAND', '낸드')),
}
STOP = set('AI CAPA Q P KST UTC HTTP HTTPS ETF IPO YoY MoM QoQ EPS PER PBR BUY SELL HOLD USD KRW CEO GDP CPI FOMC USA US NYSE NASDAQ EBITDA EBIT ROE ROA IR PDF CPI PMI LNG CNBC Reuters Bloomberg NEWS News Research Update Daily Summary The This That With From For And But Target Price Buy Sell Strong Investment Finance Monday Tuesday Wednesday Thursday Friday Saturday Sunday'.casefold().split())
KOREAN_STOP = {'시장', '주가', '기업', '업체', '고객사', '증권', '리서치', '수주', '매출', '영업이익', '뉴스', '전망', '목표주가', '투자의견', '오늘', '동사', '당사', '미국', '한국', '중국', '일본', '수출', '가격', '생산', '투자', '자료', '정부', '보고서', '이번', '산업', '사업', '실적'}
BUSINESS = re.compile(r'수주|수출|출하|판매량|생산량|증설|가동률|공급부족|병목|채택|계약|고객사|공급업체|판가|가격|제품.?믹스|마진|이익률|원가|CAPA|capacity|backlog|lead.?time|order|contract|adopt|utilization|margin|shipment|pricing', re.I)
NUMBER = re.compile(r'\d[\d,.]*\s*(?:%|퍼센트|억|조|만\s*대|대\b|배|개월|주\b|GW|MW|TWh|톤|million|billion|bn\b|mn\b|weeks?|months?)|[$₩]\s*\d[\d,.]*', re.I)
NOISE = re.compile(r'목표[주]?가|급등|상한가|테마주|관련주|찌라시|루머|소문|매수.?추천|무료.?방|VIP|수익.?인증|종목.?추천|target price|price target|rumou?r', re.I)


def save_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def load_json(path, default):
    if not path.exists():
        return default
    # Invalid state must never silently replace existing accumulated history.
    return json.loads(path.read_text(encoding='utf-8'))


def fetch_channel(channel, cutoff, now, before=None):
    errors, found, seen = [], {}, set()
    summary = {'channel': channel, 'tier': 'D1' if channel in D1 else 'D2',
               'http_status': None, 'today_post_count': 0, 'timestamp_count': 0,
               'errors_count': 0, 'fetched_pages': 0, 'history_complete': False,
               'backfill_next_before': before}
    deadline = time.monotonic() + CHANNEL_SECONDS
    base = f'https://t.me/s/{channel}'
    url = f'{base}?before={before}' if before else base
    for page in range(1, MAX_PAGES + 1):
        if time.monotonic() >= deadline:
            add_error(errors, channel, 'pagination', 'Time budget reached; history incomplete.', url)
            break
        try:
            request = Request(url, headers={'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity'})
            with urlopen(request, timeout=min(20, max(1, deadline - time.monotonic()))) as response:
                summary['http_status'] = response.status
                if response.status != 200:
                    add_error(errors, channel, 'http', 'Non-200 response.', url, response.status)
                    break
                html = response.read().decode(response.headers.get_content_charset() or 'utf-8')
            records, timestamps = parse_page(channel, html, now.isoformat(timespec='seconds'), errors, url)
            summary['fetched_pages'] += 1
            summary['timestamp_count'] += timestamps
            if timestamps == 0:
                add_error(errors, channel, 'parse', 'No <time datetime> extracted; history unverified.', url)
                break
            if not records:
                add_error(errors, channel, 'parse', 'No valid timestamped posts; history unverified.', url)
                break
            new = {post_key(p) for p in records} - seen
            seen.update(post_key(p) for p in records)
            for p in records:
                published = parse_timestamp(p['published_at_kst'])
                if cutoff <= published <= now:
                    found.setdefault(post_key(p), p)
            if any(parse_timestamp(p['published_at_kst']) < cutoff for p in records):
                summary['history_complete'] = not errors
                break
            if not new:
                add_error(errors, channel, 'pagination', 'Repeated page; history incomplete.', url)
                break
            before = min(int(p['message_id'].rsplit('/', 1)[1]) for p in records)
            summary['backfill_next_before'] = before
            if before == 1:
                summary['history_complete'] = not errors
                break
            url = f'{base}?before={before}'
            time.sleep(0.15)
        except HTTPError as exc:
            summary['http_status'] = exc.code
            add_error(errors, channel, 'http', f'HTTP {exc.code}: {exc.reason}', url, exc.code)
            exc.close()
            break
        except (OSError, URLError) as exc:
            add_error(errors, channel, 'http', f'{type(exc).__name__}: {exc}', url)
            break
        except Exception as exc:
            add_error(errors, channel, 'parse', f'{type(exc).__name__}: {exc}', url)
            break
    else:
        add_error(errors, channel, 'pagination', 'Page budget reached; history incomplete.', url)
    summary['today_post_count'] = sum(parse_timestamp(p['published_at_kst']).date() == now.date() for p in found.values())
    if summary['history_complete']:
        summary['backfill_next_before'] = None
    summary['errors_count'] = len(errors)
    summary['earliest_fetched_at_kst'] = min((p['published_at_kst'] for p in found.values()), default=None)
    print(f"RESULT | {channel} | HTTP {summary['http_status']} | timestamps {summary['timestamp_count']} | today {summary['today_post_count']} | history complete {summary['history_complete']} | errors {len(errors)}", flush=True)
    return list(found.values()), summary, errors


def entities(body):
    result = {}
    folded = body.casefold()
    for canonical, (category, aliases) in ALIASES.items():
        for alias in aliases:
            pattern = re.escape(alias.casefold())
            if alias.isascii():
                pattern = r'(?<![a-z0-9])' + pattern + r'(?![a-z0-9])'
            if re.search(pattern, folded):
                result[canonical.casefold()] = (canonical, category)
                break

    def add(name, category):
        name = name.strip(' #$[]():,.-•■▶◈')
        name = re.sub(r'(?:은|는|의|에서|으로|가|이)$', '', name) if len(name) > 3 and re.search('[가-힣]', name) else name
        if len(name) < 2 or len(name) > 40 or name.casefold() in STOP or name in KOREAN_STOP:
            return
        # Canonical aliases always win over spelling variants.
        for canonical, (cat, aliases) in ALIASES.items():
            if any(name.casefold() == a.casefold() for a in aliases):
                result[canonical.casefold()] = (canonical, cat)
                return
        result.setdefault(name.casefold(), (name, category))

    for name in re.findall(r'([가-힣A-Za-z][가-힣A-Za-z0-9& .-]{1,25}?)\s*\(\s*(?:A)?\d{6}\s*\)', body):
        add(name, 'company')
    for ticker in re.findall(r'(?<!\w)\$([A-Z]{1,6})(?![a-zA-Z])|(?:NYSE|NASDAQ)\s*:\s*([A-Z]{1,6})', body):
        add(next(x for x in ticker if x), 'ticker')
    for name in re.findall(r'#([가-힣A-Za-z][가-힣A-Za-z0-9_]{1,25})', body):
        add(name, 'tag')
    for name in re.findall(r'([가-힣A-Za-z][가-힣A-Za-z0-9_-]{1,24})\s+(?:프로젝트|Project)', body, re.I):
        add(name + ' 프로젝트', 'project')
    for name in re.findall(r'([가-힣A-Za-z][가-힣A-Za-z0-9_-]{1,24})(?:의|는|가|에서)\s*(?:신규\s*)?(?:수주|계약|증설|채택|가동률|가격|공급)', body):
        add(name, 'business_entity')
    for name in re.findall(r'\b(?:[A-Z][a-z]+(?:[A-Z][a-zA-Z0-9]+)+|[A-Z][a-z]{2,18}|[A-Z]{2,8}\d{0,3})\b', body):
        add(name, 'name_or_technology')
    # Korean report titles often put a company after a research label.
    for line in body.splitlines()[:5]:
        match = re.match(r'^\s*(?:\[[^]]{1,35}\]\s*|[■▶●]\s*)([가-힣A-Za-z][가-힣A-Za-z0-9&_-]{1,22})\s*(?:[:：\(]|$)', line)
        if match:
            add(match[1], 'name_or_industry')
    return sorted(result.values(), key=lambda x: x[0].casefold())


def signals(body):
    evidence = []
    for match in NUMBER.finditer(body):
        context = body[max(0, match.start() - 100):match.end() + 100].replace('\n', ' ')
        if BUSINESS.search(context) and context not in evidence:
            evidence.append(context)
    paths = []
    for path, terms in [('Q', r'수주|수출|출하|판매량|생산|증설|가동률|채택|계약|CAPA|backlog|capacity|shipment|order|contract'),
                        ('P', r'판가|가격|pricing|price'), ('mix', r'믹스|고부가|프리미엄|mix'),
                        ('margin', r'마진|이익률|원가|margin|cost')]:
        if re.search(terms, body, re.I):
            paths.append(path)
    noise = NOISE.findall(body)
    return {'business_numbers': evidence[:8], 'profit_paths': paths,
            'noise_flags': sorted(set(noise)),
            'eligible': bool(evidence) and not re.search(r'찌라시|루머|소문|무료.?방|VIP|수익.?인증|rumou?r', body, re.I)}


def normalized(body):
    text = unicodedata.normalize('NFKC', body).casefold()
    text = re.sub(r'https?://\S+', '', text)
    lines = [line for line in text.splitlines() if not re.search(r'텔레그램|telegram|컴플라이언스|무단.*배포|투자.*책임|연구원|애널리스트|\*?t\.?\s*\d{2,3}-', line)]
    return re.sub(r'[^가-힣a-z0-9%]+', '', '\n'.join(lines))


def source_links(body):
    sources = set()
    for raw in re.findall(r'https?://[^\s<>]+', body):
        parts = urlsplit(raw.rstrip(').,]'))
        host = parts.netloc.casefold()
        # Ignore channel home pages, signup links and URL shorteners: these are
        # not proof of a shared original article. Telegram message links qualify.
        if host in {'t.me', 'telegram.me'} and not re.search(r'/\d+$', parts.path):
            continue
        if host in {'bit.ly', 'han.gl', 'tinyurl.com', 'url.kr'}:
            continue
        if not re.search(r'\d{3,}|\.pdf$|/article/|/news/|/report/', parts.path + '?' + parts.query, re.I):
            continue
        query = '&'.join(q for q in parts.query.split('&') if q and not q.startswith(('utm_', 'fbclid=', 'gclid=')))
        sources.add(urlunsplit(('https', host, parts.path, query, '')))
    return sources


def simhash(text):
    tokens = [text[i:i + 5] for i in range(0, max(0, len(text) - 4), 3)]
    weights = [0] * 64
    for token in set(tokens):
        value = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), 'big')
        for i in range(64):
            weights[i] += 1 if value & (1 << i) else -1
    return sum(1 << i for i, weight in enumerate(weights) if weight >= 0)


def clusters(posts):
    """Deterministic source clusters: exact/near-copy text or same original URL."""
    parent = list(range(len(posts)))
    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    def union(a, b):
        a, b = root(a), root(b)
        parent[max(a, b)] = min(a, b)
    exact, links, buckets, hashes, lengths = {}, {}, defaultdict(list), {}, {}
    for i, post in enumerate(posts):
        text = normalized(post['body'])
        fingerprint = hashlib.sha256(text.encode()).hexdigest()
        if text and fingerprint in exact:
            union(i, exact[fingerprint])
        exact[fingerprint] = i
        for link in source_links(post['body']):
            if link in links:
                union(i, links[link])
            links[link] = i
        if len(text) < 120:
            continue
        value = simhash(text)
        keys = [(part, (value >> (part * 16)) & 65535) for part in range(4)]
        possible = {j for key in keys for j in buckets[key]}
        for j in possible:
            if min(len(text), lengths[j]) / max(len(text), lengths[j]) >= 0.75 and (value ^ hashes[j]).bit_count() <= 3:
                union(i, j)
        hashes[i], lengths[i] = value, len(text)
        for key in keys:
            buckets[key].append(i)
    return {post_key(p): f'source-{root(i)}' for i, p in enumerate(posts)}


def independent_names(posts, source_groups):
    # Maximum matching ensures one count per channel AND per original source.
    graph = defaultdict(set)
    for p in posts:
        graph[p['channel']].add(source_groups[post_key(p)])
    assigned = {}
    def assign(channel, seen):
        for group in sorted(graph[channel]):
            if group in seen:
                continue
            seen.add(group)
            if group not in assigned or assign(assigned[group], seen):
                assigned[group] = channel
                return True
        return False
    for channel in sorted(graph, key=lambda c: (c in D2, c.casefold())):
        assign(channel, set())
    return sorted(set(assigned.values()))


def analyze(posts, now, baseline_complete, first_seen_registry=None):
    since = now - timedelta(hours=72)
    recent = [p for p in posts if parse_timestamp(p['published_at_kst']) >= since]
    group = clusters(recent)
    by_entity = defaultdict(list)
    names = {}
    for p in posts:
        for entity, category in entities(p['body']):
            key = entity.casefold()
            names[key] = (entity, category)
            by_entity[key].append(p)
    rows, candidates = [], []
    first_seen_registry = first_seen_registry if first_seen_registry is not None else {}
    for key in sorted(by_entity):
        records = by_entity[key]
        latest = [p for p in records if parse_timestamp(p['published_at_kst']) >= since]
        prior = [p for p in records if parse_timestamp(p['published_at_kst']) < since]
        independent = independent_names(latest, group)
        entity, category = names[key]
        observed_first = records[0]['published_at_kst']
        first_seen_registry[key] = min(first_seen_registry.get(key, observed_first), observed_first)
        row = {'entity': entity, 'category': category,
               'first_seen': first_seen_registry[key], 'last_seen': records[-1]['published_at_kst'],
               'mention_count': len(records), 'channel_count': len({p['channel'] for p in records}),
               'mentions_72h': len(latest), 'prior_mentions': len(prior),
               'independent_channels': len(independent),
               'permalinks': [p['permalink'] for p in records]}
        rows.append(row)
        eligible = [(p, signals(p['body'])) for p in latest if signals(p['body'])['eligible']]
        if not eligible:
            continue
        evidence = []
        for p, signal in eligible:
            # Evidence numbers must be near this entity, not elsewhere in a long digest.
            aliases = ALIASES.get(entity, (category, (entity,)))[1]
            contexts = [s for s in signal['business_numbers'] if any(a.casefold() in s.casefold() for a in aliases)]
            if not contexts:
                continue
            evidence.append({'channel': p['channel'], 'tier': 'D1' if p['channel'] in D1 else 'D2',
                             'message_id': p['message_id'], 'permalink': p['permalink'],
                             'published_at_kst': p['published_at_kst'], 'source_cluster': group[post_key(p)],
                             'business_numbers': contexts[:3], 'profit_paths': signal['profit_paths'],
                             'noise_flags': signal['noise_flags']})
        strong_posts = [p for p in latest if any(e['message_id'] == p['message_id'] and e['channel'] == p['channel'] for e in evidence)]
        strong_independent = independent_names(strong_posts, group)
        if not evidence:
            continue
        if len(independent) < 2 and len(prior) > 2:
            continue
        if len(prior) > 2 and len(latest) < max(3, 2 * len(prior)):
            continue
        d1 = any(p['channel'] in D1 for p in strong_posts)
        status = 'follow_up' if d1 and len(strong_independent) >= 2 else 'radar_only'
        novelty = 'rare_in_observed_prior_window' if baseline_complete and len(prior) <= 2 else 'unconfirmed'
        reasons = [f'최근 72시간 {len(latest)}개 게시물, 재전송 제거 후 {len(independent)}개 독립 채널',
                   f'엔티티 주변 사업 숫자 포착 {len(evidence)}개 게시물, 숫자 근거 독립 채널 {len(strong_independent)}개']
        reasons.append('직전 관측 구간 언급이 드묾' if novelty != 'unconfirmed' else '30일 이력 또는 신규성 근거가 부족하므로 신규 등장 미확정')
        candidates.append({'entity': entity, 'category': category, 'first_seen': row['first_seen'],
                           'mentions_72h': len(latest), 'independent_channels': len(independent),
                           'independent_channel_names': independent, 'prior_mentions': len(prior),
                           'business_independent_channels': len(strong_independent),
                           'status': status, 'novelty': novelty, 'official_verification': 'pending',
                           'evidence_posts': evidence[:12], 'why_unusual': reasons,
                           'noise_penalty': sum(bool(e['noise_flags']) for e in evidence)})
    candidates.sort(key=lambda c: (c['status'] != 'follow_up', c['noise_penalty'], -c['business_independent_channels'], -c['independent_channels'], c['entity'].casefold()))
    return rows, candidates


def main(output_dir=Path('telegram_discovery'), now=None):
    now = (now or datetime.now(KST)).astimezone(KST)
    cutoff = now - timedelta(days=30)
    root = Path(output_dir)
    state_path = root / 'baseline' / 'state.json'
    state = load_json(state_path, {'posts': [], 'coverage': {}, 'started_at_kst': now.isoformat(timespec='seconds')})
    previous = {post_key(p): p for p in state['posts'] if cutoff <= parse_timestamp(p['published_at_kst']) <= now}
    coverage = state.get('coverage', {})
    summaries, errors, fetched = [], [], []
    def collect(channel):
        # Retry a bounded 30-day bootstrap until complete. Afterwards use a
        # 72-hour overlap, widened to cover any missed run.
        old = coverage.get(channel, {})
        requested = cutoff
        if old.get('history_complete'):
            requested = max(cutoff, min(now - timedelta(hours=72), parse_timestamp(old['last_success_at_kst']) - timedelta(hours=1)))
        cursor = old.get('backfill_next_before')
        if cursor and not old.get('history_complete'):
            fresh, current, fresh_errors = fetch_channel(channel, now - timedelta(hours=72), now)
            history, historical, history_errors = fetch_channel(channel, cutoff, now, before=cursor)
            combined = {post_key(p): p for p in fresh + history}
            current['timestamp_count'] += historical['timestamp_count']
            current['fetched_pages'] += historical['fetched_pages']
            current['errors_count'] = len(fresh_errors) + len(history_errors)
            current['today_post_count'] = sum(parse_timestamp(p['published_at_kst']).date() == now.date() for p in combined.values())
            current['history_complete'] = historical['history_complete'] and current['history_complete']
            current['backfill_next_before'] = historical['backfill_next_before']
            current['earliest_fetched_at_kst'] = min((p['published_at_kst'] for p in combined.values()), default=None)
            return list(combined.values()), current, fresh_errors + history_errors
        return fetch_channel(channel, requested, now)
    with ThreadPoolExecutor(max_workers=4) as executor:
        for channel, result in zip(CHANNELS, executor.map(collect, CHANNELS)):
            records, summary, channel_errors = result
            fetched.extend(records)
            summaries.append(summary)
            errors.extend(channel_errors)
            old = coverage.get(channel, {})
            complete = summary['history_complete']
            coverage[channel] = {
                'history_complete': complete and (old.get('history_complete', False) or not old),
                'last_success_at_kst': now.isoformat(timespec='seconds') if complete else old.get('last_success_at_kst', now.isoformat(timespec='seconds')),
                'earliest_observed_at_kst': min(filter(None, [old.get('earliest_observed_at_kst'), summary['earliest_fetched_at_kst']]), default=None),
                'latest_run_complete': complete, 'errors_count': len(channel_errors),
                'backfill_next_before': summary.get('backfill_next_before'),
            }
            # A successful full-cutoff retry completes a formerly partial bootstrap.
            if complete and not old.get('history_complete'):
                coverage[channel]['history_complete'] = True
    for p in fetched:
        previous.setdefault(post_key(p), p)
    posts = sorted(previous.values(), key=lambda p: (parse_timestamp(p['published_at_kst']), post_key(p)))
    today_posts = [p for p in fetched if parse_timestamp(p['published_at_kst']).date() == now.date()]
    daily_path = root / f'{now.date().isoformat()}.json'
    daily = merge_existing(daily_path, today_posts, now.date(), errors)
    baseline_complete = all(coverage.get(c, {}).get('history_complete') for c in CHANNELS)
    first_seen_registry = state.get('first_seen_registry', {})
    rows, candidates = analyze(posts, now, baseline_complete, first_seen_registry)
    metadata = {'collected_at_kst': now.isoformat(timespec='seconds'), 'window_start_kst': cutoff.isoformat(timespec='seconds'),
                'window_end_kst': now.isoformat(timespec='seconds'), 'schema_version': SCHEMA_VERSION,
                'baseline_status': 'complete' if baseline_complete else 'partial',
                'coverage': coverage, 'errors': errors}
    save_json(daily_path, {'date_kst': now.date().isoformat(), 'collected_at_kst': metadata['collected_at_kst'],
                           'total_posts': len(daily), 'channels': summaries, 'posts': daily, 'errors': errors})
    save_json(state_path, dict(metadata, started_at_kst=state['started_at_kst'], first_seen_registry=first_seen_registry, total_posts=len(posts), posts=posts))
    save_json(root / 'baseline' / 'entities.json', dict(metadata, total_entities=len(rows), entities=rows))
    save_json(root / 'candidates' / f'{now.date().isoformat()}.json', dict(metadata, date_kst=now.date().isoformat(),
                                                                 total_candidates=len(candidates), candidates=candidates))
    print(f'DAILY={len(daily)} BASELINE_POSTS={len(posts)} ENTITIES={len(rows)} CANDIDATES={len(candidates)} ERRORS={len(errors)} BASELINE_STATUS={metadata["baseline_status"]}', flush=True)
    for candidate in candidates[:3]:
        print('CANDIDATE_EXAMPLE=' + json.dumps(candidate, ensure_ascii=False), flush=True)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        lines = [f'## Discovery Scout {now.date()} (Asia/Seoul)', '', '| Channel | HTTP | Today | Timestamps | Errors |', '| --- | ---: | ---: | ---: | ---: |']
        lines.extend(f"| {s['channel']} | {s['http_status']} | {s['today_post_count']} | {s['timestamp_count']} | {s['errors_count']} |" for s in summaries)
        lines += ['', f'Daily posts: **{len(daily)}**. Baseline posts: **{len(posts)}**. Entities: **{len(rows)}**.',
                  f'Candidates: **{len(candidates)}**. Baseline status: **{metadata["baseline_status"]}**. Errors: **{len(errors)}**.',
                  '', 'Candidates are unverified follow-up prompts, not investment conclusions.']
        for c in candidates[:3]:
            lines += ['', f"- **{c['entity']}** ({c['status']}): {c['independent_channels']} independent channels; novelty {c['novelty']}."]
        Path(os.environ['GITHUB_STEP_SUMMARY']).open('a', encoding='utf-8').write('\n'.join(lines) + '\n')
    return {'daily': daily, 'baseline_posts': posts, 'entities': rows, 'candidates': candidates, 'channels': summaries, 'errors': errors}


if __name__ == '__main__':
    main()
