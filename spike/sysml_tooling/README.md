# Spike: driving daltskin/sysml-v2-lsp from Python

Isolated exploration only. Nothing here is wired into the main app.

## What this proves

1. We can get **diagnostics** (validation errors) for a `.sysml` file from Python, with
   full message/line/column/severity — the shape Layer-3's verify loop will need to feed
   errors back into regeneration.
2. We can get a **Mermaid diagram** derived from a valid SysML v2 model, from Python.

Both work. See "Findings" below for the two things that weren't quite as advertised.

## How it was installed

```bash
npm init -y
npm install sysml-v2-lsp
```

That's the whole install — `node_modules/sysml-v2-lsp/dist/server/` ships **pre-built**
JS bundles (`server.js` for LSP, `mcpServer.js` for MCP), so no `npm run build` /
ANTLR/TypeScript compile step was needed.

The **Python client** (`clients/python/sysml_lsp_client.py`) and the `.sysml` example
files, however, are only in the **GitHub repo**, not the published npm package. I cloned
the repo (`git clone --depth 1 https://github.com/daltskin/sysml-v2-lsp.git`) to read
`sysml_lsp_client.py` and the server source (`server/src/mcp/mermaidGenerator.ts`,
`server/src/mcpServer.ts`) for reference, then wrote a self-contained driver
(`run_spike.py`) based on what I learned there, pointed at the npm-installed
`dist/server/*.js`. The clone itself has since been deleted — it's not needed at runtime.

## How to run

```bash
cd spike/sysml_tooling
python run_spike.py
```

Takes ~10-20s (the LSP server's DFA warms up on the first file parsed).

## Files

- `samples/valid.sysml` — a small valid model: a `part def Vehicle` with a length
  attribute, a `part vehicle` instance, and a `requirement def` with a `subject` and a
  `require constraint` referencing an SI unit (`SI::m`).
- `samples/invalid.sysml` — the same model with two deliberate errors: a malformed
  keyword (`requirment` instead of `requirement`) and a broken reference (`subject veh :
  UndefinedVehicle`).
- `run_spike.py` — the driver. Part 1 speaks the standard LSP protocol to get
  diagnostics; Part 2 speaks MCP to get a Mermaid diagram. Self-contained (stdlib only,
  matching the reference Python client's zero-dependency approach).

## Findings / friction (the actual point of a spike)

**1. Two different servers, two different wire formats — not one LSP endpoint.**
The npm package bundles two separate stdio servers:
- `dist/server/server.js` — standard LSP, JSON-RPC framed with `Content-Length` headers
  (what VS Code speaks).
- `dist/server/mcpServer.js` — a **Model Context Protocol** server (`@modelcontextprotocol
  /sdk`), JSON-RPC but **newline-delimited**, no `Content-Length` framing at all.

Mermaid generation (`server/src/mcp/mermaidGenerator.ts`) is wired in **only** as an MCP
tool (`preview`) on the second server — it is not reachable through the standard LSP
methods at all. I confirmed this by grepping the server source for
`generateMermaidDiagram` before writing any client code, rather than guessing. This
matters for integration: driving "validation" and "diagram" needs two different client
implementations/protocols, not one.

**2. The `preview` MCP tool is designed for an LLM, not a script.** Its response embeds
an "ACTION REQUIRED" instruction block telling an AI agent to call a second tool
(`renderMermaidDiagram`) to actually display the diagram, and to suppress raw output from
its own reply. For scripted use we don't call `renderMermaidDiagram` at all — the Mermaid
markup is already sitting in the `preview` response's second content block as
`mermaidMarkup`, so a script just parses that JSON directly and ignores the
agent-directed instructions in the first block.

**3. The reference Python client's `select.select()` on `proc.stdout` doesn't work on
Windows** (`OSError: [WinError 10093]`) — `select()` there only supports sockets, not
pipes. This is environment-specific (this spike ran on Windows, not WSL as the task
assumed — Node/npm/git were all available directly on Windows here). I replaced it with a
blocking read loop guarded by a background `threading.Timer` for the timeout, rather than
silently keeping the broken approach.

**4. A single syntax error can mask a semantic one in the same block.** `invalid.sysml`
has two deliberate errors (malformed keyword + broken reference), but the malformed
`requirment` keyword corrupted parsing badly enough that the `subject veh :
UndefinedVehicle` broken reference never got its own diagnostic — the parser reinterpreted
`subject`/`require` as bogus identifiers instead of reaching semantic resolution. Worth
keeping in mind for the verify loop: don't assume one pass surfaces every problem in a
badly-broken file.

## Actual output

### Part 1 — Diagnostics (LSP)

```
Server capabilities: codeActionProvider, completionProvider, definitionProvider,
documentFormattingProvider, documentRangeFormattingProvider, documentSymbolProvider,
foldingRangeProvider, hoverProvider, referencesProvider, renameProvider,
semanticTokensProvider, textDocumentSync

------------------------------------------------------------------------
FILE: valid.sysml
------------------------------------------------------------------------
  CLEAN — no diagnostics reported.

------------------------------------------------------------------------
FILE: invalid.sysml
------------------------------------------------------------------------
  5 diagnostic(s):

  [Error] line 12, col 16 -> line 12, col 19
      message : 'def' is a reserved SysML keyword and cannot be used as an identifier here.
      code    : None
      source  : sysml

  [Error] line 15, col 9 -> line 15, col 16
      message : 'subject' is a reserved SysML keyword and cannot be used as an identifier here.
      code    : None
      source  : sysml

  [Error] line 16, col 9 -> line 16, col 16
      message : 'require' is a reserved SysML keyword and cannot be used as an identifier here.
      code    : None
      source  : sysml

  [Error] line 12, col 5 -> line 12, col 15
      message : Unknown keyword 'requirment'. Did you mean 'requirement'?
      code    : None
      source  : sysml

  [Error] line 12, col 5 -> line 12, col 15
      message : Unexpected 'requirment'. Expected a SysML keyword (package, part, attribute, action, etc.)
      code    : None
      source  : sysml
```

Each diagnostic's raw JSON (as returned by the server) was also printed by the driver,
e.g.:
```json
{"severity": 1, "range": {"start": {"line": 11, "character": 4}, "end": {"line": 11, "character": 14}}, "message": "Unknown keyword 'requirment'. Did you mean 'requirement'?", "source": "sysml"}
```

### Part 2 — Mermaid diagram (MCP)

```
MCP server: sysml-v2 v0.1.4
tools/call returned 2 content block(s).

MERMAID DIAGRAM (title: General View showing 5 elements with specialisation and containment relationships)
%%{init: {"theme": "base", "themeVariables": {"primaryColor": "#e8f5e9", "lineColor": "#546E7A"}}}%%
classDiagram
    class BrakingSystem {
        <<Package>>
        +vehicle : Vehicle
    }
    class Vehicle {
        <<PartDef>>
        +stoppingDistance : ISQ::LengthValue
    }
    class vehicle {
        <<Part>>
        +stoppingDistance : stoppingDistance
    }
    class StoppingDistanceRequirement
    <<RequirementDef>> StoppingDistanceRequirement
    class veh
    <<Subject>> veh
    BrakingSystem *-- Vehicle : contains
    Vehicle <|-- vehicle : specializes
    BrakingSystem *-- vehicle : contains
    BrakingSystem *-- StoppingDistanceRequirement : contains
    Vehicle <|-- veh : specializes
    classDef PackageStyle fill:#e8f4f8,stroke:#2196F3,stroke-width:2px,color:#1565C0
    classDef PartDefStyle fill:#e8f5e9,stroke:#4CAF50,stroke-width:2px,color:#2E7D32
    classDef PartStyle fill:#f1f8e9,stroke:#8BC34A,stroke-width:2px,color:#33691E
    classDef RequirementDefStyle fill:#fff9c4,stroke:#F9A825,stroke-width:2px,color:#F57F17
    classDef SubjectStyle fill:#f5f5f5,stroke:#9E9E9E,stroke-width:2px,color:#212121
    cssClass "BrakingSystem" PackageStyle
    cssClass "Vehicle" PartDefStyle
    cssClass "vehicle" PartStyle
    cssClass "StoppingDistanceRequirement" RequirementDefStyle
    cssClass "veh" SubjectStyle
```

Full raw run captured with `python run_spike.py > output.txt` for reference during
integration planning.
