# Clang AST MCP Server — Requirements Specification

## Objective

Reduce Claude Code's token consumption when exploring C++ codebases by providing an MCP server backed by a Clang-based AST index. Instead of Claude discovering code through repeated glob, grep, and full-file reads, the server delivers precisely scoped chunks — function bodies, class outlines, call sites — in response to structured queries.

## Problem: How Claude Code Explores Code Today

When Claude Code needs to understand a codebase, it runs an iterative discovery loop:

1. **Glob** — list files matching a pattern. Tokens spent on the file listing.
2. **Grep** — search for keywords across files. Tokens spent on match context.
3. **Read** — open 3–8 full files to locate the relevant section. This is where 80%+ of tokens are consumed.
4. **Reason** — use the ~5% of retrieved code that was actually relevant to the query.

Each step is a separate tool call with a full round-trip through the context window. A typical question like "where is X implemented and how does it work" can burn 50–100k tokens across 6–10 tool calls, when the answer lives in 200 lines.

This cost compounds: in a typical coding session Claude may perform 20–30 context lookups.

## Query Patterns

The following are the core query types the server must support. They fall into two categories: **targeted lookups** (queries 1–4, 6) where Claude already knows the symbol name and needs its details, and **exploratory queries** (queries 5, 7) where Claude is discovering which symbols are relevant. Targeted lookups are individually cheap but frequent. Exploratory queries are where the bulk of token waste occurs — the grepai benchmark showed a single exploratory question spawning 5 subagents and 139 tool calls as Claude iterates through grep → read → grep → read cycles.

### 1. Find a symbol definition

Claude needs to locate where a function, class, or variable is defined.

**Today:** grep for the symbol name, read 3+ files that mention it, identify which is the definition.

**Required response:** The single chunk containing the definition — full signature, doc comment, file path, line range, parent scope, and body. No surrounding file content.

**Data flow:**

```
Q: get_symbol("AudioProcessor::processBlock")
```
```json
A: {
  "symbol": "MyPlugin::processBlock",
  "kind": "method",
  "signature": "void processBlock(AudioBuffer<float>&, MidiBuffer&) override",
  "parent": "class MyPlugin : public AudioProcessor",
  "file": "src/PluginProcessor.cpp",
  "lines": [142, 198],
  "body": "<just those 56 lines>",
  "doc": "/// Main audio callback - runs on audio thread"
}
```

### 2. Get a class or file outline

Claude needs to understand a class interface or the structure of a file without reading implementation details.

**Today:** grep for the class name, read the header (and often the .cpp file just to find the header).

**Required response:** Class declaration with all member signatures (no bodies), base classes, and doc comments. For files, a list of top-level declarations with signatures.

**Data flow:**

```
Q: get_outline("MyPlugin")
```
```json
A: {
  "name": "MyPlugin",
  "kind": "class",
  "bases": ["juce::AudioProcessor", "juce::AudioProcessorValueTreeState::Listener"],
  "file": "src/PluginProcessor.h",
  "lines": [12, 87],
  "members": [
    "void prepareToPlay(double, int) override",
    "void processBlock(AudioBuffer<float>&, MidiBuffer&) override",
    "void parameterChanged(const String&, float) override",
    "AudioProcessorValueTreeState apvts"
  ],
  "doc": "/// Main processor for the XYZ plugin"
}
```

### 3. Find references / call sites

Claude needs to know what calls or uses a given symbol.

**Today:** grep for the symbol name across all files, read every match, manually filter false positives (comments, strings, unrelated overloads).

**Required response:** A list of call/usage sites, each with the enclosing function name, file path, line number, and a one-line context snippet. Must resolve symbols precisely (no false positives from string matching).

**Data flow:**

```
Q: get_references("MyPlugin::parameterChanged")
```
```json
A: {
  "symbol": "MyPlugin::parameterChanged",
  "references": [
    {
      "caller": "MyPlugin::loadPreset",
      "file": "src/Presets.cpp",
      "line": 87,
      "context": "parameterChanged(paramId, newValue);"
    },
    {
      "caller": "HostCallback::notify",
      "file": "src/HostSync.cpp",
      "line": 34,
      "context": "processor.parameterChanged(id, val);"
    }
  ]
}
```

### 4. Get a type hierarchy

Claude needs to understand inheritance relationships to write correct overrides or understand polymorphic behaviour.

**Today:** Claude infers hierarchy from includes and base class names, often requiring multiple file reads to trace the chain. Frequently gets it wrong.

**Required response:** Direct inheritance tree — base classes (upward) and known subclasses (downward) — with file locations for each.

**Data flow:**

```
Q: get_hierarchy("MyPlugin")
```
```json
A: {
  "symbol": "MyPlugin",
  "bases": [
    { "name": "juce::AudioProcessor", "file": "juce_AudioProcessor.h", "line": 44 },
    { "name": "juce::AudioProcessorValueTreeState::Listener", "file": "juce_AudioProcessorValueTreeState.h", "line": 210 }
  ],
  "derived": [
    { "name": "MyPluginWithSidechain", "file": "src/SidechainVariant.h", "line": 8 }
  ]
}
```

### 5. Keyword search across symbols and documentation

Claude needs to find code related to a feature or concept when it doesn't know the exact symbol name.

**Today:** grep for keywords, read files, grep more, read more files — an expensive exploration spiral.

**Required response:** A ranked list of matching chunks (scored by relevance), each with symbol name, signature, file path, and a short snippet. Search corpus includes symbol names, doc comments, and string literals.

**Data flow:**

```
Q: search("parameter smoothing audio callback")
```
```json
A: {
  "query": "parameter smoothing audio callback",
  "results": [
    {
      "symbol": "SmoothedParameter::process",
      "signature": "float process(int numSamples)",
      "file": "src/DSP/SmoothedParameter.cpp",
      "lines": [45, 72],
      "snippet": "/// Applies exponential smoothing to parameter value over the given block size",
      "score": 0.91
    },
    {
      "symbol": "MyPlugin::processBlock",
      "signature": "void processBlock(AudioBuffer<float>&, MidiBuffer&) override",
      "file": "src/PluginProcessor.cpp",
      "lines": [142, 198],
      "snippet": "/// Main audio callback - applies smoothed gain and filter cutoff",
      "score": 0.74
    }
  ]
}
```

### 6. Get a resolved function signature

Claude needs to know what a function accepts and returns, with types fully resolved (no unresolved typedefs or aliases).

**Today:** Read the header or source file.

**Required response:** The fully resolved signature, including parameter names and types, return type, and any relevant attributes (override, const, noexcept).

**Data flow:**

```
Q: get_signature("MyPlugin::processBlock")
```
```json
A: {
  "symbol": "MyPlugin::processBlock",
  "resolved_signature": "void processBlock(juce::AudioBuffer<float>& buffer, juce::MidiBuffer& midiMessages) override",
  "return_type": "void",
  "parameters": [
    { "name": "buffer", "type": "juce::AudioBuffer<float>&" },
    { "name": "midiMessages", "type": "juce::MidiBuffer&" }
  ],
  "attributes": ["override"],
  "file": "src/PluginProcessor.h",
  "line": 28
}
```

### 7. Behavioural exploration

Claude needs to find code related to a described behaviour or feature when it doesn't know any of the involved symbol names. These are the highest-cost queries — they describe what the code *does*, not what it's *called*, and typically require tracing across multiple classes and files.

**Today:** Claude spawns an Explore subagent which runs repeated cycles of Grep → Read → Grep → Read, broadening its search each iteration. The grepai benchmark recorded 139 tool calls across 5 subagents for five questions of this type.

**Example questions (from the grepai benchmark against Excalidraw, 155k lines):**

- "Locate the exact mathematical function used to determine if a user's cursor is hovering inside a 'diamond' shape."
- "Explain how the application calculates the intersection point when an arrow is attached to an ellipse."
- "Find the algorithm responsible for simplifying or smoothing the points of a 'freedraw' line after the user releases the mouse."
- "Identify the code responsible for snapping dragged elements to the grid."
- "How does the codebase handle sending an element 'backward' in the z-order?"

**Equivalent C++/JUCE questions:**

- "How does the plugin handle parameter smoothing in the audio callback?"
- "Where is the filter coefficient recalculation triggered when a parameter changes?"
- "How does the preset system save and restore plugin state?"
- "Find where the sidechain input is routed to the compressor."
- "How does the UI update when a parameter changes via host automation?"

**Required response:** A ranked set of relevant symbols with enough context to answer the question without further file reads. Unlike query 5 (keyword search), these questions are natural language describing behaviour that may span multiple classes and a call chain.

**Data flow:**

```
Q: search("how does the UI update when a parameter changes via host automation")
```
```json
A: {
  "query": "how does the UI update when a parameter changes via host automation",
  "results": [
    {
      "symbol": "MyPlugin::parameterChanged",
      "signature": "void parameterChanged(const String& parameterID, float newValue) override",
      "file": "src/PluginProcessor.cpp",
      "lines": [210, 225],
      "snippet": "/// APVTS listener callback - forwards parameter changes to the editor",
      "score": 0.93
    },
    {
      "symbol": "EditorComponent::updateFromProcessor",
      "signature": "void updateFromProcessor()",
      "file": "src/EditorComponent.cpp",
      "lines": [88, 134],
      "snippet": "/// Reads current parameter values and updates all slider/button positions",
      "score": 0.87
    },
    {
      "symbol": "ParameterAttachment::setValue",
      "signature": "void setValue(float newValue, juce::NotificationType notification)",
      "file": "src/ParameterAttachment.cpp",
      "lines": [45, 62],
      "snippet": "/// Propagates value change to the attached Component on the message thread",
      "score": 0.79
    }
  ]
}
```

## Design Principles

- **Chunks, not files.** Every response delivers the minimum code needed to answer the query. Claude can request a full body if an outline isn't sufficient, but the default is minimal.
- **Precision over recall.** False positives in reference lookups waste tokens. Clang's semantic resolution (USRs) is preferred over text matching.
- **Incremental.** Re-indexing the full codebase on every change is not acceptable. Only changed files should be re-parsed.

## Target Codebase Profile

- ~300,000 lines of project source across ~1,600 classes
- ~1,000,000 lines of third-party library code (primarily JUCE), of which public headers should be indexed
- Build system: CMake with `compile_commands.json` available

## Further Reading

- **grepai benchmark: grepai vs grep on Claude Code** — Controlled benchmark on the Excalidraw codebase (155k lines) showing 55% fewer tool calls and 27.5% cost reduction with semantic search. Includes real session traces from Claude Code's JSON logs. https://yoanbernabeu.github.io/grepai/blog/benchmark-grepai-vs-grep-claude-code/
- **Claude Code built-in tools reference** — Documented tool interfaces (Glob, Grep, Read) and the Explore subagent's system prompt, confirming the Glob → Grep → Read discovery loop. https://www.vtrivedy.com/posts/claudecode-tools-reference
- **Claude Code internal tools and prompts (reverse-engineered)** — Full tool schemas and subagent prompts extracted from Claude Code, including the Explore agent's read-only constraint and tool selection heuristics. https://gist.github.com/bgauryy/0cdb9aa337d01ae5bd0c803943aa36bd
- **Tracing Claude Code's LLM traffic** — Detailed walkthrough of intercepting Claude Code's actual API calls, showing the main agent / subagent interaction pattern and tool call sequences. https://medium.com/@georgesung/tracing-claude-codes-llm-traffic-agentic-loop-sub-agents-tool-use-prompts-7796941806f5
- **Claude Context (Zilliz)** — Open-source MCP server using AST-based chunking and vector search for codebase indexing. Supports C++ via tree-sitter. Reports ~40% token reduction. https://github.com/zilliztech/claude-context
- **Milvus blog: "Why I'm Against Claude Code's Grep-Only Retrieval"** — Analysis of Claude Code's grep-based exploration cost, with side-by-side comparison against semantic code search. https://milvus.io/blog/why-im-against-claude-codes-grep-only-retrieval-it-just-burns-too-many-tokens.md