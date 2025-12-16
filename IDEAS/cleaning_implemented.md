# House Cleaning (Implemented)

## Core Logic (Cleaning 2.0)
- [x] **Sessions**: Persistent cleaning states stored in `cleaning_sessions` DB table. Pause/Resume supported.
- [x] **Multi-Zone**: Select multiple zones (Kitchen, Bath, etc.) to generate a combined flow.
- [x] **Smart Flow**:
    1.  **Prep**: Soak dishes/toilets (if Deep mode).
    2.  **Global Base**: Trash run, Tidy up.
    3.  **Zones**: Specific tasks per zone.
    4.  **Floors**: Global vacuum/mop phase.
    5.  **Finish**: Final touches.
- [x] **Modes**: Maintenance (Speed) vs Deep (Thorough).

## Integration
- [x] **XP System**: Points awarded per step completion.
- [x] **Menu**: Accessible via "House" -> "Cleaning Now".
