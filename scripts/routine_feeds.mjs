#!/usr/bin/env node
/** Bounded RSS/Atom input. Feed text is untrusted data, never instructions. */
import { Resolver } from "node:dns/promises";
import http from "node:http";
import https from "node:https";
import { BlockList, isIP } from "node:net";
import { pathToFileURL } from "node:url";
import { DomHandler, DomUtils, getFeed, Parser } from "htmlparser2";

export const LIMITS = Object.freeze({
  collectionMs: 20_000, feedMs: 6_000, concurrency: 8, redirects: 3,
  feedBytes: 1_048_576, totalBytes: 8_388_608, items: 12,
});
const MESSAGES = Object.freeze({
  input_invalid: "Invalid feed declaration.", url_unsafe: "URL is outside the allowed policy.",
  runtime_unsupported: "Feed collection requires Node 22 or newer.",
  dns_failed: "Source name could not be resolved.", address_unsafe: "Source resolved outside the public address policy.",
  request_failed: "Source request failed.", http_status: "Source returned an unsuccessful HTTP status.",
  redirect_invalid: "Source returned an invalid redirect.", redirect_limit: "Source exceeded the redirect limit.",
  redirect_downgrade: "Source redirected from HTTPS to HTTP.", encoding_unsupported: "Source response was compressed.",
  feed_timeout: "Source exceeded its time budget.", collection_timeout: "Collection time budget exhausted.",
  feed_bytes: "Source exceeded its byte budget.", total_bytes: "Collection byte budget exhausted.",
  feed_invalid: "Source was not a supported feed.", empty: "Source contained no usable article entries.",
  collector_failed: "Feed collector failed.",
});
class FeedError extends Error {
  constructor(code) { super(MESSAGES[code]); this.code = code; }
}
const failure = (code) => new FeedError(code);
const errorData = (error) => {
  const code = error instanceof FeedError ? error.code : "collector_failed";
  return { code, message: MESSAGES[code] };
};
const blocked = new BlockList();
for (const [address, prefix] of [
  ["0.0.0.0", 8], ["10.0.0.0", 8], ["100.64.0.0", 10], ["127.0.0.0", 8],
  ["169.254.0.0", 16], ["172.16.0.0", 12], ["192.0.0.0", 24], ["192.0.2.0", 24],
  ["192.31.196.0", 24], ["192.52.193.0", 24], ["192.88.99.0", 24], ["192.168.0.0", 16],
  ["192.175.48.0", 24], ["198.18.0.0", 15], ["198.51.100.0", 24], ["203.0.113.0", 24],
  ["224.0.0.0", 4], ["240.0.0.0", 4],
]) blocked.addSubnet(address, prefix, "ipv4");
for (const [address, prefix] of [
  ["2001::", 23], ["2001:db8::", 32], ["2002::", 16], ["2620:4f:8000::", 48], ["3fff::", 20],
]) blocked.addSubnet(address, prefix, "ipv6");
const globalV6 = new BlockList();
globalV6.addSubnet("2000::", 3, "ipv6");
export function publicAddress(address) {
  const family = isIP(address);
  return family === 4 ? !blocked.check(address, "ipv4")
    : family === 6 && globalV6.check(address, "ipv6") && !blocked.check(address, "ipv6");
}
function credentialKey(key) {
  const name = key.toLowerCase().replace(/[^a-z0-9]/g, "");
  return /token|password|passwd|secret|credential|signature|session|cookie/.test(name)
    || /^(?:key|apikey|accesskey|privatekey|auth|authorization|authentication|authkey|authcode|sig|pass|passphrase|pwd|bearer|jwt)$/.test(name)
    || /^(?:xamz|xgoog|oauth)/.test(name);
}
export function safeUrl(value, base) {
  if (typeof value !== "string" || !value || value.length > 2048 || /[\s\x00-\x1f\x7f\\#]/.test(value)) {
    throw failure("url_unsafe");
  }
  let url;
  try { url = new URL(value, base); } catch { throw failure("url_unsafe"); }
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.port || url.hash) {
    throw failure("url_unsafe");
  }
  let hostname = url.hostname.replace(/^\[|\]$/g, "").replace(/\.$/, "");
  if (isIP(hostname)) {
    if (!publicAddress(hostname)) throw failure("url_unsafe");
  } else if (hostname.length > 253 || !hostname.includes(".")
      || !hostname.split(".").every((label) => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label))
      || /(?:^|\.)(?:localhost|local|internal|invalid|test)$|(?:^|\.)home\.arpa$/.test(hostname)) {
    throw failure("url_unsafe");
  }
  url.hostname = isIP(hostname) === 6 ? `[${hostname}]` : hostname;
  for (const key of url.searchParams.keys()) if (credentialKey(key)) throw failure("url_unsafe");
  if (url.href.length > 2048) throw failure("url_unsafe");
  return url;
}
function exactObject(value, keys) {
  return value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));
}
export function validateInput(value) {
  if (!exactObject(value, ["schema", "feeds"]) || value.schema !== 1 || !Array.isArray(value.feeds)
      || value.feeds.length < 1 || value.feeds.length > 40) throw failure("input_invalid");
  const ids = new Set(), urls = new Set();
  return value.feeds.map((feed) => {
    if (!exactObject(feed, ["id", "url"]) || typeof feed.id !== "string"
        || !/^[a-z0-9][a-z0-9_-]{0,63}$/.test(feed.id) || ids.has(feed.id)) throw failure("input_invalid");
    const url = safeUrl(feed.url);
    const canonical = new URL(url);
    canonical.searchParams.sort();
    if (urls.has(canonical.href)) throw failure("input_invalid");
    ids.add(feed.id); urls.add(canonical.href);
    return { id: feed.id, url };
  });
}
function abortable(promise, signal) {
  if (signal.aborted) return Promise.reject(signal.reason);
  return new Promise((resolve, reject) => {
    const abort = () => reject(signal.reason);
    signal.addEventListener("abort", abort, { once: true });
    promise.then(resolve, reject).finally(() => signal.removeEventListener("abort", abort));
  });
}
async function resolveHost(hostname, signal) {
  const resolver = new Resolver({ timeout: 1500, tries: 1 });
  const cancel = () => resolver.cancel();
  signal.addEventListener("abort", cancel, { once: true });
  try {
    const groups = await Promise.all(["resolve4", "resolve6"].map(async (method) => {
      try { return await resolver[method](hostname); }
      catch (error) {
        if (["ENODATA", "ENOTFOUND"].includes(error.code)) return [];
        throw failure("dns_failed");
      }
    }));
    return groups.flat();
  } finally { signal.removeEventListener("abort", cancel); }
}
async function pinAddress(url, signal, resolve) {
  const hostname = url.hostname.replace(/^\[|\]$/g, "");
  let answers;
  try { answers = isIP(hostname) ? [hostname] : await abortable(Promise.resolve(resolve(hostname, signal)), signal); }
  catch (error) { throw signal.aborted ? signal.reason : error instanceof FeedError ? error : failure("dns_failed"); }
  if (!Array.isArray(answers) || !answers.length) throw failure("dns_failed");
  if (!answers.every((address) => typeof address === "string" && publicAddress(address))) throw failure("address_unsafe");
  return { address: answers[0], family: isIP(answers[0]) };
}
function requestOnce(url, pin, signal, maxBytes, takeBytes, request) {
  return new Promise((resolve, reject) => {
    let req, response, settled = false;
    const finish = (error, result) => {
      if (settled) return;
      settled = true;
      signal.removeEventListener("abort", abort);
      response?.destroy(); req?.destroy();
      error ? reject(error) : resolve(result);
    };
    const abort = () => finish(signal.reason);
    if (signal.aborted) { reject(signal.reason); return; }
    signal.addEventListener("abort", abort, { once: true });
    try {
      req = request(url, {
        agent: false, autoSelectFamily: false, maxHeaderSize: 16_384,
        headers: { accept: "application/atom+xml, application/rss+xml, application/xml, text/xml", "accept-encoding": "identity", "user-agent": "AtelierFeedCollector/1" },
        lookup(_hostname, options, callback) {
          if (options?.all) callback(null, [pin]);
          else callback(null, pin.address, pin.family);
        },
      }, (res) => {
        response = res;
        if (settled) { res.destroy(); return; }
        res.on("error", () => finish(failure("request_failed")));
        const status = res.statusCode;
        if ([301, 302, 303, 307, 308].includes(status)) {
          finish(null, { location: res.headers.location }); return;
        }
        if (status < 200 || status >= 300) { finish(failure("http_status")); return; }
        if (res.headers["content-encoding"] && res.headers["content-encoding"].trim().toLowerCase() !== "identity") {
          finish(failure("encoding_unsupported")); return;
        }
        const declaredLength = res.headers["content-length"];
        if (typeof declaredLength === "string" && /^\d+$/.test(declaredLength) && Number(declaredLength) > maxBytes) {
          finish(failure("feed_bytes")); return;
        }
        const chunks = [];
        res.on("data", (chunk) => {
          if (settled) return;
          try { takeBytes(chunk.length); chunks.push(chunk); }
          catch (error) { finish(error); }
        });
        res.on("aborted", () => finish(failure("request_failed")));
        res.on("end", () => finish(null, { body: Buffer.concat(chunks) }));
      });
      req.on("error", () => finish(signal.aborted ? signal.reason : failure("request_failed")));
      req.end();
    } catch { finish(failure("request_failed")); }
  });
}
export function plainText(value, limit) {
  let text = "", hidden = 0;
  new Parser({
    onopentag(name) {
      if (["script", "style", "template"].includes(name)) hidden++;
      if (!hidden && /^(?:br|p|div|li|h[1-6]|blockquote|tr)$/.test(name)) text += " ";
    },
    onclosetag(name) {
      if (["script", "style", "template"].includes(name) && hidden) hidden--;
      if (!hidden && /^(?:p|div|li|h[1-6]|blockquote|tr)$/.test(name)) text += " ";
    },
    ontext(part) { if (!hidden) text += part; },
  }, { decodeEntities: true }).end(String(value ?? ""));
  return Array.from(text.replace(/[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]/g, " ").replace(/\s+/g, " ").trim()).slice(0, limit).join("");
}
function isoDate(value) {
  if (!value) return null;
  const raw = value.trim(), match = /^(\d{4}-\d{2}-\d{2})(?:[Tt].*(?:[Zz]|[+-]\d{2}:?\d{2}))?$/.exec(raw);
  let dayText = match?.[1];
  if (!match) {
    const rfc = /^(?:[A-Za-z]{3},?\s+)?(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})\s+(\d{2}):(\d{2})(?::(\d{2}))?\s+(GMT|UT|UTC|[+-]\d{4}|[ECMP][SD]T)$/i.exec(raw);
    if (!rfc || Number(rfc[4]) > 23 || Number(rfc[5]) > 59 || Number(rfc[6] ?? 0) > 59) return null;
    const offset = /^[+-](\d{2})(\d{2})$/.exec(rfc[7]);
    if (offset && (Number(offset[1]) > 23 || Number(offset[2]) > 59)) return null;
    const month = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"].indexOf(rfc[2].toLowerCase()) + 1;
    dayText = `${rfc[3]}-${String(month).padStart(2, "0")}-${rfc[1].padStart(2, "0")}`;
  }
  const day = new Date(`${dayText}T00:00:00Z`);
  if (!Number.isFinite(day.getTime()) || day.toISOString().slice(0, 10) !== dayText) return null;
  const date = new Date(raw);
  return Number.isFinite(date.getTime()) ? date.toISOString() : null;
}
export function parseFeedDocument(body, base, maxItems = LIMITS.items) {
  try {
    if (body.length > LIMITS.feedBytes) throw failure("feed_bytes");
    const source = new TextDecoder("utf-8", { fatal: true }).decode(body);
    const handler = new DomHandler();
    const instruction = handler.onprocessinginstruction.bind(handler);
    handler.onprocessinginstruction = (name, data) => {
      if (name.startsWith("!")) throw failure("feed_invalid");
      instruction(name, data);
    };
    const open = handler.onopentag.bind(handler), close = handler.onclosetag.bind(handler);
    let depth = 0, nodes = 0;
    const parser = new Parser(handler, { xmlMode: true, decodeEntities: true });
    // The pinned parser otherwise hides unmatched closing tokens from handlers.
    const closeToken = parser.onclosetag.bind(parser);
    parser.onclosetag = (start, end) => {
      if (source.slice(start, end) !== parser.stack[0]) throw failure("feed_invalid");
      closeToken(start, end);
    };
    handler.onopentag = (name, attributes) => {
      if (++depth > 64 || ++nodes > 50_000) throw failure("feed_invalid");
      open(name, attributes);
    };
    handler.onclosetag = (name, implied) => {
      if (implied && !source.slice(parser.startIndex, parser.endIndex + 1).endsWith("/>")) throw failure("feed_invalid");
      depth--; close(name);
    };
    parser.end(source);
    const roots = handler.dom.filter((node) => node.type === "tag");
    if (roots.length !== 1 || handler.dom.some((node) => node.type === "text" && node.data.trim())) throw failure("feed_invalid");
    const root = roots[0];
    if (!["rss", "feed", "rdf:RDF"].includes(root.name)
        || (root.name === "rss" && !root.children.some((node) => node.name === "channel"))) throw failure("feed_invalid");
    const feed = getFeed([root]);
    if (!feed) throw failure("feed_invalid");
    const elements = DomUtils.getElementsByTagName(feed.type === "atom" ? "entry" : "item", root.children);
    const text = (node, tag) => {
      const child = node.children.find((entry) => entry.name === tag);
      return (child?.attribs.type === "xhtml" ? DomUtils.getInnerHTML(child) : DomUtils.textContent(child ?? [])).trim();
    };
    const result = [], seen = new Set();
    for (const [index, item] of feed.items.entries()) {
      const node = elements[index];
      let link = item.link;
      if (feed.type === "atom") {
        link = node.children.find((child) => child.name === "link" && (!child.attribs.rel || child.attribs.rel === "alternate")
          && (!child.attribs.type || /^(?:text\/html|application\/xhtml\+xml)$/i.test(child.attribs.type)))?.attribs.href;
      }
      let url;
      try { url = safeUrl(link, base).href; } catch { continue; }
      if (seen.has(url)) continue;
      seen.add(url);
      result.push({
        title: plainText(text(node, "title"), 240), url,
        published_at: isoDate(feed.type === "atom" ? text(node, "published") || text(node, "updated") : text(node, "pubDate") || text(node, "dc:date")),
        summary: plainText(text(node, feed.type === "atom" ? "summary" : "description"), 700) || plainText(text(node, feed.type === "atom" ? "content" : "content:encoded"), 700),
      });
      if (result.length >= Math.min(maxItems, LIMITS.items)) break;
    }
    return result;
  } catch (error) { throw error instanceof FeedError ? error : failure("feed_invalid"); }
}
/** Dependencies are injectable for offline transports; embedded callers may only tighten limits. */
export async function collect(input, dependencies = {}) {
  const declared = validateInput(input), limits = { ...LIMITS, ...dependencies.limits };
  for (const [key, value] of Object.entries(limits)) {
    if (!Object.hasOwn(LIMITS, key) || !Number.isSafeInteger(value) || value < 1 || value > LIMITS[key]) throw failure("input_invalid");
  }
  const resolve = dependencies.resolve ?? resolveHost;
  const request = dependencies.request ?? ((url, options, callback) => (url.protocol === "https:" ? https : http).request(url, options, callback));
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(failure("collection_timeout")), limits.collectionMs);
  let next = 0, totalBytes = 0;
  const feeds = new Array(declared.length), counts = { declared: declared.length, attempted: 0, reached: 0, usable: 0, items: 0 };
  async function worker() {
    while (next < declared.length) {
      const index = next++, source = declared[index];
      const row = { id: source.id, status: "skipped", bytes: 0, items: [] };
      feeds[index] = row;
      if (controller.signal.aborted || totalBytes >= limits.totalBytes) {
        row.error = errorData(controller.signal.reason ?? failure("total_bytes")); continue;
      }
      counts.attempted++;
      const perFeed = new AbortController();
      const signal = AbortSignal.any([controller.signal, perFeed.signal]);
      const feedTimer = setTimeout(() => perFeed.abort(failure("feed_timeout")), limits.feedMs);
      let reached = false;
      try {
        let url = source.url;
        for (let redirects = 0; ; redirects++) {
          const pin = await pinAddress(url, signal, resolve);
          const reply = await requestOnce(url, pin, signal, limits.feedBytes - row.bytes, (length) => {
            const accepted = Math.min(length, limits.feedBytes - row.bytes, limits.totalBytes - totalBytes);
            row.bytes += accepted; totalBytes += accepted;
            if (accepted < length) {
              if (totalBytes >= limits.totalBytes) controller.abort(failure("total_bytes"));
              throw failure(totalBytes >= limits.totalBytes ? "total_bytes" : "feed_bytes");
            }
          }, (target, options, callback) => request(target, options, (response) => {
            if (!reached) { reached = true; counts.reached++; }
            callback(response);
          }));
          if (Object.hasOwn(reply, "body")) {
            row.items = parseFeedDocument(reply.body, url, limits.items);
            row.status = row.items.length ? "ok" : "empty";
            if (!row.items.length) row.error = errorData(failure("empty"));
            break;
          }
          if (redirects >= limits.redirects) throw failure("redirect_limit");
          let redirected;
          try { redirected = safeUrl(reply.location, url); } catch { throw failure("redirect_invalid"); }
          if (url.protocol === "https:" && redirected.protocol !== "https:") throw failure("redirect_downgrade");
          url = redirected;
        }
      } catch (error) {
        row.status = "failed";
        row.error = errorData(error);
      } finally { clearTimeout(feedTimer); }
    }
  }
  try { await Promise.all(Array.from({ length: Math.min(limits.concurrency, declared.length) }, worker)); }
  finally { clearTimeout(timer); }
  counts.usable = feeds.filter((feed) => feed.items.length).length;
  counts.items = feeds.reduce((sum, feed) => sum + feed.items.length, 0);
  const gaps = feeds.filter((feed) => feed.error).map((feed) => ({ source: feed.id, ...feed.error }));
  return { schema: 1, collected_at: new Date().toISOString(), status: gaps.length ? "degraded" : "ok", counts, feeds, gaps,
    summary_provenance: "feed content, not article full text" };
}
async function main() {
  try {
    if (Number(process.versions.node.split(".")[0]) < 22) throw failure("runtime_unsupported");
    if (process.argv.length > 3 || (process.argv[2] && process.argv[2] !== "--validate")) throw failure("input_invalid");
    const chunks = []; let bytes = 0;
    for await (const chunk of process.stdin) {
      if ((bytes += chunk.length) > 131_072) throw failure("input_invalid");
      chunks.push(chunk);
    }
    let input;
    try { input = JSON.parse(Buffer.concat(chunks).toString("utf8")); } catch { throw failure("input_invalid"); }
    const result = process.argv[2] === "--validate" ? { schema: 1, valid: true, feeds: validateInput(input).length } : await collect(input);
    process.stdout.write(`${JSON.stringify(result)}\n`);
  } catch (error) {
    process.stdout.write(`${JSON.stringify({ schema: 1, status: "invalid", error: errorData(error) })}\n`);
    process.exitCode = 2;
  }
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();
