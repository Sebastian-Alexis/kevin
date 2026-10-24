# Interaction edge cases on the MacBook (2026-10-24)

10 cases chosen to stress the executor rather than the planner: drags (resize handle shrink/grow, slider, text selection), precise scrolling, right-click menus, double-click selection, keyboard-driven settings, modifier-click, hidden menu bar in full screen. Graded from the saved frames.

| case | result | turns / s | note |
| --- | --- | --- | --- |
| drag-shrink | pass | 9 / 58.6 | one horizontal drag on the right-edge handle; object shrank proportionally |
| drag-grow | pass | 3 / 14.7 | one drag |
| slider | pass | 7 / 38.2 | no readout on that page; thumb at ~79% by geometry |
| textsel | pass | 16 / 65.4 | first drag missed by ~4 px at 13-px text, second selected 'brown' exactly; bold applied |
| scroll-precise | pass | 4 / 31.7 | cmd+f jump |
| right-click | pass | 4 / 30.0 | Back / Forward / Reload; Esc closed it |
| dblclick | pass | 4 / 18.8 | 'originally' highlighted |
| keyboard-nav | pass | 5 / 30.9 | read without changing |
| modifier-click | pass | 14 / 86.9 → 6 / 21.5 | FIRST PASS FAILED: cmd on a click was dropped (link opened in the same tab, 14 turns); fixed: modifiers held across the click on the Pi; rerun 6 turns |
| hidden-menubar | pass | 3 / 26.7 | menu bar revealed at the top edge |

Executor fixes from this battery: drag = hover, press, hold 120 ms, frame-paced moves, dwell, release (canvas games saw a click before); `left_click_drag` requires an explicit `start_coordinate`; `left_mouse_down`/`left_mouse_up` for held drags; modifier-held clicks (`text: "cmd"` on a click); `zoom` settles first and enlarges up to 4x (small text was unreadable at 2x); launcher state survives editing keys (cmd+a inside Raycast).
