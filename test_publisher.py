import base64
import io
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import publisher


class Upload:
    def __init__(self, filename, content):
        self.filename = filename
        self.file = io.BytesIO(content)


class Fields(dict):
    def __init__(self, kind, files, slug="demo", title="演示页面"):
        super().__init__(files=files)
        self.values = {"kind": kind, "slug": slug, "title": title}

    def getfirst(self, name, default=""):
        return self.values.get(name, default)


class PublisherTests(unittest.TestCase):
    def test_single_html_becomes_index(self):
        fields = Fields("file", [Upload("report.html", b'<link href="style.css"><h1>ok</h1>')])
        stage = publisher.normalize_upload(fields)
        self.assertEqual(set(stage["files"]), {"index.html"})
        self.assertIn("相对资源", " ".join(stage["warnings"]))

    def test_folder_keeps_resources_and_skips_hidden(self):
        fields = Fields("folder", [
            Upload("my-site/index.html", b'<link href="assets/site.css">'),
            Upload("my-site/assets/site.css", b"body{}"),
            Upload("my-site/.DS_Store", b"hidden"),
        ])
        stage = publisher.normalize_upload(fields)
        self.assertEqual(set(stage["files"]), {"index.html", "assets/site.css"})
        self.assertIn("跳过", " ".join(stage["warnings"]))

    def test_bad_path_and_missing_entry_are_rejected(self):
        with self.assertRaises(publisher.PublishError):
            publisher.normalize_upload(Fields("folder", [Upload("site/../index.html", b"x")]))
        with self.assertRaises(publisher.PublishError):
            publisher.normalize_upload(Fields("folder", [Upload("site/page.html", b"x")]))

    def test_publish_replaces_old_files_in_one_nonforce_commit(self):
        old_sites = [{"slug": "demo", "title": "旧标题", "updated_at": "2026-01-01T00:00:00+00:00"}]
        calls = []
        def fake_github(method, path, token, data=None):
            calls.append((method, path, data))
            if path == "":
                return {"full_name": "jamiepak/html-publish-hub", "permissions": {"push": True}}
            if path == "/git/ref/heads/gh-pages":
                return {"object": {"sha": "old-head"}}
            if path == "/git/commits/old-head":
                return {"tree": {"sha": "old-tree"}}
            if path == "/git/trees/old-tree?recursive=1":
                return {"truncated": False, "tree": [
                    {"path": "sites/demo/index.html", "type": "blob", "sha": "old-index"},
                    {"path": "sites/demo/old.css", "type": "blob", "sha": "old-css"},
                    {"path": "sites.json", "type": "blob", "sha": "manifest"},
                ]}
            if path == "/git/blobs/manifest":
                return {"content": base64.b64encode(json.dumps(old_sites).encode()).decode()}
            if method == "POST" and path == "/git/blobs":
                return {"sha": "blob-" + str(len(calls))}
            if method == "POST" and path == "/git/trees":
                return {"sha": "new-tree"}
            if method == "POST" and path == "/git/commits":
                return {"sha": "new-head"}
            if method == "PATCH":
                return {"ref": "gh-pages"}
            raise AssertionError((method, path))

        stage = {"slug": "demo", "title": "新标题", "files": {"index.html": b"<h1>new</h1>"}, "digest": "a" * 64}
        with patch.object(publisher, "github", fake_github):
            result = publisher.publish(stage, "test-token")
        tree = next(data for method, path, data in calls if path == "/git/trees")
        self.assertIn({"path": "sites/demo/old.css", "mode": "100644", "type": "blob", "sha": None}, tree["tree"])
        self.assertEqual(tree["base_tree"], "old-tree")
        self.assertEqual(next(data for method, path, data in calls if method == "PATCH")["force"], False)
        self.assertEqual(result["commit"], "new-head")

    def test_local_preview_requires_origin_and_confirmation(self):
        old_port = publisher.PORT
        server = ThreadingHTTPServer(("127.0.0.1", 0), publisher.Handler)
        publisher.PORT = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{publisher.PORT}"
        try:
            page = urllib.request.urlopen(base + "/").read().decode()
            self.assertIn(publisher.CSRF, page)
            boundary = "test-boundary"
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"kind\"\r\n\r\nfile\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"slug\"\r\n\r\ndemo\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\nDemo\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"demo.html\"\r\n"
                "Content-Type: text/html\r\n\r\n<h1>demo</h1>\r\n"
                f"--{boundary}--\r\n"
            ).encode()
            req = urllib.request.Request(base + "/api/preview", data=body, method="POST", headers={
                "Origin": base, "X-Publish-Session": publisher.CSRF,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            })
            preview = json.load(urllib.request.urlopen(req))
            self.assertEqual(preview["files"][0]["path"], "index.html")
            bad = urllib.request.Request(base + "/api/publish", data=json.dumps({"stage_id": preview["stage_id"], "confirm_public": True}).encode(), method="POST", headers={
                "Origin": "https://elsewhere.example", "X-Publish-Session": publisher.CSRF,
                "Content-Type": "application/json",
            })
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(bad)
            self.assertEqual(caught.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            publisher.PORT = old_port
            publisher.STAGED.clear()


if __name__ == "__main__":
    unittest.main()
