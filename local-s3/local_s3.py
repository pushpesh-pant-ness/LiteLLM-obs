"""Minimal S3-compatible blob store on local disk - for Langfuse in local/dev setups.

Why: Langfuse v3 needs an S3 API for raw events. This keeps everything on this machine
with no extra image (runs on python:3.12-alpine, stdlib only). It is NOT a general S3
server and has no host port - only containers on the compose network can reach it.

Supports what the AWS SDK sends for Langfuse, path-style:
  PUT/HEAD bucket, PUT/GET/HEAD/DELETE object, ListObjectsV2, DeleteObjects (POST ?delete),
  aws-chunked uploads (streaming signatures / trailing checksums).
Auth: checks the access key id in the Authorization header; signatures are not verified.
"""

import hashlib
import json
import os
import shutil
import sys
import threading
import urllib.parse
from datetime import datetime, timezone
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

ROOT = os.path.abspath(os.environ.get("S3_DATA_DIR", "/data"))
ACCESS_KEY = os.environ.get("S3_ACCESS_KEY_ID", "")
BUCKETS = [b for b in os.environ.get("S3_BUCKETS", "langfuse").split(",") if b]
PORT = int(os.environ.get("S3_PORT", "9000"))
NS = "http://s3.amazonaws.com/doc/2006-03-01/"
META = ".s3meta"
_lock = threading.Lock()


def _safe_path(bucket, key=""):
    p = os.path.abspath(os.path.join(ROOT, bucket, key))
    if not p.startswith(os.path.join(ROOT, bucket)) or META in p.split(os.sep):
        raise ValueError("bad path")
    return p


def _meta_path(bucket, key):
    return os.path.abspath(os.path.join(ROOT, META, bucket, key + ".json"))


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _decode_aws_chunked(raw):
    """aws-chunked: '<hex>[;chunk-signature=..]\\r\\n<data>\\r\\n' ... '0\\r\\n<trailers>\\r\\n\\r\\n'."""
    out, i = bytearray(), 0
    while True:
        j = raw.index(b"\r\n", i)
        size = int(raw[i:j].split(b";")[0], 16)
        i = j + 2
        if size == 0:
            return bytes(out)
        out += raw[i:i + size]
        i += size + 2


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "local-s3/1.0"

    def log_message(self, fmt, *args):  # quieter logs: method, path, status only
        if os.environ.get("S3_VERBOSE"):
            super().log_message(fmt, *args)

    # ------------------------------------------------------------- plumbing
    def _send(self, status, body=b"", ctype="application/xml", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("x-amz-request-id", hashlib.md5(os.urandom(8)).hexdigest()[:16])
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _error(self, status, code, msg):
        body = f'<?xml version="1.0" encoding="UTF-8"?><Error><Code>{code}</Code><Message>{escape(msg)}</Message></Error>'
        self._send(status, body.encode())

    def _parse(self):
        u = urllib.parse.urlsplit(self.path)
        parts = urllib.parse.unquote(u.path).lstrip("/").split("/", 1)
        bucket = parts[0]
        key = parts[1] if len(parts) > 1 else ""
        q = urllib.parse.parse_qs(u.query, keep_blank_values=True)
        return bucket, key, q

    def _body(self):
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            data = bytearray()
            while True:
                line = self.rfile.readline()
                size = int(line.split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    break
                data += self.rfile.read(size)
                self.rfile.readline()
            raw = bytes(data)
        else:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        sha = self.headers.get("x-amz-content-sha256", "")
        enc = self.headers.get("Content-Encoding", "")
        if sha.startswith("STREAMING-") or "aws-chunked" in enc:
            raw = _decode_aws_chunked(raw)
        return raw

    def _authorized(self):
        if not ACCESS_KEY:
            return True
        auth = self.headers.get("Authorization", "")
        if "Credential=" in auth:
            return auth.split("Credential=", 1)[1].split("/", 1)[0] == ACCESS_KEY
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        cred = (q.get("X-Amz-Credential") or [""])[0]
        return cred.split("/", 1)[0] == ACCESS_KEY

    def _dispatch(self):
        if not self._authorized():
            return self._error(403, "InvalidAccessKeyId", "unknown access key")
        try:
            bucket, key, q = self._parse()
            if not bucket:
                return self._list_buckets()
            bdir = _safe_path(bucket)
            if not key:
                return self._bucket_op(bucket, bdir, q)
            if not os.path.isdir(bdir):
                return self._error(404, "NoSuchBucket", bucket)
            return self._object_op(bucket, key, q)
        except ValueError:
            return self._error(400, "InvalidRequest", "bad path")
        except Exception as e:  # keep serving
            print(f"error {self.command} {self.path}: {e!r}", file=sys.stderr, flush=True)
            return self._error(500, "InternalError", str(e))

    do_GET = do_PUT = do_HEAD = do_DELETE = do_POST = lambda self: self._dispatch()

    # ------------------------------------------------------------- buckets
    def _list_buckets(self):
        names = sorted(d for d in os.listdir(ROOT) if d != META and os.path.isdir(os.path.join(ROOT, d)))
        items = "".join(f"<Bucket><Name>{escape(n)}</Name><CreationDate>{_iso(os.path.getctime(os.path.join(ROOT, n)))}</CreationDate></Bucket>" for n in names)
        self._send(200, f'<?xml version="1.0" encoding="UTF-8"?><ListAllMyBucketsResult xmlns="{NS}"><Buckets>{items}</Buckets></ListAllMyBucketsResult>'.encode())

    def _bucket_op(self, bucket, bdir, q):
        cmd = self.command
        if cmd == "PUT":
            self._body()
            os.makedirs(bdir, exist_ok=True)
            return self._send(200, headers={"Location": f"/{bucket}"})
        if not os.path.isdir(bdir):
            return self._error(404, "NoSuchBucket", bucket)
        if cmd == "HEAD":
            return self._send(200)
        if cmd == "POST" and "delete" in q:
            return self._delete_objects(bucket)
        if cmd == "GET" and "location" in q:
            return self._send(200, f'<?xml version="1.0" encoding="UTF-8"?><LocationConstraint xmlns="{NS}"/>'.encode())
        if cmd == "GET":
            return self._list_objects(bucket, bdir, q)
        return self._error(501, "NotImplemented", f"{cmd} on bucket")

    def _list_objects(self, bucket, bdir, q):
        prefix = (q.get("prefix") or [""])[0]
        max_keys = int((q.get("max-keys") or ["1000"])[0])
        start = (q.get("continuation-token") or q.get("start-after") or q.get("marker") or [""])[0]
        keys = []
        for dirpath, _, files in os.walk(bdir):
            for f in files:
                k = os.path.relpath(os.path.join(dirpath, f), bdir).replace(os.sep, "/")
                if k.startswith(prefix) and k > start:
                    keys.append(k)
        keys.sort()
        page, truncated = keys[:max_keys], len(keys) > max_keys
        items = []
        for k in page:
            st = os.stat(os.path.join(bdir, k))
            items.append(f"<Contents><Key>{escape(k)}</Key><LastModified>{_iso(st.st_mtime)}</LastModified>"
                         f"<Size>{st.st_size}</Size><StorageClass>STANDARD</StorageClass></Contents>")
        nxt = f"<NextContinuationToken>{escape(page[-1])}</NextContinuationToken>" if truncated and page else ""
        body = (f'<?xml version="1.0" encoding="UTF-8"?><ListBucketResult xmlns="{NS}"><Name>{escape(bucket)}</Name>'
                f"<Prefix>{escape(prefix)}</Prefix><KeyCount>{len(page)}</KeyCount><MaxKeys>{max_keys}</MaxKeys>"
                f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>{nxt}{''.join(items)}</ListBucketResult>")
        self._send(200, body.encode())

    def _delete_objects(self, bucket):
        root = ET.fromstring(self._body())
        deleted = []
        for obj in root.iter():
            if obj.tag.endswith("Object"):
                k = next((c.text for c in obj if c.tag.endswith("Key")), None)
                if k:
                    self._remove(bucket, k)
                    deleted.append(f"<Deleted><Key>{escape(k)}</Key></Deleted>")
        self._send(200, f'<?xml version="1.0" encoding="UTF-8"?><DeleteResult xmlns="{NS}">{"".join(deleted)}</DeleteResult>'.encode())

    # ------------------------------------------------------------- objects
    def _remove(self, bucket, key):
        for p in (_safe_path(bucket, key), _meta_path(bucket, key)):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass

    def _object_op(self, bucket, key, q):
        path = _safe_path(bucket, key)
        cmd = self.command
        if cmd == "PUT":
            src = self.headers.get("x-amz-copy-source")
            if src:
                sb, sk = urllib.parse.unquote(src).lstrip("/").split("/", 1)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                shutil.copyfile(_safe_path(sb, sk), path)
                etag = hashlib.md5(open(path, "rb").read()).hexdigest()
                return self._send(200, f'<?xml version="1.0" encoding="UTF-8"?><CopyObjectResult><ETag>"{etag}"</ETag><LastModified>{_iso(os.path.getmtime(path))}</LastModified></CopyObjectResult>'.encode())
            data = self._body()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.tmp-{threading.get_ident()}"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
            etag = hashlib.md5(data).hexdigest()
            mp = _meta_path(bucket, key)
            os.makedirs(os.path.dirname(mp), exist_ok=True)
            with open(mp, "w") as f:
                json.dump({"etag": etag, "content_type": self.headers.get("Content-Type", "application/octet-stream")}, f)
            return self._send(200, headers={"ETag": f'"{etag}"'})
        if cmd == "DELETE":
            self._remove(bucket, key)
            return self._send(204)
        if cmd in ("GET", "HEAD"):
            if not os.path.isfile(path):
                return self._error(404, "NoSuchKey", key)
            meta = {}
            try:
                with open(_meta_path(bucket, key)) as f:
                    meta = json.load(f)
            except (FileNotFoundError, ValueError):
                pass
            with open(path, "rb") as f:
                data = f.read()
            headers = {"ETag": f'"{meta.get("etag") or hashlib.md5(data).hexdigest()}"',
                       "Last-Modified": formatdate(os.path.getmtime(path), usegmt=True),
                       "Accept-Ranges": "bytes"}
            rng = self.headers.get("Range", "")
            if rng.startswith("bytes=") and cmd == "GET":
                a, b = rng[6:].split("-", 1)
                a = int(a) if a else max(0, len(data) - int(b))
                b = int(b) if b and rng[6:].split("-", 1)[0] else len(data) - 1
                headers["Content-Range"] = f"bytes {a}-{b}/{len(data)}"
                return self._send(206, data[a:b + 1], meta.get("content_type", "application/octet-stream"), headers)
            if cmd == "HEAD":
                self.send_response(200)
                self.send_header("Content-Type", meta.get("content_type", "application/octet-stream"))
                self.send_header("Content-Length", str(len(data)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                return
            return self._send(200, data, meta.get("content_type", "application/octet-stream"), headers)
        return self._error(501, "NotImplemented", f"{cmd} on object")


if __name__ == "__main__":
    os.makedirs(os.path.join(ROOT, META), exist_ok=True)
    for b in BUCKETS:
        os.makedirs(os.path.join(ROOT, b), exist_ok=True)
    print(f"local-s3 listening on :{PORT}, data in {ROOT}, buckets {BUCKETS}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
