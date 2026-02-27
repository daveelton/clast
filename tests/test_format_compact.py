"""Tests for the compact output format module."""

from dataclasses import dataclass
from clang_ast_mcp.db import Symbol
from clang_ast_mcp.format_compact import (
    build_declaration,
    format_symbol,
    format_symbol_list,
    format_search,
    format_outline_class,
    format_outline_file,
    format_references,
    format_hierarchy,
    format_index,
    format_status,
    format_error,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _sym(**overrides) -> Symbol:
    """Create a Symbol with sensible defaults, overriding as needed."""
    defaults = dict(
        usr="c:@S@MyPlugin@F@processBlock#",
        name="processBlock",
        qualified_name="MyPlugin::processBlock",
        kind="method",
        signature="processBlock(AudioBuffer<float> &, MidiBuffer &)",
        return_type="void",
        parameters=[
            {"name": "buffer", "type": "AudioBuffer<float> &"},
            {"name": "midiMessages", "type": "MidiBuffer &"},
        ],
        attributes=["override"],
        parent_usr="c:@S@MyPlugin",
        parent_name="MyPlugin",
        file="src/PluginProcessor.cpp",
        line_start=142,
        line_end=198,
        doc="/// Main audio callback - runs on audio thread",
        body="void MyPlugin::processBlock(...) {\n    ...\n}",
        bases=[],
        base_names=[],
    )
    defaults.update(overrides)
    return Symbol(**defaults)


@dataclass
class FakeSearchResult:
    """Mimics the SearchResult from SymbolSearch."""
    symbol: Symbol
    score: float


# ── build_declaration ────────────────────────────────────────────────


class TestBuildDeclaration:

    def test_method(self):
        sym = _sym()
        result = build_declaration(sym)
        assert result == "void MyPlugin::processBlock(AudioBuffer<float> & buffer, MidiBuffer & midiMessages) override"

    def test_method_no_param_names(self):
        sym = _sym()
        result = build_declaration(sym, include_param_names=False)
        assert result == "void MyPlugin::processBlock(AudioBuffer<float> &, MidiBuffer &) override"

    def test_constructor(self):
        sym = _sym(kind="constructor", qualified_name="MyPlugin::MyPlugin",
                   return_type="", parameters=[], attributes=[])
        result = build_declaration(sym)
        assert result == "MyPlugin::MyPlugin()"

    def test_destructor(self):
        sym = _sym(kind="destructor", qualified_name="MyPlugin::~MyPlugin",
                   return_type="", parameters=[], attributes=[])
        result = build_declaration(sym)
        assert result == "MyPlugin::~MyPlugin()"

    def test_class_with_bases(self):
        sym = _sym(kind="class", qualified_name="MyPlugin",
                   signature="class MyPlugin : public juce::AudioProcessor",
                   base_names=["public juce::AudioProcessor", "public juce::APVTS::Listener"],
                   return_type="", parameters=[], attributes=[])
        result = build_declaration(sym)
        assert result == "class MyPlugin : public juce::AudioProcessor, public juce::APVTS::Listener"

    def test_class_no_bases(self):
        sym = _sym(kind="class", qualified_name="SimpleClass",
                   signature="class SimpleClass", base_names=[],
                   return_type="", parameters=[], attributes=[])
        result = build_declaration(sym)
        assert result == "class SimpleClass"

    def test_struct(self):
        sym = _sym(kind="struct", qualified_name="Vec2",
                   signature="struct Vec2", base_names=[],
                   return_type="", parameters=[], attributes=[])
        result = build_declaration(sym)
        assert result == "struct Vec2"

    def test_enum(self):
        sym = _sym(kind="enum", qualified_name="FilterType",
                   signature="enum FilterType",
                   return_type="", parameters=[], attributes=[])
        result = build_declaration(sym)
        assert result == "enum FilterType"

    def test_field_with_signature(self):
        sym = _sym(kind="field", qualified_name="MyPlugin::apvts",
                   signature="AudioProcessorValueTreeState MyPlugin::apvts",
                   return_type="AudioProcessorValueTreeState",
                   parameters=[], attributes=[])
        result = build_declaration(sym)
        assert result == "AudioProcessorValueTreeState MyPlugin::apvts"

    def test_namespace(self):
        sym = _sym(kind="namespace", qualified_name="DSP",
                   signature="namespace DSP",
                   return_type="", parameters=[], attributes=[])
        result = build_declaration(sym)
        assert result == "namespace DSP"

    def test_typedef(self):
        sym = _sym(kind="typedef", qualified_name="SampleType",
                   signature="using SampleType = float",
                   return_type="", parameters=[], attributes=[])
        result = build_declaration(sym)
        assert result == "using SampleType = float"

    def test_function_no_params(self):
        sym = _sym(kind="function", qualified_name="main",
                   return_type="int", parameters=[], attributes=[])
        result = build_declaration(sym)
        assert result == "int main()"

    def test_method_multiple_attributes(self):
        sym = _sym(attributes=["const", "noexcept", "override"])
        result = build_declaration(sym)
        assert result.endswith("const noexcept override")


# ── format_symbol ────────────────────────────────────────────────────


class TestFormatSymbol:

    def test_with_body(self):
        sym = _sym()
        result = format_symbol(sym, include_body=True)
        assert result.startswith("src/PluginProcessor.cpp:142\n")
        assert "/// Main audio callback" in result
        assert "void MyPlugin::processBlock(...)" in result
        # Should NOT have "(56 lines)" when body is present
        assert "lines)" not in result.split("\n")[0]

    def test_without_body(self):
        sym = _sym()
        result = format_symbol(sym, include_body=False)
        assert "(57 lines)" in result.split("\n")[0]
        assert result.rstrip().endswith(";")

    def test_class_without_body(self):
        sym = _sym(kind="class", qualified_name="MyPlugin",
                   base_names=["public Base"],
                   body="", doc="")
        result = format_symbol(sym, include_body=False)
        assert "{ ... }" in result

    def test_class_with_body(self):
        sym = _sym(kind="class", qualified_name="MyPlugin",
                   base_names=["public Base"],
                   body="class MyPlugin : public Base {\n    <body>\n};",
                   doc="/// A class")
        result = format_symbol(sym, include_body=True)
        assert "<body>" in result
        assert "/// A class" in result

    def test_no_doc(self):
        sym = _sym(doc="")
        result = format_symbol(sym, include_body=True)
        lines = result.split("\n")
        # Second line should be the body, not doc
        assert not lines[1].startswith("///")


# ── format_symbol_list ───────────────────────────────────────────────


class TestFormatSymbolList:

    def test_multiple_matches(self):
        syms = [
            _sym(qualified_name="A::processBlock", file="a.cpp", line_start=10, line_end=30),
            _sym(qualified_name="B::processBlock", file="b.cpp", line_start=50, line_end=80),
        ]
        result = format_symbol_list(syms, "processBlock")
        assert result.startswith("2 matches:")
        assert "a.cpp:10" in result
        assert "b.cpp:50" in result


# ── format_search ────────────────────────────────────────────────────


class TestFormatSearch:

    def test_no_results(self):
        result = format_search("nonexistent", [])
        assert 'No matches for "nonexistent"' in result

    def test_with_results(self):
        sym = _sym()
        results = [FakeSearchResult(symbol=sym, score=12.5)]
        result = format_search("process", results)
        assert "src/PluginProcessor.cpp:142" in result
        assert "MyPlugin::processBlock" in result
        assert "/// Main audio callback" in result


# ── format_outline_class ─────────────────────────────────────────────


class TestFormatOutlineClass:

    def test_class_outline(self):
        cls = _sym(kind="class", qualified_name="MyPlugin",
                   base_names=["public juce::AudioProcessor"],
                   doc="/// Main processor")
        members = [
            _sym(kind="method", qualified_name="prepareToPlay",
                 name="prepareToPlay",
                 signature="prepareToPlay(double, int)",
                 return_type="void",
                 parameters=[{"name": "sampleRate", "type": "double"},
                             {"name": "samplesPerBlock", "type": "int"}],
                 attributes=["override"], doc=""),
            _sym(kind="field", qualified_name="MyPlugin::apvts",
                 name="apvts",
                 signature="AudioProcessorValueTreeState apvts",
                 return_type="AudioProcessorValueTreeState",
                 parameters=[], attributes=[], doc=""),
        ]
        result = format_outline_class(cls, members)
        assert "class MyPlugin" in result
        assert "void prepareToPlay(double sampleRate, int samplesPerBlock) override;" in result
        assert "AudioProcessorValueTreeState apvts;" in result
        assert result.rstrip().endswith("};")


# ── format_outline_file ──────────────────────────────────────────────


class TestFormatOutlineFile:

    def test_file_outline(self):
        syms = [
            _sym(kind="class", qualified_name="MyPlugin",
                 line_start=12, line_end=86,
                 base_names=["public juce::AudioProcessor"],
                 doc="/// Main processor"),
            _sym(kind="function", qualified_name="helperFunction",
                 line_start=90, line_end=104,
                 return_type="void",
                 parameters=[{"name": "n", "type": "int"}],
                 attributes=[], doc=""),
        ]
        result = format_outline_file("src/PluginProcessor.h", syms)
        assert result.startswith("src/PluginProcessor.h")
        assert "/// Main processor" in result
        assert "(75 lines)" in result


# ── format_references ────────────────────────────────────────────────


class TestFormatReferences:

    def test_basic_refs(self):
        sym = _sym(qualified_name="MyPlugin::parameterChanged")
        refs = [
            {"caller": "MyPlugin::loadPreset", "file": "src/Presets.cpp", "line": 87,
             "context": "    parameterChanged(paramId, newValue);"},
        ]
        result = format_references(sym, refs, 1)
        assert "1 references" in result
        assert "MyPlugin::loadPreset" in result
        assert "src/Presets.cpp:87" in result

    def test_with_expanded_context(self):
        sym = _sym(qualified_name="MyPlugin::parameterChanged")
        refs = [
            {"caller": "loadPreset", "file": "src/Presets.cpp", "line": 87,
             "context": "> 87 |     parameterChanged(paramId, newValue);"},
        ]
        result = format_references(sym, refs, 1)
        assert "> 87 |" in result


# ── format_hierarchy ─────────────────────────────────────────────────


class TestFormatHierarchy:

    def test_with_bases_and_derived(self):
        cls = _sym(kind="class", qualified_name="MyPlugin",
                   file="src/PluginProcessor.h")
        bases = [
            {"name": "juce::AudioProcessor", "file": "juce_AudioProcessor.h", "line": 44},
        ]
        derived = [
            {"name": "MyPluginWithSidechain", "file": "src/SidechainVariant.h", "line": 8},
        ]
        result = format_hierarchy(cls, bases, derived)
        assert "MyPlugin  src/PluginProcessor.h" in result
        assert "bases:" in result
        assert "juce::AudioProcessor  juce_AudioProcessor.h:44" in result
        assert "derived:" in result
        assert "MyPluginWithSidechain  src/SidechainVariant.h:8" in result


# ── format_index / format_status / format_error ──────────────────────


class TestFormatIndex:

    def test_basic(self):
        result = format_index({
            "project_root": "/path/to/project",
            "files_total": 42,
            "files_unchanged": 8,
            "errors": 0,
            "db_stats": {"symbols": 1240, "references": 3800, "files": 50},
        })
        assert "Indexed /path/to/project" in result
        assert "42 files" in result
        assert "8 unchanged" in result
        assert "1240 symbols" in result


class TestFormatStatus:

    def test_basic(self):
        result = format_status({"symbols": 1240, "references": 3800, "files": 50})
        assert result == "1240 symbols, 3800 references, 50 files"


class TestFormatError:

    def test_with_suggestion(self):
        result = format_error("Not found.", "Try searching.")
        assert result == "Not found. Try searching."

    def test_without_suggestion(self):
        result = format_error("Not found.")
        assert result == "Not found."
