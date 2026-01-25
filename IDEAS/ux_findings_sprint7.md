# UX Findings - Sprint 7 Dogfooding

## Bugs Found

### 1. Pantry add parsed as expense during FSM
- **Issue:** ввод количества при добавлении продукта перехватывался парсером трат.
- **Fix:** парсеры “естественного” текста и трат теперь не срабатывают, когда активен FSM.

## UX Friction Points

### 1. Симулятор отставал от реального UI
- **Action:** обновлены сценарии в `tools/sim_scenarios.py`, добавлены сценарии кафе и тихого режима.
