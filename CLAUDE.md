# Team workflow

Main session (Opus 5.5) is the Coordinator. Agents live in `.claude/agents/`:
planner (Fable 5.2), implementer + reviewer (Opus 5.5), verifier + grep + search (Haiku 5.5).

Handoff protocol:
1. planner issues scoped tasks with acceptance criteria.
2. implementer returns changed files and decision notes.
3. reviewer flags issues with actionable fixes.
4. verifier confirms behavior with explicit checks.
5. Coordinator resolves conflicts and publishes final output.

Repo: https://github.com/lampn0/PDF_Editor.git
