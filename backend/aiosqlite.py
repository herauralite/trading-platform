import asyncio, sqlite3
from functools import partial

class _DummyThread:
    daemon = True

class Cursor:
    def __init__(self,c): self._c=c
    async def execute(self,*a,**k): await asyncio.to_thread(self._c.execute,*a,**k); return self
    async def executemany(self,*a,**k): await asyncio.to_thread(self._c.executemany,*a,**k); return self
    async def fetchone(self): return await asyncio.to_thread(self._c.fetchone)
    async def fetchmany(self,size=None): return await asyncio.to_thread(self._c.fetchmany,*([] if size is None else [size]))
    async def fetchall(self): return await asyncio.to_thread(self._c.fetchall)
    async def close(self): await asyncio.to_thread(self._c.close)
    @property
    def description(self): return self._c.description
    @property
    def rowcount(self): return self._c.rowcount
    @property
    def lastrowid(self): return self._c.lastrowid

class Connection:
    def __init__(self, database, **kwargs):
        self._database=database; self._kwargs=kwargs; self._conn=None; self._thread=_DummyThread()
    def __await__(self):
        return self._connect().__await__()
    async def _connect(self):
        if self._conn is None:
            self._conn = await asyncio.to_thread(partial(sqlite3.connect, self._database, **self._kwargs))
        return self
    async def cursor(self): return Cursor(await asyncio.to_thread(self._conn.cursor))
    async def execute(self,*a,**k): return Cursor(await asyncio.to_thread(self._conn.execute,*a,**k))
    async def executemany(self,*a,**k): return Cursor(await asyncio.to_thread(self._conn.executemany,*a,**k))
    async def commit(self): await asyncio.to_thread(self._conn.commit)
    async def rollback(self): await asyncio.to_thread(self._conn.rollback)
    async def close(self): await asyncio.to_thread(self._conn.close)
    async def create_function(self,*a,**k): await asyncio.to_thread(self._conn.create_function,*a,**k)
    @property
    def isolation_level(self): return self._conn.isolation_level
    @isolation_level.setter
    def isolation_level(self,v): self._conn.isolation_level=v
    @property
    def row_factory(self): return self._conn.row_factory
    @row_factory.setter
    def row_factory(self,v): self._conn.row_factory=v
    @property
    def text_factory(self): return self._conn.text_factory
    @text_factory.setter
    def text_factory(self,v): self._conn.text_factory=v


def connect(database, **kwargs):
    return Connection(database, **kwargs)

Error=sqlite3.Error; DatabaseError=sqlite3.DatabaseError; IntegrityError=sqlite3.IntegrityError
NotSupportedError=sqlite3.NotSupportedError; OperationalError=sqlite3.OperationalError; ProgrammingError=sqlite3.ProgrammingError
sqlite_version=sqlite3.sqlite_version; sqlite_version_info=sqlite3.sqlite_version_info
PARSE_DECLTYPES=sqlite3.PARSE_DECLTYPES; PARSE_COLNAMES=sqlite3.PARSE_COLNAMES
Binary=sqlite3.Binary; Row=sqlite3.Row
