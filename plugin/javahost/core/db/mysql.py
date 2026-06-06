# coding: utf-8
"""
MySQL and MariaDB connection helpers — all practical server versions.

MySQL and MariaDB are closely related but use different JDBC drivers and URL
schemes, so each is its own engine instance (same class, different config).
"""
from __future__ import annotations

from . import _base
from ..util import validate


class _MySQLFamily(_base.Engine):
    scheme = "mysql"

    def build_url(self, host, port, db, *, ssl=True, params=None):
        db = validate.identifier(db, "database")
        q = _base.safe_params(params)
        if ssl:
            # MySQL Connector/J 8+ uses sslMode; MariaDB uses sslMode too.
            q = [("sslMode", "REQUIRED")] + q
        query = ["%s=%s" % (k, v) for k, v in q]
        url = "jdbc:%s://%s:%d/%s" % (self.scheme, _base.safe_host(host), port, db)
        return url + ("?" + "&".join(query) if query else "")


class MySQLEngine(_MySQLFamily):
    name = "mysql"
    label = "MySQL"
    scheme = "mysql"
    default_port = 3306
    prefixes = ("mysql",)
    driver_class = "com.mysql.cj.jdbc.Driver"
    driver_modern = "com.mysql:mysql-connector-j:9.1.0"     # Java 8+; server 5.7 .. 9.x
    driver_legacy = "mysql:mysql-connector-java:5.1.49"     # old driver class com.mysql.jdbc.Driver
    detect_cmds = (("mysql", "--version"), ("mysqld", "--version"))
    versions = ["5.5", "5.6", "5.7", "8.0", "8.1", "8.2", "8.3", "8.4", "9.0", "9.1", "9.2"]


class MariaDBEngine(_MySQLFamily):
    name = "mariadb"
    label = "MariaDB"
    scheme = "mariadb"
    default_port = 3306
    prefixes = ("mariadb",)
    driver_class = "org.mariadb.jdbc.Driver"
    driver_modern = "org.mariadb.jdbc:mariadb-java-client:3.5.1"   # Java 8+
    driver_legacy = "org.mariadb.jdbc:mariadb-java-client:2.7.12"  # Java 8 / older stacks
    detect_cmds = (("mariadb", "--version"), ("mysql", "--version"))
    versions = ["10.2", "10.3", "10.4", "10.5", "10.6", "10.11",
                "11.0", "11.1", "11.2", "11.4", "11.5", "11.6", "11.7"]


MYSQL = MySQLEngine()
MARIADB = MariaDBEngine()
