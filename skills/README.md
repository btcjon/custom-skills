# Skill layout

One package per directory:

```
skills/
  <category>/
    <skill-name>/
      SKILL.md
      scripts/
      references/
      tests/
```

Current categories:

- `discovery/` — finding, ranking, and suggesting skills

Rules:

- `SKILL.md` `name:` must match the directory name.
- Do not nest two skill packages in one folder.
- Keep receipts and transcripts out of git.
