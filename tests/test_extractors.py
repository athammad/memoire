"""Unit tests for static analysis extractors, detectors, and scoring."""

import json
import pytest

from memoire.processor import (
    _detect_side_effects,
    _detect_side_effects_ts,
    _detect_side_effects_go,
    _extract_python,
    _extract_state_mutations,
    _extract_state_mutations_ts,
    _extract_typescript,
    _extract_go,
)
from memoire.sdk import _is_test_path, _causal_score, _compute_causal_reachability, _detect_causal_cycles
from memoire.processor import (
    _strip_fences,
    _parse_relationships,
    _detect_side_effects_rust,
    _detect_side_effects_java,
    _detect_side_effects_ruby,
    _detect_side_effects_c,
    _extract_state_mutations_rust,
    _extract_state_mutations_java,
    _extract_state_mutations_ruby,
    _extract_rust,
    _extract_java,
    _extract_ruby,
    _extract_c,
)


# ---------------------------------------------------------------------------
# _is_test_path
# ---------------------------------------------------------------------------

class TestIsTestPath:
    def test_python_test_prefix(self):
        assert _is_test_path("tests/test_db.py")

    def test_python_test_suffix(self):
        assert _is_test_path("memoire/db_test.py")

    def test_python_tests_dir(self):
        assert _is_test_path("tests/utils.py")

    def test_typescript_dot_test(self):
        assert _is_test_path("src/auth.test.ts")

    def test_typescript_dot_spec(self):
        assert _is_test_path("src/auth.spec.tsx")

    def test_typescript_jest_dir(self):
        assert _is_test_path("src/__tests__/auth.ts")

    def test_go_test_suffix(self):
        assert _is_test_path("pkg/db/db_test.go")

    def test_non_test_python(self):
        assert not _is_test_path("memoire/db.py")

    def test_non_test_typescript(self):
        assert not _is_test_path("src/auth.ts")

    def test_non_test_go(self):
        assert not _is_test_path("pkg/db/db.go")


# ---------------------------------------------------------------------------
# Side-effect detection — Python
# ---------------------------------------------------------------------------

class TestDetectSideEffectsPython:
    def test_network_requests(self):
        assert "network" in _detect_side_effects("import requests\nrequests.get(url)")

    def test_network_httpx(self):
        assert "network" in _detect_side_effects("import httpx\nhttpx.post(url, json=data)")

    def test_file_io_open(self):
        assert "file_io" in _detect_side_effects("with open('file.txt', 'w') as f: f.write(data)")

    def test_file_io_write_text(self):
        assert "file_io" in _detect_side_effects("Path('out.txt').write_text(content)")

    def test_subprocess(self):
        assert "subprocess" in _detect_side_effects("import subprocess\nsubprocess.run(['ls'])")

    def test_database_sqlite(self):
        assert "database" in _detect_side_effects("import sqlite3\nconn = sqlite3.connect('db')")

    def test_cache_redis(self):
        assert "cache" in _detect_side_effects("import redis\nr = redis.Redis()")

    def test_multiple_categories(self):
        code = "requests.get(url)\nopen('f.txt')"
        detected = _detect_side_effects(code)
        assert "network" in detected
        assert "file_io" in detected

    def test_pure_function(self):
        assert _detect_side_effects("def add(a, b):\n    return a + b") == []


# ---------------------------------------------------------------------------
# Side-effect detection — TypeScript
# ---------------------------------------------------------------------------

class TestDetectSideEffectsTS:
    def test_network_fetch(self):
        assert "network" in _detect_side_effects_ts("const res = await fetch('/api')")

    def test_network_axios(self):
        assert "network" in _detect_side_effects_ts("axios.post('/api', data)")

    def test_file_io_fs(self):
        assert "file_io" in _detect_side_effects_ts("fs.writeFile('out.txt', data)")

    def test_subprocess_exec(self):
        assert "subprocess" in _detect_side_effects_ts("exec('ls -la', callback)")

    def test_database_prisma(self):
        assert "database" in _detect_side_effects_ts("await prisma.user.findMany()")

    def test_pure_function(self):
        assert _detect_side_effects_ts("const add = (a: number, b: number) => a + b") == []


# ---------------------------------------------------------------------------
# Side-effect detection — Go
# ---------------------------------------------------------------------------

class TestDetectSideEffectsGo:
    def test_network_http(self):
        code = 'import "net/http"\nhttp.Get(url)'
        assert "network" in _detect_side_effects_go(code)

    def test_file_io_os(self):
        assert "file_io" in _detect_side_effects_go("os.Create('file.txt')")

    def test_subprocess_exec(self):
        assert "subprocess" in _detect_side_effects_go("exec.Command('ls', '-la')")

    def test_database_sql(self):
        code = 'import "database/sql"\nsql.Open("postgres", dsn)'
        assert "database" in _detect_side_effects_go(code)

    def test_pure_function(self):
        assert _detect_side_effects_go("func add(a, b int) int { return a + b }") == []


# ---------------------------------------------------------------------------
# Mutation detection
# ---------------------------------------------------------------------------

class TestExtractStateMutations:
    def test_basic_assignment(self):
        code = "class Auth:\n    def login(self):\n        self.token = 'abc'"
        assert "token" in _extract_state_mutations(code)

    def test_multiple_attrs(self):
        code = "self.user = x\nself.session = y\nself.count = 0"
        attrs = _extract_state_mutations(code)
        assert "user" in attrs
        assert "session" in attrs
        assert "count" in attrs

    def test_skips_private(self):
        code = "self._token = 'abc'\nself.public = 1"
        attrs = _extract_state_mutations(code)
        assert "_token" not in attrs
        assert "public" in attrs

    def test_skips_comparison(self):
        code = "if self.token == 'abc': pass"
        assert _extract_state_mutations(code) == []

    def test_empty(self):
        assert _extract_state_mutations("def pure(x): return x + 1") == []


class TestExtractStateMutationsTS:
    def test_basic_assignment(self):
        code = "class Auth { login() { this.token = 'abc'; } }"
        assert "token" in _extract_state_mutations_ts(code)

    def test_skips_private(self):
        code = "this._token = 'abc'; this.user = 'x';"
        attrs = _extract_state_mutations_ts(code)
        assert "_token" not in attrs
        assert "user" in attrs

    def test_skips_equality(self):
        assert _extract_state_mutations_ts("if (this.token === 'abc') {}") == []


# ---------------------------------------------------------------------------
# _extract_python
# ---------------------------------------------------------------------------

class TestExtractPython:
    def test_imports(self):
        code = "import os\nfrom pathlib import Path"
        rels = _extract_python(code, "memoire/cli.py")
        relations = {r["relation"] for r in rels}
        targets = {r["target"] for r in rels}
        assert "IMPORTS" in relations
        assert "os" in targets
        assert "pathlib" in targets

    def test_inherits(self):
        code = "class Dog(Animal):\n    pass"
        rels = _extract_python(code, "app/models.py")
        inherits = [r for r in rels if r["relation"] == "INHERITS"]
        assert any(r["source"] == "Dog" and r["target"] == "Animal" for r in inherits)

    def test_test_file_emits_asserts_on(self):
        code = "from memoire.db import get_db"
        rels = _extract_python(code, "tests/test_db.py")
        assert any(r["relation"] == "ASSERTS_ON" and r["target"] == "memoire.db" for r in rels)
        assert any(r["cost"] == "high" for r in rels if r["relation"] == "ASSERTS_ON")

    def test_non_test_no_asserts_on(self):
        code = "from memoire.db import get_db"
        rels = _extract_python(code, "memoire/cli.py")
        assert not any(r["relation"] == "ASSERTS_ON" for r in rels)


# ---------------------------------------------------------------------------
# _extract_typescript
# ---------------------------------------------------------------------------

class TestExtractTypeScript:
    def test_imports(self):
        code = "import { foo } from './foo'\nimport bar from '../bar'"
        rels = _extract_typescript(code, "src/app.ts")
        targets = {r["target"] for r in rels if r["relation"] == "IMPORTS"}
        assert "./foo" in targets
        assert "../bar" in targets

    def test_inherits(self):
        code = "class Dog extends Animal {}"
        rels = _extract_typescript(code, "src/models.ts")
        assert any(r["relation"] == "INHERITS" and r["source"] == "Dog" for r in rels)

    def test_implements(self):
        code = "class MyService implements IService, IDisposable {}"
        rels = _extract_typescript(code, "src/service.ts")
        impl = [r for r in rels if r["relation"] == "IMPLEMENTS"]
        targets = {r["target"] for r in impl}
        assert "IService" in targets
        assert "IDisposable" in targets

    def test_test_file_emits_asserts_on(self):
        code = "import { db } from '../db'"
        rels = _extract_typescript(code, "src/db.test.ts")
        assert any(r["relation"] == "ASSERTS_ON" for r in rels)

    def test_spec_file_emits_asserts_on(self):
        code = "import { auth } from '../auth'"
        rels = _extract_typescript(code, "src/__tests__/auth.spec.ts")
        assert any(r["relation"] == "ASSERTS_ON" for r in rels)

    def test_non_test_no_asserts_on(self):
        code = "import { db } from '../db'"
        rels = _extract_typescript(code, "src/service.ts")
        assert not any(r["relation"] == "ASSERTS_ON" for r in rels)


# ---------------------------------------------------------------------------
# _extract_go
# ---------------------------------------------------------------------------

class TestExtractGo:
    def test_single_import(self):
        code = 'import "fmt"'
        rels = _extract_go(code, "main.go")
        assert any(r["target"] == "fmt" for r in rels)

    def test_import_block(self):
        code = 'import (\n    "fmt"\n    "os"\n)'
        rels = _extract_go(code, "main.go")
        targets = {r["target"] for r in rels if r["relation"] == "IMPORTS"}
        assert "fmt" in targets
        assert "os" in targets

    def test_test_file_emits_asserts_on(self):
        code = 'import "fmt"'
        rels = _extract_go(code, "pkg/db/db_test.go")
        assert any(r["relation"] == "ASSERTS_ON" for r in rels)

    def test_non_test_no_asserts_on(self):
        code = 'import "fmt"'
        rels = _extract_go(code, "pkg/db/db.go")
        assert not any(r["relation"] == "ASSERTS_ON" for r in rels)


# ---------------------------------------------------------------------------
# _compute_causal_reachability
# ---------------------------------------------------------------------------

class TestComputeCausalReachability:
    def test_linear_chain(self):
        # A → B → C: A can reach B and C (2), B can reach C (1), C can reach none (0)
        edges = [("A", "B"), ("B", "C")]
        r = _compute_causal_reachability(edges)
        assert r["A"] == 2
        assert r["B"] == 1
        assert r["C"] == 0

    def test_fan_out(self):
        # A → B, A → C, A → D: A reaches 3
        edges = [("A", "B"), ("A", "C"), ("A", "D")]
        r = _compute_causal_reachability(edges)
        assert r["A"] == 3
        assert r["B"] == 0

    def test_diamond(self):
        # A → B, A → C, B → D, C → D: A reaches B, C, D (3)
        edges = [("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")]
        r = _compute_causal_reachability(edges)
        assert r["A"] == 3
        assert r["B"] == 1
        assert r["C"] == 1
        assert r["D"] == 0

    def test_isolated_node(self):
        edges = [("A", "B")]
        r = _compute_causal_reachability(edges)
        assert r["A"] == 1
        assert r["B"] == 0

    def test_empty(self):
        assert _compute_causal_reachability([]) == {}


# ---------------------------------------------------------------------------
# _causal_score
# ---------------------------------------------------------------------------

class TestCausalScore:
    def _row(self, access_count=0, side_effects=None, updated_at=None):
        import datetime
        return {
            "access_count": access_count,
            "side_effects": side_effects or [],
            "updated_at": updated_at or datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

    def test_higher_reachability_scores_higher(self):
        row = self._row()
        low = _causal_score(row, causal_in=0, reachability=1)
        high = _causal_score(row, causal_in=0, reachability=10)
        assert high > low

    def test_side_effects_increase_score(self):
        plain = _causal_score(self._row(side_effects=[]), causal_in=0, reachability=0)
        with_effects = _causal_score(self._row(side_effects=["network", "database"]), causal_in=0, reachability=0)
        assert with_effects > plain

    def test_access_count_increases_score(self):
        low = _causal_score(self._row(access_count=0), causal_in=0, reachability=0)
        high = _causal_score(self._row(access_count=50), causal_in=0, reachability=0)
        assert high > low

    def test_old_file_scores_lower(self):
        import datetime
        old_ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)).isoformat()
        recent = _causal_score(self._row(), causal_in=0, reachability=0)
        old = _causal_score(self._row(updated_at=old_ts), causal_in=0, reachability=0)
        assert recent > old


# ---------------------------------------------------------------------------
# _detect_causal_cycles
# ---------------------------------------------------------------------------

class TestDetectCausalCycles:
    def test_no_cycles_dag(self):
        # A → B → C is a valid DAG
        edges = [("A", "B"), ("B", "C")]
        assert _detect_causal_cycles(edges) == []

    def test_simple_cycle(self):
        # A → B → A is a cycle
        edges = [("A", "B"), ("B", "A")]
        cycles = _detect_causal_cycles(edges)
        assert len(cycles) == 1
        assert "A" in cycles[0] and "B" in cycles[0]

    def test_self_loop(self):
        edges = [("A", "A")]
        cycles = _detect_causal_cycles(edges)
        assert len(cycles) == 1

    def test_three_node_cycle(self):
        # A → B → C → A
        edges = [("A", "B"), ("B", "C"), ("C", "A")]
        cycles = _detect_causal_cycles(edges)
        assert len(cycles) >= 1

    def test_cycle_among_dag(self):
        # D → E is fine; A → B → A is a cycle — both present
        edges = [("A", "B"), ("B", "A"), ("D", "E")]
        cycles = _detect_causal_cycles(edges)
        assert len(cycles) >= 1
        cycle_text = " ".join(cycles)
        assert "A" in cycle_text or "B" in cycle_text

    def test_empty(self):
        assert _detect_causal_cycles([]) == []

    def test_diamond_no_cycle(self):
        # A → B, A → C, B → D, C → D — diamond, valid DAG
        edges = [("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")]
        assert _detect_causal_cycles(edges) == []

    def test_isolated_nodes_no_cycle(self):
        edges = [("A", "B"), ("C", "D")]
        assert _detect_causal_cycles(edges) == []


# ---------------------------------------------------------------------------
# Phase 4 — LLM response parsing helpers
# ---------------------------------------------------------------------------

class TestStripFences:
    def test_strips_json_fence(self):
        raw = "```json\n{\"relationships\": []}\n```"
        assert _strip_fences(raw) == '{"relationships": []}'

    def test_strips_plain_fence(self):
        raw = "```\n{\"relationships\": []}\n```"
        assert _strip_fences(raw) == '{"relationships": []}'

    def test_no_fence_unchanged(self):
        raw = '{"relationships": []}'
        assert _strip_fences(raw) == raw

    def test_multiline_fence(self):
        raw = "```json\n{\n  \"relationships\": []\n}\n```"
        result = _strip_fences(raw)
        assert result.startswith("{")
        assert result.endswith("}")


class TestParseRelationships:
    _SAMPLE = json.dumps({
        "entities": [],
        "relationships": [
            {"source": "THEORY.md", "relation": "SPECIFIES", "target": "memoire/sdk.py",
             "rationale": "Theory specifies the SDK contract"},
            {"source": "memoire/sdk.py", "relation": "IMPLEMENTS", "target": "THEORY.md",
             "rationale": "SDK implements the theory"},
        ],
    })

    def test_extracts_relationships(self):
        rels = _parse_relationships(self._SAMPLE)
        assert len(rels) == 2

    def test_fields_present(self):
        rels = _parse_relationships(self._SAMPLE)
        assert rels[0]["source"] == "THEORY.md"
        assert rels[0]["relation"] == "SPECIFIES"
        assert rels[0]["target"] == "memoire/sdk.py"
        assert "rationale" in rels[0]

    def test_missing_relation_defaults_to_relates_to(self):
        raw = json.dumps({"relationships": [{"source": "A", "target": "B"}]})
        rels = _parse_relationships(raw)
        assert rels[0]["relation"] == "RELATES_TO"

    def test_empty_relationships(self):
        raw = json.dumps({"relationships": []})
        assert _parse_relationships(raw) == []

    def test_from_fenced_response(self):
        fenced = f"```json\n{self._SAMPLE}\n```"
        rels = _parse_relationships(fenced)
        assert len(rels) == 2


# ---------------------------------------------------------------------------
# Phase 5 — _is_test_path (new languages)
# ---------------------------------------------------------------------------

class TestIsTestPathPhase5:
    def test_rust_test_suffix(self):
        assert _is_test_path("src/db_test.rs")

    def test_rust_tests_dir(self):
        assert _is_test_path("tests/integration.rs")

    def test_java_test_suffix(self):
        assert _is_test_path("src/test/java/AuthTest.java")

    def test_java_spec_suffix(self):
        assert _is_test_path("src/UserSpec.java")

    def test_ruby_spec(self):
        assert _is_test_path("spec/models/user_spec.rb")

    def test_ruby_test(self):
        assert _is_test_path("test/user_test.rb")

    def test_c_test_prefix(self):
        assert _is_test_path("tests/test_parser.c")

    def test_cpp_test_suffix(self):
        assert _is_test_path("tests/parser_test.cpp")

    def test_non_test_rust(self):
        assert not _is_test_path("src/db.rs")

    def test_non_test_java(self):
        assert not _is_test_path("src/main/java/Auth.java")

    def test_non_test_ruby(self):
        assert not _is_test_path("app/models/user.rb")

    def test_non_test_c(self):
        assert not _is_test_path("src/parser.c")


# ---------------------------------------------------------------------------
# Side-effect detection — Rust
# ---------------------------------------------------------------------------

class TestDetectSideEffectsRust:
    def test_network_reqwest(self):
        assert "network" in _detect_side_effects_rust("use reqwest::Client;")

    def test_network_tcp(self):
        assert "network" in _detect_side_effects_rust("use std::net::TcpStream;")

    def test_file_io_fs(self):
        assert "file_io" in _detect_side_effects_rust("use std::fs;\nfs::write('f', b'x').unwrap();")

    def test_subprocess(self):
        assert "subprocess" in _detect_side_effects_rust("use std::process::Command;\nCommand::new('ls');")

    def test_database_sqlx(self):
        assert "database" in _detect_side_effects_rust("use sqlx::PgPool;")

    def test_pure_function(self):
        assert _detect_side_effects_rust("fn add(a: i32, b: i32) -> i32 { a + b }") == []


# ---------------------------------------------------------------------------
# Side-effect detection — Java
# ---------------------------------------------------------------------------

class TestDetectSideEffectsJava:
    def test_network_http(self):
        assert "network" in _detect_side_effects_java("import java.net.HttpURLConnection;")

    def test_file_io(self):
        assert "file_io" in _detect_side_effects_java("import java.io.FileWriter;")

    def test_subprocess(self):
        assert "subprocess" in _detect_side_effects_java("Runtime.exec(cmd);")

    def test_database_jdbc(self):
        assert "database" in _detect_side_effects_java("import java.sql.Connection;")

    def test_pure_function(self):
        assert _detect_side_effects_java("public int add(int a, int b) { return a + b; }") == []


# ---------------------------------------------------------------------------
# Side-effect detection — Ruby
# ---------------------------------------------------------------------------

class TestDetectSideEffectsRuby:
    def test_network_net_http(self):
        assert "network" in _detect_side_effects_ruby("require 'net/http'\nNet::HTTP.get(uri)")

    def test_file_io(self):
        assert "file_io" in _detect_side_effects_ruby("File.write('out.txt', data)")

    def test_subprocess_system(self):
        assert "subprocess" in _detect_side_effects_ruby("system('ls -la')")

    def test_database_activerecord(self):
        assert "database" in _detect_side_effects_ruby("User.where(active: true)")

    def test_pure_function(self):
        assert _detect_side_effects_ruby("def add(a, b) = a + b") == []


# ---------------------------------------------------------------------------
# Side-effect detection — C/C++
# ---------------------------------------------------------------------------

class TestDetectSideEffectsC:
    def test_network_socket(self):
        assert "network" in _detect_side_effects_c("int fd = socket(AF_INET, SOCK_STREAM, 0);")

    def test_file_io_fopen(self):
        assert "file_io" in _detect_side_effects_c("FILE *f = fopen('out.txt', 'w');")

    def test_subprocess_system(self):
        assert "subprocess" in _detect_side_effects_c("system('ls');")

    def test_database_sqlite(self):
        assert "database" in _detect_side_effects_c("sqlite3_exec(db, sql, 0, 0, 0);")

    def test_pure_function(self):
        assert _detect_side_effects_c("int add(int a, int b) { return a + b; }") == []


# ---------------------------------------------------------------------------
# Mutation detection — Rust / Java / Ruby
# ---------------------------------------------------------------------------

class TestExtractStateMutationsRust:
    def test_basic(self):
        code = "impl Auth { fn login(&mut self) { self.token = String::new(); } }"
        assert "token" in _extract_state_mutations_rust(code)

    def test_skips_private(self):
        code = "self._token = x; self.user = y;"
        attrs = _extract_state_mutations_rust(code)
        assert "_token" not in attrs
        assert "user" in attrs

    def test_skips_comparison(self):
        assert _extract_state_mutations_rust("if self.token == x {}") == []


class TestExtractStateMutationsJava:
    def test_basic(self):
        code = "class Auth { void login() { this.token = generate(); } }"
        assert "token" in _extract_state_mutations_java(code)

    def test_skips_equality(self):
        assert _extract_state_mutations_java("if (this.token == null) {}") == []


class TestExtractStateMutationsRuby:
    def test_basic(self):
        code = "def login\n  @token = generate_token\nend"
        assert "token" in _extract_state_mutations_ruby(code)

    def test_skips_comparison(self):
        assert _extract_state_mutations_ruby("@token == 'abc'") == []


# ---------------------------------------------------------------------------
# _extract_rust
# ---------------------------------------------------------------------------

class TestExtractRust:
    def test_use_imports(self):
        code = "use std::fs;\nuse tokio::runtime::Runtime;"
        rels = _extract_rust(code, "src/db.rs")
        targets = {r["target"] for r in rels if r["relation"] == "IMPORTS"}
        assert "std" in targets
        assert "tokio" in targets

    def test_trait_impl(self):
        code = "impl Display for User { fn fmt(&self, f: &mut Formatter) -> fmt::Result {} }"
        rels = _extract_rust(code, "src/user.rs")
        assert any(r["relation"] == "IMPLEMENTS" and r["source"] == "User" for r in rels)

    def test_test_file_emits_asserts_on(self):
        code = "use crate::auth;"
        rels = _extract_rust(code, "tests/auth_test.rs")
        assert any(r["relation"] == "ASSERTS_ON" for r in rels)

    def test_non_test_no_asserts_on(self):
        code = "use crate::auth;"
        rels = _extract_rust(code, "src/auth.rs")
        assert not any(r["relation"] == "ASSERTS_ON" for r in rels)


# ---------------------------------------------------------------------------
# _extract_java
# ---------------------------------------------------------------------------

class TestExtractJava:
    def test_imports(self):
        code = "import java.util.List;\nimport com.example.service.UserService;"
        rels = _extract_java(code, "src/main/java/App.java")
        targets = {r["target"] for r in rels if r["relation"] == "IMPORTS"}
        assert "java.util" in targets
        assert "com.example.service" in targets

    def test_inherits(self):
        code = "public class Dog extends Animal {}"
        rels = _extract_java(code, "src/Dog.java")
        assert any(r["relation"] == "INHERITS" and r["source"] == "Dog" for r in rels)

    def test_implements(self):
        code = "public class MyService implements IService, Closeable {}"
        rels = _extract_java(code, "src/MyService.java")
        impl_targets = {r["target"] for r in rels if r["relation"] == "IMPLEMENTS"}
        assert "IService" in impl_targets

    def test_test_file_emits_asserts_on(self):
        code = "import com.example.Auth;"
        rels = _extract_java(code, "src/test/java/AuthTest.java")
        assert any(r["relation"] == "ASSERTS_ON" for r in rels)


# ---------------------------------------------------------------------------
# _extract_ruby
# ---------------------------------------------------------------------------

class TestExtractRuby:
    def test_require(self):
        code = "require 'json'\nrequire_relative 'models/user'"
        rels = _extract_ruby(code, "app/controllers/auth.rb")
        targets = {r["target"] for r in rels if r["relation"] == "IMPORTS"}
        assert "json" in targets
        assert "models/user" in targets

    def test_inherits(self):
        code = "class Dog < Animal\nend"
        rels = _extract_ruby(code, "app/models/dog.rb")
        assert any(r["relation"] == "INHERITS" and r["source"] == "Dog" for r in rels)

    def test_test_file_emits_asserts_on(self):
        code = "require_relative '../user'"
        rels = _extract_ruby(code, "spec/user_spec.rb")
        assert any(r["relation"] == "ASSERTS_ON" for r in rels)

    def test_non_test_no_asserts_on(self):
        code = "require 'json'"
        rels = _extract_ruby(code, "app/models/user.rb")
        assert not any(r["relation"] == "ASSERTS_ON" for r in rels)


# ---------------------------------------------------------------------------
# _extract_c
# ---------------------------------------------------------------------------

class TestExtractC:
    def test_system_include(self):
        code = "#include <stdio.h>\n#include <stdlib.h>"
        rels = _extract_c(code, "src/main.c")
        targets = {r["target"] for r in rels if r["relation"] == "IMPORTS"}
        assert "stdio.h" in targets
        assert "stdlib.h" in targets

    def test_local_include(self):
        code = '#include "auth.h"'
        rels = _extract_c(code, "src/main.c")
        assert any(r["target"] == "auth.h" for r in rels)

    def test_cpp_inherits(self):
        code = "class Dog : public Animal { };"
        rels = _extract_c(code, "src/dog.cpp")
        assert any(r["relation"] == "INHERITS" and r["source"] == "Dog" for r in rels)

    def test_test_file_emits_asserts_on(self):
        code = '#include "auth.h"'
        rels = _extract_c(code, "tests/test_auth.c")
        assert any(r["relation"] == "ASSERTS_ON" for r in rels)

    def test_non_test_no_asserts_on(self):
        code = '#include "auth.h"'
        rels = _extract_c(code, "src/main.c")
        assert not any(r["relation"] == "ASSERTS_ON" for r in rels)
