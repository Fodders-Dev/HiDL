# UX Findings - Sprint 7 Dogfooding

## Bugs Found

### 1. Scenario Button Mismatch (cleaning_quick)
- **Issue:** Scenario expects inline buttons like `⚡ По верхам (15 мин)` but actual buttons are `⚡ По верхам 15 мин` (no parentheses).
- **Severity:** Low (scenario issue, not bot issue)
- **Fix:** Update `sim_scenarios.py` to match actual button labels.

### 2. Onboarding doesn't show FSM prompts
- **Issue:** After `/start` for new user, bot shows main menu immediately instead of registration flow.
- **Observation:** Likely because `EnsureUserMiddleware` auto-creates user before handler runs.
- **Severity:** Medium (UX confusion - user isn't asked for name/timezone)
- **Fix:** Review `EnsureUserMiddleware` and `handlers/start.py` registration flow.

## UX Friction Points

### 1. Main Menu Too Crowded
- 10 reply buttons on main menu is overwhelming
- Consider grouping into sub-menus or using inline buttons

### 2. "Дом" submenu structure
- Entry to "Дом" shows another menu. User has to click twice to start cleaning.
- Consider: Show quick actions directly on entry.

## Ideas for Improvement

### 1. Knowledge Base Structure (Priority)
- Create `data/knowledge/` directory with JSON files:
  - `cleaning_tips.json` - советы по уборке
  - `recipes.json` - рецепты
  - `affirmations.json` - поддержка
  - `self_care.json` - уход за собой
  - `home_repair.json` - ремонт

### 2. Natural Language in /ask_mom
- Currently `/ask_mom` is a stub
- Should route queries through KnowledgeService to find relevant tips

### 3. Contextual Help
- When user is in certain flow (e.g., cooking), offer relevant tips proactively
