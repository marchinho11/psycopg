import string
import hashlib
from io import BytesIO, StringIO
from itertools import cycle

import pytest

from psycopg3 import pq
from psycopg3 import errors as e
from psycopg3.adapt import Format
from psycopg3.types.numeric import Int4

eur = "\u20ac"

sample_records = [(Int4(10), Int4(20), "hello"), (Int4(40), None, "world")]

sample_values = "values (10::int, 20::int, 'hello'::text), (40, NULL, 'world')"

sample_tabledef = "col1 int primary key, col2 int, data text"

sample_text = b"""\
10\t20\thello
40\t\\N\tworld
"""

sample_binary = """
5047 434f 5059 0aff 0d0a 00
00 0000 0000 0000 00
00 0300 0000 0400 0000 0a00 0000 0400 0000 1400 0000 0568 656c 6c6f

0003 0000 0004 0000 0028 ffff ffff 0000 0005 776f 726c 64

ff ff
"""

sample_binary_rows = [
    bytes.fromhex("".join(row.split())) for row in sample_binary.split("\n\n")
]

sample_binary = b"".join(sample_binary_rows)


@pytest.mark.parametrize("format", [Format.TEXT, Format.BINARY])
def test_copy_out_read(conn, format):
    if format == pq.Format.TEXT:
        want = [row + b"\n" for row in sample_text.splitlines()]
    else:
        want = sample_binary_rows

    cur = conn.cursor()
    with cur.copy(
        f"copy ({sample_values}) to stdout (format {format.name})"
    ) as copy:
        for row in want:
            got = copy.read()
            assert got == row

        assert copy.read() == b""
        assert copy.read() == b""

    assert copy.read() == b""


@pytest.mark.parametrize("format", [Format.TEXT, Format.BINARY])
def test_copy_out_iter(conn, format):
    if format == pq.Format.TEXT:
        want = [row + b"\n" for row in sample_text.splitlines()]
    else:
        want = sample_binary_rows
    cur = conn.cursor()
    with cur.copy(
        f"copy ({sample_values}) to stdout (format {format.name})"
    ) as copy:
        assert list(copy) == want


@pytest.mark.parametrize(
    "format, buffer",
    [(Format.TEXT, "sample_text"), (Format.BINARY, "sample_binary")],
)
def test_copy_in_buffers(conn, format, buffer):
    cur = conn.cursor()
    ensure_table(cur, sample_tabledef)
    with cur.copy(f"copy copy_in from stdin (format {format.name})") as copy:
        copy.write(globals()[buffer])

    data = cur.execute("select * from copy_in order by 1").fetchall()
    assert data == sample_records


def test_copy_in_buffers_pg_error(conn):
    cur = conn.cursor()
    ensure_table(cur, sample_tabledef)
    with pytest.raises(e.UniqueViolation):
        with cur.copy("copy copy_in from stdin (format text)") as copy:
            copy.write(sample_text)
            copy.write(sample_text)
    assert conn.pgconn.transaction_status == conn.TransactionStatus.INERROR


def test_copy_bad_result(conn):
    conn.autocommit = True

    cur = conn.cursor()

    with pytest.raises(e.SyntaxError):
        with cur.copy("wat"):
            pass

    with pytest.raises(e.ProgrammingError):
        with cur.copy("select 1"):
            pass

    with pytest.raises(e.ProgrammingError):
        with cur.copy("reset timezone"):
            pass


def test_copy_in_str(conn):
    cur = conn.cursor()
    ensure_table(cur, sample_tabledef)
    with cur.copy("copy copy_in from stdin (format text)") as copy:
        copy.write(sample_text.decode("utf8"))

    data = cur.execute("select * from copy_in order by 1").fetchall()
    assert data == sample_records


def test_copy_in_str_binary(conn):
    cur = conn.cursor()
    ensure_table(cur, sample_tabledef)
    with pytest.raises(e.QueryCanceled):
        with cur.copy("copy copy_in from stdin (format binary)") as copy:
            copy.write(sample_text.decode("utf8"))

    assert conn.pgconn.transaction_status == conn.TransactionStatus.INERROR


def test_copy_in_buffers_with_pg_error(conn):
    cur = conn.cursor()
    ensure_table(cur, sample_tabledef)
    with pytest.raises(e.UniqueViolation):
        with cur.copy("copy copy_in from stdin (format text)") as copy:
            copy.write(sample_text)
            copy.write(sample_text)

    assert conn.pgconn.transaction_status == conn.TransactionStatus.INERROR


def test_copy_in_buffers_with_py_error(conn):
    cur = conn.cursor()
    ensure_table(cur, sample_tabledef)
    with pytest.raises(e.QueryCanceled) as exc:
        with cur.copy("copy copy_in from stdin (format text)") as copy:
            copy.write(sample_text)
            raise Exception("nuttengoggenio")

    assert "nuttengoggenio" in str(exc.value)
    assert conn.pgconn.transaction_status == conn.TransactionStatus.INERROR


@pytest.mark.parametrize("format", [Format.TEXT, Format.BINARY])
def test_copy_in_records(conn, format):
    cur = conn.cursor()
    ensure_table(cur, sample_tabledef)

    with cur.copy(f"copy copy_in from stdin (format {format.name})") as copy:
        for row in sample_records:
            copy.write_row(row)

    data = cur.execute("select * from copy_in order by 1").fetchall()
    assert data == sample_records


@pytest.mark.parametrize("format", [Format.TEXT, Format.BINARY])
def test_copy_in_records_binary(conn, format):
    cur = conn.cursor()
    ensure_table(cur, "col1 serial primary key, col2 int, data text")

    with cur.copy(
        f"copy copy_in (col2, data) from stdin (format {format.name})"
    ) as copy:
        for row in sample_records:
            copy.write_row((None, row[2]))

    data = cur.execute("select * from copy_in order by 1").fetchall()
    assert data == [(1, None, "hello"), (2, None, "world")]


def test_copy_in_allchars(conn):
    cur = conn.cursor()
    ensure_table(cur, sample_tabledef)

    conn.client_encoding = "utf8"
    with cur.copy("copy copy_in from stdin (format text)") as copy:
        for i in range(1, 256):
            copy.write_row((i, None, chr(i)))
        copy.write_row((ord(eur), None, eur))

    data = cur.execute(
        """
select col1 = ascii(data), col2 is null, length(data), count(*)
from copy_in group by 1, 2, 3
"""
    ).fetchall()
    assert data == [(True, True, 1, 256)]


@pytest.mark.slow
def test_copy_from_to(conn):
    # Roundtrip from file to database to file blockwise
    gen = DataGenerator(conn, nrecs=1024, srec=10 * 1024)
    gen.ensure_table()
    cur = conn.cursor()
    with cur.copy("copy copy_in from stdin") as copy:
        for block in gen.blocks():
            copy.write(block)

    gen.assert_data()

    f = StringIO()
    with cur.copy("copy copy_in to stdout") as copy:
        for block in copy:
            f.write(block.decode("utf8"))

    f.seek(0)
    assert gen.sha(f) == gen.sha(gen.file())


@pytest.mark.slow
def test_copy_from_to_bytes(conn):
    # Roundtrip from file to database to file blockwise
    gen = DataGenerator(conn, nrecs=1024, srec=10 * 1024)
    gen.ensure_table()
    cur = conn.cursor()
    with cur.copy("copy copy_in from stdin") as copy:
        for block in gen.blocks():
            copy.write(block.encode("utf8"))

    gen.assert_data()

    f = BytesIO()
    with cur.copy("copy copy_in to stdout") as copy:
        for block in copy:
            f.write(block)

    f.seek(0)
    assert gen.sha(f) == gen.sha(gen.file())


@pytest.mark.slow
def test_copy_from_insane_size(conn):
    # Trying to trigger a "would block" error
    gen = DataGenerator(
        conn, nrecs=4 * 1024, srec=10 * 1024, block_size=20 * 1024 * 1024
    )
    gen.ensure_table()
    cur = conn.cursor()
    with cur.copy("copy copy_in from stdin") as copy:
        for block in gen.blocks():
            copy.write(block)

    gen.assert_data()


def test_copy_rowcount(conn):
    gen = DataGenerator(conn, nrecs=3, srec=10)
    gen.ensure_table()

    cur = conn.cursor()
    with cur.copy("copy copy_in from stdin") as copy:
        for block in gen.blocks():
            copy.write(block)
    assert cur.rowcount == 3

    gen = DataGenerator(conn, nrecs=2, srec=10, offset=3)
    with cur.copy("copy copy_in from stdin") as copy:
        for rec in gen.records():
            copy.write_row(rec)
    assert cur.rowcount == 2

    with cur.copy("copy copy_in to stdout") as copy:
        for block in copy:
            pass
    assert cur.rowcount == 5

    with pytest.raises(e.BadCopyFileFormat):
        with cur.copy("copy copy_in (id) from stdin") as copy:
            for rec in gen.records():
                copy.write_row(rec)
    assert cur.rowcount == -1


def test_copy_query(conn):
    cur = conn.cursor()
    with cur.copy("copy (select 1) to stdout") as copy:
        assert cur.query == b"copy (select 1) to stdout"
        assert cur.params is None
        list(copy)


def ensure_table(cur, tabledef, name="copy_in"):
    cur.execute(f"drop table if exists {name}")
    cur.execute(f"create table {name} ({tabledef})")


class DataGenerator:
    def __init__(self, conn, nrecs, srec, offset=0, block_size=8192):
        self.conn = conn
        self.nrecs = nrecs
        self.srec = srec
        self.offset = offset
        self.block_size = block_size

    def ensure_table(self):
        cur = self.conn.cursor()
        ensure_table(cur, "id integer primary key, data text")

    def records(self):
        for i, c in zip(range(self.nrecs), cycle(string.ascii_letters)):
            s = c * self.srec
            yield (i + self.offset, s)

    def file(self):
        f = StringIO()
        for i, s in self.records():
            f.write("%s\t%s\n" % (i, s))

        f.seek(0)
        return f

    def blocks(self):
        f = self.file()
        while True:
            block = f.read(self.block_size)
            if not block:
                break
            yield block

    def assert_data(self):
        cur = self.conn.cursor()
        cur.execute("select id, data from copy_in order by id")
        for record in self.records():
            assert record == cur.fetchone()

        assert cur.fetchone() is None

    def sha(self, f):
        m = hashlib.sha256()
        while 1:
            block = f.read()
            if not block:
                break
            if isinstance(block, str):
                block = block.encode("utf8")
            m.update(block)
        return m.hexdigest()
