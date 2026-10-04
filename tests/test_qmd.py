"""QMD adapter outcomes. Real lexical checks run when npm dependencies are installed."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import _node
import _paths
import semantic
import semantic_eval
from _paths import reset


class QmdAdapterTest(unittest.TestCase):
    def setUp(self):
        self.temp = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="atelier-qmd-test-"))).resolve()
        self.vault = self.temp / "vault"
        shutil.copytree(semantic.ROOT / "tests/fixtures/qmd", self.vault)
        # Generated here: ignored cache/dependency paths must still be exercised
        # when the fixture is checked out from Git on a fresh machine.
        for name in (*semantic.HARD_DIRS, ".hidden"):
            directory = self.vault / name
            directory.mkdir(exist_ok=True)
            (directory / "excluded.md").write_text("forbiddensentinel must not be indexed")
        self.cache = self.temp / "cache"
        self.enterContext(patch.dict(os.environ, {"OV": str(self.vault), "ATELIER_QMD_HOME": str(self.cache)}))
        os.environ.pop("ATELIER_QMD_PROFILE", None)
        self.enterContext(patch.object(semantic, "CONFIG_PATH", self.temp / "semantic.toml"))
        # The installed Reflect CLI must never answer a fixture query; Reflect tests install a fake one.
        self.enterContext(patch.object(semantic, "REFLECT", str(self.temp / "absent-reflect")))
        # A private paths.local.toml (a real raw_store above all) must never reach these fixtures.
        self.registry = self.temp / "atelier/harness"
        self.registry.mkdir(parents=True)
        shutil.copy(semantic.ROOT / "harness/paths.toml", self.registry)
        self.enterContext(patch.object(_paths, "_atelier_root", return_value=self.registry.parent))
        reset()
        self.addCleanup(reset)

    def use_store(self, value):
        (self.registry / "paths.local.toml").write_text(f'[paths]\nraw_store = "{value}"\n', encoding="utf-8")
        reset()

    def mirror_store(self):
        """Move raw/ into a store mirror beside a secure note; the vault keeps only the folder links."""
        store = self.temp / "store"
        (store / "personal/secure").mkdir(parents=True)
        (store / "personal/secure/ledger.md").write_text("# Ledger\n\nsecuresentinel account notes\n")
        shutil.move(self.vault / "raw", store / "raw")
        (self.vault / "raw").symlink_to(store / "raw", target_is_directory=True)
        (self.vault / "personal").mkdir()
        (self.vault / "personal/secure").symlink_to(store / "personal/secure", target_is_directory=True)
        return store

    def cli(self, *argv):
        with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            code = semantic.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def fake_reflect(self, hits=(), body=None, stale=False):
        """Install bin/reflect: it logs its argv, then answers with `hits` in the CLI's JSON or runs `body`."""
        answer = self.temp / "reflect.json"
        answer.write_text(json.dumps({"query": "q", "stale": stale, "results": list(hits)}, ensure_ascii=False))
        script = self.temp / "bin" / "reflect"
        script.parent.mkdir(exist_ok=True)
        tail = body or f'cat "{answer}"'
        script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{self.temp}/reflect.argv"\n{tail}\n')
        script.chmod(0o755)
        self.enterContext(patch.object(semantic, "REFLECT", str(script)))
        return script

    def reflect_argv(self):
        """The last fake Reflect argv, consumed so a later "not called" check starts clean."""
        log = self.temp / "reflect.argv"
        if not log.exists():
            return None
        argv = log.read_text(encoding="utf-8").splitlines()
        log.unlink()
        return argv

    @staticmethod
    def hit(path, score=-1.5):
        return {"path": path, "title": "Title", "snippet": "matched text", "score": score}

    @staticmethod
    def qmd_row():
        return {"path": "wiki/rate-limits.md", "scope": "active", "score": .5, "title": "t", "line": 1, "snippet": "s"}

    def fake_index(self):
        directory = semantic.prepare(self.vault)
        (directory / "index.sqlite").touch()
        return directory

    def query_source(self):
        directory = semantic.prepare(self.vault)
        with closing(sqlite3.connect(directory / "index.sqlite")) as database, database:
            database.execute("CREATE TABLE fixture (value TEXT NOT NULL)")
            database.execute("INSERT INTO fixture VALUES ('stable query state')")
        models = self.cache / "assets" / "qmd" / "models"
        models.mkdir(parents=True)
        (models / "fixture.gguf").write_bytes(b"GGUF" + b"\0" * 20)
        return directory, models

    @staticmethod
    def source_snapshot(root):
        result = {}
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            info = path.stat()
            result[path.relative_to(root).as_posix()] = (
                info.st_mode, info.st_size, info.st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        return result

    def test_absent_query_and_status_do_not_create_an_index(self):
        for command in (["query", "idea"], ["status"]):
            with self.subTest(command=command), patch.object(semantic, "bridge") as child:
                code, out, err = self.cli(*command)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("index is absent", err)
                self.assertFalse(self.cache.exists())
                child.assert_not_called()

    def test_private_scopes_and_hidden_operational_paths(self):
        cases = {
            "wiki/a.md": "active", "topic/inbox/a.md": "inbox", "archive/a.md": "archive",
            "sessions/a.md": "process", "topic/raw/a.txt": "raw", "archive/raw/a.md": "raw",
            "topic/cache/a.md": None, "topic/_tools/a.md": None, "topic/_meta/a.md": None,
            "_routine_prompts/a.md": None, ".secret/a.md": None, "topic/.hidden/a.md": None,
            "archive/orphan-stubs/a.md": None, "node_modules/a.md": None, "../a.md": None,
            "personal/secure/a.md": "active", "topic/secure/raw/a.csv": "raw",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(semantic.scope_for(name, self.vault), expected)
        config = semantic.collection_config(self.vault)
        self.assertEqual(set(config["collections"]), set(semantic.SCOPES))
        self.assertTrue(config["collections"]["active"]["includeByDefault"])
        self.assertFalse(config["collections"]["raw"]["includeByDefault"])
        self.assertNotIn("update", str(config))

    @unittest.skipUnless((semantic.ROOT / "node_modules/@tobilu/qmd/package.json").is_file(), "run npm ci for real QMD")
    def test_missing_models_refuse_query_without_download(self):
        self.fake_index()
        code, out, err = self.cli("query", "中文 mixed query")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("model missing", err)
        self.assertIn("download is disabled", err)
        self.assertFalse((self.cache / "assets/qmd/models").exists())

    def test_hardware_profiles_separate_model_and_index_identity(self):
        current = semantic.settings()
        self.assertEqual(current["profile"], "m3-16gb")
        self.assertEqual(current["runtime"]["parallelism"], 1)
        self.assertEqual(current["runtime"]["max_docs_per_batch"], 4)
        self.assertIn("0.6B", current["models"]["embed"])
        small_index = semantic.state_dir(self.vault)
        with patch.dict(os.environ, {"ATELIER_QMD_PROFILE": "m5-64gb"}):
            future = semantic.settings()
            self.assertEqual(future["runtime"]["parallelism"], 2)
            self.assertEqual(future["runtime"]["candidate_limit"], 60)
            self.assertIn("4B", future["models"]["embed"])
            self.assertNotEqual(small_index, semantic.state_dir(self.vault))
        self.assertFalse(self.cache.exists())
        semantic.CONFIG_PATH.write_text('[runtime]\nembed_context_tokens = 3072\n')
        self.assertNotEqual(small_index, semantic.state_dir(self.vault))

    def test_hardware_overrides_and_invalid_settings(self):
        semantic.CONFIG_PATH.write_text('profile = "m5-64gb"\n[runtime]\nparallelism = 1\ngpu = "cpu"\n')
        self.assertEqual(semantic.settings()["runtime"]["parallelism"], 1)
        with patch.dict(os.environ, {"ATELIER_QMD_PROFILE": "m3-16gb"}):
            self.assertEqual(semantic.settings()["profile"], "m3-16gb")
            self.assertEqual(semantic.settings()["runtime"]["gpu"], "cpu")
        for invalid in ('profile = "unknown"', 'profile = ["m3-16gb"]', '[runtime]\nparallelism = 0',
                        '[runtime]\nparallelism = 9', '[runtime]\ngpu = "magic"',
                        '[runtime]\ncandidate_limit = 201', '[runtime]\nthreads = 2',
                        '[models]\nembed = "https://example.invalid/model"', '[embedding]\nmodel = "legacy"'):
            semantic.CONFIG_PATH.write_text(invalid)
            with self.subTest(invalid=invalid), self.assertRaises(semantic.SearchError):
                semantic.settings()

    def test_prepare_query_copy_is_verified_and_keeps_models_at_source(self):
        source, models = self.query_source()
        before = self.source_snapshot(self.cache)
        workspace = self.temp / "model-workspace"
        workspace.mkdir()
        destination = workspace / "qmd"

        overrides = semantic.prepare_query_copy(self.vault, destination)

        self.assertEqual(overrides, {
            "ATELIER_QMD_HOME": str(destination),
            semantic.STAGED_MODEL_DIRECTORY_ENV: str(models.resolve()),
        })
        copied = destination / source.name
        self.assertEqual((copied / "index.sqlite").read_bytes(), (source / "index.sqlite").read_bytes())
        self.assertEqual((copied / "index.yml").read_bytes(), (source / "index.yml").read_bytes())
        self.assertFalse(any(destination.rglob("*.gguf")))
        self.assertEqual(self.source_snapshot(self.cache), before)

        completed = subprocess.CompletedProcess([], 0, '{}', '')
        with patch.dict(os.environ, overrides), patch.object(semantic._node, "run", return_value=completed) as child:
            self.assertEqual(semantic.bridge(self.vault, "status"), {})
        request = json.loads(child.call_args.kwargs["input"])
        self.assertEqual(request["database"], str(copied / "index.sqlite"))
        self.assertEqual(request["modelDirectory"], str(models.resolve()))
        self.assertEqual(child.call_args.kwargs["env"]["XDG_CACHE_HOME"], str(destination / "assets"))

        completed = subprocess.CompletedProcess([], 0, '[]', '')
        with patch.dict(os.environ, overrides), patch.object(semantic._node, "run", return_value=completed) as child:
            self.assertEqual(semantic.bridge(self.vault, "query", roles=["embed"], mode="vector"), [])
        request = json.loads(child.call_args.kwargs["input"])
        self.assertEqual(request["database"], str(copied / "index.sqlite"))
        self.assertEqual(request["modelDirectory"], str(models.resolve()))

    def test_prepare_query_copy_refuses_active_or_changing_source(self):
        source, _ = self.query_source()
        workspace = self.temp / "model-workspace"
        workspace.mkdir()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(source / "index.sqlite") + suffix)
            sidecar.touch()
            with self.subTest(sidecar=suffix), self.assertRaisesRegex(semantic.SearchError, "active or uncheckpointed"):
                semantic.prepare_query_copy(self.vault, workspace / f"qmd{suffix}")
            self.assertFalse((workspace / f"qmd{suffix}").exists())
            sidecar.unlink()

        original_copy = semantic._copy_verified_file

        def add_wal_after_copy(source_file, target, expected):
            original_copy(source_file, target, expected)
            if source_file.name == "index.yml":
                Path(str(source / "index.sqlite") + "-wal").touch()

        with patch.object(semantic, "_copy_verified_file", side_effect=add_wal_after_copy), \
             self.assertRaisesRegex(semantic.SearchError, "active or uncheckpointed"):
            semantic.prepare_query_copy(self.vault, workspace / "qmd-raced")
        self.assertFalse((workspace / "qmd-raced").exists())
        Path(str(source / "index.sqlite") + "-wal").unlink()

        def change_db_after_copy(source_file, target, expected):
            original_copy(source_file, target, expected)
            if source_file.name == "index.yml":
                database = source / "index.sqlite"
                database.write_bytes(database.read_bytes() + b"changed")

        with patch.object(semantic, "_copy_verified_file", side_effect=change_db_after_copy), \
             self.assertRaisesRegex(semantic.SearchError, "changed while the copy was prepared"):
            semantic.prepare_query_copy(self.vault, workspace / "qmd-changed")
        self.assertFalse((workspace / "qmd-changed").exists())

    def test_prepare_query_copy_refuses_nonregular_inputs_and_existing_destination(self):
        source, models = self.query_source()
        workspace = self.temp / "model-workspace"
        workspace.mkdir()
        existing = workspace / "existing"
        existing.mkdir()
        with self.assertRaisesRegex(semantic.SearchError, "must not already exist"):
            semantic.prepare_query_copy(self.vault, existing)

        shutil.rmtree(models)
        models.symlink_to(self.vault, target_is_directory=True)
        with self.assertRaisesRegex(semantic.SearchError, "symlink"):
            semantic.prepare_query_copy(self.vault, workspace / "qmd-model-link")
        models.unlink()
        models.mkdir()
        config = source / "index.yml"
        saved = config.read_bytes()
        config.unlink()
        config.mkdir()
        with self.assertRaisesRegex(semantic.SearchError, "regular file"):
            semantic.prepare_query_copy(self.vault, workspace / "qmd-config-dir")
        config.rmdir()
        config.write_bytes(saved)

    def test_staged_state_cannot_enable_init_or_index(self):
        source, _ = self.query_source()
        workspace = self.temp / "model-workspace"
        workspace.mkdir()
        overrides = semantic.prepare_query_copy(self.vault, workspace / "qmd")
        before = self.source_snapshot(self.cache)
        with patch.dict(os.environ, overrides):
            with self.assertRaisesRegex(semantic.SearchError, "cannot be used for init or index"):
                semantic.bridge(self.vault, "index")
            for command in (("init",), ("index", "--lexical-only")):
                with self.subTest(command=command):
                    code, out, err = self.cli(*command)
                    self.assertEqual((code, out), (2, ""))
                    self.assertIn("cannot be used", err)
        self.assertEqual(self.source_snapshot(self.cache), before)
        self.assertTrue((source / "index.sqlite").is_file())

    @unittest.skipUnless((semantic.ROOT / "node_modules/@tobilu/qmd/package.json").is_file(), "run npm ci for real QMD")
    def test_hardware_limits_reach_child_without_inference(self):
        completed = subprocess.CompletedProcess([], 0, '[]', '')
        with patch.object(semantic.subprocess, "run", return_value=completed) as child:
            semantic.bridge(self.vault, "query", roles=["embed"], mode="vector")
        call = child.call_args.kwargs
        request = json.loads(call["input"])
        self.assertEqual(request["runtime"]["max_docs_per_batch"], 4)
        self.assertEqual(request["roles"], ["embed"])
        self.assertEqual(call["env"]["QMD_EMBED_PARALLELISM"], "1")
        self.assertEqual(call["env"]["QMD_EMBED_CONTEXT_SIZE"], "2048")
        self.assertEqual(call["env"]["QMD_LLAMA_GPU"], "")
        self.assertEqual(call["timeout"], 180)

    @unittest.skipUnless((semantic.ROOT / "node_modules/@tobilu/qmd/package.json").is_file(), "run npm ci for real QMD")
    def test_node_startup_hooks_cannot_run_in_any_qmd_child(self):
        self.fake_index()
        (self.cache / "assets" / "qmd" / "models").mkdir(parents=True)
        marker = self.temp / "ambient-hook-executed"
        hook = self.temp / "ambient-hook.cjs"
        hook.write_text(f"require('node:fs').writeFileSync({json.dumps(str(marker))}, 'executed');")
        fake_bin = self.temp / "bin"
        fake_bin.mkdir()
        fake_node = fake_bin / "node"
        fake_node.write_text("#!/bin/sh\nexit 37\n")
        fake_node.chmod(0o755)
        overrides = {"NODE_OPTIONS": f"--require={hook}", "QMD_TEST_OVERRIDE": "untrusted"}
        with patch.dict(os.environ, {**overrides, "PATH": str(fake_bin)}):
            code, out, err = self.cli("status")
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["backend"], "qmd")
            self.assertFalse(marker.exists())
            completed = subprocess.CompletedProcess([], 0)
            with patch.object(semantic.subprocess, "run", return_value=completed) as download:
                code, _, err = self.cli("init", "--download-models")
            self.assertEqual(code, 0, err)
            self.assertTrue(set(overrides).isdisjoint(download.call_args.kwargs["env"]))
            self.assertEqual(download.call_args.kwargs["env"]["PATH"], _node.SYSTEM_PATH)
            self.assertEqual(download.call_args.args[0][0], str(_node.node_executable()))

    @unittest.skipUnless((semantic.ROOT / "node_modules/@tobilu/qmd/package.json").is_file(), "run npm ci for real QMD")
    def test_native_null_and_partial_embeddings_fail_closed(self):
        code = '''
          import assert from "node:assert/strict";
          import { guardEmbeddings } from "./scripts/qmd.mjs";
          for (const result of [null, {embedding: []}, {embedding: [NaN]}]) {
            const llm = {embed: async () => result, embedBatch: async () => [result]};
            guardEmbeddings(llm);
            await assert.rejects(() => llm.embed("query"), /invalid embeddings/);
            await assert.rejects(() => llm.embedBatch(["query"]), /invalid embeddings/);
          }
          const partial = {embed: async () => ({embedding: [1, 0]}), embedBatch: async () => []};
          guardEmbeddings(partial);
          await assert.rejects(() => partial.embedBatch(["query"]), /invalid embeddings/);
          assert.deepEqual(await partial.embed("query"), {embedding: [1, 0]});
        '''
        result = subprocess.run(["node", "--input-type=module", "--eval", code], cwd=semantic.ROOT,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless((semantic.ROOT / "node_modules/@tobilu/qmd/package.json").is_file(), "run npm ci for real QMD")
    def test_native_embed_session_uses_the_index_deadline(self):
        code = '''
          import assert from "node:assert/strict";
          import { resolve } from "node:path";
          import { pathToFileURL } from "node:url";
          const { LlamaCpp } = await import(new URL("llm.js", import.meta.resolve("@tobilu/qmd")));
          const realSetTimeout = globalThis.setTimeout;
          const pause = () => new Promise(resolve => realSetTimeout(resolve, 25));
          const timers = [];
          globalThis.fetch = async () => { throw new Error("Network forbidden in session fixture"); };
          LlamaCpp.prototype.ensureLlama = async allowBuild => assert.equal(allowBuild, false);
          LlamaCpp.prototype.tokenize = async text => [...text].map(c => c.codePointAt(0));
          LlamaCpp.prototype.detokenize = async tokens => String.fromCodePoint(...tokens);
          LlamaCpp.prototype.embed = async () => { await pause(); return {embedding: [1, 0, 0]}; };
          LlamaCpp.prototype.embedBatch = async texts => {
            await pause(); return texts.map(() => ({embedding: [1, 0, 0]}));
          };
          globalThis.setTimeout = (fn, ms, ...args) => {
            timers.push(ms);
            return realSetTimeout(fn, ms === 1800000 ? 5 : ms, ...args);
          };
          process.argv[1] = resolve("scripts/qmd.mjs");
          await import(pathToFileURL(process.argv[1]).href);
          process.stderr.write(`session-timers=${JSON.stringify(timers)}\\n`);
        '''
        request = {
            "command": "index", "database": ":memory:",
            "config": {"collections": {"fixture": {
                "path": str(semantic.ROOT / "tests/fixtures/qmd"), "pattern": "**/*.md",
            }}, "models": {}},
            "roles": [], "lexicalOnly": False, "modelDirectory": str(self.temp / "models"),
            "runtime": {"max_docs_per_batch": 1, "max_batch_mb": 1, "index_timeout_seconds": 3600},
        }
        result = _node.run(
            ["--input-type=module", "--eval", code], cwd=semantic.ROOT,
            env=_node.system_env(XDG_CACHE_HOME=str(self.temp / "assets")),
            timeout=30, input=json.dumps(request),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("session-timers=[3600000]", result.stderr)
        self.assertNotIn("Session expired", result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report["update"]["indexed"], report["embed"]["chunksEmbedded"]), (7, 7))
        self.assertEqual((report["embed"]["errors"], report["status"]["needsEmbedding"]), (0, 0))

    def test_reject_changed_policy_and_outside_path(self):
        directory = self.fake_index()
        with patch.object(semantic, "bridge") as child:
            code, _, err = self.cli("query", "idea", "--mode", "lexical", "--path", str(self.temp))
            self.assertEqual(code, 2)
            self.assertIn("inside", err)
            (directory / "index.yml").write_text("{}")
            code, _, err = self.cli("query", "idea", "--mode", "lexical")
            self.assertEqual(code, 2)
            self.assertIn("policy changed", err)
            child.assert_not_called()

    def test_derived_state_symlinks_cannot_write_into_vault(self):
        directory = semantic.state_dir(self.vault)
        self.cache.mkdir()
        directory.symlink_to(self.vault, target_is_directory=True)
        code, out, err = self.cli("init")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("symlinks", err)
        self.assertFalse((self.vault / "index.yml").exists())
        directory.unlink()
        directory.mkdir()
        for name in ("index.sqlite", "index.sqlite-wal", "index.sqlite-shm", "index.yml", "index.yml.pending"):
            link = directory / name
            link.symlink_to(self.vault / "must-not-write")
            code, _, err = self.cli("init")
            self.assertEqual(code, 2, err)
            self.assertFalse((self.vault / "must-not-write").exists())
            link.unlink()
        for name in ("assets", "assets/qmd", "assets/qmd/models"):
            link = self.cache / name
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(self.vault, target_is_directory=True)
            with patch.object(semantic.subprocess, "run") as download:
                code, _, err = self.cli("init", "--download-models")
            self.assertEqual(code, 2, err)
            download.assert_not_called()
            link.unlink()

    def test_filters_scopes_duplicates_and_replaced_symlinks(self):
        self.fake_index()
        external = self.temp / "outside.md"
        external.write_text("not indexed")
        (self.vault / "wiki/link.md").symlink_to(external)
        def row(path, scope="active", score=.12):
            return {"path": path, "scope": scope, "score": score, "snippet": "bounded", "line": 1}
        rows = [row("cache/excluded.md"), row("wiki/link.md"), row("../outside.md"),
                row("raw/import/evidence.md", "raw"), row("work/任务编排.md"),
                row("wiki/rate-limits.md"), row("wiki/rate-limits.md")]
        with patch.object(semantic, "bridge", return_value=rows) as child:
            code, out, _ = self.cli("query", "retry", "--mode", "lexical", "--path", "wiki")
        self.assertEqual(code, 0)
        self.assertEqual([row["path"] for row in json.loads(out)], ["wiki/rate-limits.md"])
        self.assertEqual(child.call_args.args[1], "query")
        self.assertEqual(child.call_args.kwargs["collections"], ["active"])
        self.assertEqual(json.loads(out)[0]["backend"], "qmd")

    def test_raw_store_roots_raw_and_secure_collections(self):
        plain = semantic.collection_config(self.vault)["collections"]
        self.assertEqual({entry["path"] for entry in plain.values()}, {str(self.vault)})
        store = self.mirror_store()
        self.use_store(store)
        config = semantic.collection_config(self.vault)["collections"]
        self.assertEqual(config, {**plain, "raw": {**plain["raw"], "path": str(store)}, semantic.SECURE: {
            **plain["active"], "path": str(store), "pattern": "**/secure/**/*.md"}})
        self.assertTrue(config[semantic.SECURE]["includeByDefault"])
        for value in ("relative/store", "~/store", ""):
            self.use_store(value)
            with self.subTest(value=value), self.assertRaisesRegex(_paths.PathsError, "absolute"):
                semantic.collection_config(self.vault)
        self.use_store(self.temp / "unmounted")
        with patch.object(semantic, "bridge") as child:
            code, out, err = self.cli("index", "--lexical-only")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("raw_store is not a directory", err)
        self.assertFalse(self.cache.exists())
        child.assert_not_called()

    def test_store_mirror_admits_only_raw_and_secure_folder_links(self):
        store = self.mirror_store()
        elsewhere = self.temp / "elsewhere"
        (elsewhere / "raw").mkdir(parents=True)
        for name in ("raw/stray.md", "loose.md"):
            (elsewhere / name).write_text("stray")
        for name in ("journal/entry.md", "alias/raw/aliased.md"):
            (store / name).parent.mkdir(parents=True)
            (store / name).write_text("mirrored")
        (self.vault / "real").mkdir()
        for link, target in (("topic/raw", elsewhere / "raw"),               # raw link outside the store
                             ("finance/secure", store / "personal/secure"),   # secure link to another path
                             ("journal", store / "journal"),                  # mirrored, but neither raw nor secure
                             ("alias", self.vault / "real"),                  # a second link before the raw link
                             ("real/raw", store / "alias/raw"),
                             ("personal/secure/escape", elsewhere)):          # a link inside the store
            (self.vault / link).parent.mkdir(parents=True, exist_ok=True)
            (self.vault / link).symlink_to(target, target_is_directory=True)
        (self.vault / "wiki/ledger.md").symlink_to(store / "personal/secure/ledger.md")

        def row(path, scope=semantic.SECURE):
            return {"path": path, "scope": scope, "score": .5, "snippet": "bounded", "line": 1}
        rows = [row("raw/import/evidence.md", "raw"), row("personal/secure/ledger.md"),
                row("topic/raw/stray.md", "raw"), row("finance/secure/ledger.md"), row("journal/entry.md", "active"),
                row("alias/raw/aliased.md", "raw"), row("personal/secure/escape/loose.md"),
                row("wiki/ledger.md", "active"), row("wiki/rate-limits.md", "active")]

        def search(*flags):
            with patch.object(semantic, "bridge", return_value=rows) as child:
                code, out, err = self.cli("query", "notes", "--mode", "lexical", *flags)
            self.assertEqual(code, 0, err)
            return child.call_args.kwargs["collections"], [(item["path"], item["scope"]) for item in json.loads(out)]

        self.fake_index()
        self.assertEqual(search("--scope", "all"), (list(semantic.SCOPES), [("wiki/rate-limits.md", "active")]))
        code, _, err = self.cli("query", "notes", "--mode", "lexical", "--path", "personal/secure")
        self.assertEqual(code, 2)
        self.assertIn("inside", err)
        self.use_store(store)
        self.fake_index()
        self.assertEqual(search(), (["active", semantic.SECURE],
                                    [("personal/secure/ledger.md", "active"), ("wiki/rate-limits.md", "active")]))
        with patch.object(semantic, "bridge", return_value=rows):
            hits = {item["path"]: item for item in json.loads(self.cli("query", "notes", "--mode", "lexical")[1])}
        self.assertEqual(hits["personal/secure/ledger.md"].keys() & {"title", "line", "snippet"}, set())
        self.assertEqual(hits["personal/secure/ledger.md"]["representation"], "path_only")
        self.assertEqual(hits["wiki/rate-limits.md"]["snippet"], "bounded")
        self.assertEqual(search("--scope", "raw"), (["raw"], [("raw/import/evidence.md", "raw")]))
        self.assertEqual(search("--scope", "all")[0], [*semantic.SCOPES, semantic.SECURE])
        self.assertEqual(search("--path", "personal/secure")[1], [("personal/secure/ledger.md", "active")])
        self.assertEqual(search("--scope", "raw", "--path", "raw")[1], [("raw/import/evidence.md", "raw")])

    def test_bad_query_payloads_and_child_exit_are_not_empty_success(self):
        self.fake_index()
        for payload in ({}, [None], [{"path": "wiki/rate-limits.md", "scope": "active", "score": float("nan")} ]):
            with self.subTest(payload=payload), patch.object(semantic, "bridge", return_value=payload):
                code, out, _ = self.cli("query", "retry", "--mode", "lexical")
                self.assertEqual((code, out), (2, ""))
        if not (semantic.ROOT / "node_modules/@tobilu/qmd/package.json").is_file():
            return
        for code, output in ((2, "[]"), (0, "not-json")):
            process = subprocess.CompletedProcess([], code, output, "fixture child error")
            with patch.object(semantic.subprocess, "run", return_value=process):
                result, out, _ = self.cli("query", "retry", "--mode", "lexical")
                self.assertEqual((result, out), (2, ""))

    def test_status_exposes_readiness_without_model_inference(self):
        source, _ = self.query_source()
        before = self.source_snapshot(self.cache)
        observed = {}
        native = {"totalDocuments": 3, "needsEmbedding": 0, "hasVectorIndex": True, "models_ready": False}

        def staged_status(vault, command):
            observed["home"] = semantic.cache_root()
            observed["state"] = semantic.state_dir(vault)
            self.assertEqual(command, "status")
            self.assertNotEqual(observed["state"], source)
            (observed["state"] / "index.sqlite-wal").touch()
            (observed["home"] / "assets" / "runtime-cache").mkdir(parents=True)
            return native

        with patch.object(semantic, "bridge", side_effect=staged_status):
            code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["ready"])
        self.assertFalse(json.loads(out)["models_ready"])
        self.assertEqual(json.loads(out)["freshness"], "unchecked")
        self.assertFalse(observed["home"].exists())
        self.assertEqual(self.source_snapshot(self.cache), before)
        self.assertFalse((source / "index.sqlite-wal").exists())

    def test_bad_dates_and_bounds(self):
        self.fake_index()
        for flags in (["--top", "0"], ["--after", "yesterday"],
                      ["--after", "2026-09-07", "--before", "2026-09-01"]):
            with self.subTest(flags=flags):
                code, out, _ = self.cli("query", "retry", "--mode", "lexical", *flags)
                self.assertEqual((code, out), (2, ""))

    def test_evaluation_metrics_and_empty_gold(self):
        args = semantic_eval.build_parser().parse_args(["run", "--mode", "lexical"])
        with patch.object(semantic, "query", side_effect=[[{"path": "a.md"}], []]) as search:
            metrics = semantic_eval.evaluate([{"query": "first", "target": "a.md"},
                                              {"query": "second", "target": "b.md"}], args)
        self.assertEqual((metrics["n_queries"], metrics["MRR@10"], metrics["recall@5"]), (2, .5, .5))
        # The evaluator names the backend it measured; QMD stays its default.
        self.assertEqual((search.call_args.args[0].backend, metrics["config"]["backend"]), ("qmd", "qmd"))
        with self.assertRaisesRegex(ValueError, "empty"):
            semantic_eval.evaluate([], args)

    def test_score_kind_names_the_pipeline_that_produced_the_score(self):
        """A reranked and an unreranked hybrid row do not carry the same kind of score."""
        self.fake_index()
        row = {"path": "wiki/rate-limits.md", "scope": "active", "score": 1.0, "title": "t", "line": 1, "snippet": "s"}
        for flags, expected in ((["--mode", "hybrid"], "hybrid"),
                                (["--mode", "hybrid", "--no-rerank"], "hybrid-no-rerank"),
                                (["--mode", "lexical"], "lexical"),
                                (["--mode", "vector"], "vector")):
            with self.subTest(flags=flags), patch.object(semantic, "bridge", return_value=[row]):
                code, out, err = self.cli("query", "retry", *flags)
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)[0]["score_kind"], expected)

    @unittest.skipUnless((semantic.ROOT / "node_modules/@tobilu/qmd/package.json").is_file(), "run npm ci for real QMD")
    def test_a_not_ready_index_refuses_queries_instead_of_returning_empty(self):
        """SearchError's contract: an unavailable result, never a successful empty query.

        The refusal itself lives in scripts/qmd.mjs. Pin it here so a dependency
        bump or a bridge edit cannot quietly turn "index not ready" into "[]",
        which an agent reads as an authoritative "you have no notes on this".
        """
        code, _, err = self.cli("index", "--lexical-only")
        self.assertEqual(code, 0, err)
        for mode in ("vector", "hybrid"):
            with self.subTest(mode=mode):
                code, out, err = self.cli("query", "quantum", "--mode", mode)
                self.assertEqual((code, out), (2, ""), f"{mode} on a lexical-only index must not succeed empty")
                # Which layered check fires first (embeddings vs models) is not the
                # contract; refusing with a remediation instead of `[]` is.
                self.assertIn("semantic.py", err)
        code, out, err = self.cli("query", "quantum", "--mode", "lexical")
        self.assertEqual(code, 0, err)

        empty = self.temp / "empty-vault"
        empty.mkdir()
        with patch.dict(os.environ, {"OV": str(empty)}):
            reset()
            code, _, err = self.cli("index", "--lexical-only")
            self.assertEqual(code, 0, err)
            code, out, err = self.cli("query", "anything", "--mode", "lexical")
            self.assertEqual((code, out), (2, ""), "a zero-document index must not succeed empty")
            self.assertIn("no searchable documents", err)
        reset()

    @unittest.skipUnless((semantic.ROOT / "node_modules/@tobilu/qmd/package.json").is_file(), "run npm ci for real QMD")
    def test_real_qmd_index_scope_edit_delete_and_symlink_boundaries(self):
        external = self.temp / "external"
        external.mkdir()
        (external / "outside.md").write_text("outsidesentinel")
        (self.vault / "outside-link").symlink_to(external, target_is_directory=True)
        (self.vault / "wiki/file-link.md").symlink_to(external / "outside.md")
        code, out, err = self.cli("index", "--lexical-only")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"]["totalDocuments"], 7)
        before = self.source_snapshot(self.cache)
        self.assertFalse((self.cache / "assets/qmd/models").exists())
        code, out, err = self.cli("status")
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["totalDocuments"], 7)
        self.assertFalse(report["ready"])
        self.assertFalse(report["models_ready"])
        self.assertIn("embed", report["missing_models"])
        self.assertEqual(self.source_snapshot(self.cache), before)
        self.assertFalse((self.cache / "assets/qmd/models").exists())
        def find(text, *flags):
            code, output, errors = self.cli("query", text, "--mode", "lexical", *flags)
            self.assertEqual(code, 0, errors)
            return json.loads(output)
        self.assertEqual(find("forbiddensentinel", "--scope", "all"), [])
        self.assertEqual(find("outsidesentinel", "--scope", "all"), [])
        for scope in ("raw", "archive", "inbox", "process"):
            sentinel = "archivesentinel" if scope == "archive" else scope + "sentinel"
            self.assertEqual(find(sentinel), [])
            self.assertEqual(len(find(sentinel, "--scope", scope)), 1)
        target = self.vault / "wiki/rate-limits.md"
        target.write_text("# New subject\n\nreplacementsentinel quantum computing\n")
        (self.vault / "inbox/pending.md").unlink()
        code, out, err = self.cli("index", "--lexical-only")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["update"]["removed"], 1)
        self.assertEqual(find("synchronized"), [])
        self.assertEqual(find("replacementsentinel")[0]["path"], "wiki/rate-limits.md")
        self.assertEqual(find("inboxsentinel", "--scope", "inbox"), [])

    @unittest.skipUnless((semantic.ROOT / "node_modules/@tobilu/qmd/package.json").is_file(), "run npm ci for real QMD")
    def test_real_qmd_indexes_raw_and_secure_notes_from_the_store(self):
        store = self.mirror_store()
        for name, text in (("cache/excluded.md", "forbiddensentinel"), ("archive/old/secure/parked.md", "parkedsentinel")):
            (store / name).parent.mkdir(parents=True)
            (store / name).write_text(text)
        self.use_store(store)
        code, out, err = self.cli("index", "--lexical-only")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"]["totalDocuments"], 8)

        def find(text, *flags):
            code, output, errors = self.cli("query", text, "--mode", "lexical", *flags)
            self.assertEqual(code, 0, errors)
            return [(row["path"], row["scope"], row["representation"]) for row in json.loads(output)]
        self.assertEqual(find("securesentinel"), [("personal/secure/ledger.md", "active", "path_only")])
        self.assertEqual(find("rawsentinel"), [])
        self.assertEqual(find("rawsentinel", "--scope", "raw"), [("raw/import/evidence.md", "raw", "raw_text")])
        for sentinel in ("forbiddensentinel", "parkedsentinel"):
            self.assertEqual(find(sentinel, "--scope", "all"), [])

    def test_auto_answers_lexical_and_hybrid_through_reflect_and_vector_through_qmd(self):
        script = self.fake_reflect([self.hit("wiki/rate-limits.md", 0.0), self.hit("work/任务编排.md")])
        on_path = {"PATH": f"{script.parent}{os.pathsep}{os.environ.get('PATH', '')}"}
        for mode in ("lexical", "hybrid"):
            # The module default resolves `reflect` through PATH, where the fake comes first.
            with self.subTest(mode=mode), patch.object(semantic, "REFLECT", "reflect"), \
                 patch.dict(os.environ, on_path), patch.object(semantic, "bridge") as child:
                code, out, err = self.cli("query", "-rate limits", "--mode", mode, "--top", "5")
                self.assertEqual(code, 0, err)
                child.assert_not_called()
                shared = {"title": "Title", "snippet": "matched text", "scope": "active", "source": "local",
                          "backend": "reflect", "score_kind": "lexical", "representation": "authored"}
                self.assertEqual(json.loads(out), [{"path": "wiki/rate-limits.md", "score": 0.0, **shared},
                                                   {"path": "work/任务编排.md", "score": -1.5, **shared}])
                self.assertEqual(self.reflect_argv(), ["--graph", str(self.vault), "search", "--json",
                                                       "--limit", "20", "--", "-rate limits"])
        self.assertFalse(self.cache.exists(), "a Reflect answer needs no QMD index")
        self.assertEqual(err, "")
        # A stale index is announced once; Reflect's routine skipped-symlink note is not relayed.
        self.fake_reflect([self.hit("wiki/rate-limits.md")], stale=True,
                          body=f'echo "note: skipped 3 entries" >&2; cat "{self.temp}/reflect.json"')
        code, out, err = self.cli("query", "retry")
        self.assertEqual((code, len(json.loads(out))), (0, 1), err)
        self.assertIn("index is stale", err)
        self.assertNotIn("skipped", err)
        self.assertEqual(self.reflect_argv()[-1], "retry")
        self.fake_index()
        with patch.object(semantic, "bridge", return_value=[self.qmd_row()]) as child:
            code, out, err = self.cli("query", "retry", "--mode", "vector")
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.reflect_argv())
        self.assertEqual(child.call_args.kwargs["mode"], "vector")
        self.assertEqual([(row["backend"], row["score_kind"]) for row in json.loads(out)], [("qmd", "vector")])

    def test_auto_falls_back_to_qmd_when_reflect_cannot_answer(self):
        self.fake_index()
        out_of_scope = [self.hit("cache/excluded.md"), self.hit("archive/old-plan.md")]
        cases = (("missing binary", None, None, "No such file"),
                 ("nonzero exit", (), 'echo "no search index" >&2; exit 4', "no search index"),
                 ("invalid JSON", (), "echo not-json", "unexpected JSON"),
                 ("missing field", (), """echo '{"stale": false, "results": [{"path": "a.md"}]}'""", "unexpected JSON"),
                 ("invalid score", [{**self.hit("wiki/rate-limits.md"), "score": "high"}], None, "invalid score"),
                 ("timeout", (), "exec sleep 5", "timed out"),
                 ("nothing survives the filters", out_of_scope, None, ""))
        for label, hits, body, reason in cases:
            with self.subTest(label), patch.object(semantic, "REFLECT_TIMEOUT", .5):
                if hits is None:
                    self.enterContext(patch.object(semantic, "REFLECT", str(self.temp / "absent-reflect")))
                else:
                    self.fake_reflect(hits, body)
                with patch.object(semantic, "bridge", return_value=[self.qmd_row()]) as child:
                    code, out, err = self.cli("query", "retry", "--mode", "lexical")
                self.assertEqual(code, 0, err)
                child.assert_called_once()
                self.assertEqual([row["backend"] for row in json.loads(out)], ["qmd"])
                self.assertIn(reason, err)
                self.assertEqual("falling back to QMD" in err, bool(reason))

    def test_reflect_backend_surfaces_failure_and_never_runs_qmd(self):
        self.fake_index()
        with patch.object(semantic, "REFLECT_TIMEOUT", .5), patch.object(semantic, "bridge") as child:
            for label, body in (("nonzero exit", 'echo "no search index" >&2; exit 4'),
                                ("invalid JSON", "echo not-json"), ("timeout", "exec sleep 5")):
                self.fake_reflect(body=body)
                with self.subTest(label):
                    code, out, err = self.cli("query", "retry", "--backend", "reflect")
                    self.assertEqual((code, out), (2, ""))
                    self.assertIn("Reflect", err)
            with patch.object(semantic, "REFLECT", str(self.temp / "absent-reflect")):
                self.assertEqual(self.cli("query", "retry", "--backend", "reflect")[:2], (2, ""))
            code, out, err = self.cli("query", "retry", "--backend", "reflect", "--mode", "vector")
            self.assertEqual((code, out), (2, ""))
            self.assertIn("--backend qmd", err)
            self.fake_reflect([self.hit("cache/excluded.md")])
            self.assertEqual(self.cli("query", "retry", "--backend", "reflect")[:2], (0, "[]\n"))
            child.assert_not_called()

    def test_reflect_rows_pass_the_same_filters_as_qmd_rows(self):
        external = self.temp / "outside.md"
        external.write_text("not in the vault")
        (self.vault / "wiki/link.md").symlink_to(external)
        (self.vault / "wiki/notes.txt").write_text("plain text outside raw")
        old = datetime(2020, 1, 1).timestamp()
        os.utime(self.vault / "work/任务编排.md", (old, old))
        self.fake_reflect([self.hit(path) for path in (
            "cache/excluded.md", ".hidden/excluded.md", "../outside.md", "wiki/link.md", "wiki/notes.txt",
            "wiki/absent.md", "archive/old-plan.md", "raw/import/evidence.md", "inbox/pending.md", "sessions/run.md",
            "wiki/rate-limits.md", "wiki/rate-limits.md", "work/任务编排.md", "reflections/精力管理.md")])

        def search(*flags, limit="20"):
            code, out, err = self.cli("query", "retry", "--backend", "reflect", *flags)
            self.assertEqual(code, 0, err)
            self.assertEqual(self.reflect_argv()[4:6], ["--limit", limit])
            return [(row["path"], row["scope"], row["representation"]) for row in json.loads(out)]
        active = [("wiki/rate-limits.md", "active", "authored"), ("work/任务编排.md", "active", "authored"),
                  ("reflections/精力管理.md", "active", "authored")]
        self.assertEqual(search(), active)
        self.assertEqual(search("--top", "2"), active[:2])
        self.assertEqual(search("--path", "work", "--top", "10", limit="40"), active[1:2])
        self.assertEqual(search("--after", "2021-01-01", limit="40"), [active[0], active[2]])
        self.assertEqual(search("--before", "2021-01-01", limit="40"), active[1:2])
        self.assertEqual(search("--scope", "raw"), [("raw/import/evidence.md", "raw", "raw_text")])
        self.assertEqual(search("--scope", "all"), [
            ("archive/old-plan.md", "archive", "authored"), ("raw/import/evidence.md", "raw", "raw_text"),
            ("inbox/pending.md", "inbox", "authored"), ("sessions/run.md", "process", "authored"), *active])

    def test_reflect_hits_inside_secure_folders_return_paths_only(self):
        for name in ("personal/secure/ledger.md", "secure/top.md", "archive/secure/parked.md", "wiki/secure.md"):
            (self.vault / name).parent.mkdir(parents=True, exist_ok=True)
            (self.vault / name).write_text("secure fixture")
        self.fake_reflect([self.hit(name) for name in (
            "personal/secure/ledger.md", "secure/top.md", "archive/secure/parked.md", "wiki/secure.md")])

        def search(*flags):
            code, out, err = self.cli("query", "ledger", "--backend", "reflect", *flags)
            self.assertEqual(code, 0, err)
            return {row["path"]: row for row in json.loads(out)}
        rows = search()
        self.assertEqual(list(rows), ["personal/secure/ledger.md", "secure/top.md", "wiki/secure.md"])
        for name in ("personal/secure/ledger.md", "secure/top.md"):
            self.assertEqual(rows[name].keys() & {"title", "line", "snippet"}, set())
            self.assertEqual((rows[name]["representation"], rows[name]["scope"]), ("path_only", "active"))
        self.assertEqual(rows["wiki/secure.md"]["snippet"], "matched text")  # a note, not a secure folder
        # Like QMD's secure collection, secure notes never surface outside the active scope.
        self.assertEqual(search("--scope", "archive"), {})
        self.assertNotIn("archive/secure/parked.md", search("--scope", "all"))
        # Should Reflect ever follow the raw-store secure link, the hit is still redacted.
        shutil.rmtree(self.vault / "personal")
        self.use_store(self.mirror_store())
        self.fake_reflect([self.hit("personal/secure/ledger.md")])
        row = search()["personal/secure/ledger.md"]
        self.assertEqual((row["representation"], row.keys() & {"title", "snippet"}), ("path_only", set()))

    def test_auto_fills_scopes_reflect_never_indexes_from_qmd_lexically(self):
        reflected = [self.hit(path) for path in ("wiki/rate-limits.md", "archive/old-plan.md", "work/任务编排.md")]
        self.fake_reflect(reflected)
        # Without a QMD index the Reflect rows still answer, and the gap is announced.
        with patch.object(semantic, "bridge") as child:
            code, out, err = self.cli("query", "retry", "--scope", "all")
        self.assertEqual(code, 0, err)
        child.assert_not_called()
        self.assertEqual([row["backend"] for row in json.loads(out)], ["reflect"] * 3)
        self.assertIn("index is absent", err)
        self.assertIn("returning Reflect rows without raw, process", err)
        self.fake_index()
        filled = [{**self.qmd_row(), "path": "sessions/run.md", "scope": "process"},
                  {**self.qmd_row(), "path": "raw/import/evidence.md", "scope": "raw"}]
        with patch.object(semantic, "bridge", return_value=filled) as child:
            code, out, err = self.cli("query", "retry", "--scope", "all")
        self.assertEqual(code, 0, err)
        self.assertEqual(child.call_count, 1)
        self.assertEqual((child.call_args.kwargs["mode"], child.call_args.kwargs["collections"]),
                         ("lexical", ["raw", "process"]))
        self.assertNotIn("roles", child.call_args.kwargs)  # no model loads beside a Reflect answer
        self.assertEqual([(row["path"], row["backend"], row["score_kind"]) for row in json.loads(out)], [
            ("wiki/rate-limits.md", "reflect", "lexical"), ("sessions/run.md", "qmd", "lexical"),
            ("archive/old-plan.md", "reflect", "lexical"), ("raw/import/evidence.md", "qmd", "lexical"),
            ("work/任务编排.md", "reflect", "lexical")])
        with patch.object(semantic, "bridge", return_value=filled):
            self.assertEqual([row["path"] for row in json.loads(self.cli("query", "retry", "--scope", "all",
                                                                         "--top", "2")[1])],
                             ["wiki/rate-limits.md", "sessions/run.md"])
        # A vault that does not hide its process folder from Reflect gets one row per path.
        self.fake_reflect([self.hit("sessions/run.md"), self.hit("wiki/rate-limits.md")])
        with patch.object(semantic, "bridge", return_value=filled[:1]):
            code, out, err = self.cli("query", "retry", "--scope", "process")
        self.assertEqual(code, 0, err)
        self.assertEqual([row["path"] for row in json.loads(out)], ["sessions/run.md"])
        # Scopes Reflect fully indexes never touch QMD.
        self.fake_reflect([self.hit("archive/old-plan.md")])
        with patch.object(semantic, "bridge") as child:
            self.assertEqual(self.cli("query", "retry", "--scope", "archive")[0], 0)
        child.assert_not_called()

    def link_archive(self, store):
        """Move archive/ into the store beside an archived secure note; the vault keeps one link."""
        shutil.move(self.vault / "archive", store / "archive")
        (store / "archive/old/secure").mkdir(parents=True)
        (store / "archive/old/secure/parked.md").write_text("# Parked\n\nparkedsentinel ledger\n")
        (self.vault / "archive").symlink_to(store / "archive", target_is_directory=True)

    def test_linked_archive_is_read_from_the_store_and_fills_reflect_answers(self):
        store = self.mirror_store()
        self.use_store(store)
        self.link_archive(store)
        archive = semantic.collection_config(self.vault)["collections"]["archive"]
        self.assertEqual(archive["path"], str(store))
        self.assertIn("**/secure/**", archive["ignore"])
        self.assertTrue(semantic.store_mirror("archive/old-plan.md", self.vault, store))
        self.fake_index()
        self.fake_reflect([self.hit("wiki/rate-limits.md")])
        parked = {**self.qmd_row(), "path": "archive/old-plan.md", "scope": "archive"}
        with patch.object(semantic, "bridge", return_value=[parked]) as child:
            code, out, err = self.cli("query", "plan", "--scope", "all")
        self.assertEqual(code, 0, err)
        self.assertIn("archive", child.call_args.kwargs["collections"])
        self.assertEqual([row["path"] for row in json.loads(out)], ["wiki/rate-limits.md", "archive/old-plan.md"])

    @unittest.skipUnless((semantic.ROOT / "node_modules/@tobilu/qmd/package.json").is_file(), "run npm ci for real QMD")
    def test_real_qmd_indexes_a_linked_archive_but_not_its_secure_notes(self):
        store = self.mirror_store()
        self.use_store(store)
        self.link_archive(store)
        code, _, err = self.cli("index", "--lexical-only")
        self.assertEqual(code, 0, err)
        for sentinel, expected in (("archivesentinel", ["archive/old-plan.md"]), ("parkedsentinel", [])):
            code, out, err = self.cli("query", sentinel, "--mode", "lexical", "--scope", "all", "--backend", "qmd")
            self.assertEqual(code, 0, err)
            self.assertEqual([row["path"] for row in json.loads(out)], expected)

    def test_auto_adds_secure_paths_beside_reflect_hits(self):
        self.use_store(self.mirror_store())
        self.fake_index()
        self.fake_reflect([self.hit("wiki/rate-limits.md")])
        secure = {**self.qmd_row(), "path": "personal/secure/ledger.md", "scope": semantic.SECURE}
        with patch.object(semantic, "bridge", return_value=[secure]) as child:
            code, out, err = self.cli("query", "ledger")
        self.assertEqual(code, 0, err)
        self.assertEqual(child.call_args.kwargs["collections"], [semantic.SECURE])
        rows = json.loads(out)
        self.assertEqual([(row["path"], row["scope"], row["representation"]) for row in rows], [
            ("wiki/rate-limits.md", "active", "authored"), ("personal/secure/ledger.md", "active", "path_only")])
        self.assertEqual(rows[1].keys() & {"title", "line", "snippet"}, set())

    def test_qmd_backend_keeps_the_qmd_path_and_never_runs_reflect(self):
        self.fake_reflect([self.hit("wiki/rate-limits.md")])
        code, out, err = self.cli("query", "retry", "--backend", "qmd", "--mode", "lexical")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("index is absent", err)
        self.fake_index()
        with patch.object(semantic, "bridge", return_value=[self.qmd_row()]) as child:
            code, out, err = self.cli("query", "retry", "--backend", "qmd", "--mode", "lexical")
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.reflect_argv())
        self.assertEqual(child.call_args.kwargs["collections"], ["active"])
        self.assertEqual(json.loads(out), [{**self.qmd_row(), "source": "local", "backend": "qmd",
                                            "score_kind": "lexical", "representation": "authored"}])


if __name__ == "__main__":
    unittest.main()
