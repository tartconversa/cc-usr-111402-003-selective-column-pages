"""列页格式（version=2）的契约、编码、稀疏读取、损坏检测与原子发布。"""
import copy
import json
import os
import random
import struct
import tempfile
import unittest
from pathlib import Path

from app import columnar as C
from app.baseline import read as baseline_read

SAMPLE = json.loads((Path(__file__).resolve().parents[1]
                     / 'fixtures' / 'sample.json').read_text(encoding='utf-8'))
I64_MIN, I64_MAX = -(2 ** 63), 2 ** 63 - 1


def make_data(rows, schema=None):
    schema = schema or {'region': 'text', 'amount': 'i64'}
    half = max(1, (len(rows) + 1) // 2)
    return {
        'version': 1, 'schema': schema,
        'groups': [{'rows': rows[:half]}, {'rows': rows[half:]}],
    }


class RoundTripTests(unittest.TestCase):
    def _assert_equivalent(self, data, page_rows):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'd.colpf')
            C.write_container(path, data, page_rows=page_rows)
            rows = [r for g in data['groups'] for r in g['rows']]
            positions = list(range(len(rows)))
            expected = baseline_read(data, list(data['schema']), positions)
            with C.ColumnFile(path) as cf:
                got = cf.read(list(data['schema']), positions)
            self.assertEqual(got, expected)
            # 重复且乱序的行号，按请求顺序返回
            shuffled = positions[::-1] + positions[:3]
            self.assertEqual(
                C.read_column_file(path, list(data['schema']), shuffled)[0],
                baseline_read(data, list(data['schema']), shuffled))

    def test_sample_all_page_sizes(self):
        for pr in (1, 2, 3, 1000):
            self._assert_equivalent(SAMPLE, pr)

    def test_randomized_equivalence(self):
        rnd = random.Random(20261114)
        regions = ['CN', 'US', 'EU', 'JP', '', 'CN', 'CN', None, 'US']
        rows = []
        for i in range(500):
            rows.append({
                'region': rnd.choice(regions),
                'amount': rnd.choice([None, rnd.randint(I64_MIN, I64_MAX),
                                      rnd.choice([7, 7, 7, -3, -3])]),
            })
        data = make_data(rows)
        for pr in (1, 7, 64, 1000):
            self._assert_equivalent(data, pr)

    def test_extreme_i64_bounds(self):
        rows = [{'amount': I64_MIN}, {'amount': I64_MAX},
                {'amount': 0}, {'amount': -1}, {'amount': I64_MAX - 1}]
        data = make_data([{'region': 'x', 'amount': r['amount']} for r in rows])
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'd.colpf')
            C.write_container(path, data, page_rows=2)
            got, _ = C.read_column_file(path, ['amount'], [0, 1, 2, 3, 4])
            self.assertEqual([r['amount'] for r in got],
                             [I64_MIN, I64_MAX, 0, -1, I64_MAX - 1])

    def test_bool_is_not_integer(self):
        data = copy.deepcopy(SAMPLE)
        data['groups'][0]['rows'][0]['amount'] = True
        with self.assertRaisesRegex(ValueError, '范围'):
            with tempfile.TemporaryDirectory() as td:
                C.write_container(os.path.join(td, 'd.colpf'), data)

    def test_unknown_type_rejected(self):
        data = copy.deepcopy(SAMPLE)
        data['schema']['amount'] = 'f64'
        with self.assertRaisesRegex(ValueError, '未知列类型'):
            with tempfile.TemporaryDirectory() as td:
                C.write_container(os.path.join(td, 'd.colpf'), data)


class NullAndEmptyStringTests(unittest.TestCase):
    def test_null_does_not_consume_value_stream_index(self):
        data = {'version': 1,
                'schema': {'region': 'text', 'amount': 'i64'},
                'groups': [{'rows': [
                    {'region': 'CN', 'amount': 1},
                    {'region': None, 'amount': None},
                    {'region': '', 'amount': 2},
                    {'region': None, 'amount': 3}]}]}
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'd.colpf')
            C.write_container(path, data, page_rows=10)
            got, stats = C.read_column_file(
                path, ['region', 'amount'], [0, 1, 2, 3])
            self.assertEqual(got, [
                {'region': 'CN', 'amount': 1},
                {'region': None, 'amount': None},
                {'region': '', 'amount': 2},
                {'region': None, 'amount': 3},
            ])
            with C.ColumnFile(path) as cf:
                desc = cf._index['amount'][0]
                self.assertEqual(desc['non_null'], 3)   # 两个空值不进值流
                self.assertTrue(desc['has_nulls'])
                self.assertGreater(desc['bitmap_len'], 0)

    def test_empty_string_is_a_value_not_null(self):
        data = {'version': 1, 'schema': {'region': 'text'},
                'groups': [{'rows': [{'region': ''}, {'region': 'x'}]}]}
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'd.colpf')
            C.write_container(path, data)
            got, _ = C.read_column_file(path, ['region'], [0, 1])
            self.assertEqual(got, [{'region': ''}, {'region': 'x'}])
            with C.ColumnFile(path) as cf:
                desc = cf._index['region'][0]
                self.assertFalse(desc['has_nulls'])      # 无 null，位图省略
                self.assertEqual(desc['non_null'], 2)


class EncodingSelectionTests(unittest.TestCase):
    def test_i64_encodings_all_exercised(self):
        rle_data = make_data([{'region': 'a', 'amount': 5} for _ in range(20)])
        delta_data = make_data([{'region': 'a', 'amount': i} for i in range(20)])
        # 交替极大/极小值使差分与游程都不省字节，强制普通定长退路
        plain_rows = []
        for i in range(20):
            plain_rows.append({'region': 'a',
                               'amount': I64_MAX - i if i % 2 == 0 else I64_MIN + i})
        plain_data = make_data(plain_rows)
        seen = set()
        with tempfile.TemporaryDirectory() as td:
            for idx, data in enumerate((rle_data, delta_data, plain_data)):
                path = os.path.join(td, 'd%d.colpf' % idx)
                C.write_container(path, data)
                layout = C.inspect_layout(path)
                seen |= {p['encoding'] for p in layout['pages']
                         if p['column'] == 'amount'}
        self.assertIn(C.I64_RLE, seen)
        self.assertIn(C.I64_DELTA, seen)
        self.assertIn(C.I64_PLAIN, seen)

    def test_text_dict_and_plain(self):
        # 全部落同一页、重复度高 → 字典
        dict_data = {'version': 1, 'schema': {'region': 'text'},
                     'groups': [{'rows': [{'region': r} for r in
                                          ['CN'] * 12 + ['US'] * 8]}]}
        # 全唯一短词且切两页 → 普通
        plain_data = {'version': 1, 'schema': {'region': 'text'},
                      'groups': [{'rows': [{'region': 'u%d' % i}
                                           for i in range(10)]},
                                 {'rows': [{'region': 'v%d' % i}
                                           for i in range(10)]}]}
        with tempfile.TemporaryDirectory() as td:
            p1 = os.path.join(td, 'dict.colpf')
            C.write_container(p1, dict_data)
            self.assertTrue(any(p['encoding'] == C.TEXT_DICT
                                for p in C.inspect_layout(p1)['pages']))
            p2 = os.path.join(td, 'plain.colpf')
            C.write_container(p2, plain_data)
            self.assertTrue(any(p['encoding'] == C.TEXT_PLAIN
                                for p in C.inspect_layout(p2)['pages']))

    def test_same_column_different_pages_different_encodings(self):
        # 第一页高度重复→RLE，第二页随机→PLAIN
        rows = [{'region': 'a', 'amount': 9} for _ in range(10)]
        rows += [{'region': 'a', 'amount': v}
                 for v in random.Random(3).sample(range(-10 ** 9, 10 ** 9), 10)]
        data = make_data(rows)
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'd.colpf')
            C.write_container(path, data, page_rows=10)
            layout = C.inspect_layout(path)
            codes = [p['encoding'] for p in layout['pages']
                     if p['column'] == 'amount']
            self.assertEqual(len(codes), 2)
            self.assertNotEqual(codes[0], codes[1])
            for p in layout['pages']:
                self.assertTrue(p['encoding_reason'])     # 理由可查看

    def test_layout_exposes_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'd.colpf')
            C.write_container(path, SAMPLE, page_rows=1)
            layout = C.inspect_layout(path)
            self.assertEqual(layout['row_count'], 3)
            self.assertEqual(layout['group_boundaries'], [0, 2, 3])
            self.assertTrue(all('offset' in p and 'length' in p
                                and 'encoding_reason' in p
                                for p in layout['pages']))


class SparseReadTests(unittest.TestCase):
    def test_projection_touches_only_selected_column_pages(self):
        data = {'version': 1,
                'schema': {'region': 'text', 'amount': 'i64'},
                'groups': [{'rows': [
                    {'region': 'r%d' % (i % 50), 'amount': i}
                    for i in range(400)]}]}
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'd.colpf')
            C.write_container(path, data, page_rows=100)
            with C.ColumnFile(path) as cf:
                amount_pages = [d for d in cf.footer['pages']
                                if d['column'] == 'amount']
                cf.read(['amount'], [0])
                ranges = cf.stats.as_dict()['ranges']

            def touches_page(d):
                return any(not (hi <= d['offset']
                                or lo >= d['offset'] + d['length'])
                           for lo, hi in ranges)

            # region 页一个都不能被触碰；amount 第一页必须被触碰
            self.assertFalse(any(touches_page(d) for d in cf.footer['pages']
                                 if d['column'] == 'region'))
            self.assertTrue(touches_page(amount_pages[0]))

    def test_large_page_sparse_does_not_decode_whole_page(self):
        data = {'version': 1, 'schema': {'amount': 'i64'},
                'groups': [{'rows': [{'amount': i} for i in range(2000)]}]}
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'd.colpf')
            C.write_container(path, data, page_rows=10000)   # 单大页，DELTA
            with C.ColumnFile(path) as cf:
                self.assertEqual(len(cf._index['amount']), 1)
                desc = cf._index['amount'][0]
                self.assertEqual(desc['encoding'], C.I64_DELTA)
                cf.read(['amount'], [1999])
                stats = cf.stats.as_dict()
            # 只解码从最近锚点到目标的少量值，而非整页2000个
            self.assertLess(stats['decoded_values'],
                            C.ANCHOR_EVERY + 5)
            self.assertGreater(stats['decoded_values'], 0)

    def test_rle_sparse_decodes_runs_not_values(self):
        data = {'version': 1, 'schema': {'amount': 'i64'},
                'groups': [{'rows': [{'amount': 7} for _ in range(1000)]}]}
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'd.colpf')
            C.write_container(path, data)
            with C.ColumnFile(path) as cf:
                cf.read(['amount'], [999])
                stats = cf.stats.as_dict()
            self.assertEqual(stats['decoded_values'], 1)  # 单游程只解一个值

    def test_telemetry_shape(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'd.colpf')
            C.write_container(path, SAMPLE, page_rows=1)
            _, stats = C.read_column_file(path, ['amount'], [2, 2, 0])
            self.assertIn('ranges', stats)
            self.assertIn('bytes_read', stats)
            self.assertIn('decoded_values', stats)
            self.assertIn('peak_buffer', stats)
            self.assertGreater(stats['bytes_read'], 0)

    def test_position_out_of_range(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'd.colpf')
            C.write_container(path, SAMPLE)
            with self.assertRaisesRegex(ValueError, '越界'):
                C.read_column_file(path, ['amount'], [3])
            with self.assertRaisesRegex(ValueError, '未知列'):
                C.read_column_file(path, ['nope'], [0])


class AnchorSeekTests(unittest.TestCase):
    """跨多个定位锚点（>ANCHOR_EVERY）的稀疏解码，专门覆盖锚点选择边界。"""

    def _round_trip(self, data, columns, positions):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'd.colpf')
            C.write_container(path, data)
            got, _ = C.read_column_file(path, columns, positions)
        self.assertEqual(got, baseline_read(data, columns, positions))

    def test_rle_multiple_anchors_scattered(self):
        vals = []
        for run in range(20):
            vals += [run * 100] * 30               # 600 个非空，多个锚点
        data = {'version': 1, 'schema': {'a': 'i64'},
                'groups': [{'rows': [{'a': v} for v in vals]}]}
        # 包含锚点边界 255/256/511/512 两侧、首尾、重复、乱序
        positions = [599, 0, 256, 255, 512, 511, 300, 256, 599, 1]
        self._round_trip(data, ['a'], positions)

    def test_delta_multiple_anchors_scattered(self):
        data = {'version': 1, 'schema': {'a': 'i64'},
                'groups': [{'rows': [{'a': i * 7 - 3000} for i in range(900)]}]}
        positions = [899, 0, 256, 257, 511, 512, 700, 256, 899]
        self._round_trip(data, ['a'], positions)

    def test_dict_multiple_anchors_scattered(self):
        words = ['alpha', 'beta', 'gamma', 'delta']
        data = {'version': 1, 'schema': {'t': 'text'},
                'groups': [{'rows': [{'t': words[i % 4]} for i in range(600)]}]}
        positions = [599, 257, 256, 300, 0, 512, 513, 599]
        self._round_trip(data, ['t'], positions)

    def test_plain_text_multiple_anchors_scattered(self):
        data = {'version': 1, 'schema': {'t': 'text'},
                'groups': [{'rows': [{'t': 'word-%04d' % i}
                                     for i in range(600)]}]}
        positions = [599, 257, 256, 300, 0, 512, 599]
        self._round_trip(data, ['t'], positions)

    def test_fuzz_equivalence_with_nulls_and_all_encodings(self):
        rnd = random.Random(0xC01D)
        words = ['CN', 'US', '', 'EU', 'CN', 'JP', None]
        n = 1500
        rows = []
        for i in range(n):
            region = rnd.choice(words)
            mode = rnd.randrange(4)
            if mode == 0:
                amount = rnd.choice([5, 5, 9])           # 倾向 RLE
            elif mode == 1:
                amount = i                              # 倾向 DELTA
            elif mode == 2:
                amount = rnd.choice([None, rnd.randint(I64_MIN, I64_MAX)])
            else:
                amount = rnd.randint(I64_MIN, I64_MAX)  # 随机
            rows.append({'region': region, 'amount': amount})
        data = {'version': 1,
                'schema': {'region': 'text', 'amount': 'i64'},
                'groups': [{'rows': rows[:900]}, {'rows': rows[900:]}]}
        columns = ['region', 'amount']
        for _ in range(25):
            k = rnd.randrange(1, 40)
            positions = [rnd.randrange(n) for _ in range(k)]
            if rnd.random() < 0.5:
                positions.append(rnd.choice(positions))  # 重复
            rnd.shuffle(positions)                        # 乱序
            self._round_trip(data, columns, positions)
        # 全量扫描也必须与旧路径逐行一致
        self._round_trip(data, columns, list(range(n)))


class DeltaBoundaryTests(unittest.TestCase):
    def test_encoder_falls_back_when_delta_overflows_i64(self):
        # 相邻差分 I64_MIN-I64_MAX-1 = -2^64，超出i64：编码端不得静默，
        # 必须退回到普通定长编码（页头仍自描述为 PLAIN）。
        values = [I64_MAX, I64_MIN]
        page_bytes, desc = C.build_page(
            'amount', 'i64', 0, 0, values, [True, True])
        self.assertEqual(desc['encoding'], C.I64_PLAIN)
        self.assertIn('退路', desc['encoding_reason'])
        # 退路编码仍可无损还原
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'd.colpf')
            data = {'version': 1, 'schema': {'amount': 'i64'},
                    'groups': [{'rows': [{'amount': v} for v in values]}]}
            C.write_container(path, data)
            got, _ = C.read_column_file(path, ['amount'], [0, 1])
            self.assertEqual([r['amount'] for r in got], values)

    def test_decoder_rejects_overflowing_varint(self):
        # 手工构造一个10字节仍置高位的永不终止 uvarint
        body = bytes([0x80] * 10)
        with self.assertRaises(C.CorruptError):
            C._uvarint_bytes(body, 0)
        # 10字节但末字节承载超64位
        with self.assertRaises(C.CorruptError):
            C._uvarint_bytes(bytes([0x80] * 9 + [0x02]), 0)

    def test_unzigzag_bounds(self):
        # 64位无符号域恰好与i64双射：两端都必须合法还原
        self.assertEqual(C.unzigzag(0), 0)
        self.assertEqual(C.unzigzag(1), -1)
        self.assertEqual(C.unzigzag(2 ** 64 - 2), I64_MAX)
        self.assertEqual(C.unzigzag(2 ** 64 - 1), I64_MIN)
        # 越过64位无符号域必须报错，不得静默截断
        with self.assertRaises(C.CorruptError):
            C.unzigzag(2 ** 64)
        with self.assertRaises(C.CorruptError):
            C.unzigzag(-1)

    def test_delta_accumulation_overflow_rejected(self):
        # 直接构造差分流：首值 I64_MAX，再加一个 +1 差分，重构越过 i64 上界
        stream = C.svarint_pack(I64_MAX) + C.svarint_pack(1)
        anchors = (C.uvarint_pack(0)
                   + C.uvarint_pack(len(C.svarint_pack(I64_MAX)))
                   + struct.pack('<q', I64_MAX))
        header = {'encoding': C.I64_DELTA, 'flags': 0}
        with tempfile.TemporaryDirectory() as td:
            class _Harness:
                stats = C.ReadStats()
                _slice = C.ColumnFile._slice
                _decode_delta = C.ColumnFile._decode_delta
            with self.assertRaisesRegex(C.CorruptError, 'i64边界'):
                _Harness()._decode_delta(stream, anchors, [1], 2)

    def test_legitimate_extreme_values_round_trip(self):
        rows = [{'region': 'a', 'amount': I64_MAX},
                {'region': 'a', 'amount': I64_MIN},
                {'region': 'a', 'amount': I64_MIN + 1}]
        data = {'version': 1, 'schema': {'region': 'text', 'amount': 'i64'},
                'groups': [{'rows': rows}]}
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'd.colpf')
            C.write_container(path, data)
            got, _ = C.read_column_file(path, ['amount'], [0, 1, 2])
            self.assertEqual([r['amount'] for r in got],
                             [I64_MAX, I64_MIN, I64_MIN + 1])


class _DecoderHarness:
    """绕过CRC直接驱动解码方法，验证解码层自身的不变量检查。"""
    stats = C.ReadStats()
    _slice = C.ColumnFile._slice
    _decode_text_dict = C.ColumnFile._decode_text_dict
    _decode_rle = C.ColumnFile._decode_rle
    _decode_plain_i64 = C.ColumnFile._decode_plain_i64


class DecoderInvariantTests(unittest.TestCase):
    def test_dict_reference_out_of_range_rejected_without_crc(self):
        # 字典含2项，索引流却引用编号5；解码层必须独立拦截，不依赖CRC
        dict_bytes = C.uvarint_pack(2)
        for s in ('CN', 'US'):
            b = s.encode('utf-8')
            dict_bytes += C.uvarint_pack(len(b)) + b
        idx_start = len(dict_bytes)
        stream = dict_bytes + C.uvarint_pack(5)
        anchors = C.uvarint_pack(0) + C.uvarint_pack(0)
        with self.assertRaisesRegex(C.CorruptError, '字典引用越界'):
            _DecoderHarness()._decode_text_dict(stream, anchors, [0], 1)

    def test_rle_run_overrun_rejected(self):
        # 声称游程长度10，但页内只有3个非空值
        stream = C.uvarint_pack(10) + struct.pack('<q', 7)
        with self.assertRaisesRegex(C.CorruptError, '游程长度非法'):
            _DecoderHarness()._decode_rle(stream, b'', [0], 3)

    def test_plain_i64_length_mismatch_rejected(self):
        # non_null=2 但值流只有8字节
        with self.assertRaisesRegex(C.CorruptError, '长度不一致'):
            _DecoderHarness()._decode_plain_i64(struct.pack('<q', 1), [0], 2)

    def test_page_header_fields_round_trip(self):
        values = [1, 1, 2]
        page_bytes, desc = C.build_page(
            'amount', 'i64', 3, 27, values, [True, True, True])
        header = C.parse_header(page_bytes[:C.HEADER_SIZE])
        self.assertEqual(header['encoding'], desc['encoding'])
        self.assertEqual(header['row_start'], 27)
        self.assertEqual(header['count'], 3)
        self.assertEqual(header['non_null'], 3)
        self.assertEqual(header['flags'], 0)
        # 页头声明的三段长度加固定头必须等于整页长度
        self.assertEqual(
            C.HEADER_SIZE + header['bitmap_len'] + header['stream_len']
            + header['anchor_len'], len(page_bytes))
        # 描述符与页头逐字段一致（_verify_descriptor 的契约）
        C._verify_descriptor(desc, header)


class CorruptionTests(unittest.TestCase):
    def _write(self, data=SAMPLE, **kw):
        td = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(td, ignore_errors=True))
        path = os.path.join(td, 'd.colpf')
        C.write_container(path, data, **kw)
        return path

    def _flip(self, path, offset, n=1):
        with open(path, 'r+b') as f:
            f.seek(offset)
            blob = bytearray(f.read(n))
            for i in range(n):
                blob[i] ^= 0xFF
            f.seek(offset)
            f.write(bytes(blob))

    def _find_page(self, path, column, require_nulls=False):
        with C.ColumnFile(path) as cf:
            pages = [d for d in cf.footer['pages'] if d['column'] == column]
            if require_nulls:
                pages = [d for d in pages if d['has_nulls']]
            return pages[0]

    def test_truncated_inputs_rejected(self):
        path = self._write()
        size = os.path.getsize(path)
        for cut in (0, 8, size - 1, size - 12, size // 2):
            with open(path, 'rb') as f:
                blob = f.read(cut)
            tmp = '%s.cut%d' % (path, cut)
            with open(tmp, 'wb') as f:
                f.write(blob)
            with self.assertRaises(C.CorruptError):
                with C.ColumnFile(tmp):
                    pass

    def test_header_crc_corruption(self):
        path = self._write(page_rows=1)
        d = self._find_page(path, 'amount')
        self._flip(path, d['offset'] + 4)          # 页头固定区
        with self.assertRaises(C.CorruptError):
            C.read_column_file(path, ['amount'], [d['row_start']])

    def test_stream_crc_corruption(self):
        path = self._write(page_rows=1)
        d = next(p for p in self._all_pages(path)
                 if p['column'] == 'amount' and p['stream_len'] > 0)
        off = d['offset'] + C.HEADER_SIZE + d['bitmap_len']
        self._flip(path, off)
        with self.assertRaises(C.CorruptError):
            C.read_column_file(path, ['amount'], [d['row_start']])

    def _all_pages(self, path):
        with C.ColumnFile(path) as cf:
            return copy.deepcopy(cf.footer['pages'])

    def test_bitmap_crc_corruption(self):
        path = self._write(page_rows=1)
        d = next(p for p in self._all_pages(path) if p['has_nulls'])
        bm_off = d['offset'] + C.HEADER_SIZE
        self._flip(path, bm_off)
        # 载荷CRC是第一道闸门，读取该页任意行都必须报错（绝不静默改判空值）
        with self.assertRaises(C.CorruptError):
            C.read_column_file(path, [d['column']], [d['row_start']])

    def test_footer_crc_corruption(self):
        path = self._write()
        size = os.path.getsize(path)
        self._flip(path, size - 13)                # 页脚JSON最后字节
        with self.assertRaises(C.CorruptError):
            with C.ColumnFile(path):
                pass

    def test_bad_magic_rejected(self):
        path = self._write()
        self._flip(path, 0)
        with self.assertRaises(C.CorruptError):
            with C.ColumnFile(path):
                pass

    def test_dict_reference_out_of_range(self):
        data = {'version': 1, 'schema': {'region': 'text'},
                'groups': [{'rows': [{'region': r}
                                     for r in ['CN'] * 12 + ['US'] * 8]}]}
        path = self._write(data)
        d = next(p for p in self._all_pages(path)
                 if p['encoding'] == C.TEXT_DICT)
        # 翻转索引流末字节：先被载荷/值流CRC拦截；CRC本身就是完整性闸门
        off = (d['offset'] + C.HEADER_SIZE + d['bitmap_len']
               + d['stream_len'] - 1)
        self._flip(path, off)
        with self.assertRaises(C.CorruptError):
            C.read_column_file(path, ['region'], list(range(20)))

    def test_corruption_never_becomes_null(self):
        path = self._write(page_rows=1)
        d = next(p for p in self._all_pages(path)
                 if p['column'] == 'amount' and p['stream_len'] > 0)
        off = d['offset'] + C.HEADER_SIZE + d['bitmap_len']
        self._flip(path, off)
        with self.assertRaises(C.CorruptError):
            C.read_column_file(path, ['amount'], [d['row_start']])

    def test_length_mismatch_rejected(self):
        path = self._write()
        size = os.path.getsize(path)
        with open(path, 'r+b') as f:
            f.seek(size - 12)
            flen, fcrc = struct.unpack('<II', f.read(8))
            f.seek(size - 12)
            f.write(struct.pack('<II', flen + 4, fcrc))
        with self.assertRaises(C.CorruptError):
            with C.ColumnFile(path):
                pass

    def test_full_scan_detects_trailing_garbage(self):
        path = self._write(SAMPLE, page_rows=1)
        with open(path, 'ab') as f:
            f.write(b'\x00\x00')
        with self.assertRaises(C.CorruptError):
            with C.ColumnFile(path) as cf:
                cf.scan_all()


class PublishTests(unittest.TestCase):
    @staticmethod
    def _container_path(td, name):
        manifest = C.verify_manifest(td, name)
        return os.path.join(td, manifest['container']), manifest

    def test_manifest_roundtrip_and_verify(self):
        with tempfile.TemporaryDirectory() as td:
            manifest = C.publish_dataset(SAMPLE, td, name='lake')
            self.assertTrue(manifest['container'].startswith('lake-'))
            self.assertTrue(os.path.exists(
                os.path.join(td, 'lake.manifest.json')))
            self.assertEqual(C.verify_manifest(td, 'lake'), manifest)
            path, _ = self._container_path(td, 'lake')
            got, _ = C.read_column_file(
                path, SAMPLE['query']['columns'], SAMPLE['query']['positions'])
            self.assertEqual(got, baseline_read(
                SAMPLE, SAMPLE['query']['columns'], SAMPLE['query']['positions']))

    def test_idempotent_content_addressing_reuses_container(self):
        with tempfile.TemporaryDirectory() as td:
            first = C.publish_dataset(SAMPLE, td, name='lake')
            second = C.publish_dataset(copy.deepcopy(SAMPLE), td, name='lake')
            self.assertEqual(first['container'], second['container'])
            self.assertEqual(len([n for n in os.listdir(td)
                                  if n.endswith('.colpf')]), 1)

    def _assert_clean_failure(self, td, name, good_manifest, good_path):
        self.assertEqual(C.verify_manifest(td, name), good_manifest)
        self.assertFalse(any(n.startswith('.staging') for n in os.listdir(td)))
        # 服役容器未被替换，仍可正常查询
        got, _ = C.read_column_file(good_path, ['amount'], [0])
        self.assertEqual(got, [{'amount': 10}])

    def test_failed_publish_leaves_no_half_pages(self):
        tampered = copy.deepcopy(SAMPLE)
        tampered['marker'] = 'new'
        for fail_point in ('container', 'placed'):
            with tempfile.TemporaryDirectory() as td:
                C.publish_dataset(SAMPLE, td, name='lake')
                good_manifest = C.verify_manifest(td, 'lake')
                good_path = os.path.join(td, good_manifest['container'])
                before_files = set(os.listdir(td))
                with self.assertRaises(RuntimeError):
                    C.publish_dataset(tampered, td, name='lake',
                                      fail_after=fail_point)
                # 清单未提交，服役容器与文件集合保持发布前状态
                self.assertEqual(set(os.listdir(td)), before_files)
                self._assert_clean_failure(td, 'lake', good_manifest, good_path)

    def test_successful_republish_switches_manifest_atomically(self):
        with tempfile.TemporaryDirectory() as td:
            first = C.publish_dataset(SAMPLE, td, name='lake')
            tampered = copy.deepcopy(SAMPLE)
            tampered['groups'][0]['rows'][0]['amount'] = 999
            second = C.publish_dataset(tampered, td, name='lake')
            self.assertNotEqual(first['container'], second['container'])
            # 旧容器保留（不可变），清单整体指向新容器且自洽
            self.assertTrue(os.path.exists(os.path.join(td, first['container'])))
            path, manifest = self._container_path(td, 'lake')
            self.assertEqual(manifest, second)
            got, _ = C.read_column_file(path, ['amount'], [0])
            self.assertEqual(got, [{'amount': 999}])

    def test_container_swap_detected_by_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            C.publish_dataset(SAMPLE, td)
            path, manifest = self._container_path(td, 'dataset')
            other = copy.deepcopy(SAMPLE)
            other['groups'][0]['rows'][0]['amount'] = 999
            C.write_container(path, other)   # 直接替换服役容器内容
            with self.assertRaisesRegex(C.CorruptError, '摘要|尺寸'):
                C.verify_manifest(td)


class CoexistenceTests(unittest.TestCase):
    def test_reading_old_json_does_not_rewrite_it(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, 'sample.json')
            Path(src).write_text(
                Path(__file__).resolve().parents[1].joinpath(
                    'fixtures', 'sample.json').read_text(encoding='utf-8'),
                encoding='utf-8')
            before = Path(src).read_bytes()
            mtime = os.path.getmtime(src)
            data = json.loads(before)
            # 内存资料走旧路径
            got = C.read_dataset(data, data['query']['columns'],
                                 data['query']['positions'])
            self.assertEqual(got, baseline_read(
                data, data['query']['columns'], data['query']['positions']))
            self.assertEqual(Path(src).read_bytes(), before)
            self.assertEqual(os.path.getmtime(src), mtime)

    def test_dispatch_distinguishes_formats(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'd.colpf')
            C.write_container(path, SAMPLE)
            got = C.read_dataset(path, ['amount'], [2, 0, 2])
            self.assertEqual(got, baseline_read(SAMPLE, ['amount'], [2, 0, 2]))

    def test_existing_service_unchanged(self):
        from app.service import inspect
        out = inspect(SAMPLE)
        self.assertEqual(out['input_digest'],
                         __import__('app.storage', fromlist=['digest']).digest(SAMPLE))
        self.assertEqual(len(out['result']), 3)


if __name__ == '__main__':
    unittest.main()
