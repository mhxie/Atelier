"""Offline feed collector checks: no real DNS, socket, service, or model calls."""
from __future__ import annotations

import json
from pathlib import Path
import unittest

import _node


ROOT = Path(__file__).resolve().parents[1]
PRELUDE = r'''
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { collect, LIMITS, parseFeedDocument, plainText, publicAddress, safeUrl, validateInput } from "./scripts/routine_feeds.mjs";
const item = (number = 1) => `<item><title>Article ${number}</title><link>https://example.com/article/${number}</link><description>&lt;p&gt;Hello &amp;amp; world&lt;/p&gt;</description><pubDate>Tue, 08 Sep 2026 08:00:00 GMT</pubDate></item>`;
const rss = (items = item()) => `<rss version="2.0"><channel><title>Example</title>${items}</channel></rss>`;
const input = (count = 1) => ({ schema: 1, feeds: Array.from({ length: count }, (_, i) => ({ id: `source_${i}`, url: `https://example.com/feed/${i}` })) });
const publicDNS = async () => ["93.184.216.34"];
function transport(replies, inspect = () => {}) {
  let index = 0;
  return (url, options, callback) => {
    const req = new EventEmitter();
    let response, timer, destroyed = false;
    req.destroy = () => { destroyed = true; clearTimeout(timer); response?.destroy(); };
    req.end = () => {
      inspect(url, options);
      const reply = typeof replies === "function" ? replies(url, index++) : replies[index++];
      if (reply?.hang) return;
      const respond = () => {
        if (destroyed) return;
        if (reply?.error) { req.emit("error", new Error(reply.error)); return; }
        response = new PassThrough();
        response.statusCode = reply?.statusCode ?? 200;
        response.headers = reply?.headers ?? {};
        callback(response);
        if (!response.destroyed) {
          for (const chunk of reply?.chunks ?? [reply?.body ?? rss()]) {
            if (response.destroyed) break;
            response.write(Buffer.from(chunk));
          }
          if (!reply?.bodyHang) response.end();
        }
      };
      if (reply?.delay) timer = setTimeout(respond, reply.delay);
      else queueMicrotask(respond);
    };
    return req;
  };
}
'''


class RoutineFeedsTest(unittest.TestCase):
    def node(self, script):
        result = _node.run(
            ["--input-type=module", "-e", PRELUDE + script],
            cwd=ROOT, env=_node.system_env(), timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_static_url_and_declaration_policy(self):
        self.node(r'''
for (const address of ["127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1", "192.168.1.1", "198.18.0.1", "192.0.2.1", "224.0.0.1", "255.255.255.255", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:93.184.216.34", "2001:db8::1", "2002::1", "3fff::1"]) assert.equal(publicAddress(address), false, address);
assert.equal(publicAddress("93.184.216.34"), true);
assert.equal(publicAddress("2606:4700:4700::1111"), true);
for (const url of ["file:///etc/passwd", "http://localhost/feed", "http://x.local/feed", "http://127.1/feed", "http://2130706433/feed", "http://0x7f000001/feed", "http://[::ffff:127.0.0.1]/feed", "http://example.com:8080/feed", "https://example.com:80/feed", "https://name:pass@example.com/feed", "https://example.com/feed#", "https://example.com/feed?api_key=secret", "https://example.com/feed?access_token=secret", "https://example.com/feed?X-Amz-Signature=secret", "https://example.com/feed?clientSecret=secret", "https://example.com/\\path"]) assert.throws(() => safeUrl(url), { code: "url_unsafe" }, url);
assert.equal(safeUrl("https://EXAMPLE.com.:443/feed?channel_id=abc&search_query=cat&start=0").hostname, "example.com");
for (const key of ["pass", "passphrase", "bearer", "jwt"]) assert.throws(() => safeUrl(`https://example.com/feed?user=alice&${key}=SECRET`), { code: "url_unsafe" });
for (const declaration of [{}, { schema: 1, feeds: [] }, { ...input(), extra: true }, { schema: 1, feeds: [{ id: "bad/id", url: "https://example.com/feed" }] }, { schema: 1, feeds: [{ ...input().feeds[0], extra: true }] }, input(41), { schema: 1, feeds: [input().feeds[0], input().feeds[0]] }, { schema: 1, feeds: [{ id: "a", url: "https://EXAMPLE.com:443/feed?a=1&b=2" }, { id: "b", url: "https://example.com./feed?b=2&a=1" }] }]) assert.throws(() => validateInput(declaration), { code: "input_invalid" });
assert.equal(validateInput(input(40)).length, 40);
''')

    def test_rss_atom_dates_and_plain_text(self):
        self.node(r'''
const entries = parseFeedDocument(Buffer.from(rss()), "https://example.com/feed");
assert.equal(entries[0].summary, "Hello & world");
assert.equal(entries[0].published_at, "2026-09-08T08:00:00.000Z");
const quotedHtml = rss().replace(/<description>.*?<\/description>/, '<description><![CDATA[<!DOCTYPE html><html><body><p>Quoted HTML summary</p></body></html>]]></description>');
assert.equal(parseFeedDocument(Buffer.from(quotedHtml), "https://example.com/feed")[0].summary, "Quoted HTML summary");
assert.equal(parseFeedDocument(Buffer.from('<!-- <!DOCTYPE html> -->' + rss()), "https://example.com/feed").length, 1);
const atom = `<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Example</title><link rel="self" href="https://example.com/entry.xml"/><link rel="alternate" type="text/html" href="/article"/><published>2026-09-08T08:00:00Z</published><updated>2026-09-09T08:00:00Z</updated><summary type="html">&lt;p&gt;First&lt;/p&gt;&lt;p&gt;Second&lt;/p&gt;&lt;script&gt;bad()&lt;/script&gt;</summary></entry></feed>`;
const [article] = parseFeedDocument(Buffer.from(atom), "https://example.com/feed");
assert.equal(article.url, "https://example.com/article");
assert.equal(article.published_at, "2026-09-08T08:00:00.000Z");
assert.equal(article.summary, "First Second");
const xhtml = '<div xmlns="http://www.w3.org/1999/xhtml"><p>First &amp; second.</p><p>Third.</p><script>hidden()</script><style>hidden{}</style></div>';
const xhtmlAtom = atom.replace('<title>Example</title>', `<title type="xhtml">${xhtml}</title>`).replace(/<summary.*?<\/summary>/, `<summary type="xhtml">${xhtml}</summary>`);
const [formatted] = parseFeedDocument(Buffer.from(xhtmlAtom), "https://example.com/feed");
assert.equal(formatted.title, "First & second. Third.");
assert.equal(formatted.summary, "First & second. Third.");
const contentAtom = xhtmlAtom.replaceAll("summary", "content");
assert.equal(parseFeedDocument(Buffer.from(contentAtom), "https://example.com/feed")[0].summary, formatted.summary);
const emptySummary = contentAtom.replace('<content type="xhtml">', '<summary type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"/></summary><content type="xhtml">');
assert.equal(parseFeedDocument(Buffer.from(emptySummary), "https://example.com/feed")[0].summary, formatted.summary);
assert.equal(parseFeedDocument(Buffer.from(atom.replace('rel="alternate"', 'rel="enclosure"')), "https://example.com/feed").length, 0);
assert.equal(parseFeedDocument(Buffer.from(atom.replace('rel="alternate"', '')), "https://example.com/feed").length, 1);
for (const date of ["not-a-date", "2026-02-31", "123", "2026-09-08T12:30:00", "Fri, 31 Feb 2026 08:00:00 GMT", "Tue, 08 Sep 2026 08:00:00", "Tue, 08 Sep 2026 08:00:00 +9999", "Tue, 08 Sep 2026 08:00:00 +0060"]) {
  const changed = rss().replace("Tue, 08 Sep 2026 08:00:00 GMT", date);
  assert.equal(parseFeedDocument(Buffer.from(changed), "https://example.com/feed")[0].published_at, null);
}
assert.equal(plainText("A<br>B<p>C &amp; D</p><style>bad</style><script>bad</script>\u0001E", 700), "A B C & D E");
assert.equal(plainText("😀".repeat(800), 240).length, 480);
''')

    def test_parser_rejects_nonfeeds_and_caps_items(self):
        self.node(r'''
for (const body of ["", "<html><rss><channel/></rss></html>", "<rss/>", "<rss><channel><item></rss>", "<rss><channel>", "<feed/><feed/>", "<rss><channel></unexpected></channel></rss>", "</unexpected><feed/>", '<!DOCTYPE feed [<!ENTITY x SYSTEM "https://example.com/entity">]><feed/>', `<feed>${"<x>".repeat(65)}${"</x>".repeat(65)}</feed>`]) assert.throws(() => parseFeedDocument(Buffer.from(body), "https://example.com/feed"), { code: "feed_invalid" });
assert.throws(() => parseFeedDocument(Buffer.alloc(LIMITS.feedBytes + 1), "https://example.com/feed"), { code: "feed_bytes" });
assert.throws(() => parseFeedDocument(Buffer.from([255, 255]), "https://example.com/feed"), { code: "feed_invalid" });
let items = Array.from({ length: 25 }, (_, i) => item(i).replace(`Article ${i}`, "T".repeat(500)).replace("Hello &amp;amp; world", "S".repeat(1000))).join("");
const parsed = parseFeedDocument(Buffer.from(rss(items)), "https://example.com/feed");
assert.equal(parsed.length, 12);
assert.ok(parsed.every((entry) => entry.title.length === 240 && entry.summary.length === 700 && entry.url.length <= 2048));
for (const link of ["javascript:alert(1)", "http://127.0.0.1/path", "https://example.com/article?token=secret", `https://example.com/${"a".repeat(2050)}`, ""]) {
  assert.equal(parseFeedDocument(Buffer.from(rss().replace("https://example.com/article/1", link)), "https://example.com/feed").length, 0);
}
assert.equal(parseFeedDocument(Buffer.from(rss(item() + item())), "https://example.com/feed").length, 1);
''')

    def test_dns_all_answers_and_pinned_connection(self):
        self.node(r'''
let calls = 0, resolutions = 0;
const result = await collect(input(), { resolve: async () => { resolutions++; return ["93.184.216.34", "2606:4700:4700::1111"]; }, request: transport([{}], (url, options) => {
  calls++;
  assert.equal(url.hostname, "example.com");
  assert.equal(options.agent, false);
  assert.equal(options.autoSelectFamily, false);
  assert.equal(options.headers["accept-encoding"], "identity");
  assert.equal(options.headers.authorization, undefined);
  assert.equal(options.headers.cookie, undefined);
  options.lookup("example.com", {}, (error, address, family) => { assert.equal(error, null); assert.equal(address, "93.184.216.34"); assert.equal(family, 4); });
  options.lookup("example.com", { all: true }, (error, addresses) => assert.deepEqual(addresses, [{ address: "93.184.216.34", family: 4 }]));
}) });
assert.equal(result.status, "ok");
assert.equal(calls, 1); assert.equal(resolutions, 1);
for (const answers of [["93.184.216.34", "127.0.0.1"], ["93.184.216.34", "::ffff:10.0.0.1"], ["not-an-ip"]]) {
  const denied = await collect(input(), { resolve: async () => answers, request: () => assert.fail("unsafe DNS reached HTTP") });
  assert.equal(denied.feeds[0].error.code, "address_unsafe");
  assert.equal(denied.counts.reached, 0);
}
const unresolved = await collect(input(), { resolve: async () => { throw new Error("SECRET https://example.com/private"); }, request: () => assert.fail() });
assert.equal(unresolved.feeds[0].error.code, "dns_failed");
assert.ok(!JSON.stringify(unresolved).includes("SECRET"));
''')

    def test_redirects_revalidate_and_do_not_rebind(self):
        self.node(r'''
let calls = 0, resolutions = 0;
const rebound = await collect(input(), { resolve: async () => ++resolutions === 1 ? ["93.184.216.34"] : ["127.0.0.1"], request: transport([{ statusCode: 302, headers: { location: "/second" } }], () => calls++) });
assert.equal(rebound.feeds[0].error.code, "address_unsafe");
assert.equal(calls, 1); assert.equal(resolutions, 2);
for (const [location, code] of [["http://example.com/insecure", "redirect_downgrade"], ["http://127.0.0.1/private", "redirect_invalid"], ["https://example.com/?token=secret", "redirect_invalid"], [undefined, "redirect_invalid"]]) {
  const result = await collect(input(), { resolve: publicDNS, request: transport([{ statusCode: 302, headers: { location } }]) });
  assert.equal(result.feeds[0].error.code, code);
}
let requested = 0;
const loop = await collect(input(), { resolve: publicDNS, request: transport(() => ({ statusCode: 302, headers: { location: "/loop" } }), () => requested++) });
assert.equal(requested, 4); assert.equal(loop.feeds[0].error.code, "redirect_limit");
const okay = await collect(input(), { resolve: publicDNS, request: transport([{ statusCode: 302, headers: { location: "/next" } }, { body: rss() }]) });
assert.equal(okay.status, "ok"); assert.equal(okay.counts.reached, 1);
''')

    def test_partial_all_failure_and_output_privacy(self):
        self.node(r'''
const result = await collect(input(6), { resolve: publicDNS, request: transport([{ body: rss() }, { statusCode: 403 }, { body: rss("") }, { body: "<html>not a feed</html>" }, { error: "SECRET https://example.com/private" }, { headers: { "content-encoding": "gzip" } }]) });
assert.deepEqual(result.counts, { declared: 6, attempted: 6, reached: 5, usable: 1, items: 1 });
assert.equal(result.status, "degraded");
assert.equal(result.gaps.length, 5);
assert.deepEqual(result.feeds.map((feed) => feed.status), ["ok", "failed", "empty", "failed", "failed", "failed"]);
assert.deepEqual(result.gaps.map((gap) => gap.code), ["http_status", "empty", "feed_invalid", "request_failed", "encoding_unsupported"]);
assert.equal(result.summary_provenance, "feed content, not article full text");
assert.ok(!JSON.stringify(result).includes("SECRET"));
assert.ok(!JSON.stringify(result).includes("/feed/"));
const failed = await collect(input(2), { resolve: publicDNS, request: transport([{ statusCode: 500 }, { statusCode: 503 }]) });
assert.equal(failed.status, "degraded"); assert.equal(failed.counts.usable, 0); assert.equal(failed.gaps.length, 2);
''')

    def test_deadlines_cover_dns_request_body_and_queue(self):
        self.node(r'''
for (const dependencies of [
  { resolve: () => new Promise(() => {}), request: () => assert.fail("DNS did not complete") },
  { resolve: publicDNS, request: transport([{ hang: true }]) },
  { resolve: publicDNS, request: transport([{ bodyHang: true }]) },
]) {
  const start = performance.now();
  const result = await collect(input(), { ...dependencies, limits: { feedMs: 10 } });
  assert.equal(result.feeds[0].error.code, "feed_timeout");
  assert.ok(performance.now() - start < 1000);
}
const whole = await collect(input(20), { resolve: () => new Promise(() => {}), request: () => assert.fail(), limits: { collectionMs: 10 } });
assert.equal(whole.counts.attempted, 8);
assert.equal(whole.feeds.filter((feed) => feed.status === "skipped").length, 12);
assert.ok(whole.gaps.every((gap) => gap.code === "collection_timeout"));
''')

    def test_byte_concurrency_and_total_item_budgets(self):
        self.node(r'''
for (const headers of [{}, { "content-length": "1" }, { "content-length": "invalid" }]) {
  const tooLarge = await collect(input(), { resolve: publicDNS, request: transport([{ headers, chunks: ["x".repeat(80), "x".repeat(80)] }]), limits: { feedBytes: 100 } });
  assert.equal(tooLarge.feeds[0].error.code, "feed_bytes"); assert.equal(tooLarge.feeds[0].bytes, 100);
}
const feedSize = Buffer.byteLength(rss());
for (const contentLength of [String(feedSize + 1), "9".repeat(500)]) {
  const advertised = await collect(input(2), { resolve: publicDNS, request: transport([
    { headers: { "content-length": contentLength }, body: "x".repeat(feedSize + 1) },
    { headers: { "content-length": String(feedSize) }, body: rss() },
  ]), limits: { concurrency: 1, feedBytes: feedSize, totalBytes: feedSize + 1 } });
  assert.equal(advertised.feeds[0].error.code, "feed_bytes");
  assert.equal(advertised.feeds[0].bytes, 0);
  assert.equal(advertised.feeds[1].status, "ok");
  assert.deepEqual(advertised.counts, { declared: 2, attempted: 2, reached: 2, usable: 1, items: 1 });
}
const aggregate = await collect(input(4), { resolve: publicDNS, request: transport(() => ({ body: rss() })), limits: { concurrency: 1, totalBytes: 100 } });
assert.equal(aggregate.counts.attempted, 1);
assert.deepEqual(aggregate.feeds.map((feed) => feed.status), ["failed", "skipped", "skipped", "skipped"]);
assert.ok(aggregate.gaps.every((gap) => gap.code === "total_bytes"));
assert.equal(aggregate.feeds.reduce((sum, feed) => sum + feed.bytes, 0), 100);
let active = 0, peak = 0;
const bounded = await collect(input(40), { resolve: async () => { peak = Math.max(peak, ++active); await new Promise((resolve) => setTimeout(resolve, 1)); active--; return ["93.184.216.34"]; }, request: transport(() => ({ body: rss(Array.from({ length: 20 }, (_, i) => item(i)).join("")) })) });
assert.ok(peak <= 8); assert.equal(bounded.counts.items, 480); assert.equal(bounded.counts.usable, 40);
await assert.rejects(collect(input(), { limits: { concurrency: 9 } }), { code: "input_invalid" });
''')

    def test_validate_cli_never_needs_network(self):
        declaration = {"schema": 1, "feeds": [{"id": "example", "url": "https://example.com/feed"}]}
        result = _node.run(
            [ROOT / "scripts/routine_feeds.mjs", "--validate"], cwd=ROOT,
            env=_node.system_env(), timeout=5, input=json.dumps(declaration),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), {"schema": 1, "valid": True, "feeds": 1})
        for raw in ("not JSON SECRET", json.dumps({**declaration, "private": "SECRET"}), "x" * 131_073):
            result = _node.run(
                [ROOT / "scripts/routine_feeds.mjs", "--validate"], cwd=ROOT,
                env=_node.system_env(), timeout=5, input=raw,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stderr, "")
            self.assertNotIn("SECRET", result.stdout)
            self.assertEqual(json.loads(result.stdout)["error"]["code"], "input_invalid")

    def test_validate_cli_rejects_unsupported_node_before_collecting(self):
        script = (
            'Object.defineProperty(process.versions, "node", {value: "20.0.0"});'
            f'process.argv = [process.execPath, {json.dumps(str(ROOT / "scripts/routine_feeds.mjs"))}, "--validate"];'
            'await import("./scripts/routine_feeds.mjs");'
        )
        result = _node.run(["--input-type=module", "-e", script], cwd=ROOT,
                           env=_node.system_env(), timeout=5,
                           input=json.dumps({"schema": 1, "feeds": [{"id": "example", "url": "https://example.com/feed"}]}))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "runtime_unsupported")


if __name__ == "__main__":
    unittest.main()
