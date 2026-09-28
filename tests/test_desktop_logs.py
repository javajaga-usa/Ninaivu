from ninaivu.desktop.logs import LogTail


def test_tail_follows_append_and_truncation(tmp_path):
    path = tmp_path / 'server.log'
    tail = LogTail(path)
    assert tail.read() == ''
    path.write_text('First line\n', encoding='utf-8')
    assert tail.read() == 'First line\n'
    assert tail.read() == ''
    with path.open('a', encoding='utf-8') as stream:
        stream.write('Second line\n')
    assert tail.read() == 'Second line\n'
    path.write_text('New\n', encoding='utf-8')
    assert tail.read() == 'New\n'


def test_tail_bounds_large_logs_and_handles_split_utf8(tmp_path):
    path = tmp_path / 'server.log'
    path.write_bytes(b'old line\n' * 100)
    tail = LogTail(path, limit=64)
    text = tail.read()
    assert len(text) < 120
    assert 'most recent' in text
    assert tail.read() == ''
    path.write_bytes(b'\xe2\x82')
    assert tail.read() == ''
    with path.open('ab') as stream:
        stream.write(b'\xac\n')
    assert tail.read() == '\u20ac\n'


def test_tail_strips_terminal_escapes(tmp_path):
    path = tmp_path / 'server.log'
    path.write_bytes(b'\x1b[31mERROR\x1b[0m\n')
    assert LogTail(path).read() == 'ERROR\n'


def test_tail_follows_a_replaced_log_file(tmp_path):
    path = tmp_path / 'server.log'
    path.write_bytes(b'Previous server\n')
    tail = LogTail(path)
    assert tail.read() == 'Previous server\n'
    replacement = tmp_path / 'replacement.log'
    replacement.write_bytes(b'Restarted server with a longer message\n')
    replacement.replace(path)
    assert tail.read() == 'Restarted server with a longer message\n'


def test_tail_shows_long_output_even_without_newlines(tmp_path):
    path = tmp_path / 'server.log'
    path.write_bytes(b'x' * 200)
    text = LogTail(path, limit=64).read()
    assert text.endswith('x' * 64)
