#!/usr/bin/env node
/** Compile the renderer's bounded JSON component tree with no includes or fonts. */

import mjml2html from "mjml";
import { Parser } from "htmlparser2";

const MAX_BYTES = 2_000_000;
const ATTRS = new Map(Object.entries({
  mjml: "lang dir", "mj-head": "", "mj-title": "", "mj-preview": "",
  "mj-attributes": "", "mj-all": "font-family color",
  "mj-body": "width background-color css-class",
  "mj-section": "padding background-color border border-top border-bottom css-class",
  "mj-group": "width background-color direction vertical-align css-class",
  "mj-column": "width padding vertical-align css-class",
  "mj-text": "padding align color font-size font-weight line-height css-class",
  "mj-table": "padding width align css-class",
  "mj-divider": "padding border-width border-color css-class",
  "mj-style": "inline",
}).map(([tag, names]) => [tag, new Set(names.split(" ").filter(Boolean))]));
const HTML_ATTRS = new Map(Object.entries({
  a: "href style title", b: "", blockquote: "", br: "", code: "style",
  div: "class", em: "", h1: "class", h2: "class", h3: "class", h4: "", h5: "", h6: "", hr: "",
  li: "class id style", ol: "class start style", p: "class style", pre: "",
  small: "", span: "class style", strong: "", table: "class style",
  tbody: "", td: "class colspan rowspan width style", th: "class colspan rowspan style",
  thead: "", tr: "class", ul: "class style",
}).map(([tag, names]) => [tag, new Set(names.split(" ").filter(Boolean))]));

function validateHtml(content) {
  const parser = new Parser({
    oncomment() {
      throw new Error("unsafe HTML comment");
    },
    onopentag(tag, attrs) {
      const allowed = HTML_ATTRS.get(tag);
      if (!allowed) throw new Error(`unsafe HTML tag: ${tag}`);
      for (const [name, value] of Object.entries(attrs)) {
        if (!allowed.has(name) || (name === "href" && !/^https?:\/\//i.test(value))
            || (name === "style" && /[\\\x00-\x08\x0b\x0c\x0e-\x1f]|\/\*|@|(?:url|expression)\s*\(|(?:javascript|data|file):/i.test(value))) {
          throw new Error(`unsafe HTML attribute: ${tag}.${name}`);
        }
      }
    },
  }, { decodeEntities: true });
  parser.end(content);
}

function validate(node, depth = 0) {
  const allowed = node && !Array.isArray(node) && ATTRS.get(node.tagName);
  if (!allowed || depth > 24) throw new Error("invalid MJML tree");
  const attrs = node.attributes ?? {};
  if (!attrs || typeof attrs !== "object" || Array.isArray(attrs)) throw new Error("invalid MJML attributes");
  for (const [name, value] of Object.entries(attrs)) {
    const rendered = String(value);
    if (!allowed.has(name) || !["string", "number", "boolean"].includes(typeof value)
        || rendered.length > 500 || /[&";<>\\\x00-\x1f]|\/\*|@|(?:url|expression)\s*\(|(?:javascript|data|file):/i.test(rendered)
        || (name === "css-class" && !/^[\w-]+(?: [\w-]+)*$/.test(rendered))) {
      throw new Error(`invalid ${node.tagName} attribute`);
    }
  }
  const content = Object.hasOwn(node, "content");
  if (content === Object.hasOwn(node, "children")) throw new Error("invalid MJML node shape");
  if (content) {
    if (typeof node.content !== "string") throw new Error("invalid MJML content");
    if (node.tagName === "mj-style" && /[<\\\x00-\x08\x0b\x0c\x0e-\x1f]|\/\*|@|(?:url|expression)\s*\(/i.test(node.content)) {
      throw new Error("unsafe CSS");
    }
    if (node.tagName !== "mj-style") validateHtml(node.content);
  } else {
    if (!Array.isArray(node.children)) throw new Error("invalid MJML children");
    node.children.forEach((child) => validate(child, depth + 1));
  }
}

try {
  const chunks = [];
  let bytes = 0;
  for await (const chunk of process.stdin) {
    if ((bytes += chunk.length) > MAX_BYTES) throw new Error(`input exceeds ${MAX_BYTES} bytes`);
    chunks.push(chunk);
  }
  const document = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  validate(document);
  if (document.tagName !== "mjml") throw new Error("root tag must be mjml");
  const result = await mjml2html(document, {
    validationLevel: "strict", ignoreIncludes: true, useMjmlConfigOptions: false, fonts: {}, minify: true,
  });
  if (typeof result?.html !== "string" || result.errors?.length) {
    throw new Error(result?.errors?.map((error) => error.formattedMessage ?? error.message).join("; ") || "no HTML");
  }
  process.stdout.write(result.html);
} catch (error) {
  process.stderr.write(`mjml bridge: ${error instanceof Error ? error.message : error}\n`);
  process.exitCode = 1;
}
