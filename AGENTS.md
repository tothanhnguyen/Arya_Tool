# Repository Instructions

## Protect `.env`

- The `.env` file is user-owned and may contain secrets.
- No agent may create, edit, overwrite, regenerate, restore, copy into, or
  otherwise modify `.env` unless the user explicitly requests that exact
  change.
- Do not run setup commands or wizards that can write `.env` without the
  user's explicit instruction.
- When checking configuration, never print secret values. Report only whether
  required variables are set, empty, or invalid.
- Changes to `.env.example` do not authorize corresponding changes to `.env`.
- If modifying `.env` becomes necessary, stop and ask the user first.
