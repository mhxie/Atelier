// One JSON request on stdin, one JSON response on stdout. No vault writes.
import { readFileSync, openSync, readSync, closeSync, statSync } from "node:fs";
import { createStore, extractSnippet } from "@tobilu/qmd";
import { resolveModelFile } from "node-llama-cpp";
import { pathToFileURL } from "node:url";

// QMD 2.8.3's SDK tokenizer uses the global instance, unlike embedding/search.
// Share the per-store instance, as QMD's CLI does, to avoid a second model/runtime.
const { setDefaultLlamaCpp } = await import(new URL("llm.js", import.meta.resolve("@tobilu/qmd")));
// Its public embed wrapper drops maxDurationMs; use the pinned store API.
const { generateEmbeddings } = await import(new URL("store.js", import.meta.resolve("@tobilu/qmd")));

// The short-lived official CLI uses the same Metal teardown mitigation.
if (process.platform === "darwin") process.env.GGML_METAL_NO_RESIDENCY ??= "1";
console.log = (...args) => console.error(...args);

export function guardEmbeddings(llm) {
  // QMD catches inference errors and returns null; never turn failure into [].
  for (const method of ["embed", "embedBatch"]) {
    const original = llm[method].bind(llm);
    llm[method] = async (...args) => {
      const result = await original(...args);
      const vectors = method === "embed" ? [result] : result;
      if (!Array.isArray(vectors) || vectors.length !== (method === "embed" ? 1 : args[0].length)
          || vectors.some(row => !Array.isArray(row?.embedding) || !row.embedding.length
            || !row.embedding.every(Number.isFinite))) {
        throw new Error(`QMD ${method} returned incomplete or invalid embeddings`);
      }
      return result;
    };
  }
}

async function main() {
  let store;
  try {
    const request = JSON.parse(readFileSync(0, "utf8"));
    const missing = {};
    for (const [role, uri] of Object.entries(request.config.models)) {
      try {
        const path = await resolveModelFile(uri, { directory: request.modelDirectory, download: false, cli: false });
        const file = openSync(path, "r");
        try {
          const magic = Buffer.alloc(4);
          if (statSync(path).size < 24 || readSync(file, magic, 0, 4, 0) !== 4 || magic.toString() !== "GGUF") {
            throw new Error("not a GGUF file");
          }
        } finally { closeSync(file); }
        request.config.models[role] = path;
      } catch (error) { missing[role] = error.message; }
    }
    for (const role of request.roles) {
      if (missing[role]) throw new Error(`local QMD ${role} model missing or invalid: ${missing[role]}; run semantic.py init --download-models`);
    }
    store = await createStore({ dbPath: request.database, config: request.config });
    const llm = store.internal.llm;
    setDefaultLlamaCpp(llm);
    guardEmbeddings(llm);
    let result;
    if (request.command === "index") {
      if (!request.lexicalOnly) await llm.ensureLlama(false); // Packaged backend only: no build/download fallback.
      const update = await store.update();
      if (update.skipped) throw new Error(`QMD skipped ${update.skipped} unreadable files; index is incomplete`);
      const embed = request.lexicalOnly ? null : await generateEmbeddings(store.internal, {
        maxDocsPerBatch: request.runtime.max_docs_per_batch,
        maxBatchBytes: request.runtime.max_batch_mb * 1024 * 1024,
        maxDurationMs: request.runtime.index_timeout_seconds * 1000,
      });
      if (embed?.errors) throw new Error(`QMD failed to embed ${embed.errors} chunks`);
      result = { update, embed, status: await store.getStatus() };
      if (!request.lexicalOnly && result.status.needsEmbedding) {
        throw new Error(`QMD still has ${result.status.needsEmbedding} documents awaiting embeddings`);
      }
    } else if (request.command === "status") {
      result = { ...await store.getStatus(), models_ready: !missing.embed && !missing.rerank, missing_models: missing };
    } else if (request.command === "query") {
      const status = await store.getStatus();
      if (!status.totalDocuments) throw new Error("QMD index has no searchable documents; run semantic.py index");
      if (request.mode !== "lexical" && (!status.hasVectorIndex || status.needsEmbedding)) {
        throw new Error("QMD embeddings are incomplete; run semantic.py index (or explicitly select --mode lexical)");
      }
      if (request.mode !== "lexical") await llm.ensureLlama(false);
      if (request.mode === "hybrid" && request.rerank && !(await llm.ensureRerankContexts()).length) {
        throw new Error("QMD reranker is unavailable; retry with explicit --no-rerank");
      }
      const options = { collection: request.collections, limit: request.limit };
      let rows;
      if (request.mode === "lexical") rows = await store.searchLex(request.query, options);
      else if (request.mode === "vector") rows = await store.searchVector(request.query, options);
      else {
        rows = await store.search({
          ...(request.expand ? { query: request.query } : {
            queries: [{ type: "lex", query: request.query }, { type: "vec", query: request.query }],
          }),
          collections: request.collections,
          limit: request.limit,
          candidateLimit: request.limit,
          rerank: request.rerank,
        });
      }
      result = rows.map(row => {
        const split = row.displayPath.indexOf("/");
        if (split < 1) throw new Error("QMD returned an invalid document identifier");
        const excerpt = extractSnippet(row.body ?? "", request.query, 500, row.bestChunkPos ?? row.chunkPos);
        return {
          path: row.displayPath.slice(split + 1),
          scope: row.displayPath.slice(0, split),
          title: row.title,
          score: row.score,
          line: excerpt.line,
          snippet: excerpt.snippet.slice(0, 600),
        };
      });
    } else throw new Error(`Unknown QMD operation: ${request.command}`);
    process.stdout.write(JSON.stringify(result) + "\n");
  } catch (error) {
    console.error(`qmd: ${error.stack ?? error}`);
    process.exitCode = 2;
  } finally {
    await store?.close();
    setDefaultLlamaCpp(null);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();
