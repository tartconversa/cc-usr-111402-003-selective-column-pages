# 列页二进制格式（data_version=2）

`app/columnar.py` 在不改动 version=1 行组 JSON 的前提下，新增自描述的列页容器，
用于按列投影与按物理行号稀疏解码，减少读入字节与解码对象。旧文件只读、不改写。

## 容器布局

全部多字节整数小端。

```
HEAD(8)   = b'CLPF' | 数据版本u16(=2) | 保留u16(=0)
page *    = 按列连续存放的页（同列各页物理相邻，投影时互不干扰）
footer    = JSON(UTF-8, 排序键, 无空白)
TAIL(12)  = footer_len(u32) | footer_crc(u32) | b'CLPF'
```

页脚记录 schema、行数、行组边界 `group_boundaries`、逻辑页边界 `page_bounds`
以及每页描述符（offset/length/编码/理由/三段长度/各层 CRC）。打开时从尾部
固定 12 字节定位并整体校验页脚，再据此建立列→页的定位索引。

### 页结构

固定 46 字节页头 + 有效载荷。

页头 `<HBBBBqIIIII` + 三个 CRC：标记 `CP`、页头版本、编码标记、空值标志、
列类型 id、`row_start`、逻辑行数 `count`、非空数 `non_null`、位图/值流/锚点
三段长度，随后是页头 CRC、位图 CRC、值流 CRC。

有效载荷 = `空位图? | 值流 | 定位锚点`。

- 仅当页内含 null 才写空位图（按位小端）；空值只占位图，不占值流序号。
- 空串 `""` 是普通文本值，不是 null。
- 页脚另存整页载荷 CRC 作为总闸门；页头 CRC、位图 CRC、值流 CRC 分层把关。

## 编码（同一列不同页可各自选择）

选择依据（估算字节数）随页写入 `encoding_reason`，可用 `inspect_layout` 查看。

i64：

- `I64_PLAIN=1` 定长 8 字节，压缩不省时的退路。
- `I64_RLE=2` 重复段：`(run_len:uvarint, value:i64)`，相邻相等值合并。
- `I64_DELTA=3` 差分：首值 + 逐差 zigzag svarint。差分超出 i64 时不强行编码，
  该页退回 PLAIN；解码重构越过 i64 边界立即报 `CorruptError`，不静默回绕。

text（UTF-8 持久化）：

- `TEXT_PLAIN=11` 普通值：`(len:uvarint, utf8)` 序列。
- `TEXT_DICT=12` 页内字典：字典（长度前缀串）+ 每值字典编号 uvarint。
  字典总字节不低于普通值时退回 PLAIN；解码遇到编号 ≥ 字典大小报
  `字典引用越界`。

变长整数为标准无符号 LEB128（uvarint，最多 10 字节，超 64 位或未终止即损坏）；
有符号数经 zigzag 映射，`unzigzag` 对越界值显式报错。

## 定位锚点与稀疏读取

RLE、DELTA、TEXT_PLAIN、TEXT_DICT 的值流都周期性（`ANCHOR_EVERY=256` 个非空值）
写入锚点：锚点序号 + 值流内偏移（DELTA 另存重构基线值）。读取目标行时：

1. 页脚定位索引选出命中的页，未被任何投影列/行命中的页完全不读；
2. 空位图把物理行号映射为值流序号（null 行不进值流）；
3. 选取不超过目标序号的最近锚点，从值流中段开始解码，只物化命中的非空值。

因此大页稀疏读取不会先还原整列：RLE 单游程只解一个值，DELTA/文本从最近锚点
起步。PLAIN 为定长，直接按序号偏移取 8 字节。

`ColumnFile.stats`（`ReadStats`）记录实际触碰的字节范围（合并重叠区间）、
`bytes_read`、`decoded_values` 与 `peak_buffer`。页是 I/O 最小单元；页脚在
打开时一次性读入，用于定位，规模远小于数据页。

行号语义与 version=1 一致：全文件零基物理行号，可重复、可乱序，结果严格按
请求顺序返回，越界报 `行号越界`。

## 损坏处理

截断、魔数/版本不符、页头与页脚描述符不一致、任一层 CRC 不符、页长度不符、
RLE 游程越界、字典引用越界、差分量或重构值越界、UTF-8 解码失败、变长整数
未终止或超 64 位，统一抛 `columnar.CorruptError`。损坏永远不会被降级成 null
或缺值。`scan_all()` 顺序解码全部页做完整校验。

## 原子发布

`publish_dataset` 采用内容寻址的不可变容器（`<name>-<sha16>.colpf`）：

1. 容器先写暂存区并 fsync，算出 SHA-256；
2. 以摘要名 `os.replace` 放入目录（不覆盖在服役容器，同内容复用）；
3. 清单（含容器名、尺寸、SHA-256、行数、列）在暂存区写好后，
   以一次 `os.replace` 原子提交，这是唯一的可见切换点；
4. 任一步失败都回收暂存区与未被清单引用的容器，目录要么保持旧版本，
   要么整体指向已完整落盘的新版本，绝不指向半页。

`verify_manifest` 核对清单摘要与在服役容器；容器被调换或截断即报错。

## 入口

- `write_container(path, data, page_rows=...)`：version=1 内存资料 → 容器。
- `ColumnFile(path).read(columns, positions)` / `read_column_file(...)`：
  投影 + 行号选择，后者附带读取统计。
- `inspect_layout(path)`：页布局、每页编码与选择理由、锚点规模。
- `publish_dataset / verify_manifest`：目录级原子发布与校验。
- `read_dataset(source, columns, positions)`：`dict` 走旧行组路径，
  `.colpf` 路径走列页；旧 JSON 不会被写回。
