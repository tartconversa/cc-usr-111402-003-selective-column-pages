# 碳清单列页编码与按行选择解码

本项目提供碳预算资料的现有本地执行路径。当前业务行为见app/baseline.py，资料契约见[资料与接口](docs/data-contract.md)。
app/storage.py校验输入并产生确定内容摘要；app/semantics.py提供受限三值谓词；app/service.py绑定资料身份并运行现有代码。
app/columnar.py新增data_version=2的列页二进制格式，与旧行组JSON并存：支持按列投影、按物理行号稀疏解码、
整数重复段/差分压缩、文本页内字典/普通值、分层校验和与原子目录发布，字节布局见[列页格式](docs/columnar-format.md)。
fixtures/sample.json全部为合成资料，不对应真实机构的排放记录。Python 3.14，当前模块仅依赖标准库。

运行已有入口：

    python -m app.service fixtures/sample.json

执行现有行为检查：

    python -m unittest discover -s tests -v

编译检查：

    python -m compileall -q app tests
