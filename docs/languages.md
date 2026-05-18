# Language Support

memoire extracts causal structure from source code using static analysis — pattern matching on source text, no compiler or runtime needed. All languages feed into the same causal graph with the same promotion rules.

## Feature matrix

| Language | Side effects | Mutations | Imports | Inheritance | Test assertions |
|---|---|---|---|---|---|
| Python | ✓ | ✓ `self.attr` | ✓ | ✓ | ✓ |
| TypeScript / JS | ✓ | ✓ `this.attr` | ✓ | ✓ `extends` / `implements` | ✓ |
| Go | ✓ | — | ✓ | — | ✓ |
| Rust | ✓ | ✓ `self.field` | ✓ `use` | ✓ `impl Trait for` | ✓ |
| Java | ✓ | ✓ `this.field` | ✓ `import` | ✓ `extends` / `implements` | ✓ |
| Ruby | ✓ | ✓ `@attr` | ✓ `require` | ✓ `class < Parent` | ✓ |
| C / C++ | ✓ | — | ✓ `#include` | ✓ `: public` | ✓ |
| Markdown / RST | — | — | — | — | — (LLM extraction) |

---

## Side-effect categories

Each file is tagged with the runtime side-effect categories detected in it. These raise the cost-if-broken weight in scoring — a file that does a network call is harder to change safely than a pure function.

| Category | Meaning |
|---|---|
| `network` | HTTP requests, TCP connections, WebSockets |
| `file_io` | File reads, writes, deletions |
| `subprocess` | Shell commands, child processes |
| `database` | SQL queries, ORM calls |
| `cache` | Redis, Memcache, disk cache |

---

## Test file detection

Test files automatically get `ASSERTS_ON` edges (cost: `high`) for every module they import. These surface first in scoring — test failures have immediate, deterministic consequences.

| Language | Detected as test |
|---|---|
| Python | `test_*.py`, `*_test.py`, files in `tests/` or `test/` |
| TypeScript / JS | `*.test.ts`, `*.spec.ts`, `*.test.tsx`, files in `__tests__/` |
| Go | `*_test.go` |
| Rust | `*_test.rs`, files in `tests/` |
| Java | `*Test.java`, `*Spec.java`, `*IT.java`, files in `src/test/` |
| Ruby | `*_spec.rb`, `*_test.rb`, files in `spec/` or `test/` |
| C / C++ | `test_*.c`, `test_*.cpp`, `*_test.c`, `*_test.cpp`, files in `tests/` |

---

## Per-language details

### Python

```python
# Imports → IMPORTS edges
import os
from pathlib import Path

# Inheritance → INHERITS edges
class Dog(Animal):
    pass

# Mutations → DRIVES edges to importers
class Auth:
    def login(self):
        self.token = generate()   # self.token detected as mutation source
```

Side-effect patterns: `requests`, `httpx`, `aiohttp`, `sqlite3`, `redis`, `subprocess`, `open()`, `write_text()`.

---

### TypeScript / JavaScript

```typescript
// Imports → IMPORTS edges
import { db } from './db'

// Inheritance → INHERITS edges
class Dog extends Animal {}

// Interface implementation → IMPLEMENTS edges
class MyService implements IService {}

// Mutations → DRIVES edges to importers
class Auth {
  login() { this.token = generate() }  // this.token detected
}
```

Side-effect patterns: `fetch`, `axios`, `XMLHttpRequest`, `fs.writeFile`, `exec`, `spawn`, `prisma`, `mongoose`, `redis`.

---

### Go

```go
// Single and block imports → IMPORTS edges
import (
    "net/http"
    "database/sql"
)

// Test files (*_test.go) → ASSERTS_ON edges
```

Side-effect patterns: `net/http`, `http.Get`, `os.Create`, `exec.Command`, `sql.Open`, `.Exec(`.

---

### Rust

```rust
// use statements → IMPORTS edges (top-level crate extracted)
use std::fs;
use tokio::runtime::Runtime;

// Trait implementations → IMPLEMENTS edges
impl Display for User { ... }

// Mutations → DRIVES edges to importers
impl Auth {
    fn login(&mut self) { self.token = generate() }
}
```

Side-effect patterns: `reqwest`, `hyper`, `std::net`, `std::fs`, `Command::new`, `sqlx`, `diesel`.

---

### Java

```java
// Imports → IMPORTS edges (package path without class)
import java.util.List;
import com.example.service.UserService;

// Inheritance → INHERITS edges
public class Dog extends Animal {}

// Interface implementation → IMPLEMENTS edges
public class MyService implements IService, Closeable {}

// Mutations → DRIVES edges to importers
class Auth {
    void login() { this.token = generate(); }
}
```

Side-effect patterns: `java.net`, `HttpClient`, `java.io`, `ProcessBuilder`, `java.sql`, `JdbcTemplate`, `EntityManager`.

---

### Ruby

```ruby
# require / require_relative → IMPORTS edges
require 'json'
require_relative 'models/user'

# Inheritance → INHERITS edges
class Dog < Animal
end

# Instance variable mutations → DRIVES edges to importers
def login
  @token = generate_token   # @token detected
end
```

Side-effect patterns: `Net::HTTP`, `faraday`, `httparty`, `File.write`, `Open3`, `ActiveRecord`, `Redis`.

---

### C / C++

```c
// #include → IMPORTS edges
#include <stdio.h>
#include "auth.h"

// C++ class inheritance → INHERITS edges
class Dog : public Animal { };
```

Side-effect patterns: `socket(`, `fopen(`, `fwrite(`, `system(`, `popen(`, `sqlite3_exec`, `curl_easy_*`.

---

## Promotion rules

After every ingest and every 10 file changes, three promotion rules run across all languages:

1. **Fan-in → DRIVES**: any module imported by 3+ files becomes a causal root
2. **Test IMPORTS → ASSERTS_ON**: test file imports are promoted to high-cost causal edges
3. **Mutation → DRIVES**: files with detected mutations get DRIVES edges to their importers

These rules are language-agnostic — a Ruby module imported by 5 files gets promoted to DRIVES exactly like a Python module.
