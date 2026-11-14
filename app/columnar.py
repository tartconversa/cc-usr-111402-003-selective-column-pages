"""列页二进制格式（data_version=2）。

与 version=1 的行组 JSON 并存：本模块只读写自身产生的 ``.colpf`` 容器，
从不改写旧 JSON（分派见 :func:`read_dataset`）。

容器布局（全部小端）::

    HEAD(8)=b'CLPF' + 数据版本u16(=2) + 保留u16(=0)
    page ...                             # 按列连续存放，页内自描述
    footer_len(u32) + footer_crc(u32) + footer_json + b'CLPF'   # 尾12字节

页 = 固定页头(46字节) + 有效载荷(空位图? + 值流 + 定位锚点)。
页头字段与页脚描述符交叉校验；校验和分层：页头CRC覆盖页头其余字节，
位图CRC覆盖空位图，值流CRC覆盖值流与锚点，页脚再整体CRC，
页脚另存整页载荷CRC作为总闸门。截断、越界、字典引用越界、
差分量超 i64 等一律抛 :class:`CorruptError`，绝不静默当作缺值。

空值只占位图，不占值流索引；空串是普通文本值而非 null。
稀疏读取借助定位锚点从值流中段开始解码，只物化命中的非空值，
不先还原整列；I/O 的最小单元是页，列投影与页裁剪减少读入字节。
"""
import hashlib
import json
import os
import struct
import zlib

MAGIC = b'CLPF'
HEAD = MAGIC + struct.pack('<HH', 2, 0)
PAGE_MARK = 0x4350                               # 'CP'
HEADER_VERSION = 1
_TYPE_ID = {'i64': 1, 'text': 2}
ANCHOR_EVERY = 256                               # 每多少个非空值放一个锚点

# 编码标记（页头自描述，同一列不同页可不同）
I64_PLAIN = 1
I64_RLE = 2
I64_DELTA = 3
TEXT_PLAIN = 11
TEXT_DICT = 12

HAS_NULLS = 0x01

_HEADER_FMT = '<HBBBBqIIIII'                     # 不含3个CRC
_HEADER_CRC_FMT = _HEADER_FMT + 'III'
HEADER_SIZE = struct.calcsize(_HEADER_CRC_FMT)   # 46
HEADER_FIXED = struct.calcsize(_HEADER_FMT)      # 34


class CorruptError(ValueError):
    """容器字节损坏、截断或结构不一致。绝不把损坏降级为空值。"""


# ---------------------------------------------------------------- 有符号边界
I64_MIN = -(2 ** 63)
I64_MAX = 2 ** 63 - 1


def _check_i64(v):
    if type(v) is not int or not I64_MIN <= v <= I64_MAX:
        raise ValueError('整数类型或范围错误')


def zigzag(v):
    _check_i64(v)
    return (v << 1) ^ (v >> 63)


def unzigzag(u):
    if not 0 <= u <= 2 ** 64 - 1:
        raise CorruptError('无符号值超出64位')
    v = (u >> 1) ^ -(u & 1)
    if not I64_MIN <= v <= I64_MAX:
        raise CorruptError('有符号值超出i64边界')
    return v


def uvarint_pack(u):
    if not isinstance(u, int) or u < 0 or u > 2 ** 64 - 1:
        raise ValueError('无符号变长整数越界')
    out = bytearray()
    while u >= 0x80:
        out.append((u & 0x7F) | 0x80)
        u >>= 7
    out.append(u)
    return bytes(out)


def svarint_pack(v):
    return uvarint_pack(zigzag(v))


def _uvarint_bytes(raw, pos):
    shift = 0
    result = 0
    for i in range(10):
        if pos + i >= len(raw):
            raise CorruptError('输入被截断')
        b = raw[pos + i]
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            if i == 9 and b > 1:
                raise CorruptError('变长整数超出64位')
            return result, pos + i + 1
        shift += 7
    raise CorruptError('变长整数未终止')


def _i64_pack(v):
    _check_i64(v)
    return struct.pack('<q', v)


def _i64_unpack(b):
    if len(b) != 8:
        raise CorruptError('输入被截断')
    (v,) = struct.unpack('<q', b)
    return v


# ---------------------------------------------------------------- 读取统计
class ReadStats:
    """一次查询实际触碰的字节范围、解码值数与峰值缓冲。"""

    def __init__(self):
        self._touches = []               # 原始 (offset,end)，读取顺序可能非单调
        self.decoded_values = 0
        self.peak_buffer = 0
        self._buf = 0

    @property
    def ranges(self):
        return self.as_dict()['ranges']

    def touch(self, offset, end):
        if end > offset:
            self._touches.append((offset, end))

    def alloc(self, n):
        self._buf += n
        self.peak_buffer = max(self.peak_buffer, self._buf)

    def free(self, n):
        self._buf -= n

    def as_dict(self):
        ordered = sorted(self._touches)
        merged = []
        for lo, hi in ordered:
            if merged and lo <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
            else:
                merged.append((lo, hi))
        return {
            'ranges': merged,
            'bytes_read': sum(e - o for o, e in merged),
            'decoded_values': self.decoded_values,
            'peak_buffer': self.peak_buffer,
        }


# ---------------------------------------------------------------- 位图
def bitmap_pack(present):
    """present 为逐行布尔（True=非空），按位小端打包。"""
    out = bytearray((len(present) + 7) // 8)
    for i, ok in enumerate(present):
        if ok:
            out[i >> 3] |= 1 << (i & 7)
    return bytes(out)


def bitmap_unpack(raw, count):
    if len(raw) != (count + 7) // 8:
        raise CorruptError('空位图长度不一致')
    return [((raw[i >> 3] >> (i & 7)) & 1) == 1 for i in range(count)]


# ---------------------------------------------------------------- i64 编码选择
def _estimate_i64(vs):
    """返回 (编码标记, 理由, 估算字节)，理由写入页脚可供查看。"""
    n = len(vs)
    if n == 0:
        return I64_PLAIN, '空页保留普通编码', 0
    plain = 8 * n
    rle = 0
    run_count = 0
    prev = object()
    for v in vs:
        if v != prev:
            run_count += 1
            rle += 10                          # run_len(uvarint) + value(8)
        prev = v
    delta = len(svarint_pack(vs[0]))
    for a, b in zip(vs, vs[1:]):
        d = b - a
        if not I64_MIN <= d <= I64_MAX:
            delta = None
            break
        delta += len(svarint_pack(d))
    options = [(I64_PLAIN, '退路：普通定长编码', plain)]
    if rle < plain:
        options.append((I64_RLE, '重复段压缩：相邻相等值合并', rle))
    if delta is not None and delta < plain:
        options.append((I64_DELTA, '差分压缩：zigzag有符号差分', delta))
    best = min(options, key=lambda x: x[2])
    if best[0] == I64_PLAIN and len(options) > 1:
        return I64_PLAIN, '退路：估算后普通编码最短', plain
    return best


def encode_i64(values):
    """values 仅含非空 i64。返回 (编码标记, 理由, 值流, 锚点)。"""
    for v in values:
        _check_i64(v)
    code, reason, _ = _estimate_i64(values)
    n = len(values)
    if code == I64_PLAIN:
        return code, reason, b''.join(_i64_pack(v) for v in values), b''
    if code == I64_RLE:
        body = bytearray()
        anchors = bytearray()
        i = 0
        while i < n:
            j = i + 1
            while j < n and values[j] == values[i]:
                j += 1
            # 锚点按非空值序号每 ANCHOR_EVERY 个放置，记录游程头位置
            if i % ANCHOR_EVERY == 0:
                anchors += uvarint_pack(i) + uvarint_pack(len(body))
            body += uvarint_pack(j - i) + _i64_pack(values[i])
            i = j
        return code, reason, bytes(body), bytes(anchors)
    body = bytearray(svarint_pack(values[0]))
    anchors = bytearray()
    # 锚点 k 存重构值 values[k]，偏移指向“下一个待消费差分”：
    # k=0 指向 delta[1]；k>0 指向 delta[k+1]。序号0显式建锚。
    anchors += uvarint_pack(0) + uvarint_pack(len(body)) + _i64_pack(values[0])
    for k in range(1, n):
        d = values[k] - values[k - 1]
        if not I64_MIN <= d <= I64_MAX:
            raise CorruptError('差分超出有符号边界')
        body += svarint_pack(d)
        if k % ANCHOR_EVERY == 0:
            anchors += uvarint_pack(k) + uvarint_pack(len(body)) + _i64_pack(values[k])
    return I64_DELTA, reason, bytes(body), bytes(anchors)


def _anchors_parse(raw, with_baseline):
    anchors = []
    pos = 0
    while pos < len(raw):
        ordinal, pos = _uvarint_bytes(raw, pos)
        offset, pos = _uvarint_bytes(raw, pos)
        baseline = b''
        if with_baseline:
            if pos + 8 > len(raw):
                raise CorruptError('锚点被截断')
            baseline = raw[pos:pos + 8]
            pos += 8
        anchors.append((ordinal, offset, baseline))
    return anchors


def _pick_anchor(anchors, ordinal):
    picked = None
    for a in anchors:
        if a[0] <= ordinal:
            picked = a
        else:
            break
    return picked


# ---------------------------------------------------------------- text 编码选择
def _estimate_text(vs):
    n = len(vs)
    if n == 0:
        return TEXT_PLAIN, '空页保留普通编码', 0
    plain = 0
    for s in vs:
        b = s.encode('utf-8')
        plain += len(uvarint_pack(len(b))) + len(b)
    order = []
    index_of = {}
    for s in vs:
        if s not in index_of:
            index_of[s] = len(order)
            order.append(s)
    dict_body = 0
    for s in order:
        b = s.encode('utf-8')
        dict_body += len(uvarint_pack(len(b))) + len(b)
    idx_bytes = sum(len(uvarint_pack(index_of[s])) for s in vs)
    dict_total = len(uvarint_pack(len(order))) + dict_body + idx_bytes
    if dict_total < plain:
        return TEXT_DICT, '页内字典：重复文本以编号引用', dict_total
    return TEXT_PLAIN, '退路：字典不省时使用普通值', plain


def encode_text(values):
    """values 仅含非空 str；空串是合法普通值，不是 null。"""
    for s in values:
        if not isinstance(s, str):
            raise ValueError('文本类型错误')
    n = len(values)
    encoded = [s.encode('utf-8') for s in values]
    code, reason, _ = _estimate_text(values)
    if code == TEXT_PLAIN:
        body = bytearray()
        anchors = bytearray()
        # 锚点记录 (已物化序号, 下一项在值流内的偏移)。序号0显式建锚：
        # 此时尚未消费任何项，偏移为0，解码端从偏移0读序号0。
        anchors += uvarint_pack(0) + uvarint_pack(0)
        for k, b in enumerate(encoded):
            body += uvarint_pack(len(b)) + b
            if (k + 1) % ANCHOR_EVERY == 0 and k + 1 < n:
                # 下一项 k+1 从当前 body 末尾开始
                anchors += uvarint_pack(k + 1) + uvarint_pack(len(body))
        return code, reason, bytes(body), bytes(anchors)
    order = []
    index_of = {}
    for s in values:
        if s not in index_of:
            index_of[s] = len(order)
            order.append(s)
    dict_bytes = bytearray()
    for s in order:
        b = s.encode('utf-8')
        dict_bytes += uvarint_pack(len(b)) + b
    idx_stream = bytearray()
    anchors = bytearray()
    # 锚点偏移相对索引流起点；序号0显式建锚，偏移0即第一项编号
    anchors += uvarint_pack(0) + uvarint_pack(0)
    for k, s in enumerate(values):
        idx_stream += uvarint_pack(index_of[s])
        if (k + 1) % ANCHOR_EVERY == 0 and k + 1 < n:
            anchors += uvarint_pack(k + 1) + uvarint_pack(len(idx_stream))
    body = uvarint_pack(len(order)) + bytes(dict_bytes) + bytes(idx_stream)
    return TEXT_DICT, reason, body, bytes(anchors)


# ---------------------------------------------------------------- 页装配
def build_page(column, col_type, page_no, row_start, values, present):
    """装配一页，返回 (page_bytes, descriptor)。

    values/present 覆盖该页全部逻辑行；空值只进位图、不进值流。
    """
    count = len(present)
    if len(values) != sum(present):
        raise ValueError('非空值数量与空位图不一致')
    flags = HAS_NULLS if not all(present) else 0
    if flags & HAS_NULLS:
        bitmap = bitmap_pack(present)
    else:
        bitmap = b''
    if col_type == 'i64':
        code, reason, stream, anchors = encode_i64(values)
    elif col_type == 'text':
        code, reason, stream, anchors = encode_text(values)
    else:
        raise ValueError('未知列类型')
    bitmap_crc = zlib.crc32(bitmap) & 0xFFFFFFFF
    stream_crc = zlib.crc32(stream + anchors) & 0xFFFFFFFF
    header_fixed = struct.pack(
        _HEADER_FMT, PAGE_MARK, HEADER_VERSION, code, flags, _TYPE_ID[col_type],
        row_start, count, len(values), len(bitmap), len(stream), len(anchors))
    if len(header_fixed) != HEADER_FIXED:
        raise AssertionError(len(header_fixed))
    header_crc = zlib.crc32(header_fixed) & 0xFFFFFFFF
    header = header_fixed + struct.pack('<III', header_crc, bitmap_crc, stream_crc)
    payload = bitmap + stream + anchors
    descriptor = {
        'column': column,
        'page': page_no,
        'offset': None,                       # 写盘时回填
        'length': HEADER_SIZE + len(payload),
        'row_start': row_start,
        'count': count,
        'non_null': len(values),
        'encoding': code,
        'encoding_reason': reason,
        'has_nulls': bool(flags & HAS_NULLS),
        'bitmap_len': len(bitmap),
        'stream_len': len(stream),
        'anchor_len': len(anchors),
        'header_crc': header_crc,
        'bitmap_crc': bitmap_crc,
        'stream_crc': stream_crc,
        'payload_crc': zlib.crc32(payload) & 0xFFFFFFFF,
    }
    return header + payload, descriptor


# ---------------------------------------------------------------- 页头解析
def parse_header(raw):
    if len(raw) != HEADER_SIZE:
        raise CorruptError('页头长度不足')
    (mark, hver, code, flags, type_id, row_start, count,
     non_null, bm_len, st_len, an_len, h_crc, bm_crc, st_crc) = struct.unpack(
        _HEADER_CRC_FMT, raw)
    if mark != PAGE_MARK or hver != HEADER_VERSION:
        raise CorruptError('页头标记或版本错误')
    if zlib.crc32(raw[:HEADER_FIXED]) & 0xFFFFFFFF != h_crc:
        raise CorruptError('页头校验和错误')
    if count < 0 or non_null < 0 or row_start < 0:
        raise CorruptError('页头计数为负')
    return {
        'encoding': code, 'flags': flags, 'type_id': type_id,
        'row_start': row_start, 'count': count, 'non_null': non_null,
        'bitmap_len': bm_len, 'stream_len': st_len, 'anchor_len': an_len,
        'header_crc': h_crc, 'bitmap_crc': bm_crc, 'stream_crc': st_crc,
    }


def _verify_descriptor(desc, h):
    for key in ('row_start', 'count', 'non_null', 'bitmap_len', 'stream_len',
                'anchor_len', 'encoding', 'header_crc', 'bitmap_crc', 'stream_crc'):
        if desc[key] != h[key]:
            raise CorruptError('页头与页脚描述符不一致: %s' % key)
    if desc['has_nulls'] != bool(h['flags'] & HAS_NULLS):
        raise CorruptError('空位标记与页脚不一致')


# ---------------------------------------------------------------- 切页与校验
def validate_rows(data):
    """复刻 version=1 的类型与空值契约（bool 不是整数，空串非 null）。"""
    if data.get('version') != 1:
        raise ValueError('仅接受version=1资料作为编码输入')
    schema = data['schema']
    rows = [r for group in data['groups'] for r in group['rows']]
    for r in rows:
        for c, typ in schema.items():
            v = r.get(c)
            if v is None:
                continue
            if typ == 'i64':
                _check_i64(v)
            elif typ == 'text':
                if not isinstance(v, str):
                    raise ValueError('文本类型错误')
            else:
                raise ValueError('未知列类型')
    return schema, rows


def _split_pages(rows, schema, group_ends, page_rows):
    """在行组边界与 page_rows 处切页，返回 {列: [(row_start, values, present)]} 与边界。"""
    total = len(rows)
    cuts = set()
    cuts.add(0)
    cuts.add(total)
    for ge in group_ends:
        cuts.add(ge)
    start = 0
    while start < total:
        cuts.add(min(total, start + page_rows))
        start += page_rows
    cuts = sorted(c for c in cuts if 0 <= c <= total)
    bounds = list(zip(cuts, cuts[1:]))
    pages = {c: [] for c in schema}
    for start, end in bounds:
        for c, typ in schema.items():
            present, values = [], []
            for r in rows[start:end]:
                v = r.get(c)
                if v is None:
                    present.append(False)
                    continue
                if typ == 'i64':
                    _check_i64(v)
                elif typ == 'text':
                    if not isinstance(v, str):
                        raise ValueError('文本类型错误')
                else:
                    raise ValueError('未知列类型')
                present.append(True)
                values.append(v)
            pages[c].append((start, values, present))
    return pages, bounds


# ---------------------------------------------------------------- 写盘
def write_container(path, data, page_rows=10000):
    """把 version=1 内存资料编码为 version=2 容器（单文件原子替换）。"""
    schema, rows = validate_rows(data)
    group_ends = []
    acc = 0
    for g in data['groups']:
        acc += len(g['rows'])
        group_ends.append(acc)
    pages_input, bounds = _split_pages(rows, schema, group_ends, page_rows)
    blob = bytearray(HEAD)
    descriptors = []
    for c in schema:                       # 同列页连续存放，投影时互不干扰
        for page_no, (row_start, values, present) in enumerate(pages_input[c]):
            page_bytes, desc = build_page(c, schema[c], page_no, row_start, values, present)
            desc['offset'] = len(blob)
            blob += page_bytes
            descriptors.append(desc)
    footer = {
        'format': 'CLPF',
        'data_version': 2,
        'schema': dict(schema),
        'row_count': len(rows),
        'group_boundaries': [0] + group_ends,
        'page_bounds': [list(b) for b in bounds],
        'pages': descriptors,
    }
    footer_bytes = json.dumps(footer, ensure_ascii=False, sort_keys=True,
                              separators=(',', ':')).encode('utf-8')
    # 固定尾12字节：footer_len(u32) + footer_crc(u32) + 魔数(4)
    tail = footer_bytes + struct.pack(
        '<II', len(footer_bytes), zlib.crc32(footer_bytes) & 0xFFFFFFFF) + MAGIC
    directory = os.path.dirname(os.path.abspath(path)) or '.'
    tmp = os.path.join(directory, '.%s.tmp' % (os.path.basename(path) or 'colpf'))
    try:
        with open(tmp, 'wb') as f:
            f.write(blob)
            f.write(tail)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return footer


def _fsync_dir(directory):
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                return h.hexdigest()
            h.update(chunk)


def _rm_tree(path):
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            os.unlink(os.path.join(root, name))
        for name in dirs:
            os.rmdir(os.path.join(root, name))
    os.rmdir(path)


def publish_dataset(data, directory, name='dataset', page_rows=10000, fail_after=None):
    """目录发布：不可变内容寻址容器 + 清单单次原子提交。

    容器以内容摘要命名（``<name>-<sha16>.colpf``），先整体落盘再放入目录，
    不覆盖在服役容器；清单的一次 ``os.replace`` 是唯一提交点。因此发布失败
    时目录要么保持旧版本、要么整体切到已完整落盘的新版本，绝不会指向半页。
    fail_after 仅供测试注入故障。
    """
    directory = os.path.abspath(directory)
    os.makedirs(directory, exist_ok=True)
    staging = os.path.join(directory, '.staging-%s' % name)
    if os.path.exists(staging):
        _rm_tree(staging)
    os.makedirs(staging)
    placed_ca = None
    committed = False
    try:
        staged_container = os.path.join(staging, name + '.colpf')
        footer = write_container(staged_container, data, page_rows=page_rows)
        if fail_after == 'container':
            raise RuntimeError('模拟容器写入后发布失败')
        digest_hex = _sha256_file(staged_container)
        ca_name = '%s-%s.colpf' % (name, digest_hex[:16])
        ca_path = os.path.join(directory, ca_name)
        if not os.path.exists(ca_path):       # 同内容可能已发布，复用即可
            os.replace(staged_container, ca_path)
            placed_ca = ca_path
        _fsync_dir(directory)
        if fail_after == 'placed':
            raise RuntimeError('模拟容器就位后清单提交前失败')
        manifest = {
            'format': 'CLPF',
            'data_version': 2,
            'container': ca_name,
            'container_size': os.path.getsize(ca_path),
            'container_sha256': digest_hex,
            'row_count': footer['row_count'],
            'columns': list(footer['schema']),
        }
        manifest_path = os.path.join(staging, name + '.manifest.json')
        with open(manifest_path, 'wb') as f:
            f.write(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                               separators=(',', ':')).encode('utf-8'))
            f.flush()
            os.fsync(f.fileno())
        os.replace(manifest_path, os.path.join(directory, name + '.manifest.json'))
        _fsync_dir(directory)
        committed = True
        return manifest
    finally:
        # 提交前失败：回收已就位但清单未引用的容器，在服役版本保持不变
        if not committed and placed_ca and os.path.exists(placed_ca):
            os.unlink(placed_ca)
        if os.path.exists(staging):
            _rm_tree(staging)


def verify_manifest(directory, name='dataset'):
    """核对清单与容器摘要；容器被调换或截断时抛 CorruptError。"""
    directory = os.path.abspath(directory)
    manifest_path = os.path.join(directory, name + '.manifest.json')
    with open(manifest_path, 'rb') as f:
        manifest = json.loads(f.read().decode('utf-8'))
    container = os.path.join(directory, manifest['container'])
    if os.path.getsize(container) != manifest['container_size']:
        raise CorruptError('容器尺寸与清单不一致')
    if _sha256_file(container) != manifest['container_sha256']:
        raise CorruptError('容器摘要与清单不一致')
    return manifest


# ---------------------------------------------------------------- 只读容器
class ColumnFile:
    """只读列页容器；旧 JSON 不会被本类触碰或改写。"""

    def __init__(self, path):
        self.path = path
        self.stats = ReadStats()
        self.f = open(path, 'rb')
        try:
            self.size = os.path.getsize(path)
            if self.size < 8 + 12:
                raise CorruptError('输入被截断')
            head = self._read(0, 8)
            if head[:4] != MAGIC or head[4:6] != struct.pack('<H', 2):
                raise CorruptError('不是受支持的CLPF data_version=2容器')
            tail = self._read(self.size - 12, 12)
            if tail[8:12] != MAGIC:
                raise CorruptError('页脚魔数错误')
            flen, fcrc = struct.unpack('<II', tail[:8])
            footer_start = self.size - 12 - flen
            if footer_start < 8:
                raise CorruptError('页脚长度超出容器')
            footer_raw = self._read(footer_start, flen)
            if zlib.crc32(footer_raw) & 0xFFFFFFFF != fcrc:
                raise CorruptError('页脚校验和错误')
            self.footer = json.loads(footer_raw.decode('utf-8'))
            if self.footer.get('data_version') != 2 or self.footer.get('format') != 'CLPF':
                raise CorruptError('页脚身份错误')
            self._index = {}
            for desc in self.footer['pages']:
                self._index.setdefault(desc['column'], []).append(desc)
            for descs in self._index.values():
                descs.sort(key=lambda d: d['page'])
                for i, d in enumerate(descs):
                    if d['page'] != i:
                        raise CorruptError('页号不连续')
        except Exception:
            self.f.close()
            raise

    def _read(self, offset, n):
        self.f.seek(offset)
        b = self.f.read(n)
        if len(b) != n:
            raise CorruptError('输入被截断')
        self.stats.touch(offset, offset + n)
        self.stats.alloc(n)
        return b

    def close(self):
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ 内省
    def layout(self):
        """页布局、每页编码与选择理由、定位锚点规模，均可查看。"""
        return {
            'row_count': self.footer['row_count'],
            'schema': dict(self.footer['schema']),
            'group_boundaries': self.footer['group_boundaries'],
            'page_bounds': self.footer['page_bounds'],
            'pages': [
                {
                    'column': d['column'], 'page': d['page'],
                    'offset': d['offset'], 'length': d['length'],
                    'row_start': d['row_start'], 'count': d['count'],
                    'non_null': d['non_null'], 'encoding': d['encoding'],
                    'encoding_reason': d['encoding_reason'],
                    'has_nulls': d['has_nulls'], 'anchor_len': d['anchor_len'],
                } for d in self.footer['pages']
            ],
        }

    def _desc_for(self, column, position):
        for d in self._index[column]:
            if d['row_start'] <= position < d['row_start'] + d['count']:
                return d
        raise CorruptError('定位索引未覆盖行号%d' % position)

    # ------------------------------------------------------------ 投影查询
    def read(self, columns, positions):
        schema = self.footer['schema']
        if any(c not in schema for c in columns):
            raise ValueError('未知列')
        row_count = self.footer['row_count']
        if any(type(p) is not int or p < 0 or p >= row_count for p in positions):
            raise ValueError('行号越界')
        result = [{} for _ in positions]
        for c in columns:
            pages_want = {}
            for p in positions:
                desc = self._desc_for(c, p)
                pages_want.setdefault(desc['page'], (desc, set()))[1].add(p)
            page_values = {}
            for desc, wanted in pages_want.values():
                page_values.update(
                    self._read_page(c, schema[c], desc, sorted(wanted)))
            for idx, p in enumerate(positions):
                result[idx][c] = page_values[p]   # 重复/乱序按请求顺序返回
        return result

    def _open_page(self, desc, col_type):
        raw = self._read(desc['offset'], desc['length'])
        header = parse_header(raw[:HEADER_SIZE])
        _verify_descriptor(desc, header)
        if header['type_id'] != _TYPE_ID[col_type]:
            raise CorruptError('页列类型与schema不一致')
        bm, st, an = header['bitmap_len'], header['stream_len'], header['anchor_len']
        if HEADER_SIZE + bm + st + an != desc['length']:
            raise CorruptError('页长度不一致')
        bitmap = raw[HEADER_SIZE:HEADER_SIZE + bm]
        stream = raw[HEADER_SIZE + bm:HEADER_SIZE + bm + st]
        anchors = raw[HEADER_SIZE + bm + st:]
        if zlib.crc32(bitmap + stream + anchors) & 0xFFFFFFFF != desc['payload_crc']:
            raise CorruptError('整页载荷校验和错误')
        if header['flags'] & HAS_NULLS:
            if zlib.crc32(bitmap) & 0xFFFFFFFF != header['bitmap_crc']:
                raise CorruptError('空位图校验和错误')
        elif bm != 0:
            raise CorruptError('无空位标记却携带位图')
        if zlib.crc32(stream + anchors) & 0xFFFFFFFF != header['stream_crc']:
            raise CorruptError('值流校验和错误')
        self.stats.free(len(raw))
        return header, bitmap, stream, anchors

    def _read_page(self, column, col_type, desc, positions):
        """只解码 positions（全文件物理行号）命中的值，返回 {行号: 值}。"""
        header, bitmap, stream, anchors = self._open_page(desc, col_type)
        count = header['count']
        if header['flags'] & HAS_NULLS:
            present = bitmap_unpack(bitmap, count)
        else:
            present = [True] * count
        ordinal_of_row = {}
        ordinal = -1
        for row in range(count):
            if present[row]:
                ordinal += 1
                ordinal_of_row[row] = ordinal
        out = {}
        wanted_ordinals = {}
        for p in positions:
            row = p - desc['row_start']
            if not 0 <= row < count:
                raise CorruptError('页内行号越界')
            if present[row]:
                wanted_ordinals[ordinal_of_row[row]] = row
            else:
                out[p] = None                    # 空位不占值流索引
        if wanted_ordinals:
            values = self._decode_stream(
                col_type, header, stream, anchors,
                sorted(wanted_ordinals), header['non_null'])
            for ord_no, row in wanted_ordinals.items():
                out[desc['row_start'] + row] = values[ord_no]
        return out

    def _decode_stream(self, col_type, header, stream, anchors, wanted, non_null):
        code = header['encoding']
        if col_type == 'i64':
            if code == I64_PLAIN:
                return self._decode_plain_i64(stream, wanted, non_null)
            if code == I64_RLE:
                return self._decode_rle(stream, anchors, wanted, non_null)
            if code == I64_DELTA:
                return self._decode_delta(stream, anchors, wanted, non_null)
        else:
            if code == TEXT_PLAIN:
                return self._decode_text_plain(stream, anchors, wanted, non_null)
            if code == TEXT_DICT:
                return self._decode_text_dict(stream, anchors, wanted, non_null)
        raise CorruptError('未知编码标记')

    def _slice(self, stream, pos, n):
        if pos < 0 or pos + n > len(stream):
            raise CorruptError('输入被截断')
        return stream[pos:pos + n]

    def _decode_plain_i64(self, stream, wanted, non_null):
        if non_null * 8 != len(stream):
            raise CorruptError('普通i64值流长度不一致')
        out = {}
        for ord_no in wanted:
            self.stats.decoded_values += 1
            out[ord_no] = _i64_unpack(self._slice(stream, ord_no * 8, 8))
        return out

    def _decode_rle(self, stream, anchors, wanted, non_null):
        index = _anchors_parse(anchors, False)
        anchor = _pick_anchor(index, wanted[0])
        pos = anchor[1] if anchor else 0
        ordinal = anchor[0] if anchor else 0
        out = {}
        target = set(wanted)
        last = wanted[-1]
        reached_end = False
        while ordinal <= last:
            run_len, pos = _uvarint_bytes(stream, pos)
            if run_len == 0 or ordinal + run_len > non_null:
                raise CorruptError('RLE游程长度非法')
            value = _i64_unpack(self._slice(stream, pos, 8))
            pos += 8
            self.stats.decoded_values += 1       # 每游程只解码一个值
            for o in target:
                if ordinal <= o < ordinal + run_len:
                    out[o] = value
            ordinal += run_len
            reached_end = ordinal == non_null
        # 仅当解码走到值流末尾时才核对尾部，避免稀疏提前停止误报
        if reached_end and pos != len(stream):
            raise CorruptError('RLE值流长度不一致')
        if set(out) != target:
            raise CorruptError('RLE解码未覆盖请求序号')
        return out

    def _decode_delta(self, stream, anchors, wanted, non_null):
        if non_null == 0:
            if wanted:
                raise CorruptError('差分页无值却被请求')
            return {}
        index = _anchors_parse(anchors, True)
        anchor = _pick_anchor(index, wanted[0])
        # 不变量：ordinal/value 为最后一个已重构序号及其值，pos 指向下一差分
        (value,) = struct.unpack('<q', anchor[2])
        ordinal, pos = anchor[0], anchor[1]
        out = {}
        target = set(wanted)
        if ordinal in target:
            out[ordinal] = value
            self.stats.decoded_values += 1
        while ordinal < wanted[-1]:
            du, pos = _uvarint_bytes(stream, pos)
            value += unzigzag(du)
            if not I64_MIN <= value <= I64_MAX:
                raise CorruptError('差分重构超出i64边界')
            ordinal += 1
            self.stats.decoded_values += 1
            if ordinal in target:
                out[ordinal] = value
        if wanted[-1] == non_null - 1 and pos != len(stream):
            raise CorruptError('差分值流长度不一致')
        if set(out) != target:
            raise CorruptError('差分解码未覆盖请求序号')
        return out

    def _decode_text_plain(self, stream, anchors, wanted, non_null):
        index = _anchors_parse(anchors, False)
        anchor = _pick_anchor(index, wanted[0])
        # 锚点 ordinal 为下一个待物化序号，pos 指向它在值流内的起点
        pos, ordinal = anchor[1], anchor[0]
        out = {}
        target = set(wanted)
        while ordinal <= wanted[-1]:
            length, pos = _uvarint_bytes(stream, pos)
            try:
                s = self._slice(stream, pos, length).decode('utf-8')
            except UnicodeDecodeError:
                raise CorruptError('文本UTF-8解码失败')
            pos += length
            self.stats.decoded_values += 1
            if ordinal in target:
                out[ordinal] = s
            ordinal += 1
        if wanted[-1] == non_null - 1 and pos != len(stream):
            raise CorruptError('普通文本值流长度不一致')
        if set(out) != target:
            raise CorruptError('普通文本解码未覆盖请求序号')
        return out

    def _decode_text_dict(self, stream, anchors, wanted, non_null):
        dict_size, pos = _uvarint_bytes(stream, 0)
        dictionary = []
        for _ in range(dict_size):
            length, pos = _uvarint_bytes(stream, pos)
            try:
                dictionary.append(self._slice(stream, pos, length).decode('utf-8'))
            except UnicodeDecodeError:
                raise CorruptError('字典UTF-8解码失败')
            pos += length
        idx_start = pos
        index = _anchors_parse(anchors, False)   # 偏移相对索引流起点
        anchor = _pick_anchor(index, wanted[0])
        # 锚点 ordinal 为下一个待物化序号，p2 指向其编号在索引流内的起点
        p2 = idx_start + anchor[1]
        ordinal = anchor[0]
        out = {}
        target = set(wanted)
        while ordinal <= wanted[-1]:
            ref, p2 = _uvarint_bytes(stream, p2)
            if ref >= dict_size:
                raise CorruptError('字典引用越界')
            self.stats.decoded_values += 1
            if ordinal in target:
                out[ordinal] = dictionary[ref]
            ordinal += 1
        if wanted[-1] == non_null - 1 and p2 != len(stream):
            raise CorruptError('字典索引流长度不一致')
        if set(out) != target:
            raise CorruptError('字典解码未覆盖请求序号')
        return out

    # ------------------------------------------------------------ 顺序全量校验
    def scan_all(self):
        """顺序解码全部页并核对结构，返回 {列: {物理行号: 值}}，供完整性检查。"""
        columns = {}
        for c, descs in self._index.items():
            col_rows = {}
            for desc in descs:
                header, _, _, _ = self._open_page(desc, self.footer['schema'][c])
                positions = [desc['row_start'] + r for r in range(header['count'])]
                col_rows.update(
                    self._read_page(c, self.footer['schema'][c], desc, positions))
            if len(col_rows) != self.footer['row_count']:
                raise CorruptError('列行数与页脚不一致')
            columns[c] = col_rows
        covered = [d['row_start'] for d in self.footer['pages']]
        if min(covered) < 0:
            raise CorruptError('页偏移为负')
        return columns


def open_column_file(path):
    return ColumnFile(path)


def read_column_file(path, columns, positions):
    """打开、投影查询、关闭，返回 (结果行, 统计字典)。"""
    with ColumnFile(path) as cf:
        return cf.read(columns, positions), cf.stats.as_dict()


def inspect_layout(path):
    with ColumnFile(path) as cf:
        return cf.layout()


# ---------------------------------------------------------------- 新旧并存分派
def read_dataset(source, columns, positions):
    """source 为 version=1 内存资料时走旧行组路径；为 .colpf 路径时走列页。

    旧 JSON 文件仅以只读方式打开，本函数绝不写回它。
    """
    if isinstance(source, dict):
        from .baseline import read
        return read(source, columns, positions)
    with ColumnFile(source) as cf:
        return cf.read(columns, positions)
