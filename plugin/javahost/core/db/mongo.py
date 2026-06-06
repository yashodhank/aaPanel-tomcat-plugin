# coding: utf-8
"""
MongoDB connection helper — server versions 3.6 .. 8.0.

MongoDB is not JDBC; Java apps use the MongoDB Java driver with a `mongodb://`
connection string. JavaHost emits the base URI (no embedded password) plus
DB_USER/DB_PASSWORD so the app builds credentials via MongoClientSettings —
keeping the secret out of the URI string and logs.
"""
from __future__ import annotations

from . import _base
from ..util import validate


class MongoEngine(_base.Engine):
    name = "mongodb"
    label = "MongoDB"
    default_port = 27017
    prefixes = ("mongodb", "mongo")
    driver_class = "com.mongodb.client.MongoClients"
    driver_modern = "org.mongodb:mongodb-driver-sync:5.2.1"   # Java 8+; server 3.6 .. 8.0
    driver_legacy = "org.mongodb:mongodb-driver-sync:4.11.1"  # older stacks
    detect_cmds = (("mongod", "--version"), ("mongosh", "--version"), ("mongo", "--version"))
    versions = ["3.6", "4.0", "4.2", "4.4", "5.0", "6.0", "7.0", "8.0"]

    def build_url(self, host, port, db, *, ssl=True, params=None):
        db = validate.identifier(db, "database")
        q = _base.safe_params(params)
        if ssl:
            q = [("tls", "true")] + q
        query = ["%s=%s" % (k, v) for k, v in q]
        url = "mongodb://%s:%d/%s" % (_base.safe_host(host), port, db)
        return url + ("?" + "&".join(query) if query else "")

    def env_vars(self, url, user, password, java_major):
        return {
            "DB_URL": url,            # mongodb:// URI without credentials
            "DB_USER": user,
            "DB_PASSWORD": password,  # supply via MongoCredential, not in the URI
            "DB_DRIVER": self.driver_class,
            "DB_DRIVER_MAVEN": self.recommend_driver(java_major),
            "DB_AUTH_SOURCE": "admin",
        }


ENGINE = MongoEngine()
