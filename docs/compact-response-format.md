# Compact Response Format — Proposal

## Problem

The current JSON response format decomposes C++ declarations into separate fields
(`kind`, `return_type`, `parameters`, `attributes`, `parent`, `signature`), then wraps
them in JSON keys and braces. This burns tokens on structure that C++ syntax already
encodes natively — and that LLMs are heavily trained on.

**Current `ast_get_symbol` response (JSON):**

```json
{
  "symbol": "MyPlugin::processBlock",
  "kind": "method",
  "signature": "processBlock(AudioBuffer<float> &, MidiBuffer &)",
  "file": "src/PluginProcessor.cpp",
  "lines": [142, 198],
  "doc": "/// Main audio callback - runs on audio thread",
  "parent": "MyPlugin",
  "return_type": "void",
  "parameters": [
    { "name": "buffer", "type": "juce::AudioBuffer<float> &" },
    { "name": "midiMessages", "type": "juce::MidiBuffer &" }
  ],
  "attributes": ["override"],
  "body": "void MyPlugin::processBlock(...) {\n    ...\n}"
}
```

~550 characters of metadata overhead before the body even starts. The field names
`"symbol"`, `"kind"`, `"signature"`, `"return_type"`, `"parameters"`, `"attributes"`
are all redundant — a single C++ declaration line already contains all of that.

**Proposed response (plain text):**

```
src/PluginProcessor.cpp:142
/// Main audio callback - runs on audio thread
void MyPlugin::processBlock(AudioBuffer<float> & buffer, MidiBuffer & midiMessages) override
{
    auto numSamples = buffer.getNumSamples();
    ...
}
```

~180 characters of overhead. Same information. The model reads this the same way a
developer reads a file — location, doc comment, declaration, body.

## Format rules

1. **Location header:** `file:line` — the universally recognized format from compiler
   output, grep, and stack traces. When the body is absent, append the size:
   `file:line  (N lines)`. When the body is present, the length is self-evident.
2. **Doc comment:** `///` or `/** */` lines, verbatim. Omitted if none.
3. **Declaration:** A single reconstructed C++ declaration built from stored fields.
   The declaration encodes kind, return type, name, scope, parameters, and attributes
   without any labels.
4. **Body:** The raw source text. Omitted when not requested or not applicable.
5. **No echo of the query.** If the user asked for `processBlock`, don't repeat
   `"symbol": "MyPlugin::processBlock"` — it's in the declaration.

## Declaration reconstruction by kind

| Kind | Declaration format | Example |
|---|---|---|
| function | `{return_type} {qualified_name}({type name, ...}) {attrs}` | `void processAudio(float * buf, int n)` |
| method | `{return_type} {qualified_name}({type name, ...}) {attrs}` | `void MyPlugin::processBlock(AudioBuffer<float> & buffer, MidiBuffer & midiMessages) override` |
| constructor | `{qualified_name}({type name, ...})` | `MyPlugin::MyPlugin()` |
| destructor | `{qualified_name}()` | `MyPlugin::~MyPlugin()` |
| class/struct | `{class\|struct} {qualified_name} : {access base, ...}` | `class MyPlugin : public juce::AudioProcessor` |
| enum | `enum {qualified_name}` | `enum FilterType` |
| field | `{type} {qualified_name}` | `AudioProcessorValueTreeState MyPlugin::apvts` |
| variable | `{type} {qualified_name}` | `static const float defaultGain` |
| namespace | `namespace {qualified_name}` | `namespace DSP` |
| typedef | `{signature}` (stored verbatim) | `using SampleType = float` |

These are all native C++ — the model has seen billions of lines of each.

---

## Tool responses

### ast_get_symbol — single match, with body

```
src/PluginProcessor.cpp:142
/// Main audio callback - runs on audio thread
void MyPlugin::processBlock(AudioBuffer<float> & buffer, MidiBuffer & midiMessages) override
{
    auto numSamples = buffer.getNumSamples();
    auto numChannels = buffer.getNumChannels();

    for (int ch = 0; ch < numChannels; ++ch) {
        auto* data = buffer.getWritePointer(ch);
        for (int i = 0; i < numSamples; ++i) {
            data[i] *= gainSmoothed.getNextValue();
        }
    }
}
```

### ast_get_symbol — single match, without body

```
src/PluginProcessor.cpp:142  (56 lines)
/// Main audio callback - runs on audio thread
void MyPlugin::processBlock(AudioBuffer<float> & buffer, MidiBuffer & midiMessages) override;
```

### ast_get_symbol — class, with body

```
src/PluginProcessor.h:12
/// Main processor for the XYZ plugin
class MyPlugin : public juce::AudioProcessor, public juce::APVTS::Listener {
    <body>
};
```

### ast_get_symbol — class, without body

```
src/PluginProcessor.h:12  (75 lines)
/// Main processor for the XYZ plugin
class MyPlugin : public juce::AudioProcessor, public juce::APVTS::Listener { ... }
```

### ast_get_symbol — field

```
src/PluginProcessor.h:45
AudioProcessorValueTreeState MyPlugin::apvts;
```

### ast_get_symbol — multiple matches

No body in the listing — each match uses the same format as "without body".

```
3 matches:

src/PluginProcessor.cpp:142  (56 lines)
void MyPlugin::processBlock(AudioBuffer<float> & buffer, MidiBuffer & midiMessages) override

src/SidechainVariant.cpp:55  (34 lines)
void MyPluginWithSidechain::processBlock(AudioBuffer<float> & buffer, MidiBuffer & midiMessages) override

src/Synth.cpp:200  (80 lines)
void SynthProcessor::processBlock(AudioBuffer<float> & buffer, MidiBuffer & midiMessages) override
```

### ast_get_symbol — not found

```
Symbol 'processBlock' not found. Try ast_search with keywords.
```

---

### ast_get_outline — class

Looks like a header file. Members shown as declaration lines with parameter names,
no bodies.

```
src/PluginProcessor.h:12  (75 lines)
/// Main processor for the XYZ plugin
class MyPlugin : public juce::AudioProcessor, public juce::APVTS::Listener {
  void prepareToPlay(double sampleRate, int samplesPerBlock) override;
  /// Main audio callback
  void processBlock(AudioBuffer<float> & buffer, MidiBuffer & midiMessages) override;
  void parameterChanged(const String & parameterID, float newValue) override;
  float getParameterValue(int paramIdx) const override;
  AudioProcessorValueTreeState apvts;
};
```

### ast_get_outline — file

```
src/PluginProcessor.h

12  /// Main processor for the XYZ plugin
    class MyPlugin : public juce::AudioProcessor, public juce::APVTS::Listener  (75 lines)
90  void helperFunction(int n)  (15 lines)
108 static const float DEFAULT_GAIN
```

Line numbers as prefix (matching Read tool conventions). Doc comments before
declarations (matching source order). Line count for multi-line symbols.

---

### ast_get_references

```
MyPlugin::parameterChanged — 2 references

MyPlugin::loadPreset  src/Presets.cpp:87
    parameterChanged(paramId, newValue);

HostCallback::notify  src/HostSync.cpp:34
    processor.parameterChanged(id, val);
```

With `context_lines=3`:

```
MyPlugin::parameterChanged — 2 references

MyPlugin::loadPreset  src/Presets.cpp:87
   85 |     auto paramId = params[i].id;
   86 |     auto newValue = params[i].value;
 > 87 |     parameterChanged(paramId, newValue);
   88 |     updateUI(paramId);

HostCallback::notify  src/HostSync.cpp:34
   32 |     for (auto& [id, val] : changes) {
   33 |         if (val != lastValues[id]) {
 > 34 |             processor.parameterChanged(id, val);
   35 |             lastValues[id] = val;
   36 |         }
```

---

### ast_get_hierarchy

```
MyPlugin  src/PluginProcessor.h
  bases:
    juce::AudioProcessor  juce_AudioProcessor.h:44
    juce::APVTS::Listener  juce_AudioProcessorValueTreeState.h:210
  derived:
    MyPluginWithSidechain  src/SidechainVariant.h:8
```

---

### ast_search

Results sorted by relevance. Each result: location, qualified name, line count on
one line. Declaration and doc snippet indented below. No `"kind"` field needed — the
declaration syntax shows the kind.

```
src/DSP/SmoothedParameter.cpp:45  SmoothedParameter::process  (27 lines)
  float process(int numSamples)
  /// Applies exponential smoothing to parameter value over the given block size

src/PluginProcessor.cpp:142  MyPlugin::processBlock  (56 lines)
  void processBlock(AudioBuffer<float> &, MidiBuffer &) override
  /// Main audio callback - applies smoothed gain and filter cutoff
```

No results:

```
No matches for "xyz nonexistent query".
```

---

### ast_index

```
Indexed /path/to/project: 42 files (8 unchanged, 0 errors), 1240 symbols, 3800 references
```

### ast_status

```
1240 symbols, 3800 references, 50 files
```

---

## Token savings estimate

| Tool                                | JSON (chars) | Compact (chars) | Saved per call |
|-------------------------------------|--------------|-----------------|----------------|
| ast_get_symbol (1 match)            | ~550         | ~120            | ~108 tokens    |
| ast_get_outline (class, 10 members) | ~800         | ~250            | ~138 tokens    |
| ast_get_references (5 refs)         | ~600         | ~200            | ~100 tokens    |
| ast_search (10 results)             | ~2000        | ~600            | ~350 tokens    |

Over a typical session with ~25 AST tool calls, this is roughly **3,000–5,000 tokens
saved** — and more importantly, the responses are in a format the model has been
trained on, which should improve comprehension.

## Supporting research

- **"How Good Are LLMs at Processing Tool Outputs?"** (Kate et al., Oct 2025) —
  Evaluated 15 LLMs on processing JSON responses from REST APIs. Even frontier models
  struggled: GPT-4o reached only 77% accuracy on a JSON processing benchmark.
  Performance degraded substantially with response length, and models exhibited recency
  bias within JSON structures. Providing JSON schema helped (up to +12%), but generating
  Python code to parse JSON outperformed direct JSON comprehension by 3–50% on complex
  tasks. These findings suggest that returning tool results as structured JSON forces
  the model into a task it's measurably worse at than reading natural text.
  https://arxiv.org/abs/2510.15955

- **"Let Me Speak Freely? A Study on the Impact of Format Restrictions on Performance
  of Large Language Models"** (Tam et al., Jul 2024) — Studied whether constraining
  LLM output to JSON/XML/YAML degrades reasoning. Found that format restrictions
  significantly harmed reasoning-intensive tasks, and that rigid schemas performed worse
  than loose format instructions. While this paper studies output constraints rather than
  input format, the underlying mechanism is relevant: JSON structure competes with
  reasoning for the model's attention budget. A tool response that the model must first
  parse out of JSON before it can reason about the code content adds unnecessary
  cognitive overhead.
  https://arxiv.org/abs/2408.02442