# clast — development notes

## Versioned CLAUDE.md instructions for consumer projects

`CLAUDE-CLAST-ADDITION.md` contains the text that `bootstrap.sh` inserts into
the parent project's CLAUDE.md. It is wrapped in version markers:

```
<!-- clast-instructions v2 -->
...
<!-- /clast-instructions -->
```

When you change `CLAUDE-CLAST-ADDITION.md`:
1. Bump the version in the opening marker (e.g. `v2` → `v3`)
2. Bump `CLAST_VERSION` in `bootstrap.sh` to match
3. `bootstrap.sh` will then detect outdated instructions and offer to replace them