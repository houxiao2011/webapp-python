#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
DB operate mudule
"""

import time
import uuid
import functools
import threading
import logging
import sqlite3


# Dict Object

class Dict(dict):
    def __init__(self, names=(), values=(), **kw):
        super(Dict, self).__init__(**kw)
        for k, v in zip(names, values):
            self[k] = v

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(r"'Dict' object has no attribute '%s'" % key)

    def __setattr__(self, key, value):
        self[key] = value

    def next_id(t=None):
        if t is None:
            t = time.time()
        return '%015d%s000' % (int(t * 1000), uuid.uuid4().hex)

    def _profiling(start, sql=''):
        t = time.time() - start
        if t > 0.1:
            logging.warning('[PROFILING] [DB] %s: %s' % (t, sql))
        else:
            logging.info('[PROFILING] [DB] %s: %s' % (t, sql))


class DBError(Exception):
    pass


class MultiColumnsError(DBError):
    pass


class _LazyConnection(object):
    def __init__(self):
        self.connection = engine.connect()

    def cursor(self):
        if self.connection is None:
            self.connection = engine.connect()
            logging.info('open connection <%s>...' % hex(id(connection)))
            # self.connection = connection()
        return self.connection.cursor()

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def cleanup(self):
        pass
        # if self.connection:
        #     # connection = self.connection
        #     self.connection.close()
        #     self.connection = None
        #     logging.info('close connection <%s>...' % hex(id(connection)))
        #     # connection.close()


class _DbCtx(threading.local):
    def __init__(self):
        self.connection = None
        self.transactions = 0

    def is_init(self):
        return not self.connection is None

    def init(self):
        logging.info('open lazy connection...')
        self.connection = _LazyConnection()
        self.transactions = 0

    def cleanup(self):
        self.connection.cleanup()
        self.connection = None

    def cursor(self):
        return self.connection.cursor()


_db_ctx = _DbCtx()

engine = None


class _Engine(object):
    def __init__(self, connect):
        self._connect = connect

    def connect(self):
        return self._connect


# ------ MySQL -----
# def create_engine(user, password, deatabase, host='127.0.0.1', port=3306, **kw):
#     import mysql.connector
#     global engine
#     if engine is not None:
#         raise DBError('Engine is already initialized.')
#     params = dict(user=user, password=password, deatabase=deatabase, host=host, port=port)
#     defaults = dict(use_unicode=True, charset='utf-8', collation='utf8_general_ci', autocommit=False)
#     for k, v in defaults.iteritems():
#         params[k] = kw.pop(k, v)
#     params.update(kw)
#     params['buffered'] = True
#     engine = _Engine(lambda: mysql.connector.connect(**params))
#     logging.info('Init mysql engine <%s> ok.' % hex(id(engine)))

# ------ SQLite3 -----
def create_engine(db):
    conn = sqlite3.connect(db)
    global engine
    if engine is not None:
        raise DBError('Engine is already initialized.')
    engine = _Engine(conn)
    logging.info('Init mysql engine <%s> ok.' % hex(id(engine)))


class _ConnectionCtx(object):
    def __enter__(self):
        global _db_ctx
        self.should_cleanup = False
        if not _db_ctx.is_init():
            _db_ctx.init()
            self.should_cleanup = True
        return self

    def __exit__(self, exctype, excvalue, traceback):
        global _db_ctx
        if self.should_cleanup:
            _db_ctx.cleanup()


def connection():
    return _ConnectionCtx()


def with_connection(func):
    @functools.wraps(func)
    def _wrapper(*args, **kw):
        with _ConnectionCtx():
            return func(*args, **kw)

    return _wrapper


class _TransactionCtx(object):
    def __enter__(self):
        global _db_ctx
        self.should_close_conn = False
        if not _db_ctx.is_init():
            _db_ctx.init()
            self.should_close_conn = True
        _db_ctx.transactions = _db_ctx.transactions + 1
        logging.info('begin transaction...' if _db_ctx.transactions == 1 else 'join current transaction...')
        return self

    def __exit__(self, exctype, excvalue, traceback):
        global _db_ctx
        _db_ctx.transactions = _db_ctx.transactions - 1
        try:
            if _db_ctx.transactions == 0:
                if exctype is None:
                    self.commit()
                else:
                    self.rollback()
        finally:
            if self.should_close_conn:
                _db_ctx.cleanup()

    def commit(self):
        global _db_ctx
        logging.info('commit transaction...')
        try:
            _db_ctx.connection.commit()
            logging.info('commit ok.')
        except:
            logging.warning('commit failed, try to rollback...')
            _db_ctx.connection.rollback()
            logging.warning('rollback ok.')
            raise

    def rollback(self):
        global _db_ctx
        logging.warning('rollback transaction...')
        _db_ctx.connection.rollback()
        logging.info('rollback ok.')


def transaction():
    return _TransactionCtx()


def with_transaction(func):
    @functools.wraps(func)
    def _wrapper(*args, **kw):
        _start = time.time()
        with _TransactionCtx():
            return func(*args, **kw)
        _profiling(_start)

    return _wrapper


def _select(sql, first, *args):
    global _db_ctx
    cursor = None
    sql = sql.replace('%s', '?')
    logging.info('SQL: %s, ARGS: %s' % (sql, args))
    try:
        cursor = _db_ctx.connection.cursor()
        cursor.execute(sql, args)
        if cursor.description:
            names = [x[0] for x in cursor.description]
        if first:
            values = cursor.fetchone()
            if not values:
                return None
            return Dict(names, values)
        return [Dict(names, x) for x in cursor.fetchall()]
    except Exception as e:
        raise e
    finally:
        if cursor:
            cursor.close()


@with_connection
def select_one(sql, *args):
    return _select(sql, True, *args)


@with_connection
def select_int(sql, *args):
    d = _select(sql, True, *args)
    if len(d) != 1:
        raise MultiColumnsError('Expect only one column.')
    return d.values()[0]


@with_connection
def select(sql, *args):
    return _select(sql, False, *args)


@with_connection
def _update(sql, *args):
    global _db_ctx
    cursor = None
    sql = sql.replace('%s', '?')
    logging.info('SQL: %s, ARGS: %s' % (sql, args))
    try:
        if not _db_ctx.is_init():
            _db_ctx.init()

        cursor = _db_ctx.connection.cursor()
        cursor.execute(sql, args)
        r = cursor.rowcount
        if _db_ctx.transactions == 0:
            logging.info('auto commit')
            _db_ctx.connection.commit()
        return r
    finally:
        if cursor:
            cursor.close()


def insert(table, **kw):
    cols, args = zip(*kw.iteritems())
    sql = 'insert into `%s` (%s) values (%s)' % (
        table, ','.join(['`%s`' % col for col in cols]), ','.join(['?' for i in range(len(cols))]))
    return _update(sql, *args)


def update(sql, *args):
    return _update(sql, *args)


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    create_engine('www-data')
    update('drop table if exists user')
    update('create table user (id int primary key, name text, email text, passwd text, last_modified real)')

    u1 = dict(id=2000, name='Bob', email='bob@test.org', passwd='bobobob', last_modified=time.time())
    insert('user', **u1)

    update('update user set email=?, passwd=? where id=?', 'michael@example.org', '654321', 2000)

    u2 = select_one('select * from user where id=?', 2000)
    print u2.email

    import doctest
    doctest.testmod()