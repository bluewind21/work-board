import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import telegram_discovery as scout
from telegram_collect import parse_page, post_key

NOW = datetime.fromisoformat('2026-10-04T18:20:00+09:00')


def post(channel, ident, body, hours=1):
    return {'channel': channel, 'message_id': f'{channel}/{ident}',
            'collected_at_kst': NOW.isoformat(), 'published_at_kst': (NOW - timedelta(hours=hours)).isoformat(),
            'permalink': f'https://t.me/{channel}/{ident}', 'body': body}


def summary(channel, count, complete=True):
    return {'channel': channel, 'http_status': 200, 'today_post_count': count,
            'timestamp_count': count, 'errors_count': 0, 'history_complete': complete,
            'earliest_fetched_at_kst': None, 'fetched_pages': 1}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        # Synthetic fixtures must not appear in the real Actions job summary.
        env = patch.dict('os.environ', {'GITHUB_STEP_SUMMARY': ''})
        env.start()
        self.addCleanup(env.stop)

    def test_publication_date_uses_html_time_and_aware_offsets(self):
        html = '<div class="tgme_channel_history"><div class="tgme_widget_message" data-post="siglab/1"><div class="tgme_widget_message_text">2020-01-01 뉴스 날짜</div><time datetime="2026-10-03T23:30:00+00:00"></time></div></div>'
        records, count = parse_page('siglab', html, NOW.isoformat(), [], 'https://t.me/s/siglab')
        self.assertEqual(count, 1)
        self.assertEqual(records[0]['published_at_kst'], '2026-10-04T08:30:00+09:00')

    def test_new_entity_not_in_seed_and_business_numbers(self):
        body = '뉴브릿지(123456)\n뉴브릿지의 신규 수주 300억원, 가동률 95%와 마진 20%로 개선'
        self.assertIn(('뉴브릿지', 'company'), scout.entities(body))
        self.assertTrue(scout.signals(body)['eligible'])
        self.assertIn('Q', scout.signals(body)['profit_paths'])
        self.assertIn('margin', scout.signals(body)['profit_paths'])

    def test_retransmitted_article_counts_once(self):
        a = post('siglab', 1, '뉴브릿지(123456) 신규 수주 300억원 https://example.com/news/article/12345')
        b = post('corevalue', 2, '다른 제목 뉴브릿지(123456) 신규 계약 200억원 https://example.com/news/article/12345?utm_source=tg')
        groups = scout.clusters([a, b])
        self.assertEqual(groups[post_key(a)], groups[post_key(b)])
        self.assertEqual(len(scout.independent_names([a, b], groups)), 1)

    def test_fuzzy_copy_and_separate_sources(self):
        text = ('뉴브릿지 신규 수주 300억원. 공장 가동률 95%로 상승했고 고객사 공급 계약도 확대된다. ' * 6)
        a = post('siglab', 1, text)
        b = post('corevalue', 2, text + '추가 의견입니다.')
        c = post('opendisco', 3, '뉴브릿지 수출 700억원과 새 고객사 채택 5건, 생산량 40% 증가, 가격 15% 개선')
        groups = scout.clusters([a, b, c])
        self.assertEqual(groups[post_key(a)], groups[post_key(b)])
        self.assertNotEqual(groups[post_key(a)], groups[post_key(c)])

    def test_real_contract_rewrite_and_tracking_urls_are_one_source(self):
        a = post('opendisco', 1, '일진전기(103590.KS) 수주공시 1,871.7억 상대 J. MURPHY & SONS LIMITED')
        b = post('FastStockNews', 2, '일진전기(103590) 공급계약 계약총액 1,872억원, 매출대비 9.15%')
        groups = scout.clusters([a, b])
        self.assertEqual(groups[post_key(a)], groups[post_key(b)])
        self.assertEqual(scout.source_links('https://biz.example.com/article/12345?ref=naver'), scout.source_links('https://biz.example.com/article/12345'))
        extracted = [name for name, _ in scout.entities('창사 최대 프로젝트 수주 100억원 J. MURPHY & SONS LIMITED')]
        self.assertNotIn('최대 프로젝트', extracted)
        self.assertNotIn('SONS', extracted)
        self.assertNotIn('MURPHY', extracted)

    def test_generic_financial_words_are_not_entities(self):
        names = [name for name, _ in scout.entities('Monthly Blended Upfront Downstream Call Edition Revenue Billion')]
        self.assertEqual(names, [])

    def test_unrelated_digest_numbers_do_not_create_an_entity_signal(self):
        records = [post('siglab', 1, 'Abogen 소식은 단순 의견\n삼성전자 신규 수주 300억원 확대')]
        candidates = scout.analyze(records, NOW, False)[1]
        self.assertNotIn('Abogen', [c['entity'] for c in candidates])

    def test_independent_new_candidate_and_cold_start(self):
        records = [post('siglab', 1, '뉴브릿지(123456) 신규 수주 300억원, 생산량 40% 확대', 2),
                   post('corevalue', 2, '뉴브릿지(123456) 신규 고객사 채택 후 가격 15% 인상', 1)]
        rows, candidates = scout.analyze(records, NOW, False)
        candidate = next(c for c in candidates if c['entity'] == '뉴브릿지')
        self.assertEqual(candidate['independent_channels'], 2)
        self.assertEqual(candidate['status'], 'follow_up')
        self.assertEqual(candidate['novelty'], 'unconfirmed')
        self.assertEqual(candidate['official_verification'], 'pending')
        _, warm = scout.analyze(records, NOW, True)
        self.assertEqual(next(c for c in warm if c['entity'] == '뉴브릿지')['novelty'], 'rare_in_observed_prior_window')

    def test_rumor_target_price_and_price_jump_excluded(self):
        for body in ['뉴브릿지(123456) 목표가 30% 상향, 오늘 급등 20%',
                     '뉴브릿지(123456) 수주 300억원 루머, VIP 추천']:
            self.assertEqual(scout.analyze([post('siglab', 1, body)], NOW, True)[1], [])

    def test_d2_only_is_radar(self):
        records = [post('FastStockNews', 1, '뉴브릿지(123456) 수주 300억원 확대'),
                   post('quantum_algo', 2, '뉴브릿지(123456) 가동률 90% 상승, 가격 10% 인상')]
        candidates = scout.analyze(records, NOW, True)[1]
        self.assertTrue(candidates)
        self.assertTrue(all(c['status'] == 'radar_only' for c in candidates))

    def test_two_runs_preserve_dedup_sort_latest_status_and_baseline(self):
        first = post('siglab', 1, '뉴브릿지(123456) 수주 300억원', 2)
        earlier = post('corevalue', 1, '다른 기업 수출 100억원', 3)
        old = post('siglab', 10, '뉴브릿지(123456) 수주 50억원', 10 * 24)
        expired = post('siglab', 11, '오래된 자료', 31 * 24)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def initial(channel, cutoff, now):
                records = [first, old, expired] if channel == 'siglab' else []
                # fetch contract only returns within requested window
                records = [p for p in records if cutoff <= scout.parse_timestamp(p['published_at_kst'])]
                errors = [{'channel': channel, 'type': 'http', 'message': 'old failure'}] if channel == 'opendisco' else []
                return records, summary(channel, int(channel == 'siglab'), not errors), errors
            with patch.object(scout, 'fetch_channel', side_effect=initial), contextlib.redirect_stdout(io.StringIO()):
                a = scout.main(root, NOW)
            self.assertEqual(len(a['daily']), 1)
            self.assertEqual(len(a['baseline_posts']), 2)
            def second(channel, cutoff, now):
                records = [dict(first, body='changed copy'), first] if channel == 'siglab' else [earlier] if channel == 'corevalue' else []
                return records, summary(channel, int(bool(records))), []
            with patch.object(scout, 'fetch_channel', side_effect=second), contextlib.redirect_stdout(io.StringIO()):
                b = scout.main(root, NOW + timedelta(minutes=1))
            self.assertEqual(b['daily'], [earlier, first])
            self.assertEqual(b['errors'], [])
            self.assertEqual(len(b['baseline_posts']), 3)
            saved = json.loads((root / '2026-10-04.json').read_text())
            self.assertEqual(saved['total_posts'], 2)
            self.assertEqual(len(saved['channels']), 19)
            self.assertEqual(len({post_key(p) for p in saved['posts']}), 2)
            self.assertTrue((root / 'baseline/entities.json').is_file())
            self.assertTrue((root / 'candidates/2026-10-04.json').is_file())
            self.assertFalse((root.parent / 'telegram/2026-10-04.json').exists())

    def test_first_seen_survives_rolling_window(self):
        registry = {}
        first = post('siglab', 1, '뉴브릿지(123456) 수주 300억원', 700)
        scout.analyze([first], NOW, False, registry)
        newer = post('siglab', 2, '뉴브릿지(123456) 수주 500억원')
        rows, _ = scout.analyze([newer], NOW, False, registry)
        self.assertEqual(next(r for r in rows if r['entity'] == '뉴브릿지')['first_seen'], first['published_at_kst'])

    def test_schedule_and_monitor_boundary(self):
        content = (Path(__file__).resolve().parents[1] / '.github/workflows/telegram-discovery.yml').read_text()
        for utc, kst in [(21, 6), (3, 12), (9, 18)]:
            self.assertIn(f"cron: '20 {utc} * * *'", content)
            self.assertEqual((utc + 9) % 24, kst)
        self.assertIn('workflow_dispatch:', content)
        self.assertIn('git add -- telegram_discovery', content)
        self.assertNotIn('git add -- telegram/', content)


if __name__ == '__main__':
    unittest.main()
