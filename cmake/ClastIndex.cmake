# ClastIndex.cmake — CMake integration for clast (Clang AST MCP Server)
#
# Provides an `ast-index` build target that runs the clast indexer after
# your main target has built. This ensures generated headers (e.g. JUCE's
# JuceHeader.h) exist before indexing.
#
# By default the target is part of ALL, so the index is updated on every
# build. The indexer is incremental (content-hash based), so no-change
# builds add negligible overhead.
#
# Usage:
#   include(clast/cmake/ClastIndex.cmake)
#   add_clast_index(YourMainTarget)
#
# To exclude from ALL and run manually instead:
#   add_clast_index(YourMainTarget EXCLUDE_FROM_ALL)
#   cmake --build cmake-build-debug --target ast-index

function(add_clast_index DEPENDS_TARGET)
    set(CLAST_DIR "${CMAKE_CURRENT_SOURCE_DIR}/clast")
    set(CLAST_MCP "${CLAST_DIR}/.venv/bin/clang-ast-mcp")
    set(CLAST_DB  "${CLAST_DIR}/.ast-index.db")

    if(NOT EXISTS "${CLAST_MCP}")
        message(STATUS "clast: .venv not found — run clast/bootstrap.sh first. Skipping ast-index target.")
        return()
    endif()

    set(_CLAST_ALL ALL)
    if("EXCLUDE_FROM_ALL" IN_LIST ARGN)
        set(_CLAST_ALL "")
    endif()

    add_custom_target(ast-index ${_CLAST_ALL}
        COMMAND "${CLAST_MCP}" index "${CMAKE_SOURCE_DIR}"
            --compile-commands "${CMAKE_BINARY_DIR}"
            --db "${CLAST_DB}"
        DEPENDS ${DEPENDS_TARGET}
        COMMENT "Updating clast AST index"
        USES_TERMINAL
        VERBATIM
    )
endfunction()