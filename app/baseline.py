"""现有执行路径。"""
def read(data,columns,positions):
    if data['version'] != 1: raise ValueError('未知行组格式')
    schema=data['schema']
    if any(c not in schema for c in columns): raise ValueError('未知列')
    rows=[r for group in data['groups'] for r in group['rows']]
    for row in rows:
        for c,typ in schema.items():
            v=row.get(c)
            if v is None: continue
            if typ=='i64' and (type(v) is not int or not -2**63<=v<2**63): raise ValueError('整数类型或范围错误')
            if typ=='text' and not isinstance(v,str): raise ValueError('文本类型错误')
            if typ not in ('i64','text'): raise ValueError('未知列类型')
    if any(type(p) is not int or p<0 or p>=len(rows) for p in positions): raise ValueError('行号越界')
    return [{c:rows[p].get(c) for c in columns} for p in positions]

def execute(data):
    return read(data,data['query']['columns'],data['query']['positions'])
