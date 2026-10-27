"""Real SQLite contention facts for two client tests; never a provider call."""
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from codex_mem import jev_client as client
from codex_mem.store import Store
from tests.test_jev_client import QUESTIONS, response


def run(data, project, mode, ready):
    connections, writes, providers, expiry = [], [], [], []
    clock = [0.0]
    original_connection = client._connection

    class Connection:
        def __init__(self, real, metadata):
            self.real, self.metadata = real, metadata

        def execute(self, sql, *args):
            statement = ' '.join(sql.split())
            write = statement.startswith(('CREATE ', 'INSERT ', 'DELETE ', 'UPDATE ', 'REPLACE '))
            attempt = dict(connection=self.metadata['number'], statement=statement,
                           busy_timeout_ms=self.real.execute('PRAGMA busy_timeout').fetchone()[0],
                           readonly=self.metadata['readonly'])
            if write:
                writes.append(attempt)
            try:
                return self.real.execute(sql, *args)
            except sqlite3.Error as error:
                if write:
                    attempt.update(sqlite_errorcode=getattr(error, 'sqlite_errorcode', None),
                                   error_type=type(error).__name__, error_message=str(error))
                    busy = (attempt['sqlite_errorcode'] == getattr(sqlite3, 'SQLITE_BUSY', 5) or
                            (attempt['sqlite_errorcode'] is None and isinstance(error, sqlite3.OperationalError)
                             and str(error) == 'database is locked'))
                    # The real SQL call has returned SQLITE_BUSY before clock expiry.
                    if (mode == 'expire' and statement.startswith('CREATE TABLE IF NOT EXISTS jev_judgment_cache ')
                            and busy):
                        expiry.append(dict(attempt))
                        clock[0] = 1.0
                raise

        def __enter__(self):
            self.real.__enter__()
            return self

        def __exit__(self, *args):
            return self.real.__exit__(*args)

        def close(self):
            self.real.close()
            try:
                self.real.execute('SELECT 1')
            except sqlite3.ProgrammingError:
                self.metadata['closed'] = True

    def connection(*args, **kwargs):
        real = original_connection(*args, **kwargs)
        metadata = dict(number=len(connections) + 1, readonly=kwargs.get('readonly', False),
                        busy_timeout_ms=real.execute('PRAGMA busy_timeout').fetchone()[0], closed=False)
        connections.append(metadata)
        return Connection(real, metadata)

    def provider(payload):
        providers.append(1)
        return response(payload)

    def tables(store):
        return [row[0] for row in store._connection.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('jev_judgment_cache','jev_judgment_audits')")]

    with Store(data) as store:
        lock = sqlite3.connect(store.db_path)
        try:
            lock.execute('BEGIN IMMEDIATE')
            ready_path = Path(ready)
            temporary = ready_path.with_suffix('.tmp')
            temporary.write_text('writer held')
            temporary.replace(ready_path)
            if sys.stdin.readline() != 'run\n':
                raise RuntimeError('contention test start was not received')
            with patch.object(client, '_connection', connection), patch.object(
                    client, '_post', side_effect=AssertionError('No network')):
                arguments = dict(state={'query': 'Synthetic contention'}, questions=QUESTIONS,
                    policy_version='test-judgment-v1', project=project, store=store, evaluator=provider)
                if mode == 'expire':
                    arguments['timeout'] = .01
                    with patch.object(client, 'time', SimpleNamespace(monotonic=lambda: clock[0])):
                        try:
                            answers, audit = client.evaluate(**arguments)
                            error_code = None
                        except client.JevError as error:
                            answers, audit, error_code = None, error.audit, error.code
                else:
                    answers, audit = client.evaluate(**arguments)
                    error_code = None
                saved = client.record_audit(store, project, audit, route='search')
            held, under_lock = lock.in_transaction, tables(store)
            lock.rollback()
            released = not lock.in_transaction
            # A new real writer proves the held lock was released, without data writes.
            with sqlite3.connect(store.db_path, timeout=.02) as next_writer:
                next_writer.execute('BEGIN IMMEDIATE')
                next_writer.rollback()
            next_writer.close()
            return dict(answers=answers, audit=audit, error_code=error_code, audit_saved=saved,
                providers=len(providers), connections=connections, writes=writes,
                writer_held=held, lock_released=released, tables_under_lock=under_lock,
                tables_after_release=tables(store), expiry_after_real_cache_busy=expiry)
        finally:
            lock.rollback()
            lock.close()


if __name__ == '__main__':
    print(json.dumps(run(*sys.argv[1:])), flush=True)
