# Domain Docs

This repository uses a single-context domain documentation layout.

## Before exploring

- Read root `CONTEXT.md` and use its domain vocabulary.
- Read ADRs under `docs/adr/` that touch the area being changed.
- If either source is absent, proceed without creating it preemptively.

## Consumer rules

- Use the glossary's defined names in issues, tests, implementation, and reviews.
- Avoid synonyms that `CONTEXT.md` explicitly rejects.
- If a needed concept is missing, first determine whether the proposed language is unnecessary. Add domain documentation only for a genuine new concept.
- Surface conflicts with an existing ADR explicitly instead of silently overriding it.

## Layout

```text
/
├── CONTEXT.md
├── docs/
│   └── adr/
└── packages/
```
